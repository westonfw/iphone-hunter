import json
import sys
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path

from hunter import logbook
from hunter.logbook import DailyFile, Logbook, Tee, _stamp


class StampTests(unittest.TestCase):
    def test_prefixes_date_and_time(self):
        line = _stamp("启动", datetime(2026, 9, 14, 1, 2, 3))
        self.assertEqual("2026-09-14 01:02:03 启动", line)

    def test_does_not_repeat_a_time_the_message_already_has(self):
        line = _stamp("[01:02:03] 有货了", datetime(2026, 9, 14, 1, 2, 3))
        self.assertEqual("2026-09-14 01:02:03 有货了", line)

    def test_blank_lines_stay_blank(self):
        self.assertEqual("", _stamp("   "))


class DailyFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "logs"

    def tearDown(self):
        self.tmp.cleanup()

    def test_name_carries_command_and_date(self):
        f = DailyFile(self.dir, "watch", ".log")
        self.assertEqual(f"watch-{date.today().isoformat()}.log", f.path_for().name)

    def test_creates_dir_and_appends(self):
        f = DailyFile(self.dir, "watch", ".log")
        f.write("一\n")
        f.write("二\n")
        f.close()
        self.assertEqual("一\n二\n", f.path_for().read_text(encoding="utf-8"))

    def test_write_failure_is_swallowed_and_remembered(self):
        f = DailyFile(self.dir, "watch", ".log")
        f.dir = Path("/proc/nonexistent-for-test")   # mkdir 必然失败
        self.assertFalse(f.write("x\n"))
        self.assertTrue(f.broken)


class TeeTests(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.echoed = []
        stream = type("S", (), {"write": lambda _, s: self.echoed.append(s),
                                "flush": lambda _: None,
                                "isatty": lambda _: False})()
        self.tee = Tee(stream, self.lines.append)

    def test_splits_on_newlines_not_on_writes(self):
        self.tee.write("有货")
        self.tee.write("了\n")
        self.assertEqual(["有货了"], self.lines)

    def test_keeps_echoing_to_the_terminal(self):
        self.tee.write("x\n")
        self.assertEqual(["x\n"], self.echoed)

    def test_holds_a_partial_line_until_drained(self):
        self.tee.write("没换行")
        self.assertEqual([], self.lines)
        self.tee.drain()
        self.assertEqual(["没换行"], self.lines)

    def test_echo_off_still_records(self):
        quiet = Tee(self.tee.stream, self.lines.append, echo=False)
        quiet.write("静默\n")
        self.assertEqual(["静默"], self.lines)
        self.assertEqual([], self.echoed)


class LogbookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        lb = logbook.current()
        if lb:
            lb.close()
        self.tmp.cleanup()

    def make(self, **cfg):
        return Logbook(self.root, cfg, command="watch")

    def test_captures_print_while_installed(self):
        # echo=False：既验证「只写文件」这条路，也免得测试输出被这行日志弄脏
        lb = self.make(echo=False).install(argv=["watch", "--sprint"])
        print("[01:02:03] 测试机型: 🏬 五角场 可取货")
        lb.close("退出码 0")
        text = lb.log_path.read_text(encoding="utf-8")
        self.assertIn("五角场 可取货", text)
        self.assertIn("参数：watch --sprint", text)
        self.assertIn("结束 watch：退出码 0", text)

    def test_restores_stdout_on_close(self):
        real = sys.stdout
        lb = self.make().install(argv=[])
        self.assertIsNot(real, sys.stdout)
        lb.close()
        self.assertIs(real, sys.stdout)
        self.assertIsNone(logbook.current())

    def test_requests_go_to_their_own_file_not_the_terminal(self):
        lb = self.make().install(argv=[])
        logbook.log_request(url="https://x/shop/retail/pickup-message",
                            params={"parts.0": "MJT74CH/A"}, status=200, ms=812, bytes=4096)
        lb.close()
        rows = [json.loads(x) for x in lb.req_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(1, len(rows))
        self.assertEqual(200, rows[0]["status"])
        self.assertEqual({"parts.0": "MJT74CH/A"}, rows[0]["params"])
        self.assertEqual("watch", rows[0]["cmd"])
        self.assertNotIn("pickup-message", lb.log_path.read_text(encoding="utf-8"))

    def test_blocked_requests_are_recorded_too(self):
        lb = self.make().install(argv=[])
        logbook.log_request(url="https://x/y", status=541, ms=900,
                            error="Blocked: HTTP 541 @ https://x/y")
        lb.close()
        row = json.loads(lb.req_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(541, row["status"])
        self.assertIn("541", row["error"])

    def test_request_log_can_be_turned_off(self):
        lb = self.make(requests=False).install(argv=[])
        logbook.log_request(url="https://x/y", status=200)
        lb.close()
        self.assertIsNone(lb.req_path)
        self.assertEqual([], list(lb.dir.glob("*.jsonl")))

    def test_log_request_without_a_logbook_is_a_noop(self):
        logbook.log_request(url="https://x/y", status=200)   # 不该抛

    def test_disabled_means_no_files_and_no_capture(self):
        self.assertIsNone(logbook.setup(self.root, {"enabled": False}, command="watch"))
        self.assertFalse((self.root / "logs").exists())

    def test_prune_drops_files_past_keep_days(self):
        lb = self.make(keep_days=7)
        lb.dir.mkdir(parents=True)
        old = lb.dir / "watch-2020-01-01.log"
        old.write_text("x", encoding="utf-8")
        import os
        stale = time.time() - 8 * 86400
        os.utime(old, (stale, stale))
        fresh = lb.dir / f"watch-{date.today().isoformat()}.log"
        fresh.write_text("y", encoding="utf-8")
        self.assertEqual(1, lb.prune())
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_keep_days_zero_keeps_everything(self):
        lb = self.make(keep_days=0)
        lb.dir.mkdir(parents=True)
        old = lb.dir / "watch-2020-01-01.log"
        old.write_text("x", encoding="utf-8")
        import os
        os.utime(old, (0, 0))
        self.assertEqual(0, lb.prune())
        self.assertTrue(old.exists())

    def test_two_commands_write_to_separate_files(self):
        a = Logbook(self.root, {}, command="watch")
        b = Logbook(self.root, {}, command="launch")
        self.assertNotEqual(a.log_path, b.log_path)


if __name__ == "__main__":
    unittest.main()
