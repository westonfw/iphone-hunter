"""买手这一侧。

它一个请求都不巡检，库存全靠主程序喂——所以它的急刹必须分清三件事：
「确实没货」「还有货」和「我判不准」。判错第三种的代价是掐掉一次真实下单。
"""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hunter.apple import Stock, StorePickup
from hunter2.bus import Sighting
from hunter2.buyer import Buyer


def store(num='R359', state=Stock.AVAILABLE, name='南京东路'):
    # UNKNOWN 必须带 reason，否则 StorePickup 会拒绝——「不知道」退化成
    # 「没货」正是它要防的事
    kw = {'reason': '接口没返回'} if state is Stock.UNKNOWN else {}
    return StorePickup('MJYA4CH/A', num, name, state=state, **kw)



def bare_buyer(**over):
    """一个不跑 __init__ 的买手，字段全在这一处填。

    七个 fixture 各抄一遍的话，买手每加一个状态字段就有七处要改——漏一处就是
    一串 AttributeError，而那些字段恰恰是急刹的依据。
    """
    b = Buyer.__new__(Buyer)
    b.lock = threading.Lock()
    b.heard, b.heard_at, b.live, b.any_signal = {}, {}, {}, 0.0
    b.last_alive, b.master_exits, b.master_id = 0.0, 0, ''
    b.clear, b.seen_at, b.gone_seen, b.gone_at = {}, {}, {}, {}
    b.ANY = Buyer.ANY
    b.max_age = 90.0
    b.started, b.first_wait = time.monotonic(), 120.0
    b._warned_missing = b._warned_dim = False
    b.fresh, b.blind_after, b.master_stale = 15.0, 45.0, 90.0
    b.parts, b.only_stores, b.offset = ['P'], [], 0
    b.note_of, b.slug_of = {}, {}
    b.urls = Mock()
    b.urls.buy_url.return_value = 'https://x'
    b.urls.base = 'https://x'
    b.worker, b.log, b.bc = Mock(), Mock(), Mock()
    for k, v in over.items():
        setattr(b, k, v)
    return b


def saw(b, part='P', seen_ago=None, clear_ago=None, stock=None):
    """按「多少秒前」摆好买手看到的东西。

    单调时钟和墙上时钟必须一起设：前者算新鲜度，后者判先后。只设一个的话测出来
    的是一个现实里不存在的状态。
    """
    m, w = time.monotonic(), time.time()
    if seen_ago is not None:
        b.heard[part], b.heard_at[part] = m - seen_ago, w - seen_ago
    if clear_ago is not None:
        b.clear[(part, b.ANY)] = (m - clear_ago, w - clear_ago)
        b.live[part] = {st: b._offer(part, st, st, m - clear_ago)
                        for p, st in (stock or ()) if p == part}
    return b


class BuyerBrakeTests(unittest.TestCase):
    """急刹必须分清三种状态，尤其是第三种——判不准的时候刹车等于自断手脚。

    依据只有一个：主程序**最近一轮把所有型号都读准了**（心跳里的 saw_at）。
    「主程序还活着」不算，「有探针在说话」也不算——那两件事在接口抖动和被
    限流的时候照样成立，而那正是最不该刹车的时刻。
    """

    def buyer(self, fresh=15.0, blind=45.0):
        return bare_buyer(fresh=fresh, blind_after=blind)

    def test_a_fresh_sighting_means_go(self):
        """有货观察比最后那轮完整读数新——那轮读数管不了它。"""
        b = self.buyer()
        now = time.monotonic()
        saw(b, seen_ago=0.0, clear_ago=2.0)
        self.assertTrue(b.stock_live('P'))

    def test_a_reading_after_the_sighting_wins(self):
        """先报有货、之后又完整查过一轮而没有它——那就是真卖完了。"""
        b = self.buyer()
        now = time.monotonic()
        saw(b, seen_ago=2.0, clear_ago=0.0)
        self.assertFalse(b.stock_live('P'))

    def test_a_clean_reading_that_omits_this_model_means_gone(self):
        b = self.buyer()
        saw(b, clear_ago=0.0)                    # 刚把所有型号都读准了
        self.assertFalse(b.stock_live('P'))      # 快照里没有这个型号

    def test_a_stale_sighting_means_gone(self):
        b = self.buyer(fresh=15.0)
        now = time.monotonic()
        saw(b, seen_ago=40.0, clear_ago=0.0)
        self.assertFalse(b.stock_live('P'))

    def test_a_slow_round_cannot_disguise_an_old_reading_as_fresh(self):
        """**这是那条 P1。** 一轮拆成几批、每批隔十几秒：两个时刻会一起变旧，
        而先失效的是有货观察。只比新鲜度的话，刹车会踩在一个从没被查成无货的
        型号上。"""
        b = self.buyer(fresh=15.0)
        now = time.monotonic()
        saw(b, seen_ago=30.0, clear_ago=28.0)
        self.assertIsNone(b.stock_live('P'), '把三十秒前的读数当成刚刚的结论了')

    def test_nothing_read_at_all_means_blind_not_gone(self):
        """一次都没读准过就刹车会把自己废掉——那恰恰是最该老实往下走的时候。"""
        self.assertIsNone(self.buyer().stock_live('P'))

    def test_the_reading_going_stale_flips_back_to_blind(self):
        b = self.buyer(fresh=15.0)
        saw(b, clear_ago=60.0)                   # 一分钟前读准的，太旧了
        self.assertIsNone(b.stock_live('P'))

    def test_chatter_alone_is_not_evidence(self):
        """有消息在总线上飞 ≠ 有人真的读准了这一轮。"""
        b = self.buyer()
        b.any_signal = b.last_alive = time.monotonic()
        self.assertIsNone(b.stock_live('P'))

    def test_the_brake_is_wired_into_the_buyer(self):
        """autobuy 的急刹要用买手这个版本，不是 worker 自带那个按轮询写的。"""
        import inspect
        src = inspect.getsource(Buyer.__init__)
        self.assertIn('self.autobuy.stock_live = self.stock_live', src)


class BuyerIntakeTests(unittest.TestCase):
    def buyer(self, parts=('MJY64CH/A', 'MJYA4CH/A'), only=(), offset=0):
        b = bare_buyer(parts=list(parts), only_stores=list(only), offset=offset)
        b.urls.base = 'https://www.apple.com.cn'
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

        b = bare_buyer(parts=[self.PART, 'MJY64CH/A'], only_stores=[self.STORE],
                       slug_of={self.PART: 'iphone-18-pro'})
        b.urls.buy_url.return_value = f'https://www.apple.com.cn/x/{self.PART}'
        b.urls.base = 'https://www.apple.com.cn'
        self.buyer = b

        from hunter2.bus import Receiver, Sender
        self.rx = Receiver(key=self.KEY, on_sighting=b.on_bus, port=self.port,
                           kinds=('seen', 'alive'), log=lambda *a: None)
        self.rx.start()
        self.addCleanup(self.rx.close)

        from scout.main import Scout
        m = Scout.__new__(Scout)
        m.bus_id, m.only_stores, m.parts = 'scout', [self.STORE], [self.PART,
                                                                   'MJY64CH/A']
        m.reading, m.gone, m.round_no = (0.0, frozenset()), {}, 1
        m.last_beat, m.beat_every, m.shouted = 0.0, 10.0, 0
        m._out = threading.Lock()
        m.pool = Mock()
        m.pool.__len__ = Mock(return_value=1)
        m.log = lambda *a: None
        m.sender = Sender(key=self.KEY, src='scout', port=self.port,
                          peers=('127.0.0.1',), log=lambda *a: None)
        self.addCleanup(m.sender.close)
        self.master = m

    def shout(self):
        from scout.main import Scout
        Scout._shout(self.master, self.PART, [store(self.STORE)], time.time())
        for _ in range(60):
            if self.buyer.worker.observe.called:
                return
            time.sleep(0.05)

    def test_a_sighting_travels_from_the_master_to_the_buyer(self):
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

    def beat(self, saw=True, stock=()):
        """主程序发一条心跳过来，等买手收到。

        快照要跟着一起给：它才是「还有没有货」的依据，空快照的意思是
        「这一轮一家都没货」，不是「我没说」。
        """
        from scout.main import Scout
        self.master.reading = (time.time() if saw else 0.0, frozenset(stock))
        self.master.last_beat = 0.0
        Scout.beat(self.master, force=True)
        for _ in range(60):
            if self.buyer.clear or not saw:
                return
            time.sleep(0.05)

    def test_a_sighting_still_wins_over_everything(self):
        self.shout()
        self.assertTrue(self.buyer.stock_live(self.PART))

    def test_the_brake_waits_for_a_clean_reading(self):
        """**这是那条 P1 走完整条线的版本。** 主程序在说话（刚报过有货），但它
        从没说过「这一轮所有型号都读准了」——此时对别的型号刹车是没有依据的。"""
        self.shout()
        self.assertIsNone(self.buyer.stock_live('MJY64CH/A'),
                          '光凭总线上有消息就判无货了')
        self.beat()
        self.assertFalse(self.buyer.stock_live('MJY64CH/A'),
                         '探针报了「读准了」，这时候才该刹车')

    def test_a_sightless_beat_does_not_arm_the_brake(self):
        """进程活着但这一轮没读准（被 541 静默、接口返回 UNKNOWN）。"""
        self.shout()
        self.beat(saw=False)
        self.assertIsNone(self.buyer.stock_live('MJY64CH/A'))

    def test_a_replayed_packet_is_refused_on_the_wire(self):
        from hunter2.bus import Sighting as S, encode
        raw = encode(S(part=self.PART, store=self.STORE, at=time.time(), src='x'),
                     self.KEY)
        self.master.sender.open()          # socket 是懒开的
        before = self.rx.taken
        for _ in range(2):
            self.master.sender.sock.sendto(raw, ('127.0.0.1', self.port))
            time.sleep(0.3)
        self.assertEqual(1, self.rx.taken - before)

    def test_a_forged_packet_is_refused_on_the_wire(self):
        from hunter2.bus import Sighting as S, encode
        self.master.sender.open()
        taken, refused = self.rx.taken, self.rx.refused
        self.master.sender.sock.sendto(
            encode(S(part=self.PART, store=self.STORE, at=time.time(), src='x'),
                   b'wrong-key'), ('127.0.0.1', self.port))
        time.sleep(0.3)
        self.assertEqual(taken, self.rx.taken)
        self.assertGreater(self.rx.refused, refused)


class MasterHeartbeatTests(unittest.TestCase):
    """主程序自己挂了是发不出告警的，只能靠子程序发现「很久没听见」。

    而没货的时候本来就没有 seen 消息——所以必须有独立的心跳，否则子程序
    分不清「主程序死了」和「只是没放货」。那正是最危险的状态：看着一切正常，
    放货那一刻才发现根本没人在盯。
    """

    def buyer(self, stale=90.0):
        return bare_buyer(master_stale=stale)

    def beat(self, b, exits=2, who='scout', saw=True):
        """默认是一条「我看清了」的心跳；saw=False 表示进程活着但什么都没看见。"""
        from hunter2.bus import Alive
        t = time.time()
        b.heard_alive(Alive(id=who, at=t, exits=exits, src=who,
                            saw_at=t if saw else 0.0,
                            parts=tuple(b.parts) if saw else ()))

    # ---------- 刹车只认「我看清了」 ----------

    def test_a_clean_reading_without_this_part_means_sold_out(self):
        """没货就没有 seen 消息。光看 seen 的话，每个安静时段都会被误判成失明。"""
        b = self.buyer()
        self.beat(b)
        self.assertFalse(b.stock_live('P'), '主程序刚看清一轮，里面没有这个型号')

    def test_a_bare_heartbeat_is_not_evidence_of_no_stock(self):
        """**这是那条 P1。** 查询失败、返回 UNKNOWN、出口全在 541 静默里，
        进程照样发心跳。把「进程活着」当「确实没货」，等于在最不该刹车的时候
        掐掉一次正在进行的下单。"""
        b = self.buyer()
        self.beat(b, saw=False)
        self.assertIsNone(b.stock_live('P'), '拿「进程还活着」当「确实没货」了')

    def test_a_stale_reading_stops_being_evidence(self):
        """看清过、但那是半分钟前的事了——货可能是这半分钟里放的。"""
        b = self.buyer()
        self.beat(b)
        saw(b, clear_ago=60.0)
        self.assertIsNone(b.stock_live('P'))

    def test_without_any_master_it_is_blindness(self):
        self.assertIsNone(self.buyer().stock_live('P'))

    def test_a_stale_master_is_blindness_again(self):
        b = self.buyer(stale=90.0)
        self.beat(b)
        b.last_alive = time.monotonic() - 999
        saw(b, clear_ago=999.0)
        self.assertIsNone(b.stock_live('P'))

    def test_a_fresh_sighting_still_wins(self):
        b = self.buyer()
        self.beat(b)
        saw(b, seen_ago=0.0)
        self.assertTrue(b.stock_live('P'))

    def test_a_sightless_beat_does_not_roll_back_the_last_reading(self):
        """看清过就是看清过。紧接着来一条「这轮没看清」，不该把它抹掉。"""
        b = self.buyer()
        self.beat(b)
        clear = dict(b.clear)
        self.beat(b, saw=False)
        self.assertEqual(clear, b.clear)

    # ---------- 在跑但看不清 ----------

    def test_a_dim_master_is_called_out_in_the_log(self):
        """不报警（它不一定是故障），但要说出来：否则这段时间看着完全正常，
        而实际上没有任何一个型号有确定的读数。"""
        b = self.buyer()
        self.beat(b, saw=False)
        b.check_sight()
        self.assertTrue(any('完整的读数' in str(c) for c in b.log.call_args_list))
        b.bc.send.assert_not_called()

    def test_a_dim_master_is_only_called_out_once(self):
        b = self.buyer()
        self.beat(b, saw=False)
        for _ in range(5):
            b.check_sight()
        self.assertEqual(1, sum('完整的读数' in str(c) for c in b.log.call_args_list))

    def test_a_clear_master_says_nothing(self):
        b = self.buyer()
        self.beat(b)
        b.check_sight()
        self.assertFalse(any('完整的读数' in str(c) for c in b.log.call_args_list))

    # ---------- 失联告警 ----------

    def test_a_missing_master_wakes_the_user(self):
        b = self.buyer(stale=10.0)
        self.beat(b)
        b.last_alive = time.monotonic() - 60
        b.check_master()
        b.bc.send.assert_called_once()
        self.assertIn('失联', b.bc.send.call_args.args[0])
        self.assertTrue(b.bc.send.call_args.kwargs.get('wake'))

    def test_it_only_warns_once(self):
        b = self.buyer(stale=10.0)
        self.beat(b)
        b.last_alive = time.monotonic() - 60
        for _ in range(5):
            b.check_master()
        self.assertEqual(1, b.bc.send.call_count)

    def test_a_live_master_never_warns(self):
        b = self.buyer()
        self.beat(b)
        b.check_master()
        b.bc.send.assert_not_called()

    def test_never_having_seen_a_master_does_not_warn(self):
        """还没连上就报「失联」是噪音——刚启动时主程序可能还没起来。"""
        b = self.buyer(stale=10.0)
        b.check_master()
        b.bc.send.assert_not_called()

    def test_the_master_coming_back_is_reported(self):
        b = self.buyer(stale=10.0)
        self.beat(b)
        b.last_alive = time.monotonic() - 60
        b.check_master()
        b.bc.send.reset_mock()
        self.beat(b)
        self.assertIn('恢复', b.bc.send.call_args.args[0])
        b.check_master()                     # 恢复之后不该再报失联
        self.assertEqual(1, b.bc.send.call_count)

    # ---------- 分流 ----------

    def test_the_bus_dispatches_by_message_kind(self):
        from hunter2.bus import Alive, Sighting
        b = self.buyer()
        b.heard_of = Mock()
        b.heard_alive = Mock()
        b.on_bus(Alive(id='s', at=time.time()))
        b.on_bus(Sighting(part='P', store='S', at=time.time()))
        b.heard_alive.assert_called_once()
        b.heard_of.assert_called_once()


class BuyerCandidateTests(unittest.TestCase):
    """买手收到的是一家一条的信号，而 worker.observe 是整体替换。

    不攒起来的话，四家同时放货时只有最后一条活下来——另外三家一次都不会被试，
    而「首选那家恰好先卖完」正是 2026-09-18 07:06 丢单的原因。
    """

    def buyer(self, parts=('P', 'Q')):
        return bare_buyer(parts=list(parts))

    def sight(self, b, part='P', store='R359', at=None):
        from hunter2.bus import Sighting
        b.heard_of(Sighting(part=part, store=store, name=store,
                            at=at or time.time(), src='scout'))

    def stores_handed_over(self, b):
        return sorted(o.store for o in b.worker.observe.call_args.args[1])

    def test_two_stores_both_survive(self):
        b = self.buyer()
        self.sight(b, store='R359')
        self.sight(b, store='R581')
        self.assertEqual(['R359', 'R581'], self.stores_handed_over(b),
                         '后一家把前一家挤掉了')

    def test_four_stores_all_survive(self):
        b = self.buyer()
        for s in ('R359', 'R581', 'R448', 'R390'):
            self.sight(b, store=s)
        self.assertEqual(4, len(self.stores_handed_over(b)))

    def test_the_same_store_again_is_an_update_not_a_duplicate(self):
        b = self.buyer()
        self.sight(b, store='R359')
        self.sight(b, store='R359')
        self.assertEqual(['R359'], self.stores_handed_over(b))

    def test_a_newer_reading_of_a_store_replaces_the_older_one(self):
        b = self.buyer()
        self.sight(b, store='R359', at=time.time() - 5)
        old = b.live['P']['R359'].observed
        self.sight(b, store='R359')
        self.assertGreater(b.live['P']['R359'].observed, old)

    def test_a_stale_store_drops_out(self):
        """超过 candidate_max_age 的候选不能一直留着占位置。"""
        b = self.buyer()
        b.max_age = 10.0
        self.sight(b, store='R581', at=time.time() - 60)
        self.sight(b, store='R359')
        self.assertEqual(['R359'], self.stores_handed_over(b))

    def test_models_do_not_mix(self):
        b = self.buyer()
        self.sight(b, part='P', store='R359')
        self.sight(b, part='Q', store='R581')
        self.assertEqual(['R581'], self.stores_handed_over(b))
        self.assertEqual({'R359'}, set(b.live['P']))


class RestockRecoveryTests(unittest.TestCase):
    """卖完再补货，型号必须重新可买。

    重试次数和「判死」标记都靠 PurchaseWorker 里那条「明确无货」分支来清，而
    买手只收得到「有货」消息——不回灌的话，同一家店二次补货时 stock_live 已经
    是 True 了，_next() 却一直返回 None，只能重启才恢复。
    """

    def buyer(self):
        return BuyerCandidateTests.buyer(BuyerCandidateTests())

    def sight(self, b, **kw):
        BuyerCandidateTests.sight(BuyerCandidateTests(), b, **kw)

    def beat(self, b, saw_at):
        from hunter2.bus import Alive
        t = time.time()
        b.heard_alive(Alive(id='scout', at=t, exits=1, parts=('P',), src='scout', saw_at=saw_at))

    def test_a_clean_reading_without_the_part_reports_it_as_gone(self):
        b = self.buyer()
        self.sight(b, store='R359')
        b.worker.observe.reset_mock()
        self.beat(b, time.time())
        part, offers = b.worker.observe.call_args.args[:2]
        self.assertEqual('P', part)
        self.assertEqual([], offers)
        self.assertEqual(['R359'],
                         b.worker.observe.call_args.kwargs['unavailable'],
                         '没把「明确无货」递过去，重试次数和判死标记就永远清不掉')

    def test_it_only_reports_gone_once(self):
        b = self.buyer()
        self.sight(b, store='R359')
        self.beat(b, time.time())
        b.worker.observe.reset_mock()
        self.beat(b, time.time())
        b.worker.observe.assert_not_called()

    def test_a_reading_older_than_the_sighting_reports_nothing(self):
        """读数比有货观察还早，它管不了这个型号。"""
        b = self.buyer()
        self.sight(b, store='R359')
        b.worker.observe.reset_mock()
        self.beat(b, time.time() - 60)
        b.worker.observe.assert_not_called()

    def test_a_sightless_beat_reports_nothing(self):
        """这一轮没看清（被 541 静默、接口 UNKNOWN），什么都不能断定。"""
        b = self.buyer()
        self.sight(b, store='R359')
        b.worker.observe.reset_mock()
        self.beat(b, 0.0)
        b.worker.observe.assert_not_called()

    def test_restocking_the_same_store_makes_it_buyable_again(self):
        """完整链路：卖完 → 判死 → 同一家店再补货 → 重新可买。"""
        from hunter.purchase_worker import Offer, PurchaseWorker
        w = PurchaseWorker(Mock(), Mock(), max_attempts=1, log=lambda *a: None)
        off = Offer('P', 'R359', '南京东路', 'u', 'P', time.monotonic(), (0, 0))
        w.observe('P', [off])
        w.attempts['P'] = (1, w.clock())       # 打过一次，到上限了
        w.tried['P'] = {'R359'}
        w.burned.add('P')
        self.assertIsNone(w._next(), '到上限的型号本来就不该再出候选')

        w.observe('P', [], unavailable=['R359'])       # 明确卖完了
        again = Offer('P', 'R359', '南京东路', 'u', 'P', time.monotonic(), (0, 0))
        w.observe('P', [again])                        # 同一家店补货
        self.assertNotIn('P', w.burned, '补货了还停在判死状态')
        self.assertIsNotNone(w._next(), '补货之后仍然拿不到候选，只能重启才恢复')


class FirstContactTests(unittest.TestCase):
    """从没连上过主程序，也必须报警。

    密钥不一致、网络不通、主程序压根没启动——这几种情况下「多久没心跳」永远
    算不出来，光看它就会一声不吭地空等一整天。
    """

    def buyer(self, first_wait=120.0):
        return bare_buyer(first_wait=first_wait)

    def test_never_connecting_eventually_warns(self):
        b = self.buyer(first_wait=10.0)
        b.started = time.monotonic() - 60
        b.check_master()
        b.bc.send.assert_called_once()
        self.assertIn('连不上', b.bc.send.call_args.args[0])
        self.assertTrue(b.bc.send.call_args.kwargs.get('wake'))

    def test_the_message_says_what_to_check(self):
        b = self.buyer(first_wait=10.0)
        b.started = time.monotonic() - 60
        b.check_master()
        body = b.bc.send.call_args.args[1]
        self.assertIn('HUNTER_BUS_KEY', body)

    def test_the_grace_period_is_respected(self):
        """正常的启动顺序是主程序先起，但差个几十秒是常事，那时候报是噪音。"""
        b = self.buyer(first_wait=120.0)
        b.check_master()
        b.bc.send.assert_not_called()

    def test_it_only_warns_once(self):
        b = self.buyer(first_wait=10.0)
        b.started = time.monotonic() - 60
        for _ in range(5):
            b.check_master()
        self.assertEqual(1, b.bc.send.call_count)

    def test_connecting_later_clears_it(self):
        from hunter2.bus import Alive
        b = self.buyer(first_wait=10.0)
        b.started = time.monotonic() - 60
        b.check_master()
        b.bc.send.reset_mock()
        b.heard_alive(Alive(id='scout', at=time.time(), exits=1, src='scout'))
        self.assertIn('连上了', b.bc.send.call_args.args[0],
                      '报了「连不上」却没有下文，那条警报会一直挂着没结果')


class ClockDomainTests(unittest.TestCase):
    """判先后只能用发送方自己的时钟。

    mono_of 每次换算都拿当时的 monotonic/time 差去折算，两次调用之间的抖动会让
    **同一个**墙上时刻换出不同的单调时刻——于是 clear > last 在两者本来相等时
    也会成立，然后清空候选、急刹。
    """

    def test_the_same_instant_never_looks_later(self):
        """300 次真实编解码回放：同一时刻不许有一次被判成「后来又查过」。"""
        from hunter2.bus import Alive, Sighting
        bad = 0
        for _ in range(300):
            b = bare_buyer(parts=['P'])
            t = time.time()
            b.heard_of(Sighting(part='P', store='R581', name='南京东路',
                                at=t, src='scout'))
            b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t,
                                stock=('P:R581',), parts=('P',), src='scout'))
            if b.stock_live('P') is not True:
                bad += 1
        self.assertEqual(0, bad, f'{bad}/300 次把同一时刻判成了「后来确认无货」')

    def test_the_same_instant_gives_a_stable_verdict(self):
        """同一时刻的有货消息和快照，判出来的结果必须每次都一样。

        换算之后再比的话，两次 mono_of 之间的抖动会让先后随机翻转——同一个输入
        有时判「没货」（刹车），有时判「有货」（放行）。结论随机比结论错更难查。
        """
        from hunter2.bus import Alive, Sighting
        got = set()
        for _ in range(300):
            b = bare_buyer(parts=['P'])
            t = time.time()
            b.heard_of(Sighting(part='P', store='R581', at=t, src='scout'))
            b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t, stock=(),
                                parts=('P',), src='scout'))
            got.add(b.stock_live('P'))
        self.assertEqual({False}, got, f'同一输入判出了 {got}，结论是随机的')

    def test_ordering_uses_the_senders_own_stamps(self):
        import inspect
        from hunter2.buyer import Buyer
        src = inspect.getsource(Buyer.stock_live)
        self.assertIn('clear_at', src)
        self.assertIn('last_at', src)

    def test_a_genuinely_later_reading_still_wins(self):
        """修掉浮点误判不能把真正的先后也一起抹平。"""
        from hunter2.bus import Alive, Sighting
        b = bare_buyer(parts=['P'])
        t = time.time()
        b.heard_of(Sighting(part='P', store='R581', at=t - 3, src='scout'))
        b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t, stock=(),
                            parts=('P',), src='scout'))
        self.assertFalse(b.stock_live('P'))


class LostPacketTests(unittest.TestCase):
    """丢一条 seen 不能撤销还有效的候选。

    seen 走的是 UDP，丢了就是丢了。把「这一轮没收到 seen」当「没货了」，等于让
    一个丢包去掐掉正在进行的下单。所以「还有没有货」由心跳里的完整快照回答。
    """

    def buyer(self):
        return bare_buyer(parts=['P'])

    def sight(self, b, store='R581', ago=0.0):
        from hunter2.bus import Sighting
        b.heard_of(Sighting(part='P', store=store, name=store,
                            at=time.time() - ago, src='scout'))

    def beat(self, b, stock=(), ago=0.0):
        from hunter2.bus import Alive
        t = time.time() - ago
        b.heard_alive(Alive(id='scout', at=time.time(), exits=1, saw_at=t,
                            stock=tuple(stock), parts=('P',), src='scout'))

    def test_a_lost_sighting_does_not_revoke_the_candidate(self):
        """**这是那条 P1。** 货一直在，第二轮只丢了 seen，心跳正常到达。"""
        b = self.buyer()
        self.sight(b, ago=5.0)
        b.worker.observe.reset_mock()
        self.beat(b, stock=('P:R581',))          # 快照说还有货
        self.assertTrue(b.stock_live('P'), '一个丢包就把还有效的候选撤了')
        self.assertEqual({'R581'}, set(b.live['P']))

    def test_a_lost_sighting_is_recovered_from_the_snapshot(self):
        """丢包不该让我们错过一次放货——快照里有就直接把候选建出来。"""
        b = self.buyer()
        self.beat(b, stock=('P:R581',))
        self.assertEqual({'R581'}, set(b.live['P']))
        part, offers = b.worker.observe.call_args.args[:2]
        self.assertEqual(['R581'], [o.store for o in offers])
        self.assertTrue(b.stock_live('P'))

    def test_an_empty_snapshot_really_means_sold_out(self):
        """空快照是「一家都没货」，跟「我没说」必须分得开。"""
        b = self.buyer()
        self.sight(b, ago=5.0)
        self.beat(b, stock=())
        self.assertFalse(b.stock_live('P'))
        self.assertEqual({}, b.live['P'])

    def test_a_store_dropping_out_is_precise(self):
        """两家有货、其中一家卖完：只撤那一家，另一家照常。"""
        b = self.buyer()
        self.sight(b, store='R581', ago=5.0)
        self.sight(b, store='R359', ago=5.0)
        b.worker.observe.reset_mock()
        self.beat(b, stock=('P:R581',))
        self.assertEqual({'R581'}, set(b.live['P']))
        self.assertEqual(['R359'], b.worker.observe.call_args.kwargs['unavailable'])

    def test_a_snapshot_older_than_the_sighting_is_ignored(self):
        """旧快照管不了新消息——拿它撤销就是用旧信息掐新信息。"""
        b = self.buyer()
        self.sight(b, ago=0.0)
        b.worker.observe.reset_mock()
        self.beat(b, stock=(), ago=60.0)
        b.worker.observe.assert_not_called()
        self.assertEqual({'R581'}, set(b.live['P']))

    def test_stores_we_do_not_go_to_are_not_recovered(self):
        b = bare_buyer(parts=['P'], only_stores=['R581'])
        self.beat(b, stock=('P:R359',))
        self.assertEqual({}, b.live.get('P', {}))

    def test_models_we_do_not_buy_are_not_recovered(self):
        b = bare_buyer(parts=['P'])
        self.beat(b, stock=('Q:R581',))
        self.assertEqual({}, b.live.get('Q', {}))


class AtomicReadingTests(unittest.TestCase):
    """时刻和快照必须一起换。

    心跳跑在另一个线程上：分两次赋值的话它可能正好读到「新时刻 + 上一轮的快照」
    ——那个组合从来没有存在过，而买手会照着它撤销刚收到的候选。
    """

    def test_the_scout_beat_reads_the_pair_exactly_once(self):
        from scout.main import Scout
        rounds = [(1000.0, frozenset()), (1001.0, frozenset({('P', 'R581')}))]

        class Flipping(Scout):
            n = -1

            @property
            def reading(self):
                type(self).n += 1
                return rounds[type(self).n % len(rounds)]

        s = Scout.__new__(Flipping)
        s.bus_id, s.round_no, s.last_beat, s.beat_every = 'scout', 1, 0.0, 10.0
        s.parts, s.gone, s.only_stores = ['P'], {}, []
        s._out = threading.Lock()
        s.log, s.sender = Mock(), Mock()
        s.sender.oversize.return_value = False
        s.sender.shrink.side_effect = lambda m: m
        s.pool = Mock()
        s.pool.__len__ = Mock(return_value=1)
        s.beat(force=True)
        m = s.sender.send.call_args.args[0]
        got = (m.saw_at, frozenset(tuple(x.split('@', 1)) for x in m.stock))
        self.assertIn(got, rounds, f'心跳发出了跨轮次的组合：{got}')

    def test_the_scout_publishes_them_as_one(self):
        import inspect
        from scout.main import Scout
        src = inspect.getsource(Scout.poll)
        self.assertIn('self.reading = (', src)
        self.assertNotIn('self.saw_at =', src)

    def test_the_scout_beat_reads_them_once(self):
        import inspect
        from scout.main import Scout
        self.assertIn('saw_at, stock = self.reading', inspect.getsource(Scout.beat))

class SnapshotRefreshTests(unittest.TestCase):
    """每一轮快照都要把留下来的候选也刷新一遍。

    只给新门店建 Offer 的话，门店集合不变时观察时刻就一直停在第一次——过了
    candidate_max_age 那边的候选全部过期，`_next()` 返回 None，而 stock_live
    还在说 True：货一直有，重试却停了，而且没有任何一条日志会提这件事。
    """

    def buyer(self):
        return bare_buyer(parts=['P'])

    def beat(self, b, stock=('P:R581',)):
        from hunter2.bus import Alive
        t = time.time()
        b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t,
                            stock=tuple(stock), parts=('P',), src='scout'))

    def test_an_unchanged_snapshot_still_refreshes_the_candidate(self):
        b = self.buyer()
        self.beat(b)
        first = b.live['P']['R581'].observed
        time.sleep(0.02)
        b.worker.observe.reset_mock()
        self.beat(b)
        self.assertGreater(b.live['P']['R581'].observed, first,
                           '观察时刻停在第一次，候选早晚会自己过期')
        b.worker.observe.assert_called()

    def test_the_queue_is_kept_in_step(self):
        """刷新了本地却不告诉 worker，等于没刷新。"""
        b = self.buyer()
        self.beat(b)
        b.worker.observe.reset_mock()
        time.sleep(0.02)
        self.beat(b)
        part, offers = b.worker.observe.call_args.args[:2]
        self.assertEqual(['R581'], [o.store for o in offers])
        self.assertLess(time.monotonic() - offers[0].observed, 1.0)

    def test_a_still_live_model_never_ages_out(self):
        """货一直在，候选就不该过期——这正是持续重试的前提。"""
        b = self.buyer()
        b.max_age = 0.05
        for _ in range(4):
            time.sleep(0.02)
            self.beat(b)
        self.assertEqual({'R581'}, set(b.live['P']))
        self.assertTrue(b.stock_live('P'))

    def test_the_store_name_survives_a_refresh(self):
        """seen 带的是门店名，快照只有编号——刷新不能把名字刷没了。"""
        from hunter2.bus import Sighting
        b = self.buyer()
        b.heard_of(Sighting(part='P', store='R581', name='五角场',
                            at=time.time(), src='scout'))
        self.beat(b)
        self.assertEqual('五角场', b.live['P']['R581'].name)


class BrakeStoreScopeTests(unittest.TestCase):
    """急刹和候选必须用同一套门店范围。

    只认 R581 时，快照里的 P@R359 撤候选撤对了，急刹却还说「有货」——正在跑的
    那一单就停不下来。
    """

    def test_stock_at_a_store_we_avoid_is_not_stock(self):
        from hunter2.bus import Alive
        b = bare_buyer(parts=['P'], only_stores=['R581'])
        t = time.time()
        b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t,
                            stock=('P:R359',), parts=('P',), src='scout'))
        self.assertEqual({}, b.live.get('P', {}), '候选那边过滤对了')
        self.assertFalse(b.stock_live('P'), '急刹却把去不了的门店算成了有货')

    def test_a_model_we_do_not_buy_is_not_stock(self):
        from hunter2.bus import Alive
        b = bare_buyer(parts=['P'])
        t = time.time()
        b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t,
                            stock=('Q:R581',), parts=('P',), src='scout'))
        self.assertFalse(b.stock_live('P'))

    def test_a_store_we_do_go_to_still_counts(self):
        from hunter2.bus import Alive
        b = bare_buyer(parts=['P'], only_stores=['R581'])
        t = time.time()
        b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t,
                            stock=('P:R581', 'P:R359'), parts=('P',), src='scout'))
        self.assertTrue(b.stock_live('P'))
        self.assertEqual({'R581'}, set(b.live['P']))


class OldSenderTests(unittest.TestCase):
    """「没带快照」和「空快照」是两回事。

    老版本只发 saw_at，它的「没货」靠的是「你没收到 seen」——而那正是我们刚
    判定不可靠的东西。把它当空快照，新买手会被一个老探针清空候选。
    """

    def raw(self, **over):
        import json
        from hunter2.bus import sign
        body = dict({'v': 2, 'kind': 'alive', 'id': 's', 'at': 1000.0,
                     'saw_at': 999.0, 'src': 's', 'exits': 1, 'round_no': 1,
                     'parts': ['P'], 'nonce': 'n1'}, **over)
        return json.dumps({'body': body, 'mac': sign(body, b'k')}).encode()

    def decode(self, raw):
        from hunter2.bus import Decoder
        return Decoder(key=b'k', clock=lambda: 1000.0, kinds=('alive',)).decode(raw)

    def test_a_message_without_the_field_is_not_a_clean_reading(self):
        got, why = self.decode(self.raw())
        self.assertEqual('', why)
        self.assertEqual(0.0, got.saw_at, '老消息被当成了「所有门店都没货」')

    def test_an_explicit_empty_snapshot_still_counts(self):
        got, _ = self.decode(self.raw(stock=[]))
        self.assertEqual(999.0, got.saw_at)
        self.assertEqual((), got.stock)

    def test_null_is_treated_as_missing(self):
        got, _ = self.decode(self.raw(stock=None))
        self.assertEqual(0.0, got.saw_at)

    def test_an_old_sender_cannot_clear_candidates(self):
        """整条线：老探针发心跳，新买手手上的候选必须原封不动。"""
        from hunter2.bus import Sighting
        b = bare_buyer(parts=['P'])
        b.heard_of(Sighting(part='P', store='R581', at=time.time(), src='scout'))
        b.worker.observe.reset_mock()
        got, _ = self.decode(self.raw())
        b.heard_alive(got)
        self.assertEqual({'R581'}, set(b.live['P']))
        b.worker.observe.assert_not_called()

    def test_a_mixed_deployment_just_does_not_brake(self):
        """混部署的代价只是不刹车，不会误刹——方向是有意选的。"""
        got, _ = self.decode(self.raw())
        b = bare_buyer(parts=['P'])
        b.heard_alive(got)
        self.assertIsNone(b.stock_live('P'))


class PerStoreAlignmentTests(unittest.TestCase):
    """对齐要按门店比时刻，不能按型号。

    多批次查询里，后一批的一条新 seen 会把整个型号挡住——另外几家门店就再也
    校不准了：快照说 R002、R003，候选却停在 R001、R002。
    """

    def beat(self, b, stock, ago=0.0):
        from hunter2.bus import Alive
        t = time.time() - ago
        b.heard_alive(Alive(id='scout', at=time.time(), exits=1, saw_at=t,
                            stock=tuple(stock), parts=('P',), src='scout'))

    def sight(self, b, store, ago=0.0):
        from hunter2.bus import Sighting
        b.heard_of(Sighting(part='P', store=store, at=time.time() - ago,
                            src='scout'))

    def test_a_fresh_sighting_only_protects_its_own_store(self):
        b = bare_buyer(parts=['P'])
        self.sight(b, 'R001', ago=5.0)
        self.sight(b, 'R002', ago=5.0)
        # 一轮拆成几批，saw_at 取最早那批；后一批才查到 R001 又有货
        self.beat(b, ['P:R002,R003'], ago=1.0)
        self.sight(b, 'R001')                     # 比这一轮的 saw_at 新
        self.beat(b, ['P:R002,R003'], ago=1.0)   # 同一轮的心跳重发
        self.assertEqual({'R001', 'R002', 'R003'}, set(b.live['P']),
                         '一条新 seen 只保住了自己那家，别的门店被整体跳过了')

    def test_the_whole_model_is_never_skipped(self):
        """挡住整个型号的话，后面几家门店就再也没机会被校准。"""
        import inspect
        from hunter2.buyer import Buyer
        src = inspect.getsource(Buyer._reconcile)
        self.assertIn('self.seen_at.get((part, store), 0.0) > a.saw_at', src)
        self.assertNotIn('self.heard_at.get(part, 0.0) > a.saw_at', src)

    def test_a_store_with_no_fresh_sighting_is_dropped(self):
        b = bare_buyer(parts=['P'])
        self.sight(b, 'R001', ago=5.0)
        self.sight(b, 'R002', ago=5.0)
        self.beat(b, ['P:R002,R003'])
        self.assertEqual({'R002', 'R003'}, set(b.live['P']),
                         '一条新 seen 把整个型号挡住了，别的门店没能校准')

    def test_the_protection_expires_with_the_next_snapshot(self):
        b = bare_buyer(parts=['P'])
        self.sight(b, 'R001')
        self.beat(b, [], ago=1.0)                  # 比那条 seen 还早，管不了它
        self.assertEqual({'R001'}, set(b.live['P']))
        self.beat(b, [])                           # 这一份更新，说了算
        self.assertEqual({}, b.live['P'])


class LateSightingTests(unittest.TestCase):
    """迟到的旧 seen 不能复活已经撤销的候选。

    急刹判「无货」而队列里偏偏排着它，买手会去打一个明知没有的门店。
    """

    def test_a_sighting_older_than_the_reading_is_dropped(self):
        from hunter2.bus import Alive, Sighting
        b = bare_buyer(parts=['P'])
        t = time.time()
        b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t, stock=(),
                            parts=('P',), src='scout'))
        b.worker.observe.reset_mock()
        b.heard_of(Sighting(part='P', store='R581', at=t - 5, src='scout'))
        self.assertEqual({}, b.live.get('P', {}), '迟到的旧消息复活了候选')
        b.worker.observe.assert_not_called()
        self.assertFalse(b.stock_live('P'))

    def test_a_newer_sighting_still_goes_in(self):
        from hunter2.bus import Alive, Sighting
        b = bare_buyer(parts=['P'])
        t = time.time()
        b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t - 5, stock=(),
                            parts=('P',), src='scout'))
        b.heard_of(Sighting(part='P', store='R581', at=t, src='scout'))
        self.assertEqual({'R581'}, set(b.live['P']))

    def test_a_repeat_of_a_live_store_is_not_dropped(self):
        """快照里还有这家，那条 seen 只是重复，不该被当成迟到的。"""
        from hunter2.bus import Alive, Sighting
        b = bare_buyer(parts=['P'])
        t = time.time()
        b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t,
                            stock=('P:R581',), parts=('P',), src='scout'))
        b.heard_of(Sighting(part='P', store='R581', name='五角场',
                            at=t - 1, src='scout'))
        self.assertEqual('五角场', b.live['P']['R581'].name)


class SoldOutBetweenBeatsTests(unittest.TestCase):
    """两条心跳之间卖空过，买手必须知道。

    心跳十秒一条，而「有货→没货→又有货」可能整个发生在两条之间：中间那份
    「没货」的快照被后一份盖掉，重试次数和判死标记就永远清不掉——货明明有，
    却不再重试。
    """

    def beat(self, b, stock=(), gone=()):
        from hunter2.bus import Alive
        t = time.time()
        b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t,
                            stock=tuple(stock), parts=('P',),
                            gone=tuple(gone), src='scout'))

    def test_the_counter_reveals_a_sell_out_we_never_saw(self):
        b = bare_buyer(parts=['P'])
        self.beat(b, ['P:R581'], gone=('P:0',))
        b.worker.observe.reset_mock()
        self.beat(b, ['P:R581'], gone=('P:1',))   # 中间卖空过一次
        self.assertEqual(['P'], [c.args[0] for c in b.worker.restock.call_args_list],
                         '中间那次卖空丢了，重试次数清不掉')

    def test_an_unchanged_counter_says_nothing(self):
        b = bare_buyer(parts=['P'])
        self.beat(b, ['P:R581'], gone=('P:1',))
        b.worker.observe.reset_mock()
        self.beat(b, ['P:R581'], gone=('P:1',))
        b.worker.restock.assert_not_called()

    def test_a_jump_of_several_still_counts_once(self):
        """丢了几条心跳，计数一次跳好几格——照样要清一次。"""
        b = bare_buyer(parts=['P'])
        self.beat(b, ['P:R581'], gone=('P:0',))
        b.worker.observe.reset_mock()
        self.beat(b, ['P:R581'], gone=('P:4',))
        self.assertEqual(['P'], [c.args[0] for c in b.worker.restock.call_args_list])

    def test_the_candidate_survives_the_reset(self):
        """先递「明确无货」再递候选：顺序反了的话刚补回来的候选会被抹掉。"""
        b = bare_buyer(parts=['P'])
        self.beat(b, ['P:R581'], gone=('P:0',))
        self.beat(b, ['P:R581'], gone=('P:1',))
        last = b.worker.observe.call_args_list[-1]
        self.assertEqual(['R581'], [o.store for o in last.args[1]])
        self.assertEqual({'R581'}, set(b.live['P']))

    def test_the_master_counts_sell_outs(self):
        from scout.main import Scout
        s = Scout.__new__(Scout)
        s.gone, s.reading = {}, (0.0, frozenset())
        s.reading = (1.0, frozenset({('P', 'R581')}))
        had = {p for p, _ in s.stock}
        for part in had - set():
            s.gone[part] = s.gone.get(part, 0) + 1
        self.assertEqual({'P': 1}, s.gone)


class SnapshotScopeTests(unittest.TestCase):
    """快照只对发送方盯的型号完整。

    两个探针分别盯 P 和 Q 时，Q 那份快照里当然没有 P——它根本没查过 P。
    不带范围的话，它会把还有货的 P 一起撤掉。
    """

    def beat(self, b, scope, stock=()):
        from hunter2.bus import Alive
        t = time.time()
        b.heard_alive(Alive(id='sensor-q', at=t, exits=1, saw_at=t,
                            stock=tuple(stock), parts=tuple(scope),
                            src='sensor-q'))

    def test_a_snapshot_from_another_scope_leaves_us_alone(self):
        from hunter2.bus import Sighting
        b = bare_buyer(parts=['P', 'Q'])
        b.heard_of(Sighting(part='P', store='R581', at=time.time(), src='sensor-p'))
        b.worker.observe.reset_mock()
        self.beat(b, scope=['Q'])              # 只盯 Q 的探针
        self.assertEqual({'R581'}, set(b.live['P']), '别人的范围把我们的货撤了')
        self.assertTrue(b.stock_live('P'))

    def test_a_snapshot_in_scope_still_applies(self):
        from hunter2.bus import Sighting
        b = bare_buyer(parts=['P', 'Q'])
        b.heard_of(Sighting(part='P', store='R581', at=time.time() - 3,
                            src='sensor-p'))
        self.beat(b, scope=['P'])
        self.assertEqual({}, b.live['P'])
        self.assertFalse(b.stock_live('P'))

    def test_each_source_keeps_its_own_reading(self):
        """P 的探针说 P 没了，不该让 Q 的判断也跟着过期。"""
        b = bare_buyer(parts=['P', 'Q'])
        self.beat(b, scope=['Q'], stock=['Q:R581'])
        self.assertTrue(b.stock_live('Q'))
        self.assertIsNone(b.stock_live('P'), 'P 从来没人查过，不该有结论')


class StoreOrderTests(unittest.TestCase):
    """门店的名次要按它在配置里的下标。

    全给同一个数的话，后面那层排序只能按编号字典序——配了 [R581, R359] 结果
    先打 R359，而人写的顺序就是「近的排前面」。
    """

    def test_the_configured_order_is_kept(self):
        from hunter2.bus import Sighting
        b = bare_buyer(parts=['P'], only_stores=['R581', 'R359'])
        for st in ('R359', 'R581'):
            b.heard_of(Sighting(part='P', store=st, at=time.time(), src='scout'))
        offers = b.worker.observe.call_args.args[1]
        self.assertEqual(['R581', 'R359'], [o.store for o in offers])

    def test_an_unconfigured_store_sorts_last(self):
        from hunter2.bus import Sighting
        b = bare_buyer(parts=['P'])
        b.heard_of(Sighting(part='P', store='R999', at=time.time(), src='scout'))
        self.assertEqual(0, b.live['P']['R999'].priority[1])


class PlainWatchTests(unittest.TestCase):
    """`python -m hunter watch`——单机、不联机的那条路，仍然要能用。

    盯和买在同一个进程里是它的固有代价（见 scout 那边的说明），但已知的坑
    不能带着跑。
    """

    def test_the_sensor_boosts_when_it_sees_stock(self):
        """**探针是强制关掉自动下单的**，冲刺挂在「有没有买手」的分支里等于
        它永远不提速——而它恰恰是那个负责早一点看见的角色。"""
        from hunter.monitor import StockWatcher
        w = StockWatcher.__new__(StockWatcher)
        w.only_stores, w.parts, w.note_of = [], ['P'], {}
        w.cfg, w.purchase_worker = {}, None
        w.state, w.log, w.hit = Mock(), Mock(), Mock()
        w.state.get.return_value = None
        w.client = SimpleNamespace(observed_at={})
        w._buy_url = lambda p: 'url'
        w.pacer = Mock()
        w.pacer.boost.return_value = time.monotonic() + 180
        StockWatcher._check_pickup(w, 'P', [store('R581')])
        w.pacer.boost.assert_called_once()

    def test_unknown_stock_is_not_a_definitive_reading(self):
        """结果全未知时急刹不该说「没货」——那是把未知当成确定无货。"""
        from hunter.monitor import StockWatcher
        w = StockWatcher.__new__(StockWatcher)
        w.only_stores, w.parts, w.note_of = [], ['P'], {}
        w.cfg, w.purchase_worker = {}, Mock()
        w.state, w.log, w.hit, w.pacer = Mock(), Mock(), Mock(), None
        w.state.get.return_value = None
        w.client = SimpleNamespace(observed_at={})
        w._buy_url = lambda p: 'url'
        StockWatcher._check_pickup(w, 'P', [store('R581', Stock.UNKNOWN)])
        self.assertFalse(w.purchase_worker.observe.call_args.kwargs['definitive'])

    def test_a_known_result_is_definitive(self):
        from hunter.monitor import StockWatcher
        w = StockWatcher.__new__(StockWatcher)
        w.only_stores, w.parts, w.note_of = [], ['P'], {}
        w.cfg, w.purchase_worker = {}, Mock()
        w.state, w.log, w.hit, w.pacer = Mock(), Mock(), Mock(), None
        w.state.get.return_value = None
        w.client = SimpleNamespace(observed_at={})
        w._buy_url = lambda p: 'url'
        StockWatcher._check_pickup(w, 'P', [store('R581', Stock.UNAVAILABLE)])
        self.assertTrue(w.purchase_worker.observe.call_args.kwargs['definitive'])

    def test_a_non_definitive_reading_does_not_arm_the_brake(self):
        from hunter.purchase_worker import PurchaseWorker
        w = PurchaseWorker(Mock(cfg={}), Mock(), log=lambda *a: None)
        w.observe('P', [], definitive=False)
        self.assertIsNone(w.stock_live('P'), '未知被当成了确定无货')

    def test_a_definitive_reading_does_arm_the_brake(self):
        from hunter.purchase_worker import PurchaseWorker
        w = PurchaseWorker(Mock(cfg={}), Mock(), log=lambda *a: None)
        w.observe('P', [])
        self.assertFalse(w.stock_live('P'))

class SnapshotDoesNotRollBackTests(unittest.TestCase):
    """快照只能把观察时刻往前推，不能往后拉。

    刚收到的 seen 可能比快照还新（一轮拆成几批时就是这样）。无条件盖回去的话，
    候选会被改成九十秒前的观察：队列判它过期，而急刹还说有货——买不了也停不下来。
    """

    def test_a_newer_sighting_is_not_rolled_back(self):
        from hunter2.bus import Alive, Sighting
        b = bare_buyer(parts=['P'])
        t = time.time()
        b.heard_of(Sighting(part='P', store='R581', at=t, src='scout'))
        fresh = b.live['P']['R581'].observed
        b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t - 90,
                            stock=('P:R581',), parts=('P',), src='scout'))
        self.assertEqual(fresh, b.live['P']['R581'].observed,
                         '候选被一份旧快照改成九十秒前的观察了')

    def test_a_newer_snapshot_does_refresh(self):
        from hunter2.bus import Alive, Sighting
        b = bare_buyer(parts=['P'])
        t = time.time()
        b.heard_of(Sighting(part='P', store='R581', at=t - 30, src='scout'))
        b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t,
                            stock=('P:R581',), parts=('P',), src='scout'))
        self.assertLess(time.monotonic() - b.live['P']['R581'].observed, 1.0)


class SoldOutAccountingTests(unittest.TestCase):
    """售罄计数：要有基线、按来源隔离、只在真涨了的时候才重置。"""

    def beat(self, b, gone, stock=('P:R581',), src='scout'):
        from hunter2.bus import Alive
        t = time.time()
        b.heard_alive(Alive(id=src, at=t, exits=1, saw_at=t, stock=tuple(stock),
                            parts=('P',), gone=tuple(gone), src=src))

    def resets(self, b):
        """重试状态被重置了几次。**看 restock，不看 observe**——撤销候选和
        重置重试状态是两件事，混在一起做会把候选连带清空。"""
        return [c.args[0] for c in b.worker.restock.call_args_list]

    def test_the_sender_always_ships_a_baseline(self):
        """只发卖空过的那些，买手第一次收到 P:1 时没有基线可比。"""
        import inspect
        from scout.main import Scout
        self.assertIn('self.gone.get(p, 0)', inspect.getsource(Scout.beat))
        self.assertIn('for p in self.parts', inspect.getsource(Scout.beat))

    def test_the_first_sell_out_after_connecting_resets(self):
        b = bare_buyer(parts=['P'])
        self.beat(b, ['P:0'])                  # 基线
        b.worker.observe.reset_mock()
        self.beat(b, ['P:1'])                  # 第一次卖空
        self.assertTrue(self.resets(b), '第一次「售罄→补货」没能恢复重试')

    def test_a_first_contact_only_records_a_baseline(self):
        """买手起来时对端已经卖空过几次，那跟我们没关系。"""
        b = bare_buyer(parts=['P'])
        self.beat(b, ['P:5'])
        self.assertFalse(self.resets(b))

    def test_two_sources_do_not_share_a_counter(self):
        """两个探针各报各的 P:5 和 P:2，交替到达不该被当成反复卖空。"""
        b = bare_buyer(parts=['P'])
        for _ in range(3):
            self.beat(b, ['P:5'], src='sensor-a')
            self.beat(b, ['P:2'], src='sensor-b')
        b.worker.observe.reset_mock()
        self.beat(b, ['P:5'], src='sensor-a')
        self.beat(b, ['P:2'], src='sensor-b')
        self.assertFalse(self.resets(b), '两个来源的计数被混在一起比了')

    def test_one_store_leaving_is_not_a_sell_out(self):
        """R359 卖完而 R581 一直有货，失败次数不该被清掉。"""
        from hunter2.bus import Sighting
        b = bare_buyer(parts=['P'])
        t = time.time()
        for st in ('R581', 'R359'):
            b.heard_of(Sighting(part='P', store=st, at=t - 3, src='scout'))
        self.beat(b, ['P:0'], stock=('P:R581',))
        b.worker.observe.reset_mock()
        self.beat(b, ['P:0'], stock=('P:R581',))
        self.assertFalse(self.resets(b), '一家门店退出候选被当成整个型号卖空了')


class StoreScopeTests(unittest.TestCase):
    """范围的另一半：门店。

    两个探针盯同一个型号、不同门店时，少了这一半它们会互相撤掉对方的货。
    """

    def test_a_sensor_cannot_revoke_a_store_it_never_watched(self):
        from hunter2.bus import Alive, Sighting
        b = bare_buyer(parts=['P'])
        t = time.time()
        b.heard_of(Sighting(part='P', store='R581', at=t - 3, src='sensor-a'))
        b.heard_alive(Alive(id='sensor-b', at=t, exits=1, saw_at=t, stock=(),
                            parts=('P',), stores=('R359',), src='sensor-b'))
        self.assertEqual({'R581'}, set(b.live['P']),
                         '只查 R359 的探针把 R581 的货撤了')

    def test_a_sensor_can_revoke_a_store_it_does_watch(self):
        from hunter2.bus import Alive, Sighting
        b = bare_buyer(parts=['P'])
        t = time.time()
        b.heard_of(Sighting(part='P', store='R359', at=t - 3, src='sensor-b'))
        b.heard_alive(Alive(id='sensor-b', at=t, exits=1, saw_at=t, stock=(),
                            parts=('P',), stores=('R359',), src='sensor-b'))
        self.assertEqual({}, b.live['P'])

    def test_an_empty_store_scope_is_authoritative_everywhere(self):
        """没配门店 = 盯附近全部，对哪家店都算数。"""
        from hunter2.bus import Alive, Sighting
        b = bare_buyer(parts=['P'])
        t = time.time()
        b.heard_of(Sighting(part='P', store='R581', at=t - 3, src='scout'))
        b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t, stock=(),
                            parts=('P',), stores=(), src='scout'))
        self.assertEqual({}, b.live['P'])

    def test_the_master_ships_its_store_scope(self):
        import inspect
        from scout.main import Scout
        self.assertIn('stores=tuple(self.only_stores)',
                      inspect.getsource(Scout.beat))


class NonDefinitiveBrakeTests(unittest.TestCase):
    """没看清的读数不能留下「刚刚查过」的痕迹。"""

    def worker(self):
        from hunter.purchase_worker import PurchaseWorker
        return PurchaseWorker(Mock(cfg={}), Mock(), log=lambda *a: None)

    def test_an_unclear_round_invalidates_the_previous_one(self):
        """**先有货、随后没看清**：留着旧的 polled 的话，一次未知就掐掉一单。"""
        from hunter.purchase_worker import Offer
        w = self.worker()
        w.observe('P', [Offer('P', 'R581', '五角场', 'u', 'P',
                              time.monotonic(), (0, 0))])
        w.observe('P', [], definitive=False)
        self.assertIsNone(w.stock_live('P'), '拿上一轮的时刻算出了「刚刚查过」')

    def test_a_definitive_round_still_arms_it(self):
        w = self.worker()
        w.observe('P', [])
        self.assertFalse(w.stock_live('P'))

    def test_a_missing_configured_store_is_not_definitive(self):
        """配置了 R581、R359，结果里只有 R581——R359 是「不知道」，不是「没货」。"""
        from hunter.monitor import StockWatcher
        w = StockWatcher.__new__(StockWatcher)
        w.only_stores, w.parts, w.note_of = ['R581', 'R359'], ['P'], {}
        w.cfg, w.purchase_worker = {}, Mock()
        w.state, w.log, w.hit, w.pacer = Mock(), Mock(), Mock(), None
        w.state.get.return_value = None
        w.client = SimpleNamespace(observed_at={})
        w._buy_url = lambda p: 'url'
        StockWatcher._check_pickup(w, 'P', [store('R581', Stock.UNAVAILABLE)])
        self.assertFalse(w.purchase_worker.observe.call_args.kwargs['definitive'])


class StoreScopedClearTests(unittest.TestCase):
    """完整读数要按门店记，不能按型号盖一个时间戳。

    只查 R359 的探针发来的快照会给整个型号盖上时间戳，之后一条稍早的 R581
    有货消息就被当成过时的丢掉——而那个探针根本没查过 R581。
    """

    def beat(self, b, src, stores, stock=(), ago=0.0):
        from hunter2.bus import Alive
        t = time.time() - ago
        b.heard_alive(Alive(id=src, at=time.time(), exits=1, saw_at=t,
                            stock=tuple(stock), parts=('P',),
                            stores=tuple(stores), src=src))

    def test_a_scoped_reading_does_not_shadow_another_store(self):
        from hunter2.bus import Sighting
        b = bare_buyer(parts=['P'], only_stores=['R581', 'R359'])
        self.beat(b, 'sensor-b', ['R359'], stock=())          # 只查了 R359
        b.heard_of(Sighting(part='P', store='R581', at=time.time() - 1,
                            src='sensor-a'))
        self.assertEqual({'R581'}, set(b.live['P']),
                         '只查 R359 的读数把稍早的 R581 有货消息挡掉了')

    def test_the_same_store_still_shadows_an_older_sighting(self):
        from hunter2.bus import Sighting
        b = bare_buyer(parts=['P'], only_stores=['R581'])
        self.beat(b, 'sensor-a', ['R581'], stock=())
        b.heard_of(Sighting(part='P', store='R581', at=time.time() - 1,
                            src='sensor-a'))
        self.assertEqual({}, b.live.get('P', {}))

    def test_the_brake_needs_every_store_covered(self):
        """只查了一家的探针说「没货」，代表不了另一家。"""
        b = bare_buyer(parts=['P'], only_stores=['R581', 'R359'])
        self.beat(b, 'sensor-b', ['R359'], stock=())
        self.assertIsNone(b.stock_live('P'), 'R581 没人查过就判了无货')
        self.beat(b, 'sensor-a', ['R581'], stock=())
        self.assertFalse(b.stock_live('P'))

    def test_an_unscoped_source_covers_everything(self):
        b = bare_buyer(parts=['P'], only_stores=['R581', 'R359'])
        self.beat(b, 'scout', [], stock=())
        self.assertFalse(b.stock_live('P'))


class GoneBaselineTests(unittest.TestCase):
    """售罄计数的基线要独立于快照记下来。

    启动那条心跳带着 P:0 但没有 saw_at，挂在快照处理里的话基线根本记不下来；
    等真的「有货→售罄→补货」发生完，收到 P:1 又会被当成第一次接触。
    """

    def beat(self, b, gone, saw=True, stores=()):
        from hunter2.bus import Alive
        t = time.time()
        b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=t if saw else 0.0,
                            stock=(), parts=('P',) if saw else (),
                            stores=tuple(stores), gone=tuple(gone), src='scout'))

    def resets(self, b):
        return [c.args[0] for c in b.worker.restock.call_args_list]

    def test_a_startup_beat_records_the_baseline(self):
        b = bare_buyer(parts=['P'])
        self.beat(b, ['P:0'], saw=False)       # 启动心跳：还没看清过
        b.worker.observe.reset_mock()
        self.beat(b, ['P:1'], saw=False)       # 中间卖空过一次
        self.assertTrue(self.resets(b), '启动心跳里的基线没记下来')

    def test_a_sightless_beat_does_not_pretend_to_be_a_snapshot(self):
        from hunter2.bus import Sighting
        b = bare_buyer(parts=['P'])
        b.heard_of(Sighting(part='P', store='R581', at=time.time(), src='scout'))
        self.beat(b, ['P:0'], saw=False)
        self.assertEqual({'R581'}, set(b.live['P']), '没看清的心跳撤了候选')

    def test_a_reset_outside_the_scope_is_not_ours(self):
        """只监控 R359 的探针报售罄，跟我们在 R581 的失败次数没关系。"""
        from hunter2.bus import Sighting
        b = bare_buyer(parts=['P'], only_stores=['R581'])
        b.heard_of(Sighting(part='P', store='R581', at=time.time(), src='scout'))
        self.beat(b, ['P:0'], saw=False, stores=['R359'])
        b.worker.observe.reset_mock()
        self.beat(b, ['P:1'], saw=False, stores=['R359'])
        self.assertFalse(self.resets(b), '范围外的售罄清掉了我们的失败次数')

    def test_a_reset_inside_the_scope_is_ours(self):
        from hunter2.bus import Sighting
        b = bare_buyer(parts=['P'], only_stores=['R581'])
        b.heard_of(Sighting(part='P', store='R581', at=time.time(), src='scout'))
        self.beat(b, ['P:0'], saw=False, stores=['R581'])
        b.worker.observe.reset_mock()
        self.beat(b, ['P:1'], saw=False, stores=['R581'])
        self.assertEqual(['P'], self.resets(b))


class UnknownThenSoldOutTests(unittest.TestCase):
    """「未知 → 明确售罄 → 补货」必须能恢复重试。

    未知读数会先清空候选，紧接着那条明确售罄就找不到 prev 了，重置被跳过——
    同一家店再补货后，旧的失败次数、burned、tried 全都还在，队列再也不试它。
    """

    def worker(self):
        from hunter.purchase_worker import PurchaseWorker
        return PurchaseWorker(Mock(cfg={}), Mock(), max_attempts=1,
                              log=lambda *a: None)

    def offer(self, store='R581'):
        from hunter.purchase_worker import Offer
        return Offer('P', store, '五角场', 'u', 'P', time.monotonic(), (0, 0))

    def test_the_reset_survives_an_unknown_round_in_between(self):
        w = self.worker()
        w.observe('P', [self.offer()])
        w.attempts['P'] = (1, w.clock())
        w.tried['P'] = {'R581'}
        w.burned.add('P')
        w.observe('P', [], definitive=False)      # 接口抖了一下
        w.observe('P', [], ['R581'])              # 然后明确卖光
        self.assertNotIn('P', w.burned, '中间插了一次未知，重置就被跳过了')
        self.assertNotIn('P', w.tried)
        self.assertNotIn('P', w.attempts)

    def test_a_restock_after_that_is_buyable(self):
        w = self.worker()
        w.observe('P', [self.offer()])
        w.attempts['P'] = (1, w.clock())
        w.burned.add('P')
        w.observe('P', [], definitive=False)
        w.observe('P', [], ['R581'])
        w.observe('P', [self.offer()])
        self.assertIsNotNone(w._next(), '补货之后仍然拿不到候选')

    def test_a_part_never_seen_in_stock_is_not_reset(self):
        """从没见过货就不存在「卖完了」，别把它当成新一轮。"""
        w = self.worker()
        w.burned.add('P')
        w.observe('P', [], ['R581'])
        self.assertIn('P', w.burned)


class MissingEveryStoreTests(unittest.TestCase):
    """配置的门店一家都没返回时，那是「不知道」，不是「没货」。

    这条提前返回的分支绕过了后面所有的完整性检查——一家都没返回恰恰说明我们
    什么都没查到，而默认的 definitive=True 会让急刹把它当成确定无货。
    """

    def watcher(self):
        from hunter.monitor import StockWatcher
        w = StockWatcher.__new__(StockWatcher)
        w.only_stores, w.parts, w.note_of = ['R581'], ['P'], {}
        w.cfg, w.purchase_worker = {}, Mock()
        w.state, w.log, w.hit, w.pacer = Mock(), Mock(), Mock(), None
        w.state.get.return_value = None
        w.client = SimpleNamespace(observed_at={})
        w._buy_url = lambda p: 'url'
        return w

    def test_it_is_not_a_definitive_reading(self):
        from hunter.monitor import StockWatcher
        w = self.watcher()
        StockWatcher._check_pickup(w, 'P', [store('R359', Stock.AVAILABLE)])
        self.assertFalse(w.purchase_worker.observe.call_args.kwargs['definitive'],
                         '一家配置门店都没返回，却被当成了确定无货')

    def test_the_brake_abstains(self):
        from hunter.monitor import StockWatcher
        from hunter.purchase_worker import PurchaseWorker
        w = self.watcher()
        w.purchase_worker = PurchaseWorker(Mock(cfg={}), Mock(),
                                           log=lambda *a: None)
        StockWatcher._check_pickup(w, 'P', [store('R359', Stock.AVAILABLE)])
        self.assertIsNone(w.purchase_worker.stock_live('P'))


class StaleWideSnapshotTests(unittest.TestCase):
    """过时与否要一家一家判。

    一刀切的话，一条覆盖 R359+R581 的旧快照会因为「R581 还没人查过」而整条被
    放行，顺带把已经售罄的 R359 又加回来。
    """

    def beat(self, b, stores, stock, ago=0.0, src='scout'):
        from hunter2.bus import Alive
        t = time.time() - ago
        b.heard_alive(Alive(id=src, at=time.time(), exits=1, saw_at=t,
                            stock=tuple(stock), parts=('P',),
                            stores=tuple(stores), src=src))

    def test_an_old_wide_snapshot_cannot_revive_a_sold_out_store(self):
        b = bare_buyer(parts=['P'], only_stores=['R581', 'R359'])
        self.beat(b, ['R359'], [], ago=0.0)                  # R359 刚卖完
        self.beat(b, ['R359', 'R581'], ['P:R359'], ago=5.0)  # 五秒前的旧快照
        self.assertNotIn('R359', b.live.get('P', {}),
                         '旧快照把已经售罄的门店又加回来了')

    def test_an_old_unscoped_snapshot_cannot_either(self):
        b = bare_buyer(parts=['P'], only_stores=['R581', 'R359'])
        self.beat(b, ['R359'], [], ago=0.0)
        self.beat(b, [], ['P:R359'], ago=5.0)                # 不限门店的旧快照
        self.assertNotIn('R359', b.live.get('P', {}))

    def test_the_untouched_store_still_gets_the_old_news(self):
        """R581 还没人查过，那条旧快照对它仍然是新消息。"""
        b = bare_buyer(parts=['P'], only_stores=['R581', 'R359'])
        self.beat(b, ['R359'], [], ago=0.0)
        self.beat(b, ['R359', 'R581'], ['P:R359', 'P:R581'], ago=5.0)
        self.assertEqual({'R581'}, set(b.live.get('P', {})))

    def test_a_newer_wide_snapshot_is_authoritative(self):
        b = bare_buyer(parts=['P'], only_stores=['R581', 'R359'])
        self.beat(b, ['R359'], [], ago=5.0)
        self.beat(b, ['R359', 'R581'], ['P:R359'], ago=0.0)
        self.assertEqual({'R359'}, set(b.live['P']))


class ResetWithoutWipingTests(unittest.TestCase):
    """重置重试状态 ≠ 撤销候选。

    心跳因为超长降级成 saw_at=0，却仍然带着涨了的 gone 时，用 observe(part, [])
    来重置会把候选清空，而后面没有任何快照来恢复它们——急刹说有货，队列却拿
    不出候选。
    """

    def test_a_degraded_beat_resets_without_emptying_the_queue(self):
        from hunter2.bus import Alive, Sighting
        from hunter.purchase_worker import PurchaseWorker
        b = bare_buyer(parts=['P'])
        b.worker = PurchaseWorker(Mock(cfg={}), Mock(), log=lambda *a: None)
        t = time.time()
        b.heard_of(Sighting(part='P', store='R581', at=t, src='scout'))
        b.heard_alive(Alive(id='scout', at=t, exits=1, saw_at=0.0,
                            gone=('P:0',), src='scout'))
        b.heard_alive(Alive(id='scout', at=t + 1, exits=1, saw_at=0.0,
                            gone=('P:1',), src='scout'))
        self.assertTrue(b.stock_live('P'))
        self.assertIsNotNone(b.worker._next(), '重置把候选一起清空了')

    def test_restock_clears_the_retry_state(self):
        from hunter.purchase_worker import PurchaseWorker
        w = PurchaseWorker(Mock(cfg={}), Mock(), log=lambda *a: None)
        w.attempts['P'] = (3, w.clock())
        w.tried['P'] = {'R581'}
        w.burned.add('P')
        w.restock('P')
        self.assertNotIn('P', w.attempts)
        self.assertNotIn('P', w.tried)
        self.assertNotIn('P', w.burned)

    def test_restock_leaves_the_candidates_alone(self):
        from hunter.purchase_worker import Offer, PurchaseWorker
        w = PurchaseWorker(Mock(cfg={}), Mock(), log=lambda *a: None)
        w.observe('P', [Offer('P', 'R581', '五角场', 'u', 'P',
                              time.monotonic(), (0, 0))])
        w.restock('P')
        self.assertTrue(w.offers)


class OutOfOrderBeatTests(unittest.TestCase):
    """乱序心跳不能把售罄计数倒着走。

    UDP 不保证顺序。5 → 4 → 5 这种乱序会把基线改成 4，下一条正常心跳就成了
    「又卖空一次」的假信号，把 burned 和尝试次数白白清掉。
    """

    def beat(self, b, n, at):
        from hunter2.bus import Alive
        b.heard_alive(Alive(id='scout', at=at, exits=1, saw_at=0.0,
                            gone=(f'P:{n}',), src='scout'))

    def test_a_late_packet_does_not_roll_the_counter_back(self):
        b = bare_buyer(parts=['P'])
        t = time.time()
        self.beat(b, 5, t)
        self.beat(b, 4, t - 10)        # 迟到的旧包
        b.worker.restock.reset_mock()
        self.beat(b, 5, t + 1)         # 下一条正常心跳
        b.worker.restock.assert_not_called()

    def test_a_genuine_increment_still_fires(self):
        b = bare_buyer(parts=['P'])
        t = time.time()
        self.beat(b, 5, t)
        self.beat(b, 6, t + 1)
        b.worker.restock.assert_called_once()

    def test_a_restarted_source_only_rebaselines(self):
        """对端重启，计数从头开始——那不是「又卖空一次」。"""
        b = bare_buyer(parts=['P'])
        t = time.time()
        self.beat(b, 5, t)
        self.beat(b, 0, t + 1)         # 重启了
        b.worker.restock.assert_not_called()
        self.beat(b, 1, t + 2)
        b.worker.restock.assert_called_once()


class EmptyResultTests(unittest.TestCase):
    """空结果统一按未知处理。

    不限制门店时 unknown_now 和 missing 都是空的，于是一个什么都没返回的响应
    被当成了「附近一家都没货」——后面那句「保留上次状态」来得太晚。
    """

    def test_an_empty_result_is_not_definitive(self):
        from hunter.monitor import StockWatcher
        w = StockWatcher.__new__(StockWatcher)
        w.only_stores, w.parts, w.note_of = [], ['P'], {}
        w.cfg, w.purchase_worker = {}, Mock()
        w.state, w.log, w.hit, w.pacer = Mock(), Mock(), Mock(), None
        w.state.get.return_value = None
        w.client = SimpleNamespace(observed_at={})
        w._buy_url = lambda p: 'url'
        StockWatcher._check_pickup(w, 'P', [])
        self.assertFalse(w.purchase_worker.observe.call_args.kwargs['definitive'])

    def test_the_brake_abstains_on_an_empty_result(self):
        from hunter.monitor import StockWatcher
        from hunter.purchase_worker import PurchaseWorker
        w = StockWatcher.__new__(StockWatcher)
        w.only_stores, w.parts, w.note_of = [], ['P'], {}
        w.cfg = {}
        w.purchase_worker = PurchaseWorker(Mock(cfg={}), Mock(),
                                           log=lambda *a: None)
        w.state, w.log, w.hit, w.pacer = Mock(), Mock(), Mock(), None
        w.state.get.return_value = None
        w.client = SimpleNamespace(observed_at={})
        w._buy_url = lambda p: 'url'
        StockWatcher._check_pickup(w, 'P', [])
        self.assertIsNone(w.purchase_worker.stock_live('P'))
