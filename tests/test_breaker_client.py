"""AppleClient 的端点熔断：被拦之后连包都不发。

这组用例守的是 2026-09-15 那次事故的教训——当时的逻辑是「降速接着打」，
6 次探测把封禁一路续到 26 分钟；同一时刻换个进程发一模一样的请求却是 200。
所以这里最关键的断言是「静默期内 requests_made 不增加」。
"""

import unittest

from hunter.apple import AVAIL_PATH, PICKUP_PATH, AppleClient, Blocked, CoolingDown


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeResp:
    def __init__(self, status, body=b"{}"):
        self.status_code = status
        self.content = body
        self.headers = {}
        self.history = []
        self.url = "https://x/y"

    def json(self):
        return {}

    def raise_for_status(self):
        pass


class BreakerClientTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.client = AppleClient("cn", breaker={"cooldowns": (90.0, 180.0)})
        self.sent = []

        class SessionStub:
            headers: dict = {}

            def get(_s, url, **kw):
                self.sent.append(url)
                return self.next

            def close(_s):
                pass

        self.client.s = SessionStub()
        # 熔断器要用假时钟，否则得真的 sleep 90 秒
        self.client._breaker_kw = {"cooldowns": (90.0, 180.0), "clock": self.clock}
        self.client.breakers.clear()
        self.next = FakeResp(200)

    def test_block_trips_the_breaker_and_reports_the_cooldown(self):
        self.next = FakeResp(541)
        with self.assertRaises(Blocked) as cm:
            self.client._get(PICKUP_PATH)
        self.assertEqual(90.0, cm.exception.cooldown)

    def test_no_packet_leaves_during_the_silence(self):
        """静默期内必须一个包都不发——探测本身就在给封禁续期。"""
        self.next = FakeResp(541)
        with self.assertRaises(Blocked):
            self.client._get(PICKUP_PATH)
        before = self.client.requests_made

        self.clock.advance(30)
        with self.assertRaises(CoolingDown) as cm:
            self.client._get(PICKUP_PATH)

        self.assertEqual(before, self.client.requests_made)
        self.assertEqual(1, len(self.sent))   # 只有最初那一次真的发了出去
        self.assertAlmostEqual(60.0, cm.exception.left)

    def test_other_endpoints_keep_working(self):
        """541 是端点级的：pickup 被拦不该把 availability 一起停掉。"""
        self.next = FakeResp(541)
        with self.assertRaises(Blocked):
            self.client._get(PICKUP_PATH)

        self.next = FakeResp(200)
        self.client._get(AVAIL_PATH)          # 不该抛
        self.assertTrue(self.client.breaker(AVAIL_PATH).ready())
        self.assertFalse(self.client.breaker(PICKUP_PATH).ready())

    def test_recovers_at_full_speed_once_the_silence_is_over(self):
        self.next = FakeResp(541)
        with self.assertRaises(Blocked):
            self.client._get(PICKUP_PATH)

        self.clock.advance(91)
        self.next = FakeResp(200)
        self.client._get(PICKUP_PATH)

        self.assertTrue(self.client.breaker(PICKUP_PATH).ready())

    def test_repeat_block_escalates_to_the_next_tier(self):
        self.next = FakeResp(541)
        with self.assertRaises(Blocked):
            self.client._get(PICKUP_PATH)
        self.clock.advance(91)
        with self.assertRaises(Blocked) as cm:
            self.client._get(PICKUP_PATH)
        self.assertEqual(180.0, cm.exception.cooldown)


class IdentityTests(unittest.TestCase):
    def test_renew_keeps_the_identity_by_default(self):
        """同 IP 换 UA 是无效动作，默认不换（见 renew_session 的文档）。"""
        c = AppleClient("cn")
        before = c.browser
        c.renew_session()
        self.assertIs(before, c.browser)

    def test_renew_can_still_swap_identity_when_asked(self):
        c = AppleClient("cn")
        before = c.browser
        c.renew_session(new_identity=True)
        self.assertIsNot(before, c.browser)


if __name__ == "__main__":
    unittest.main()
