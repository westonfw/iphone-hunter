"""CampWorker：常驻蹲守线程——反复调 autobuy.camp、重建、把放货信号敲进 wake。

Playwright 那段没法离线测，这里用假的 buyer（autobuy）验编排：重建循环、成单收工、
限流冷却、信号敲醒、以及「只对所蹲型号的 sighting 敲醒」。
"""
import threading
import time
import unittest
from unittest.mock import Mock

from hunter.autobuy import BuyResult
from hunter.camp_worker import CampWorker

URL = "https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJY64CH/A"


class FakeBuyer:
    """假 AutoBuy：按脚本吐 camp() 结果，记下调用。"""
    def __init__(self, results):
        self._results = list(results)
        self.calls = 0
        self.stopped = False
        self.halt_for_human = False
        self.cancelled = None
        self.managed = False

    def camp(self, url, in_stock_numbers, **kw):
        self.calls += 1
        r = self._results.pop(0) if self._results else BuyResult(False, "x", url, rebuild=True)
        # halt_for_human 由结果驱动（模拟 quota/结果不明）
        if getattr(r, "quota_done", False):
            self.halt_for_human = True
        return r

    def stop(self):
        self.stopped = True


def run_worker(results, **kw):
    reports = []
    b = FakeBuyer(results)
    w = CampWorker(b, lambda res, title, url: reports.append(res), url=URL,
                   rebuild_pause=0.0, log=lambda *a: None, **kw)
    w._run()   # 同步跑（脚本用完 camp 会返回 rebuild，closed 由结果里置）
    return b, w, reports


class WorkerTests(unittest.TestCase):
    def test_it_knows_which_part_it_camps(self):
        b = FakeBuyer([])
        w = CampWorker(b, lambda *a: None, url=URL, log=lambda *a: None)
        self.assertEqual("MJY64CH/A", w.part)

    def test_success_reports_and_stops(self):
        placed = BuyResult(True, "✅ 待付款订单已创建", URL, quota_done=True)
        b, w, reports = run_worker([placed])
        self.assertEqual(1, b.calls)
        self.assertTrue(w.halted)
        self.assertEqual(1, len(reports))
        self.assertTrue(b.stopped)   # 收工时关了浏览器

    def test_rebuild_does_not_report_and_loops(self):
        # 两次 rebuild 后成单收工：rebuild 不推送
        seq = [BuyResult(False, "rebuild", URL, rebuild=True),
               BuyResult(False, "rebuild", URL, rebuild=True),
               BuyResult(True, "ok", URL, quota_done=True)]
        b, w, reports = run_worker(seq)
        self.assertEqual(3, b.calls)
        self.assertEqual(1, len(reports))   # 只有成单那次推送

    def test_blocked_cools_down_by_retry_after(self):
        seq = [BuyResult(False, "⚠️ 结账被限流", URL, retriable=False, retry_after=5.0),
               BuyResult(True, "ok", URL, quota_done=True)]
        naps = []
        b = FakeBuyer(seq)
        w = CampWorker(b, lambda *a: None, url=URL, rebuild_pause=0.0,
                       log=lambda *a: None)
        w._pause = lambda s: naps.append(s)
        w._run()
        self.assertIn(5.0, naps)   # 按 retry_after 冷却

    def test_signal_only_wakes_for_the_camped_part(self):
        b = FakeBuyer([])
        w = CampWorker(b, lambda *a: None, url=URL, log=lambda *a: None)
        w.wake.clear()
        w.observe("MJYA4CH/A", [object()])   # 别的型号
        self.assertFalse(w.wake.is_set())
        w.observe("MJY64CH/A", [object()])   # 所蹲型号
        self.assertTrue(w.wake.is_set())

    def test_observe_without_offers_does_not_wake(self):
        b = FakeBuyer([])
        w = CampWorker(b, lambda *a: None, url=URL, log=lambda *a: None)
        w.wake.clear()
        w.observe("MJY64CH/A", [])           # 有型号但没候选
        self.assertFalse(w.wake.is_set())

    def test_close_sets_wake_so_camp_can_exit(self):
        b = FakeBuyer([BuyResult(False, "rebuild", URL, rebuild=True)])
        w = CampWorker(b, lambda *a: None, url=URL, log=lambda *a: None)
        w.close(timeout=0.1)
        self.assertTrue(w.closed)
        self.assertTrue(w.wake.is_set())


if __name__ == "__main__":
    unittest.main()


class BlockedCooldownTests(unittest.TestCase):
    """上膛被 541：静默冷却、不推送，攒到 5 次才说一句——别 3 秒一撞、别刷通知。"""
    URL = "https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJYE4CH/A"

    def worker(self, seq):
        self.reports = []
        b = FakeBuyer(seq)
        w = CampWorker(b, lambda r, t, u: self.reports.append(r), url=self.URL,
                       rebuild_pause=0.0, log=lambda *a: None)
        self.naps = []
        w._pause = lambda s: self.naps.append(s)
        return b, w

    def blocked(self):
        return BuyResult(False, "⚠️ 上膛被拦", self.URL, retriable=False, retry_after=120.0)

    def test_a_block_is_silent_and_cools_down(self):
        # 3 次上膛被拦 → 成单收工。前 3 次不推送、各冷却 120s。
        b, w = self.worker([self.blocked(), self.blocked(), self.blocked(),
                            BuyResult(True, "ok", self.URL, quota_done=True)])
        w._run()
        self.assertEqual(1, len(self.reports))     # 只有成单那次推送
        self.assertEqual([120.0, 120.0, 120.0], self.naps)  # 成单后 break，不再 pause

    def test_five_in_a_row_warns_once(self):
        # 连着 5 次被拦：第 5 次推一条「进不去」提醒
        b, w = self.worker([self.blocked()] * 6)
        # 让它跑 5 次就停
        n = [0]
        orig = w._pause
        def pause(s):
            n[0] += 1
            if n[0] >= 5:
                w.closed = True
            orig(s)
        w._pause = pause
        w._run()
        self.assertEqual(1, len(self.reports))     # 第 5 次那一条
