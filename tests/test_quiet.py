import unittest
from datetime import datetime

from hunter.notify import Broadcaster, QuietHours, Notifier


class Fake(Notifier):
    name = "fake"

    def __init__(self):
        super().__init__({"enabled": True})
        self.sent = []

    def send(self, title, body, url="", critical=False):
        self.sent.append(title)


def at(hh, mm=0):
    return lambda: datetime(2026, 9, 14, hh, mm)


class WindowTests(unittest.TestCase):
    def test_crosses_midnight(self):
        q = QuietHours.from_config({"windows": ["23:30-08:00"]})
        for hh, mm in ((23, 30), (23, 59), (0, 0), (3, 0), (7, 59)):
            q.calendar = at(hh, mm)
            self.assertTrue(q.muted(), f"{hh}:{mm:02d} 应该在睡觉时段里")
        for hh, mm in ((8, 0), (12, 0), (23, 29)):
            q.calendar = at(hh, mm)
            self.assertFalse(q.muted(), f"{hh}:{mm:02d} 不该被静音")

    def test_disabled_flag_wins(self):
        q = QuietHours.from_config({"enabled": False, "windows": ["23:30-08:00"]})
        q.calendar = at(2)
        self.assertFalse(q.muted())

    def test_empty_windows_never_mute(self):
        """空 windows + enabled:true 不能把全天静音掉。"""
        q = QuietHours.from_config({"enabled": True, "windows": []})
        q.calendar = at(2)
        self.assertFalse(q.muted())
        self.assertFalse(QuietHours.from_config(None).muted())

    def test_single_string_window(self):
        q = QuietHours.from_config({"windows": "23:30-08:00"})
        q.calendar = at(2)
        self.assertTrue(q.muted())

    def test_bad_window_is_skipped_not_fatal(self):
        logs = []
        q = QuietHours.from_config({"windows": ["瞎写的", "23:30-08:00"]}, log=logs.append)
        q.calendar = at(2)
        self.assertTrue(q.muted())
        self.assertTrue(logs)


class BroadcastTests(unittest.TestCase):
    def make(self, hh):
        bc = Broadcaster({}, log=lambda *_: None,
                         quiet=QuietHours([(23 * 60 + 30, 8 * 60)], calendar=at(hh)))
        ch = Fake()
        bc.channels = [ch]
        return bc, ch

    def test_only_pay_reminder_gets_through_at_night(self):
        bc, ch = self.make(3)
        bc.send("🚨 有货了", "body", critical=True)
        bc.send("⚠️ 自动下单失败，请手动下单", "body", critical=True)
        self.assertEqual([], ch.sent, "睡觉时段里这些都不该推送")
        bc.send("💳 去付款！W123", "body", critical=True, wake=True)
        self.assertEqual(["💳 去付款！W123"], ch.sent)

    def test_daytime_is_untouched(self):
        bc, ch = self.make(12)
        bc.send("🚨 有货了", "body", critical=True)
        bc.send("💳 去付款！W123", "body", critical=True, wake=True)
        self.assertEqual(2, len(ch.sent), "时段外一条都不能少")


if __name__ == "__main__":
    unittest.main()
