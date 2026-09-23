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
    """空闲按 keep_tick 只保活，收到信号才进热档按 cadence 打 search。"""

    def fc(self):
        return placer(store="R581", stores=["R581"], place_order=False,
                      stk_timeout_ms=50)

    def test_idle_naps_the_keepalive_tick_not_the_search_cadence(self):
        import threading
        page = FakePage([FUL] + [MISS] * 8)
        fc = self.fc()
        waits = []
        n = [0]
        def fake_nap(w, seconds, stop, clock, sleep):
            waits.append(seconds)
            n[0] += 1
            return (n[0] >= 3, False)   # 没信号，第 3 次停
        fc._nap = fake_nap
        fc.camp(page, wake=threading.Event(), cadence=8, idle_cadence=240,
                hot_seconds=60, max_seconds=1e9,
                clock=lambda: 1000.0, sleep=lambda *_: None)
        # 空闲 nap 走的是保活 tick（min(15, idle_cadence)=15），不是 240
        self.assertTrue(all(w == 15 for w in waits), waits)

    def test_a_signal_switches_to_the_hot_cadence(self):
        import threading
        page = FakePage([FUL] + [MISS] * 8)
        fc = self.fc()
        waits = []
        n = [0]
        def fake_nap(w, seconds, stop, clock, sleep):
            waits.append(seconds)
            n[0] += 1
            return (n[0] >= 3, n[0] == 1)   # 第 1 次 nap 敲醒 → 之后热档
        fc._nap = fake_nap
        t = [1000.0]
        def clock():
            t[0] += 1
            return t[0]
        fc.camp(page, wake=threading.Event(), idle_cadence=240,
                hot_seconds=60, max_seconds=1e9,
                clock=clock, sleep=lambda *_: None)
        self.assertEqual(15, waits[0])   # 空闲：保活 tick
        self.assertEqual(0, waits[1])    # 被信号敲醒后：一发回来立刻发下一发，不等

    def test_an_explicit_hot_cadence_is_honoured(self):
        import threading
        page = FakePage([FUL] + [MISS] * 8)
        fc = self.fc()
        waits = []
        n = [0]
        def fake_nap(w, seconds, stop, clock, sleep):
            waits.append(seconds)
            n[0] += 1
            return (n[0] >= 3, n[0] == 1)
        fc._nap = fake_nap
        t = [1000.0]
        def clock():
            t[0] += 1
            return t[0]
        fc.camp(page, wake=threading.Event(), cadence=8, idle_cadence=240,
                hot_seconds=60, max_seconds=1e9, clock=clock, sleep=lambda *_: None)
        self.assertTrue(0 < waits[1] <= 8, waits)


class SignalStoreTests(unittest.TestCase):
    """信号带门店：热档那一发直接 selectStore 信号里的店，不先查配置第一家再换。"""

    def test_signal_already_waiting_makes_the_priming_shot_hot(self):
        import threading
        # 进场时信号已经在等（重建/冷却期间放的货）→ 预热那一发就查信号里的店
        page = FakePage([FUL, HIT, CONTACT, BANKS, MONTHS, REVIEW])
        fc = placer(store="R581", stores=["R581", "R359"], place_order=False,
                    stk_timeout_ms=50)
        wake = threading.Event(); wake.set()
        t = [1000.0]
        def clock():
            t[0] += 1
            return t[0]
        fc.camp(page, wake=wake, hint={"store": "R359", "seq": 1}, cadence=1,
                idle_cadence=1e6, hot_seconds=1e6, max_seconds=1e9,
                clock=clock, sleep=lambda *_: None)
        bodies = [c["body"] for c in page.calls if "_a=search" in c["query"]]
        self.assertEqual(1, len(bodies))
        self.assertIn("selectStore=R359", bodies[0])
        self.assertEqual("R359", fc.store_used)

    def test_hot_search_selects_the_hinted_store(self):
        import threading
        # 预热（R581，MISS）→ 蹲着 → 信号说 R359 → 这一发就查 R359 且命中
        page = FakePage([FUL, MISS, HIT, CONTACT, BANKS, MONTHS, REVIEW])
        fc = placer(store="R581", stores=["R581", "R359"], place_order=False,
                    stk_timeout_ms=50)
        wake = threading.Event()
        hint = {}
        t = [1000.0]
        def clock():
            t[0] += 1
            return t[0]
        n = [0]
        def stop():
            n[0] += 1
            if n[0] == 1:                       # 预热之后才来信号
                hint.update(store="R359", seq=1)
                wake.set()
            return n[0] > 20
        fc.camp(page, wake=wake, hint=hint, stop=stop, cadence=1, idle_cadence=1e6,
                hot_seconds=1e6, max_seconds=1e9, clock=clock, sleep=lambda *_: None)
        bodies = [c["body"] for c in page.calls if "_a=search" in c["query"]]
        self.assertEqual(2, len(bodies))
        self.assertIn("selectStore=R581", bodies[0])   # 预热还是配置第一家
        self.assertIn("selectStore=R359", bodies[1])   # 信号那一发直接查 R359
        self.assertEqual("R359", fc.store_used)

    def test_same_signal_overrides_store_only_once(self):
        import threading
        # 同一条信号（seq 不变）只覆盖一次选店：R359 结账侧说 R581 有货 → 换 R581，
        # 下一发不能被 hint 又拉回 R359
        r359_but_r581_ready = ful_stores([("R359", False), ("R581", True)])
        page = FakePage([FUL, r359_but_r581_ready, MISS, MISS])
        fc = placer(store="R581", stores=["R581", "R359"], place_order=False,
                    stk_timeout_ms=50)
        wake = threading.Event(); wake.set()
        n = [0]
        def stop():
            n[0] += 1
            return n[0] > 2
        t = [1000.0]
        def clock():
            t[0] += 10
            return t[0]
        fc.camp(page, wake=wake, hint={"store": "R359", "seq": 1}, stop=stop, cadence=1,
                idle_cadence=1e6, hot_seconds=1e6, max_seconds=1e9,
                clock=clock, sleep=lambda *_: None)
        bodies = [c["body"] for c in page.calls if "_a=search" in c["query"]]
        self.assertIn("selectStore=R359", bodies[0])
        self.assertTrue(all("selectStore=R581" in b for b in bodies[1:]), bodies[1:])

    def test_a_soft_error_does_not_end_the_session(self):
        # fetch 超时/断连（status 0）：一次不退，连着 3 次才 rebuild
        err = {"status": 0, "json": None, "error": "timeout"}
        page = FakePage([FUL, MISS, err, MISS, err, err, err])
        fc = placer(store="R581", stores=["R581"], place_order=False, stk_timeout_ms=50)
        t = [1000.0]
        def clock():
            t[0] += 1
            return t[0]
        ok, stage, detail = fc.camp(page, cadence=1, idle_cadence=1, hot_seconds=0,
                                    max_seconds=1e9, clock=clock, sleep=lambda *_: None)
        self.assertEqual("rebuild", stage)
        self.assertIn("连着 3 发", detail)
        searches = [c for c in page.calls if "_a=search" in c["query"]]
        self.assertEqual(6, len(searches))   # MISS, err, MISS, err, err, err

    def test_stalled_after_a_hit_resets_and_keeps_camping(self):
        import threading
        # 命中 → 第 3 步没有 pickupContact（时段被抢走）→ 复位第 1 步接着蹲，不退出
        from test_fastpath import FUL_NO_SLOT
        page = FakePage([FUL, HIT, FUL_NO_SLOT, FUL, MISS])
        fc = placer(store="R581", stores=["R581"], place_order=False, stk_timeout_ms=50)
        wake = threading.Event(); wake.set()
        n = [0]
        def stop():
            n[0] += 1
            return n[0] > 1
        t = [1000.0]
        def clock():
            t[0] += 10
            return t[0]
        ok, stage, _ = fc.camp(page, wake=wake, stop=stop, cadence=1, idle_cadence=1e6,
                               hot_seconds=1e6, max_seconds=1e9, clock=clock,
                               sleep=lambda *_: None)
        self.assertEqual("已停止", stage)          # 是 stop() 让它退的，不是 Stalled
        actions = [c["query"] for c in page.calls]
        self.assertTrue(any("continueFromFulfillmentToPickupContact" in q for q in actions))
        # 第 3 步失败后重新发了第 1 步，然后又 search 了
        i3 = next(i for i, q in enumerate(actions) if "continueFromFulfillmentToPickupContact" in q)
        self.assertIn("selectFulfillmentLocationAction", actions[i3 + 1])
        self.assertIn("_a=search", actions[i3 + 2])
        self.assertFalse(fc.slot)

    def test_settle_guard_finishes_the_order_record(self):
        from unittest.mock import Mock
        fc = placer(store="R581", stores=["R581"], place_order=False, stk_timeout_ms=50)
        guard = Mock()
        fc.submit_guard = guard
        fc.submitted = True
        fc.order_url = "https://secure6.www.apple.com.cn/shop/checkout/thankyou"
        fc.settle_guard()
        guard.finish.assert_called_once_with("confirmed", fc.order_url)
        # 没提交过就什么都不做
        g2 = Mock()
        fc2 = placer(store="R581", stores=["R581"], place_order=False, stk_timeout_ms=50)
        fc2.submit_guard = g2
        fc2.settle_guard()
        g2.finish.assert_not_called()

    def test_order_placer_camp_settles_the_guard(self):
        from unittest.mock import Mock, patch
        from hunter.checkout import OrderPlacer
        p = OrderPlacer(store_numbers=["R581"], log=lambda *a: None)
        fc = Mock()
        fc.camp.return_value = (False, "rebuild", "到点")
        fc.submitted = False
        fc.order_url = ""
        fc.failure_kind = ""
        fc.retry_after = 0.0
        fc.store_used = "R581"
        with patch.object(p, "_build_fc", return_value=fc):
            p.camp(Mock(url="https://secure6.www.apple.com.cn/shop/checkout"), 0.0)
        fc.settle_guard.assert_called_once()

    def test_call_id_suffix_is_base36_of_now(self):
        import re, time
        from hunter.fastpath import new_call_id
        cid = new_call_id()
        self.assertRegex(cid, r"^[a-z0-9]{10}-[a-z0-9]{8,9}$")
        ms = int(cid.split("-")[1], 36)
        self.assertLess(abs(ms - time.time() * 1000), 5000)

    def test_retry_after_from_the_response_survives(self):
        # 541 带 Retry-After: 600 → camp 的 retry_after 是 600，不被清成 0
        blocked = {"status": 541, "json": None, "retry_after": "600"}
        page = FakePage([FUL, MISS, blocked])
        fc = placer(store="R581", stores=["R581"], place_order=False, stk_timeout_ms=50)
        t = [1000.0]
        def clock():
            t[0] += 1
            return t[0]
        ok, stage, _ = fc.camp(page, cadence=1, idle_cadence=1, hot_seconds=0,
                               max_seconds=1e9, clock=clock, sleep=lambda *_: None)
        self.assertIn("被拦", stage)
        self.assertEqual(600.0, fc.retry_after)

    def test_hint_outside_the_boundary_is_ignored(self):
        import threading
        page = FakePage([FUL, MISS, MISS])
        fc = placer(store="R581", stores=["R581"], allow=["R581"], place_order=False,
                    stk_timeout_ms=50)
        wake = threading.Event(); wake.set()
        n = [0]
        def stop():
            n[0] += 1
            return n[0] > 2
        fc.camp(page, wake=wake, hint={"store": "R999"}, stop=stop, cadence=0,
                hot_seconds=1e6, max_seconds=1e9, clock=lambda: 1000.0,
                sleep=lambda *_: None)
        bodies = [c["body"] for c in page.calls if "_a=search" in c["query"]]
        self.assertTrue(all("selectStore=R581" in b for b in bodies), bodies)


class KeepAliveTests(unittest.TestCase):
    """预热打一发；之后空闲按 idle_cadence 慢打 search 保温（不用续期接口）；信号才密打。"""

    def fc(self):
        return placer(store="R581", stores=["R581"], place_order=False,
                      stk_timeout_ms=50)

    def test_primes_then_idle_keeps_searching_slowly(self):
        page = FakePage([FUL] + [MISS] * 8)
        fc = self.fc()
        t = [1000.0]
        def clock():
            t[0] += 1        # 时间会走，冷档间隔才到得了
            return t[0]
        n = [0]
        def stop():
            n[0] += 1
            return n[0] > 6
        fc.camp(page, cadence=1, idle_cadence=1, hot_seconds=0, max_seconds=1e9,
                stop=stop, clock=clock, sleep=lambda *_: None)
        actions = [c["query"] for c in page.calls]
        searches = [q for q in actions if "_a=search" in q]
        extends = [q for q in actions if "extendSessionUrl" in q]
        # 预热 1 发 + 空闲慢打：凉的 search 要 20s，只续期保不住热
        self.assertTrue(len(searches) >= 2, actions)
        # 不再用续期接口——search 本身就是交互
        self.assertEqual(0, len(extends), actions)

    def test_search_541_leaves_the_session_instead_of_probing(self):
        # 预热 MISS，下一发保温 541 → 立刻退出这段蹲守，标 blocked；不在原会话里探
        page = FakePage([FUL, MISS, {"status": 541, "json": None}, MISS, MISS])
        fc = self.fc()
        t = [1000.0]
        def clock():
            t[0] += 1
            return t[0]
        ok, stage, detail = fc.camp(page, cadence=1, idle_cadence=1, hot_seconds=0,
                                    max_seconds=1e9, clock=clock, sleep=lambda *_: None)
        self.assertFalse(ok)
        self.assertIn("被拦", stage)
        self.assertEqual("blocked", fc.failure_kind)
        searches = [c for c in page.calls if "_a=search" in c["query"]]
        self.assertEqual(2, len(searches))   # 预热 + 被拦那一发，之后没再探

    def test_idle_search_waits_for_the_idle_cadence(self):
        page = FakePage([FUL] + [MISS] * 8)
        fc = self.fc()
        t = [1000.0]
        def clock():
            t[0] += 1
            return t[0]
        n = [0]
        def stop():
            n[0] += 1
            return n[0] > 6
        fc.camp(page, cadence=1, idle_cadence=1e6, hot_seconds=0, max_seconds=1e9,
                stop=stop, clock=clock, sleep=lambda *_: None)
        searches = [c["query"] for c in page.calls if "_a=search" in c["query"]]
        # 间隔没到就只有预热那一发
        self.assertEqual(1, len(searches))

    def test_keep_awake_never_clicks_the_pickup_tab(self):
        # HAR：页面一选到店取货就自动补发 step1 + search，点 tab 等于让页面占掉会话的 10s 坑
        from hunter.fastpath import FastCheckout
        js = FastCheckout.JS_KEEP_AWAKE
        self.assertNotIn('did.push("pickup-tab', js)
        self.assertNotIn("el.click(); did.push(\"pickup-tab", js)

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
