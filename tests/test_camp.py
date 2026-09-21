"""守株待兔（camp）：停在 step1 后反复打 search，命中就下单、查不到不退。

对付「结账链条比放货窗口长」的正解——把链条砍到只剩 search 一步，贴着窗口连续查。
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import threading
import unittest

from test_fastpath import FakePage, FUL, CONTACT, BANKS, MONTHS, REVIEW, resp, placer
from hunter.fastpath import FastCheckout


def ful_stores(rows):
    """造一发 search 响应：带门店库存（rows: [(sid, ok)]），当前店无时段。

    当前店（selectStore）没时段模块 → arm_from_search 里 take_slot 失败，
    再看门店库存决定换哪家。"""
    r = resp("fulfillment", {"pickupTab": {"pickup": {}},
                             "retailStores": [{"storeId": sid,
                                               "availability": {"availableNowForAllLines": ok,
                                                                "storeAvailability": "" if ok else "目前不可取货"}}
                                              for sid, ok in rows]})
    return r


#: 当前店就有时段的一发（直接上膛）
HIT = FUL
#: 一发全不可取
MISS = ful_stores([("R581", False), ("R359", False), ("R401", False)])


class ArmTests(unittest.TestCase):
    def fc(self, **kw):
        return placer(store="R581", stores=["R581", "R359"], place_order=False, **kw)

    def test_current_store_has_a_slot_arms_immediately(self):
        fc = self.fc()
        self.assertTrue(fc.arm_from_search(FakePage([]), HIT))
        self.assertEqual("R581", fc.store_used)

    def test_all_unavailable_does_not_arm_and_does_not_raise(self):
        fc = self.fc()
        self.assertFalse(fc.arm_from_search(FakePage([]), MISS))

    def test_another_preferred_store_available_switches_target(self):
        fc = self.fc()
        # 当前 R581 无时段，但结账侧说 R359 有货 → 下一发改选 R359
        data = ful_stores([("R581", False), ("R359", True)])
        self.assertFalse(fc.arm_from_search(FakePage([]), data))
        self.assertEqual("R359", fc.store)

    def test_out_of_bound_store_is_not_chosen(self):
        fc = placer(store="R581", stores=["R581"], allow=["R581"], place_order=False)
        # R999 有货但不在边界内 → 不换
        data = ful_stores([("R581", False), ("R999", True)])
        self.assertFalse(fc.arm_from_search(FakePage([]), data))
        self.assertEqual("R581", fc.store)


class CampLoopTests(unittest.TestCase):
    def fc(self, **kw):
        return placer(store="R581", stores=["R581"], place_order=False,
                      stk_timeout_ms=50, **kw)

    def test_it_camps_until_stock_then_places(self):
        # 热档（主程序报了货）：step1 + 两发 MISS 的 search + 一发 HIT + step3~6。
        # 冷档只续会话不打 search，所以命中场景必须是热档（wake set + hot 够久）。
        page = FakePage([FUL, MISS, MISS, HIT, CONTACT, BANKS, MONTHS, REVIEW])
        fc = self.fc()
        wake = threading.Event()
        wake.set()
        ok, stage, _ = fc.camp(page, wake=wake, cadence=0, hot_seconds=1e9,
                               max_seconds=100,
                               clock=self._fake_clock(), sleep=lambda *_: None)
        self.assertTrue(ok, stage)
        self.assertIn("Review", stage)

    def test_a_wake_signal_fires_search_at_once(self):
        page = FakePage([FUL, HIT, CONTACT, BANKS, MONTHS, REVIEW])
        fc = self.fc()
        wake = threading.Event()
        wake.set()
        ok, stage, _ = fc.camp(page, wake=wake, cadence=0, hot_seconds=1e9,
                               max_seconds=100,
                               clock=self._fake_clock(), sleep=lambda *_: None)
        self.assertTrue(ok, stage)

    def test_it_rebuilds_when_max_seconds_passes(self):
        # 一直 MISS，蹲到 max_seconds 就返回 rebuild
        page = FakePage([FUL] + [MISS] * 50)
        fc = self.fc()
        t = [1000.0]
        def clock():
            t[0] += 5      # 每次问时间就前进 5s
            return t[0]
        ok, stage, _ = fc.camp(page, cadence=0, idle_cadence=0, max_seconds=20,
                               clock=clock, sleep=lambda *_: None)
        self.assertFalse(ok)
        self.assertEqual("rebuild", stage)

    def test_stop_breaks_out(self):
        page = FakePage([FUL] + [MISS] * 50)
        fc = self.fc()
        calls = [0]
        def stop():
            calls[0] += 1
            return calls[0] > 3
        ok, stage, _ = fc.camp(page, stop=stop, cadence=0, idle_cadence=0, max_seconds=1e9,
                               clock=self._fake_clock(), sleep=lambda *_: None)
        self.assertFalse(ok)
        self.assertIn("停止", stage)

    def test_session_expired_asks_for_rebuild(self):
        expired = {"status": 200, "json": {"head": {"status": 302,
                   "data": {"url": "https://www.apple.com.cn/shop/sorry/session_expired"}}}}
        page = FakePage([FUL, expired])
        fc = self.fc()
        ok, stage, _ = fc.camp(page, cadence=0, idle_cadence=0, max_seconds=100,
                               clock=self._fake_clock(), sleep=lambda *_: None)
        self.assertFalse(ok)
        self.assertEqual("rebuild", stage)

    @staticmethod
    def _fake_clock():
        t = [1000.0]
        def clock():
            t[0] += 0.01
            return t[0]
        return clock


if __name__ == "__main__":
    unittest.main()


class CadenceTests(unittest.TestCase):
    """节奏跟主程序：安静时冷档（久），收到信号进热档（密）。"""

    def fc(self):
        return placer(store="R581", stores=["R581"], place_order=False,
                      stk_timeout_ms=50)

    def test_idle_uses_the_cold_cadence(self):
        import threading
        page = FakePage([FUL] + [MISS] * 5)
        fc = self.fc()
        waits = []
        wake = threading.Event()
        # 记录每次 nap 请求的间隔；第 3 发后停
        n = [0]
        def fake_nap(w, seconds, stop, clock, sleep):
            waits.append(seconds)
            n[0] += 1
            return (n[0] >= 3, False)   # 第 3 次让它停
        fc._nap = fake_nap
        fc.camp(page, wake=wake, cadence=8, idle_cadence=240, hot_seconds=60,
                max_seconds=1e9, clock=lambda: 1000.0, sleep=lambda *_: None)
        # 没有信号 → 全是冷档 240
        self.assertTrue(all(w == 240 for w in waits), waits)

    def test_a_signal_switches_to_hot_cadence(self):
        import threading
        page = FakePage([FUL] + [MISS] * 5)
        fc = self.fc()
        waits = []
        wake = threading.Event()
        n = [0]
        def fake_nap(w, seconds, stop, clock, sleep):
            waits.append(seconds)
            n[0] += 1
            # 第 1 次 nap 返回「被信号敲醒」→ 之后应进热档
            return (n[0] >= 3, n[0] == 1)
        fc._nap = fake_nap
        t = [1000.0]
        def clock():
            t[0] += 1
            return t[0]
        fc.camp(page, wake=wake, cadence=8, idle_cadence=240, hot_seconds=60,
                max_seconds=1e9, clock=clock, sleep=lambda *_: None)
        # 第 1 次冷档，被敲醒后第 2、3 次热档
        self.assertEqual(240, waits[0])
        self.assertEqual(8, waits[1])


class KeepAliveTests(unittest.TestCase):
    """空闲时用续期接口保活（不空打 search），并踹醒客户端计时器点掉「还在吗」。"""

    def fc(self):
        return placer(store="R581", stores=["R581"], place_order=False,
                      stk_timeout_ms=50)

    def test_idle_extends_the_session_instead_of_searching(self):
        # 冷档：step1 之后每轮打的是 extendSession，不是 search
        page = FakePage([FUL] + [{"status": 200, "json": {}}] * 5)
        fc = self.fc()
        n = [0]
        def stop():
            n[0] += 1
            return n[0] > 2
        fc.camp(page, cadence=0, idle_cadence=0, max_seconds=1e9,
                stop=stop, clock=lambda: 1000.0, sleep=lambda *_: None)
        # 打出去的请求里有 extendSession，没有 search
        actions = [c["query"] for c in page.calls]
        self.assertTrue(any("extendSessionUrl" in q for q in actions), actions)
        self.assertFalse(any("search" in q for q in actions), actions)

    def test_keep_awake_logs_only_when_it_does_something(self):
        from unittest.mock import Mock
        fc = self.fc()
        logs = []
        fc.log = lambda *a: logs.append(" ".join(str(x) for x in a))
        # 什么都没做（JS 返回空串）→ 不记日志
        page = Mock(); page.evaluate.return_value = ""
        fc.keep_awake(page)
        self.assertEqual([], logs)
        # 做了动作（切 tab / 关弹窗）→ 记一行
        page.evaluate.return_value = "pickup-tab:到店取货 | close:×"
        fc.keep_awake(page)
        self.assertTrue(any("页面维护" in x and "pickup-tab" in x for x in logs))

    def test_keep_awake_survives_evaluate_errors(self):
        from unittest.mock import Mock
        fc = self.fc()
        page = Mock(); page.evaluate.side_effect = RuntimeError("boom")
        fc.keep_awake(page)   # 不抛就行
