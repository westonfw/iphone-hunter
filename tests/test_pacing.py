import unittest

from hunter.pacing import Pacer, TokenBucket, build_pacer, in_window, parse_window


class FakeClock:
    """可手动推进的单调时钟，省得测试里真的去 sleep。"""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class WindowTests(unittest.TestCase):
    def test_parses_and_matches(self):
        w = parse_window("07:40-10:00")
        self.assertEqual((460, 600), w)
        self.assertTrue(in_window(8 * 60, w))
        self.assertFalse(in_window(10 * 60, w))   # 右端开区间

    def test_wraps_midnight(self):
        w = parse_window("23:00-01:00")
        self.assertTrue(in_window(23 * 60 + 30, w))
        self.assertTrue(in_window(30, w))
        self.assertFalse(in_window(12 * 60, w))

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_window("每天早上")


class TokenBucketTests(unittest.TestCase):
    def test_refuses_to_exceed_hourly_rate(self):
        clock = FakeClock()
        b = TokenBucket(per_hour=3600, burst=10, clock=clock)   # 每秒 1 个
        b.take(10)
        self.assertAlmostEqual(5.0, b.wait_for(5), places=3)
        clock.advance(5)
        self.assertEqual(0.0, b.wait_for(5))

    def test_overdraft_is_paid_back(self):
        clock = FakeClock()
        b = TokenBucket(per_hour=3600, burst=5, clock=clock)
        b.take(15)   # 一轮超支
        self.assertAlmostEqual(11.0, b.wait_for(1), places=3)


class PacerTests(unittest.TestCase):
    def make(self, **kw):
        clock = FakeClock()
        kw.setdefault("base_interval", 30)
        kw.setdefault("budget_per_hour", 3600)
        kw.setdefault("burst", 1000)
        p = Pacer(clock=clock, sleeper=lambda _: None, log=lambda *_: None, **kw)
        return p, clock

    def test_block_halves_rate_and_success_recovers_gradually(self):
        p, _ = self.make()
        p.on_blocked()
        self.assertEqual(60, p.target())      # 减半速率 = 间隔翻倍
        p.on_blocked()
        self.assertEqual(120, p.target())
        for _ in range(3):
            p.on_ok()
        self.assertEqual(30 * 3.25, p.target())   # 加性恢复，不是一次清零

    def test_recovery_never_overshoots_base(self):
        p, _ = self.make()
        p.on_blocked()
        for _ in range(50):
            p.on_ok()
        self.assertEqual(30, p.target())

    def test_backoff_is_capped(self):
        p, _ = self.make(max_interval=300)
        for _ in range(20):
            p.on_blocked()
        self.assertEqual(300, p.target())

    def test_cold_window_slows_down(self):
        p, _ = self.make(hot_windows=[(8 * 60, 9 * 60)], cold_multiplier=5)

        class T:
            hour, minute = 8, 30
        p.calendar = lambda: T()
        self.assertTrue(p.is_hot())
        self.assertEqual(30, p.target())

        class T2:
            hour, minute = 14, 0
        p.calendar = lambda: T2()
        self.assertFalse(p.is_hot())
        self.assertEqual(150, p.target())

    def test_delays_vary_but_stay_bounded(self):
        p, _ = self.make()
        delays = [p.next_delay(0) for _ in range(2000)]
        self.assertEqual(2000, len(set(delays)))            # 没有任何一个值被复用
        self.assertTrue(all(30 * p.FLOOR <= d <= 30 * p.CEIL for d in delays))

    def test_average_delay_tracks_the_target(self):
        # clamp 式抖动会把均值压偏，平移指数分布不会——这是间隔配置还算不算数的前提
        p, _ = self.make()
        delays = [p.next_delay(0) for _ in range(20000)]
        self.assertAlmostEqual(30, sum(delays) / len(delays), delta=1.5)

    def test_budget_stretches_the_interval(self):
        # 每小时只给 60 个请求，但每轮花 2 个 → 平均间隔必须被拉到 120s
        p, clock = self.make(budget_per_hour=60, burst=2, max_interval=3600)
        p.spend(2)
        clock.advance(1)
        self.assertGreater(p.next_delay(2), 100)

    def test_retry_after_wins_over_our_guess(self):
        p, _ = self.make()
        p.on_blocked(retry_after=240)
        self.assertGreaterEqual(p.next_delay(0), 240)
        self.assertLess(p.next_delay(0), 240)   # 只生效一次，不会一直粘着

    def test_build_pacer_falls_back_to_legacy_keys(self):
        p = build_pacer({"poll_interval": 42}, log=lambda *_: None)
        self.assertEqual(42, p.base_interval)
        sp = build_pacer({"sprint_interval": 3}, sprint=True, log=lambda *_: None)
        self.assertEqual(3, sp.base_interval)

    def test_sprint_ignores_cold_windows(self):
        p = build_pacer({"pacing": {"hot_windows": ["07:00-08:00"]}},
                        sprint=True, log=lambda *_: None)
        self.assertEqual([], p.hot_windows)

    def test_build_pacer_survives_bad_window(self):
        msgs = []
        p = build_pacer({"pacing": {"hot_windows": ["07:00-08:00", "早上"]}},
                        log=msgs.append)
        self.assertEqual(1, len(p.hot_windows))
        self.assertTrue(msgs)


if __name__ == "__main__":
    unittest.main()
