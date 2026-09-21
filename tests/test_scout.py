"""主程序：轮转出口刷库存 → 广播 → 收子程序报到。

这里验的都是「买手看不见的东西」：主程序是它唯一的库存来源，主程序少喊一声，
买手就漏一次放货，而且不会有任何别的迹象。
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hunter.apple import PICKUP_PATH, Blocked, CoolingDown, Stock, StorePickup
from hunter2.bus import Alive, Enlist, Sighting

CFG = {
    "region": "cn",
    "timeout": 15,
    "watch": [
        {"part": "MJYA4CH/A", "model_slug": "iphone-17-pro", "note": "17Pro 银"},
        {"part": "MJYB4CH/A", "model_slug": "iphone-17-pro", "note": "17Pro 蓝"},
    ],
    "pickup": {"enabled": True, "location": "200000", "stores": []},
    "pacing": {"base_interval": 30, "min_interval": 4,
               "budget_per_hour": 9999, "burst": 999},
    "link": {"id": "scout", "port": 48799},
}


def store(num="R359", name="南京东路", state=Stock.AVAILABLE, reason=""):
    return StorePickup(part="MJYA4CH/A", store_number=num, store_name=name,
                       city="上海", state=state, quote="今天可取",
                       reason=reason or ("未知" if state is Stock.UNKNOWN else ""))


class FakeClient:
    """假的 AppleClient：只回放脚本，一个包都不发。"""

    base = "https://www.apple.com.cn"

    def __init__(self, **kw):
        self.breakers = {}
        self.observed_at = {}
        self.before_request = None
        self.calls = []
        self.script = []          # 每次 pickup 返回（或抛出）的东西
        self.age = 0.0            # 接口返回的时刻比「现在」早多少
        self.kw = kw

    def pickup(self, parts, location="", store=""):
        # 真的 AppleClient 每个请求前都会调它（apple.py 的 _get）。假的也必须调，
        # 否则「预算等待会不会堵住整轮」这件事根本测不出来。
        if self.before_request is not None:
            self.before_request()
        self.calls.append((tuple(parts), location))
        self.observed_at[PICKUP_PATH] = time.monotonic() - self.age
        out = self.script.pop(0) if self.script else {}
        if isinstance(out, Exception):
            raise out
        return out

    def buy_url(self, slug, part=""):
        return f"{self.base}/shop/buy-iphone/{slug}/{part}"


class Harness:
    """建一个 Scout：假的 Apple 客户端、假的总线、临时的状态目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clients = []
        self.sent = []
        self.pushed = []

    def _mk_client(self, **kw):
        c = FakeClient(**kw)
        self.clients.append(c)
        return c

    def scout(self, cfg=None):
        from scout.main import Scout
        cfg = cfg or CFG
        sender = Mock()
        sender.send.side_effect = lambda m: (self.sent.append(m), 1)[1]
        sender.oversize.return_value = False
        sender.shrink.side_effect = lambda m: m
        bc = Mock()
        bc.send.side_effect = lambda *a, **k: self.pushed.append(a)
        with patch("scout.exits.AppleClient", side_effect=self._mk_client), \
             patch("scout.main.Sender", return_value=sender), \
             patch("scout.main.Receiver", return_value=Mock()), \
             patch("scout.main.Broadcaster"), \
             patch("scout.main.AsyncBroadcaster", return_value=bc), \
             patch("hunter2.bus.bus_key", return_value=b"k"), \
             patch("scout.main.bus_key", return_value=b"k"):
            s = Scout(cfg, Path(self.tmp.name), log=lambda *a: None)
        self.direct = s.pool._exits["direct"]
        self.addCleanup(s.pool.close)
        return s

    def seen(self):
        return [m for m in self.sent if isinstance(m, Sighting)]

    def beats(self):
        return [m for m in self.sent if isinstance(m, Alive)]


class ScoutTests(Harness, unittest.TestCase):

    # ---------- 广播 ----------

    def test_stock_is_shouted(self):
        s = self.scout()
        self.direct.client.script = [{"MJYA4CH/A": [store()], "MJYB4CH/A": []}]
        s.poll(self.direct)
        self.assertEqual(1, len(self.seen()))
        self.assertEqual("MJYA4CH/A", self.seen()[0].part)
        self.assertEqual("R359", self.seen()[0].store)

    def test_it_shouts_every_round_not_only_on_change(self):
        """买手拿这个当心跳：一直收到 = 货还在。只在翻转时喊，货就「消失」了。"""
        s = self.scout()
        for _ in range(3):
            self.direct.client.script.append({"MJYA4CH/A": [store()]})
        for _ in range(3):
            s.poll(self.direct)
        self.assertEqual(3, len(self.seen()))

    def test_only_available_stores_are_shouted(self):
        s = self.scout()
        self.direct.client.script = [{"MJYA4CH/A": [
            store("R359"), store("R581", "浦东", Stock.UNAVAILABLE)]}]
        s.poll(self.direct)
        self.assertEqual(["R359"], [m.store for m in self.seen()])

    def test_all_unknown_is_not_treated_as_sold_out(self):
        """全未知当成没货，买手会踩急刹——那是最不该刹的时候。"""
        s = self.scout()
        self.direct.client.script = [{"MJYA4CH/A": [
            store("R359", state=Stock.UNKNOWN, reason="接口没返回")]}]
        s.poll(self.direct)
        self.assertEqual([], self.seen())
        self.assertEqual([], self.pushed)

    def test_the_observed_time_is_the_apis_not_now(self):
        """用「现在」的话，candidate_max_age 会把早就过期的信号当成新鲜的。"""
        s = self.scout()
        self.direct.client.script = [{"MJYA4CH/A": [store()]}]
        s.poll(self.direct)
        self.direct.client.age = 30.0      # 这一轮接口在 30 秒前就返回了
        self.direct.client.script = [{"MJYA4CH/A": [store()]}]
        s.poll(self.direct)
        a, b = self.seen()
        self.assertGreater(a.at - b.at, 25.0, "第二条的观察时刻没有回溯到接口返回的那一刻")

    def test_stores_we_do_not_go_to_are_not_shouted(self):
        s = self.scout(dict(CFG, pickup=dict(CFG["pickup"], stores=["R359"])))
        self.direct.client.script = [{"MJYA4CH/A": [store("R581", "浦东")]}]
        s.poll(self.direct)
        self.assertEqual([], self.seen())

    def test_seeing_stock_boosts_every_exit(self):
        """第一单没抢到时，后面几分钟最值钱——而且是所有出口一起提速。"""
        s = self.scout()
        with patch.object(s.pool, "boost") as boost:
            self.direct.client.script = [{"MJYA4CH/A": [store()]}]
            s.poll(self.direct)
        boost.assert_called()

    # ---------- 出错 ----------

    def test_a_later_group_failing_does_not_lose_earlier_stock(self):
        cfg = dict(CFG, watch=[
            dict(CFG["watch"][0], request_group="a"),
            dict(CFG["watch"][1], request_group="b")])
        s = self.scout(cfg)
        self.direct.client.script = [{"MJYA4CH/A": [store()]},
                                     RuntimeError("网断了")]
        s.poll(self.direct)
        self.assertEqual(1, len(self.seen()), "前一组发现的货被后一组的失败吞了")

    def test_being_blocked_reports_the_retry_after(self):
        s = self.scout()
        self.direct.client.script = [Blocked("541", retry_after=42.0)]
        ok, retry = s.poll(self.direct)
        self.assertFalse(ok)
        self.assertEqual(42.0, retry)

    def test_cooling_down_is_not_a_block(self):
        """静默期是我们自己决定先别碰，不该再记一次退避——那是给封禁续期。"""
        s = self.scout()
        self.direct.client.script = [CoolingDown(PICKUP_PATH, 30.0)]
        ok, retry = s.poll(self.direct)
        self.assertFalse(ok)
        self.assertIsNone(retry)

    def test_a_blocked_exit_backs_off_alone(self):
        s = self.scout()
        with patch("scout.exits.ExitPool._proxy_url",
                   side_effect=lambda i, p: f"http://u:k@{i}:{p}"):
            s.pool.enlist("b", "192.168.1.9", 48712, False)
        other = s.pool._exits["b"]
        before = other.due_at
        self.direct.client.script = [Blocked("541", retry_after=90.0)]
        ok, retry = s.poll(self.direct)
        s.pool.blocked(self.direct, retry)
        self.assertEqual(before, other.due_at, "另一条出口被连累了")

    # ---------- 心跳 ----------

    def test_it_beats_so_buyers_know_it_is_alive(self):
        s = self.scout()
        s.beat(force=True)
        self.assertEqual(1, len(self.beats()))
        self.assertEqual("scout", self.beats()[0].id)

    def test_the_beat_carries_how_many_exits(self):
        s = self.scout()
        with patch("scout.exits.ExitPool._proxy_url",
                   side_effect=lambda i, p: f"http://u:k@{i}:{p}"):
            s.pool.enlist("b", "192.168.1.9", 48712, False)
        s.beat(force=True)
        self.assertEqual(2, self.beats()[0].exits)

    def test_the_beat_does_not_thin_out_with_the_polling(self):
        """被拦之后轮询间隔会被退避拉到几分钟。心跳跟着一起稀的话，
        买手会在一次正常的退避里误判主程序死了，白白叫醒人。"""
        s = self.scout()
        s.beat_every = 10.0
        s.last_beat = time.monotonic() - 11
        s.beat()
        self.assertEqual(1, len(self.beats()))

    def test_it_does_not_beat_faster_than_configured(self):
        s = self.scout()
        s.beat(force=True)
        s.beat()
        self.assertEqual(1, len(self.beats()), "心跳太密，纯属烧网络")

    # ---------- 子程序报到 ----------

    def test_an_enlist_adds_an_exit(self):
        s = self.scout()
        with patch("scout.exits.ExitPool._proxy_url",
                   side_effect=lambda i, p: f"http://u:k@{i}:{p}") as url:
            s.on_bus(Enlist(id="b", proxy_port=48712), ip="192.168.1.9")
        self.assertEqual(2, len(s.pool))
        url.assert_called_with("192.168.1.9", 48712)

    def test_the_ip_comes_from_the_packet_not_the_message(self):
        """子程序对自己内网地址的猜测，在多网卡 / 容器 / WSL 下经常是错的。"""
        s = self.scout()
        with patch("scout.exits.ExitPool._proxy_url",
                   side_effect=lambda i, p: f"http://u:k@{i}:{p}") as url:
            s.on_bus(Enlist(id="b", proxy_port=48712), ip="10.0.0.5")
        self.assertEqual("10.0.0.5", url.call_args[0][0])

    def test_a_same_exit_child_does_not_become_an_exit(self):
        s = self.scout()
        s.on_bus(Enlist(id="b", proxy_port=0, direct=True), ip="192.168.1.9")
        self.assertEqual(1, len(s.pool))

    def test_a_sighting_on_the_bus_is_ignored(self):
        """主程序自己就是眼睛，不需要别人喂——收得越少，能出错的地方越少。"""
        s = self.scout()
        s.on_bus(Sighting(part="P", store="S"), ip="192.168.1.9")
        self.assertEqual(1, len(s.pool))

    # ---------- 推送给人 ----------

    def test_a_human_is_told_once_not_every_round(self):
        s = self.scout()
        for _ in range(3):
            self.direct.client.script.append({"MJYA4CH/A": [store()]})
        for _ in range(3):
            s.poll(self.direct)
        self.assertEqual(1, len(self.pushed), "每轮都推，一次补货能把手机刷爆")
        self.assertEqual(3, len(self.seen()), "推送去重不能把广播也一起去掉")

    def test_stock_coming_back_is_told_again(self):
        s = self.scout()
        self.direct.client.script = [{"MJYA4CH/A": [store()]},
                                     {"MJYA4CH/A": [store(state=Stock.UNAVAILABLE)]},
                                     {"MJYA4CH/A": [store()]}]
        for _ in range(3):
            s.poll(self.direct)
        self.assertEqual(2, len(self.pushed))

    def test_the_first_round_says_it_was_already_there(self):
        s = self.scout()
        self.direct.client.script = [{"MJYA4CH/A": [store()]}]
        s.poll(self.direct)
        self.assertIn("启动时", self.pushed[0][0])


class StartupTests(unittest.TestCase):
    def cfg(self, **kw):
        return dict(CFG, **kw)

    def make(self, cfg):
        from scout.main import Scout
        with patch("scout.exits.AppleClient", side_effect=lambda **k: Mock(breakers={})), \
             patch("scout.main.Broadcaster"), patch("scout.main.AsyncBroadcaster"):
            return Scout(cfg, Path("/tmp"), log=lambda *a: None)

    def test_no_bus_key_is_fatal(self):
        """主程序的产出就是广播出去的信号，发不出去等于白跑一整天。"""
        with patch("scout.main.bus_key", return_value=b""):
            with self.assertRaises(SystemExit) as e:
                self.make(self.cfg())
        self.assertIn("HUNTER_BUS_KEY", str(e.exception))

    def test_no_location_is_fatal(self):
        with patch("scout.main.bus_key", return_value=b"k"):
            with self.assertRaises(SystemExit) as e:
                self.make(self.cfg(pickup={"enabled": True, "location": ""}))
        self.assertIn("location", str(e.exception))

    def test_nothing_enabled_is_fatal(self):
        with patch("scout.main.bus_key", return_value=b"k"):
            with self.assertRaises(SystemExit):
                self.make(self.cfg(watch=[{"part": "X", "enabled": False}]))


if __name__ == "__main__":
    unittest.main()


class WiringTests(unittest.TestCase):
    """真的走一遍 UDP：子程序报到 → 主程序多一条出口 → 轮到它时用它发。

    这段接线只在两个进程之间成立，单元测试各自 mock 掉两头都会通过，而真跑起来
    少一条出口是**没有任何错误提示**的——巡检照常，只是慢一倍。
    """

    KEY = b"wiring-key-0123456789"

    @staticmethod
    def _free_port() -> int:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_a_child_becomes_an_exit(self):
        from hunter2.enlist import Enlister
        from scout.main import Scout

        port = self._free_port()
        cfg = dict(CFG, link={"id": "scout", "port": port, "peers": ["127.0.0.1"]})
        kid = {"link": {"id": "b", "port": port, "peers": ["127.0.0.1"]}}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        with patch("scout.exits.AppleClient", side_effect=lambda **k: FakeClient(**k)), \
             patch("scout.main.Broadcaster"), patch("scout.main.AsyncBroadcaster"), \
             patch("hunter2.bus.bus_key", return_value=self.KEY), \
             patch("hunter2.enlist.bus_key", return_value=self.KEY), \
             patch("scout.main.bus_key", return_value=self.KEY):
            s = Scout(cfg, Path(tmp.name), log=lambda *a: None)
            e = Enlister(kid, "b", log=lambda *a: None)
            s.receiver.start()
            try:
                fwd = e.start()
                for _ in range(60):
                    if len(s.pool) > 1:
                        break
                    time.sleep(0.05)
                # close() 会清空池子，所以先把要看的东西取出来
                grew, got = len(s.pool), s.pool._exits.get("b")
                direct = s.pool._exits["direct"]
            finally:
                e.close()
                s.receiver.close()
                s.pool.close()

        self.assertEqual(2, grew, "子程序报到了，主程序却没多一条出口")
        self.assertIn(f"127.0.0.1:{fwd}", got.proxy)
        self.assertIn("hunter:", got.proxy, "转发口地址里没带凭据，主程序会被 407")
        self.assertIsNot(got.pacer, direct.pacer,
                         "两条出口共用了一个节奏器——预算和退避是按 IP 算的")


class SightTests(Harness, unittest.TestCase):
    """心跳里的「我看清了」。

    买手的急刹拿它当「确实没货」，所以它必须只在**真的读准了**的时候往前推。
    推早了就是在接口抖动或者被限流的时候掐掉一次正在进行的下单。
    """

    def test_a_clean_round_advances_it(self):
        s = self.scout()
        self.direct.client.script = [{"MJYA4CH/A": [store(state=Stock.UNAVAILABLE)],
                                        "MJYB4CH/A": [store(state=Stock.UNAVAILABLE)]}]
        s.poll(self.direct)
        self.assertGreater(s.saw_at, 0.0)

    def test_unknown_does_not_count_as_seeing(self):
        """全未知当成「看清了、没货」，买手会在最不该刹的时候刹。"""
        s = self.scout()
        self.direct.client.script = [{
            "MJYA4CH/A": [store(state=Stock.UNKNOWN, reason="接口没返回")],
            "MJYB4CH/A": [store(state=Stock.UNAVAILABLE)]}]
        s.poll(self.direct)
        self.assertEqual(0.0, s.saw_at)

    def test_one_unknown_store_spoils_the_round(self):
        """没读准的那几家门店，可能正好是放货的那几家。"""
        s = self.scout()
        self.direct.client.script = [{
            "MJYA4CH/A": [store("R359", state=Stock.UNAVAILABLE),
                          store("R581", "浦东", Stock.UNKNOWN, reason="超时")],
            "MJYB4CH/A": [store(state=Stock.UNAVAILABLE)]}]
        s.poll(self.direct)
        self.assertEqual(0.0, s.saw_at)

    def test_a_failed_query_does_not_count(self):
        s = self.scout()
        self.direct.client.script = [RuntimeError("网断了")]
        s.poll(self.direct)
        self.assertEqual(0.0, s.saw_at)

    def test_being_blocked_does_not_count(self):
        """541 期间照样发心跳，但那时候什么都没看见。"""
        s = self.scout()
        self.direct.client.script = [Blocked("541", retry_after=90.0)]
        s.poll(self.direct)
        self.assertEqual(0.0, s.saw_at)

    def test_cooling_down_does_not_count(self):
        s = self.scout()
        self.direct.client.script = [CoolingDown(PICKUP_PATH, 30.0)]
        s.poll(self.direct)
        self.assertEqual(0.0, s.saw_at)

    def test_a_missing_part_spoils_the_round(self):
        """接口只回了一半型号，另一半就是没读数，不能当没货。"""
        s = self.scout()
        self.direct.client.script = [{"MJYA4CH/A": [store(state=Stock.UNAVAILABLE)]}]
        s.poll(self.direct)
        self.assertEqual(0.0, s.saw_at)

    def test_a_bad_round_does_not_roll_it_back(self):
        """看清过就是看清过。一次抖动不该把上一次的读数抹掉。"""
        s = self.scout()
        self.direct.client.script = [
            {"MJYA4CH/A": [store(state=Stock.UNAVAILABLE)],
             "MJYB4CH/A": [store(state=Stock.UNAVAILABLE)]},
            RuntimeError("网断了")]
        s.poll(self.direct)
        good = s.saw_at
        s.poll(self.direct)
        self.assertEqual(good, s.saw_at)

    def test_the_beat_carries_it(self):
        s = self.scout()
        self.direct.client.script = [{"MJYA4CH/A": [store(state=Stock.UNAVAILABLE)],
                                        "MJYB4CH/A": [store(state=Stock.UNAVAILABLE)]}]
        s.poll(self.direct)
        s.beat(force=True)
        self.assertEqual(s.saw_at, self.beats()[-1].saw_at)

    def test_a_beat_without_sight_says_so(self):
        """进程活着但什么都没看见——心跳照发，saw_at 是 0。"""
        s = self.scout()
        self.direct.client.script = [Blocked("541")]
        s.poll(self.direct)
        s.beat(force=True)
        self.assertEqual(1, len(self.beats()))
        self.assertEqual(0.0, self.beats()[-1].saw_at)


class BudgetTests(Harness, unittest.TestCase):
    """预算等待归调度器，不许在请求里原地睡。

    这个进程只有一个巡检线程：一条出口在请求里等令牌，所有出口跟着停，心跳也
    发不出去。实测一轮能堵到 120 秒，早就过了买手 90 秒的失联阈值。
    """

    def test_nothing_blocks_inside_a_request(self):
        s = self.scout()
        for e in s.pool._exits.values():
            self.assertIsNone(e.client.before_request,
                              "pacer.acquire 又被挂回请求路径了——它会同步睡眠")

    def test_a_round_never_sleeps(self):
        """六批查询、预算只剩一个令牌：旧写法在这儿一口气睡 120 秒，
        期间没有心跳，别的出口也一个都发不出去。"""
        # 六个 request_group = 一轮六个请求，这是真会发生的配置
        cfg = dict(CFG,
                   watch=[{"part": f"P{i}", "request_group": f"g{i}"} for i in range(6)],
                   pacing={"base_interval": 30, "min_interval": 4,
                           "budget_per_hour": 150, "burst": 1})
        s = self.scout(cfg)
        e = self.direct
        e.pacer.bucket.tokens = 1.0
        t0 = time.monotonic()
        s.poll(e)
        took = time.monotonic() - t0
        self.assertEqual(6, s.reqs)
        self.assertLess(took, 1.0,
                        f"一轮查询等了 {took:.0f}s 的预算，把所有出口和心跳都堵住了")

    def test_the_real_request_count_is_billed(self):
        cfg = dict(CFG, watch=[dict(CFG["watch"][0], request_group="a"),
                               dict(CFG["watch"][1], request_group="b")])
        s = self.scout(cfg)
        e = self.direct
        e.client.script = [{"MJYA4CH/A": []}, {"MJYB4CH/A": []}]
        before = e.pacer.bucket.tokens
        s.poll(e)
        s.pool.done(e, s.reqs)
        self.assertEqual(2, s.reqs)
        self.assertAlmostEqual(before - 2, e.pacer.bucket.tokens, places=3)

    def test_a_silenced_exit_is_not_billed(self):
        """熔断静默期里一个包都没发，不该扣预算。"""
        s = self.scout()
        e = self.direct
        e.client.script = [CoolingDown(PICKUP_PATH, 30.0)]
        s.poll(e)
        self.assertEqual(0, s.reqs)

    def test_the_budget_pushes_the_next_slot_out(self):
        """预算照样兑现，只是体现为「下次轮到它更晚」，而不是在请求里睡。"""
        s = self.scout(dict(CFG, pacing={"base_interval": 30, "min_interval": 4,
                                           "budget_per_hour": 150, "burst": 2}))
        e = self.direct
        e.pacer.bucket.tokens = 0.0
        s.pool.done(e, 1)
        self.assertGreater(e.due_at - time.monotonic(), 20.0)

    def test_requests_sent_before_a_block_are_still_billed(self):
        s = self.scout()
        e = self.direct
        before = e.pacer.bucket.tokens
        s.pool.blocked(e, 90.0, 3)
        self.assertAlmostEqual(before - 3, e.pacer.bucket.tokens, places=3)


class BeatThreadTests(Harness, unittest.TestCase):
    """心跳跑在自己的线程上：一次慢查询不能让买手以为主程序死了。"""

    def test_a_slow_round_does_not_starve_the_beat(self):
        s = self.scout()
        s.beat_every = 0.05
        slow = threading.Event()

        def crawl(parts, location="", store=""):
            slow.wait(1.0)                 # 模拟一次卡住的查询
            return {}

        self.direct.client.pickup = crawl
        s.receiver = Mock()
        t = threading.Thread(target=s.loop, daemon=True)
        t.start()
        try:
            time.sleep(0.6)
            beats = len(self.beats())
        finally:
            slow.set()
            s._stop.set()
            s.pool.close()
        self.assertGreater(beats, 2,
                           f"查询卡住的 0.6 秒里只发了 {beats} 条心跳——心跳被巡检堵住了")


class SlowRoundTests(Harness, unittest.TestCase):
    """一轮拆成几批、每批隔十几秒时，saw_at 不能用轮末时间。

    用轮末时间等于把三十秒前的读数包装成「刚刚看清的」，而买手会拿它盖掉一条
    同样是三十秒前的有货观察，然后中止一次根本没被查成无货的下单。
    """

    def cfg(self):
        return dict(CFG, watch=[dict(CFG["watch"][0], request_group="a"),
                                dict(CFG["watch"][1], request_group="b")])

    def test_it_records_when_the_first_batch_came_back(self):
        s = self.scout(self.cfg())
        e = self.direct
        e.age = 30.0                          # 两批都是 30 秒前返回的
        e.client.age = 30.0
        e.client.script = [{"MJYA4CH/A": [store(state=Stock.UNAVAILABLE)]},
                           {"MJYB4CH/A": [store(state=Stock.UNAVAILABLE)]}]
        s.poll(e)
        self.assertGreater(time.time() - s.saw_at, 25.0,
                           "把三十秒前的读数标成了刚刚看清的")

    def test_a_slow_round_expires_on_its_own(self):
        """整轮太慢时这个时刻自己就过期了，买手判不准，放行——这正是要的。"""
        from hunter2.buyer import Buyer
        s = self.scout(self.cfg())
        e = self.direct
        e.client.age = 30.0
        e.client.script = [{"MJYA4CH/A": [store(state=Stock.UNAVAILABLE)]},
                           {"MJYB4CH/A": [store(state=Stock.UNAVAILABLE)]}]
        s.poll(e)

        from test_roles import bare_buyer
        b = bare_buyer(parts=["MJYA4CH/A", "MJYB4CH/A"])
        b.heard = {"MJYA4CH/A": time.monotonic() - 30}
        b.heard_at = {"MJYA4CH/A": time.time() - 30}
        from hunter2.bus import Alive
        b.heard_alive(Alive(id="s", at=time.time(), exits=1, saw_at=s.saw_at, src="s"))
        self.assertIsNone(b.stock_live("MJYA4CH/A"),
                          "慢轮的读数被当成新鲜结论，掐掉了一次真实下单")


class BrokenExitTests(Harness, unittest.TestCase):
    """一条连不上的出口不能拖住好出口。

    一轮拆六批、每批连接超时 15 秒 = 90 秒一个请求都发不出去，而调度器要等整轮
    结束才能换人。心跳这段时间照常，看着一切正常。
    """

    def cfg(self):
        return dict(CFG, watch=[{"part": f"P{i}", "request_group": f"g{i}"}
                                for i in range(6)])

    def test_it_bails_out_of_the_round_at_the_first_failure(self):
        s = self.scout(self.cfg())
        e = self.direct
        e.client.script = [TimeoutError("连接超时")] * 6
        ok, retry = s.poll(e)
        self.assertEqual(1, len(e.client.calls),
                         f"坏出口打了 {len(e.client.calls)} 批，把后面几批也耗光了")
        self.assertFalse(ok)

    def test_a_broken_exit_is_put_on_the_bench(self):
        s = self.scout(self.cfg())
        e = self.direct
        e.client.script = [TimeoutError("连接超时")]
        _, retry = s.poll(e)
        # 冷却了（下一轮不会立刻又挑中它），但**第一次只晾几秒**——单条代理偶尔
        # 抖一下不该让主程序全盲一整个 30 秒。
        self.assertIsNotNone(retry, "网络故障没有触发冷却，下一轮还会挑中它")
        self.assertGreater(retry, 0.0)
        self.assertLessEqual(retry, 5.0)

    def test_repeated_failures_escalate_the_cooldown(self):
        s = self.scout(self.cfg())
        e = self.direct
        e.client.script = [TimeoutError("连接超时")] * 6
        cds = []
        for _ in range(6):
            e.client.calls = []           # 每次只喂一批
            e.client.script = [TimeoutError("连接超时")]
            _, retry = s.poll(e)
            cds.append(retry)
        # 连着失败逐步拉长、封顶 30；打通一次会清零（见 pool.ok）
        self.assertEqual([3.0, 6.0, 12.0, 24.0, 30.0, 30.0], cds)

    def test_the_healthy_exit_takes_over_right_away(self):
        s = self.scout(self.cfg())
        with patch("scout.exits.ExitPool._proxy_url",
                   side_effect=lambda i, p: f"http://u:k@{i}:{p}"):
            s.pool.enlist("b", "192.168.1.9", 48712, False)
        good = s.pool._exits["b"]
        bad = self.direct
        bad.client.script = [TimeoutError("连接超时")]
        _, retry = s.poll(bad)
        s.pool.blocked(bad, retry, s.reqs)
        for e in s.pool._exits.values():
            e.due_at = min(e.due_at, time.monotonic()) if e is good else e.due_at
        self.assertIs(good, s.pool.pick(PICKUP_PATH), "好出口没能立刻顶上")

    def test_a_failed_batch_is_still_billed(self):
        """请求是真发出去了的，只是没回来。不记账的话预算形同虚设。"""
        s = self.scout(self.cfg())
        e = self.direct
        e.client.script = [TimeoutError("连接超时")]
        s.poll(e)
        self.assertEqual(1, s.reqs)


class SnapshotTests(Harness, unittest.TestCase):
    """心跳要带这一轮的完整有货快照。

    没有它的话，买手只能把「这一轮没收到 seen」当成「没货了」，而 seen 走 UDP，
    丢一个包就会撤销一批还有效的候选。
    """

    def test_the_beat_carries_what_is_in_stock(self):
        s = self.scout()
        self.direct.client.script = [{"MJYA4CH/A": [store()],
                                      "MJYB4CH/A": [store(state=Stock.UNAVAILABLE)]}]
        s.poll(self.direct)
        s.beat(force=True)
        self.assertEqual(("MJYA4CH/A:R359",), self.beats()[-1].stock)

    def test_an_empty_snapshot_is_sent_when_nothing_is_in_stock(self):
        """空快照是「一家都没货」，跟「我没说」是两回事——后者由 saw_at=0 表示。"""
        s = self.scout()
        self.direct.client.script = [{"MJYA4CH/A": [store(state=Stock.UNAVAILABLE)],
                                      "MJYB4CH/A": [store(state=Stock.UNAVAILABLE)]}]
        s.poll(self.direct)
        s.beat(force=True)
        self.assertEqual((), self.beats()[-1].stock)
        self.assertGreater(self.beats()[-1].saw_at, 0.0)

    def test_a_round_that_was_not_clear_sends_no_snapshot(self):
        s = self.scout()
        self.direct.client.script = [Blocked("541")]
        s.poll(self.direct)
        s.beat(force=True)
        self.assertEqual(0.0, self.beats()[-1].saw_at)

    def test_every_store_is_listed(self):
        s = self.scout()
        self.direct.client.script = [{
            "MJYA4CH/A": [store("R359"), store("R581", "浦东")],
            "MJYB4CH/A": [store(state=Stock.UNAVAILABLE)]}]
        s.poll(self.direct)
        s.beat(force=True)
        self.assertEqual(("MJYA4CH/A:R359,R581",), self.beats()[-1].stock)


class MissingStoreTests(Harness, unittest.TestCase):
    """配置的门店少回来一家，这一轮就不算看清。

    少回来的那家是「不知道」，不是「没货」——算作读准的话，上一轮在那家看到的
    货会被这一轮撤掉，而它可能还好好地在那儿。
    """

    def cfg(self):
        return dict(CFG, pickup=dict(CFG["pickup"], stores=["R581", "R359"]))

    def test_a_missing_configured_store_blocks_the_clean_reading(self):
        s = self.scout(self.cfg())
        self.direct.client.script = [{
            "MJYA4CH/A": [store("R581", "浦东", Stock.UNAVAILABLE)],
            "MJYB4CH/A": [store("R581", "浦东", Stock.UNAVAILABLE)]}]
        s.poll(self.direct)
        self.assertEqual(0.0, s.saw_at, "R359 根本没返回，却算成了「看清了」")

    def test_all_configured_stores_present_is_clear(self):
        s = self.scout(self.cfg())
        both = [store("R581", "浦东", Stock.UNAVAILABLE),
                store("R359", "南京东路", Stock.UNAVAILABLE)]
        self.direct.client.script = [{"MJYA4CH/A": list(both),
                                      "MJYB4CH/A": list(both)}]
        s.poll(self.direct)
        self.assertGreater(s.saw_at, 0.0)

    def test_a_missing_store_is_called_out(self):
        s = self.scout(self.cfg())
        logs = []
        s.log = logs.append
        self.direct.client.script = [{"MJYA4CH/A": [store("R581", "浦东",
                                                          Stock.UNAVAILABLE)]}]
        s.poll(self.direct)
        self.assertTrue(any("R359" in x and "没返回" in x for x in logs))

    def test_no_configured_stores_means_nothing_to_miss(self):
        s = self.scout()
        self.direct.client.script = [{"MJYA4CH/A": [store(state=Stock.UNAVAILABLE)],
                                      "MJYB4CH/A": [store(state=Stock.UNAVAILABLE)]}]
        s.poll(self.direct)
        self.assertGreater(s.saw_at, 0.0)


class BlindAlarmTests(Harness, unittest.TestCase):
    """所有出口都被封着、持续了一阵子：主程序必须说出来。

    2026-09-20 23:32 起两条出口轮流被 541 封，到停机 20 分钟一轮没打成，期间还
    真放了一次货。日志里只有零散的「被拦」，心跳照常，买手也不报警——没有任何
    一行把「现在一条能用的都没有」点破。
    """

    def pushes(self):
        return [a[0] for a in self.pushed]

    def test_a_short_blind_spell_is_not_reported(self):
        s = self.scout(); s.blind_after = 10.0
        with patch.object(s.pool, 'all_blocked', return_value=90.0):
            s.check_blind()          # 记起点
            s.check_blind()          # 还没到时限
        self.assertEqual([], self.pushes())

    def test_a_long_blind_spell_is_reported_once(self):
        s = self.scout(); s.blind_after = 10.0
        with patch.object(s.pool, 'all_blocked', return_value=90.0):
            s.check_blind()
            s._blind_since -= 60
            for _ in range(5):
                s.check_blind()
        self.assertEqual(1, sum('全盲' in x for x in self.pushes()))

    def test_recovery_is_reported_and_the_alarm_rearms(self):
        s = self.scout(); s.blind_after = 10.0
        with patch.object(s.pool, 'all_blocked', return_value=90.0):
            s.check_blind(); s._blind_since -= 60; s.check_blind()
        with patch.object(s.pool, 'all_blocked', return_value=0.0):
            s.check_blind()
        self.assertTrue(any('恢复' in x for x in self.pushes()))
        with patch.object(s.pool, 'all_blocked', return_value=90.0):
            s.check_blind(); s._blind_since -= 60; s.check_blind()
        self.assertEqual(2, sum('全盲' in x for x in self.pushes()), '第二次全盲没再报')

    def test_one_healthy_exit_is_not_blind(self):
        s = self.scout(); s.blind_after = 10.0
        with patch.object(s.pool, 'all_blocked', return_value=0.0):
            s.check_blind(); s.check_blind()
        self.assertEqual([], self.pushes())

    def test_the_loop_actually_raises_the_alarm_while_idle(self):
        """**真跑主循环**，不看源码。出口全封时 pick 一直返回 None，主循环在那条
        空转分支里等——报警要是没接进这条分支，就是全盲 20 分钟一声不吭。"""
        s = self.scout(); s.blind_after = 0.2
        s.receiver = Mock()
        with patch.object(s.pool, 'all_blocked', return_value=90.0), \
             patch.object(s.pool, 'pick', return_value=None), \
             patch.object(s.pool, 'soonest', return_value=0.05):
            t = threading.Thread(target=s.loop, daemon=True)
            t.start()
            for _ in range(60):
                if any('全盲' in x for x in self.pushes()):
                    break
                time.sleep(0.05)
            s._stop.set()
        self.assertTrue(any('全盲' in x for x in self.pushes()),
                        '主循环空转了 3 秒，全盲一声没吭')
