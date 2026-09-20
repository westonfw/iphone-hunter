"""探针和买手彻底分开之后的两件要紧事：

  探针：看到货每一轮都广播（买手拿它当心跳，不只在状态翻转时发）
  买手：一个请求都不巡检，而它的急刹必须分清「没货」和「我瞎了」
"""
import time
import unittest
from unittest.mock import Mock, patch

from hunter.apple import Stock, StorePickup
from hunter2.bus import Sighting
from hunter2.buyer import Buyer
from hunter2.sensor import Sensor


def store(num='R359', state=Stock.AVAILABLE, name='南京东路'):
    # UNKNOWN 必须带 reason，否则 StorePickup 会拒绝——「不知道」退化成
    # 「没货」正是它要防的事
    kw = {'reason': '接口没返回'} if state is Stock.UNKNOWN else {}
    return StorePickup('MJYA4CH/A', num, name, state=state, **kw)


class SensorTests(unittest.TestCase):
    def sensor(self, only=()):
        s = Sensor.__new__(Sensor)
        s.log, s.sender, s.bus_id = Mock(), Mock(), 'sensor-a'
        s.sender.send.return_value = 1
        s.only_stores = list(only)
        s.client = Mock()
        s.client.observed_at = {}
        return s

    def shout(self, s, part='MJYA4CH/A', stores=()):
        # 调真实实现，但把父类那一套掐掉（它要状态文件和通知渠道）
        with patch('hunter.monitor.StockWatcher._check_pickup'):
            Sensor._check_pickup(s, part, list(stores))

    def test_it_shouts_every_round_not_only_on_change(self):
        """买手拿这个当心跳：一直收到 = 货还在。只在翻转时发的话，买手会以为货没了。"""
        s = self.sensor()
        for _ in range(3):
            self.shout(s, stores=[store()])
        self.assertEqual(3, s.sender.send.call_count)

    def test_it_says_nothing_when_there_is_no_stock(self):
        s = self.sensor()
        self.shout(s, stores=[store(state=Stock.UNAVAILABLE)])
        s.sender.send.assert_not_called()

    def test_unknown_state_is_not_a_sighting(self):
        s = self.sensor()
        self.shout(s, stores=[store(state=Stock.UNKNOWN)])
        s.sender.send.assert_not_called()

    def test_stores_we_do_not_go_to_are_not_shouted(self):
        s = self.sensor(only=['R581'])
        self.shout(s, stores=[store('R359'), store('R581')])
        self.assertEqual(1, s.sender.send.call_count)
        self.assertEqual('R581', s.sender.send.call_args.args[0].store)

    def test_a_send_failure_never_stops_the_round(self):
        s = self.sensor()
        s.sender.send.side_effect = OSError('网络没了')
        self.shout(s, stores=[store()])          # 不抛

    def test_without_a_key_it_just_watches(self):
        s = self.sensor()
        s.sender = None
        self.shout(s, stores=[store()])          # 不抛

    def test_the_sighting_carries_the_stores_number_and_name(self):
        s = self.sensor()
        self.shout(s, stores=[store('R359', name='南京东路')])
        got = s.sender.send.call_args.args[0]
        self.assertEqual('R359', got.store)
        self.assertEqual('南京东路', got.name)


class BuyerBrakeTests(unittest.TestCase):
    """急刹必须分清三种状态，尤其是「探针全挂」——那时候刹车等于自断手脚。"""

    def buyer(self, fresh=15.0, blind=45.0):
        b = Buyer.__new__(Buyer)
        b.lock = __import__('threading').Lock()
        b.heard, b.any_signal = {}, 0.0
        b.fresh, b.blind_after = fresh, blind
        return b

    def test_a_fresh_heartbeat_means_go(self):
        b = self.buyer()
        now = time.monotonic()
        b.heard['P'], b.any_signal = now - 2.0, now
        self.assertTrue(b.stock_live('P'))

    def test_silence_about_this_model_while_others_talk_means_gone(self):
        b = self.buyer()
        b.any_signal = time.monotonic()          # 探针还在说话
        self.assertFalse(b.stock_live('P'))      # 但很久没提这个型号

    def test_a_stale_heartbeat_means_gone(self):
        b = self.buyer(fresh=15.0)
        now = time.monotonic()
        b.heard['P'], b.any_signal = now - 40.0, now
        self.assertFalse(b.stock_live('P'))

    def test_no_sensor_at_all_means_blind_not_gone(self):
        """探针全挂的时候刹车会把自己废掉——那恰恰是最该老实往下走的时候。"""
        self.assertIsNone(self.buyer().stock_live('P'))

    def test_sensors_going_quiet_flips_back_to_blind(self):
        b = self.buyer(blind=45.0)
        b.any_signal = time.monotonic() - 60.0   # 一分钟没动静了
        self.assertIsNone(b.stock_live('P'))

    def test_the_brake_is_wired_into_the_buyer(self):
        """autobuy 的急刹要用买手这个版本，不是 worker 自带那个按轮询写的。"""
        import inspect
        src = inspect.getsource(Buyer.__init__)
        self.assertIn('self.autobuy.stock_live = self.stock_live', src)


class BuyerIntakeTests(unittest.TestCase):
    def buyer(self, parts=('MJY64CH/A', 'MJYA4CH/A'), only=(), offset=0):
        b = Buyer.__new__(Buyer)
        b.lock = __import__('threading').Lock()
        b.heard, b.any_signal = {}, 0.0
        b.fresh, b.blind_after = 15.0, 45.0
        b.parts, b.only_stores, b.offset = list(parts), list(only), offset
        b.note_of, b.slug_of = {}, {}
        b.urls = Mock()
        b.urls.buy_url.return_value = 'https://x'
        b.urls.base = 'https://www.apple.com.cn'
        b.worker = Mock()
        return b

    def heard(self, b, part='MJYA4CH/A', store='R359', ago=1.0):
        b.heard_of(Sighting(part=part, store=store, name='南京东路',
                            at=time.time() - ago, src='s1'))

    def test_a_sighting_becomes_an_offer_for_the_worker(self):
        b = self.buyer()
        self.heard(b)
        b.worker.observe.assert_called_once()
        part, offers = b.worker.observe.call_args.args[:2]
        self.assertEqual('MJYA4CH/A', part)
        self.assertEqual('R359', offers[0].store)

    def test_the_age_is_preserved(self):
        b = self.buyer()
        self.heard(b, ago=9.0)
        got = b.worker.observe.call_args.args[1][0]
        self.assertAlmostEqual(9.0, time.monotonic() - got.observed, delta=1.0)

    def test_a_model_we_do_not_buy_is_ignored(self):
        b = self.buyer(parts=['MJY64CH/A'])
        self.heard(b, part='MJYE4CH/A')
        b.worker.observe.assert_not_called()

    def test_a_store_we_do_not_go_to_is_ignored(self):
        b = self.buyer(only=['R581'])
        self.heard(b, store='R359')
        b.worker.observe.assert_not_called()

    def test_intake_feeds_the_heartbeat(self):
        b = self.buyer()
        self.assertIsNone(b.stock_live('MJYA4CH/A'))   # 还没听见任何动静
        self.heard(b)
        self.assertTrue(b.stock_live('MJYA4CH/A'))

    def test_an_ignored_sighting_still_proves_sensors_are_alive(self):
        """不买的型号也证明探针在说话。记在过滤之后的话，只盯一个子集的买手
        会一直以为自己瞎了，急刹永远不生效。"""
        b = self.buyer(parts=['MJY64CH/A'])
        self.assertIsNone(b.stock_live('MJY64CH/A'))    # 什么都没听见 = 瞎
        self.heard(b, part='MJYE4CH/A')                 # 听见了，只是不关我们的事
        self.assertFalse(b.stock_live('MJY64CH/A'))     # 那就是它真没货

    def test_the_offset_spreads_deployments(self):
        b = self.buyer(offset=1)
        self.heard(b, part='MJY64CH/A')
        self.assertEqual(1, b.worker.observe.call_args.args[1][0].priority[0])

    def test_only_sightings_go_in_never_absences(self):
        b = self.buyer()
        self.heard(b)
        self.assertEqual(2, len(b.worker.observe.call_args.args))


if __name__ == '__main__':
    unittest.main()


class BuyOnlyKeepaliveTests(unittest.TestCase):
    """只买不盯的时候，保活是这个买手**唯一**的会话来源——它不巡检、不加载
    任何页面，登录态、结账登录墙、购物袋全靠 preflight 维持。

    保活挂在 PurchaseWorker._run 里，而 Buyer 只是 start() 了它。这条接线一断
    就是静默失效：买手看着好好的，放货那一刻才发现没登录。
    """

    def worker(self, cfg=None, **kw):
        from hunter.purchase_worker import PurchaseWorker
        buyer = Mock()
        buyer.cfg = cfg if cfg is not None else {}
        buyer.order_placed = False
        buyer.halt_for_human = False
        buyer.warm_alive = False
        buyer.prepare.return_value = '已登录'
        buyer.login_days_left.return_value = None
        w = PurchaseWorker(buyer, Mock(), probe_url='https://x/P', log=lambda *a: None, **kw)
        return w, buyer

    def run_briefly(self, w, seconds=0.8):
        w.start()
        deadline = time.time() + seconds
        while time.time() < deadline and not w.buyer.prepare.called:
            time.sleep(0.02)
        w.close()

    def test_the_keepalive_runs_without_any_polling(self):
        w, buyer = self.worker()
        self.run_briefly(w)
        buyer.prepare.assert_called()

    def test_it_hands_the_probe_url_to_the_keepalive(self):
        """结账预热要拿一个型号去探路，没有它就只剩订单页探针。"""
        w, buyer = self.worker()
        self.run_briefly(w)
        self.assertEqual('https://x/P', buyer.prepare.call_args.args[0])

    def test_turning_preflight_off_really_turns_it_off(self):
        w, buyer = self.worker(cfg={'preflight': False})
        w.start()
        time.sleep(0.3)
        w.close()
        buyer.prepare.assert_not_called()

    def test_a_login_failure_wakes_the_user(self):
        """买手不盯库存，登录掉了没有别的迹象——只能靠这条推送。"""
        w, buyer = self.worker()
        buyer.signed_in = False
        buyer.prepare.return_value = '⚠️ 未登录'
        self.run_briefly(w)
        for c in w.report.call_args_list:
            if '登录' in c.args[0].stage:
                self.assertTrue(c.args[0].wake)
                return
        self.fail('登录掉了却没有推送')

    def test_the_buyer_starts_the_worker(self):
        """Buyer.loop 要真的把 worker 拉起来，否则保活一次都不会跑。"""
        import inspect
        src = inspect.getsource(Buyer.loop)
        self.assertIn('self.worker.start()', src)

    def test_the_buyer_passes_a_probe_url(self):
        import inspect
        src = inspect.getsource(Buyer.__init__)
        self.assertIn('probe_url=', src)


class EndToEndTests(unittest.TestCase):
    """真实 UDP 走一遍：探针的发送路径 → 总线 → 买手的接收路径。

    单元测试各自用 Mock 把对面掐掉了，这一条是唯一能证明两端真的对得上的。
    不碰 Apple 接口、不开浏览器。
    """

    KEY = b'e2e-key'
    PART, STORE = 'MJYA4CH/A', 'R359'

    def setUp(self):
        import socket as _s
        s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        s.bind(('', 0))
        self.port = s.getsockname()[1]
        s.close()

        b = Buyer.__new__(Buyer)
        b.lock = __import__('threading').Lock()
        b.heard, b.any_signal = {}, 0.0
        b.fresh, b.blind_after = 15.0, 45.0
        b.parts, b.only_stores, b.offset = [self.PART, 'MJY64CH/A'], [self.STORE], 0
        b.note_of, b.slug_of = {}, {self.PART: 'iphone-18-pro'}
        b.urls = Mock()
        b.urls.buy_url.return_value = f'https://www.apple.com.cn/x/{self.PART}'
        b.urls.base = 'https://www.apple.com.cn'
        b.worker = Mock()
        self.buyer = b

        from hunter2.bus import Receiver, Sender
        self.rx = Receiver(key=self.KEY, on_sighting=b.heard_of, port=self.port,
                           log=lambda *a: None)
        self.rx.start()
        self.addCleanup(self.rx.close)

        s2 = Sensor.__new__(Sensor)
        s2.log, s2.bus_id, s2.only_stores = lambda *a: None, 'sensor-a', [self.STORE]
        s2.client = Mock()
        s2.client.observed_at = {}
        s2.sender = Sender(key=self.KEY, src='sensor-a', port=self.port,
                           peers=('127.0.0.1',), log=lambda *a: None)
        self.addCleanup(s2.sender.close)
        self.sensor = s2

    def shout(self):
        with patch('hunter.monitor.StockWatcher._check_pickup'):
            Sensor._check_pickup(self.sensor, self.PART, [store(self.STORE)])
        for _ in range(60):
            if self.buyer.worker.observe.called:
                return
            time.sleep(0.05)

    def test_a_sighting_travels_from_sensor_to_buyer(self):
        self.shout()
        self.buyer.worker.observe.assert_called_once()
        part, offers = self.buyer.worker.observe.call_args.args[:2]
        self.assertEqual(self.PART, part)
        self.assertEqual(self.STORE, offers[0].store)
        self.assertIn(self.PART, offers[0].url)

    def test_the_observation_time_survives_the_wire(self):
        self.shout()
        got = self.buyer.worker.observe.call_args.args[1][0]
        self.assertLess(time.monotonic() - got.observed, 3.0)

    def test_the_heartbeat_drives_the_brake(self):
        self.shout()
        self.assertTrue(self.buyer.stock_live(self.PART))
        self.assertFalse(self.buyer.stock_live('MJY64CH/A'))

    def test_a_replayed_packet_is_refused_on_the_wire(self):
        from hunter2.bus import Sighting as S, encode
        raw = encode(S(part=self.PART, store=self.STORE, at=time.time(), src='x'),
                     self.KEY)
        self.sensor.sender.open()          # socket 是懒开的
        before = self.rx.taken
        for _ in range(2):
            self.sensor.sender.sock.sendto(raw, ('127.0.0.1', self.port))
            time.sleep(0.3)
        self.assertEqual(1, self.rx.taken - before)

    def test_a_forged_packet_is_refused_on_the_wire(self):
        from hunter2.bus import Sighting as S, encode
        self.sensor.sender.open()
        taken, refused = self.rx.taken, self.rx.refused
        self.sensor.sender.sock.sendto(
            encode(S(part=self.PART, store=self.STORE, at=time.time(), src='x'),
                   b'wrong-key'), ('127.0.0.1', self.port))
        time.sleep(0.3)
        self.assertEqual(taken, self.rx.taken)
        self.assertGreater(self.rx.refused, refused)
