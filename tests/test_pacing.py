import unittest

from hunter.pacing import (Breaker, Pacer, TokenBucket, breaker_settings,
                           build_pacer, in_window, parse_window)


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
        p, _ = self.make(max_interval=300, max_scale=50)
        for _ in range(20):
            p.on_blocked()
        self.assertEqual(300, p.target())

    def test_max_scale_caps_backoff_below_max_interval(self):
        """倍率上限要能单独把退避压住。

        原来只有 max_interval 一道闸：base 30s / max 900s 意味着倍率能冲到 ×30，
        30s 的巡检被退成 900s——放货那一刻程序是聋的。真正的等待交给 Breaker，
        Pacer 只要「明显慢一点」。
        """
        p, _ = self.make(max_interval=900, max_scale=8)
        for _ in range(20):
            p.on_blocked()
        self.assertEqual(240, p.target())

    def test_scale_snaps_back_after_a_quiet_spell(self):
        """久未被拦就直接满速——这正是「关掉重开就好了」的那一下。

        光靠 recover_step 每轮减 0.25，从 ×8 爬回去要 28 个成功轮次，而每轮又被
        退避拉长到几百秒，实际是一天都回不来。
        """
        p, clock = self.make(max_scale=8, heal_after=600, recover_step=0.25)
        for _ in range(5):
            p.on_blocked()
        self.assertEqual(8.0, p.scale)
        clock.advance(601)
        p.on_ok()
        self.assertEqual(1.0, p.scale)

    def test_scale_still_crawls_back_while_blocks_are_recent(self):
        """刚被拦过就还是加性恢复，别一成功就冲回满速。"""
        p, clock = self.make(max_scale=8, heal_after=600, recover_step=0.25)
        p.on_blocked()
        p.on_blocked()
        clock.advance(60)
        p.on_ok()
        self.assertEqual(3.75, p.scale)

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
        # 未触及上下限时，有限均匀抖动的平均值应接近目标
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


class BreakerTests(unittest.TestCase):
    """熔断器：被拦之后彻底不碰这个端点。

    这些用例照着 2026-09-15 那段真实日志写：26 次 541 全在同一个端点上，
    十有八九 60~90 秒自己就过期了，而每一次探测都在给封禁续期。
    """

    def make(self, **kw):
        clock = FakeClock()
        kw.setdefault("cooldowns", (90.0, 180.0, 300.0))
        return Breaker(clock=clock, **kw), clock

    def test_starts_ready(self):
        br, _ = self.make()
        self.assertTrue(br.ready())
        self.assertEqual(0.0, br.left())

    def test_trip_silences_the_endpoint_for_the_first_cooldown(self):
        br, clock = self.make()
        self.assertEqual(90.0, br.trip())
        self.assertFalse(br.ready())
        clock.advance(89)
        self.assertFalse(br.ready())
        self.assertAlmostEqual(1.0, br.left())
        clock.advance(2)
        self.assertTrue(br.ready())

    def test_repeat_blocks_escalate_then_stop_at_the_cap(self):
        """连击升档，但封顶 5 分钟——不是指数爆到 900s。"""
        br, clock = self.make()
        waits = []
        for _ in range(5):
            waits.append(br.trip())
            clock.advance(waits[-1] + 1)
        self.assertEqual([90.0, 180.0, 300.0, 300.0, 300.0], waits)

    def test_success_restores_full_speed_immediately(self):
        br, clock = self.make()
        br.trip()
        clock.advance(91)
        br.ok()
        self.assertTrue(br.ready())

    def test_a_long_quiet_spell_clears_the_streak(self):
        """隔了很久再被拦，是新的一次，不该接着上一轮的档位往上叠。"""
        br, clock = self.make(heal_after=600)
        br.trip()
        br.trip()                      # 已经升到第 2 挡
        clock.advance(1200)
        self.assertEqual(90.0, br.trip())   # 档位被时间清零，从头再来

    def test_settings_come_from_config(self):
        kw = breaker_settings({"pacing": {"cooldowns": [30, 60], "heal_after": 300}})
        self.assertEqual((30.0, 60.0), kw["cooldowns"])
        self.assertEqual(300.0, kw["heal_after"])

    def test_settings_ignore_garbage(self):
        kw = breaker_settings({"pacing": {"cooldowns": ["快一点"]}})
        self.assertNotIn("cooldowns", kw)
        self.assertEqual({}, breaker_settings({}))
