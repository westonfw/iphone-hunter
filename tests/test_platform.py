"""同一份代码要在三种机器上都能用：原生 Windows、WSL、原生 Linux。

原来「Windows 支持」全是 WSL 口径的——Chrome 只找 /mnt/c、用户目录靠
cmd.exe 问、toast 只在 _is_wsl() 时才发。这些在原生 Windows 上一条都不成立，
而且失败全是静默的：connect --launch 报「哪儿都没找到 Chrome」，桌面通知
干脆不发。下面这些用例就是钉住这三条分支。
"""

import importlib
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import hunter
from hunter import autobuy, notify


class ChromeLookupTests(unittest.TestCase):
    def test_finds_chrome_by_plain_windows_path(self):
        """原生 Windows 上路径是 C:\\...，不带 /mnt/c 前缀。"""
        with TemporaryDirectory() as d:
            exe = Path(d) / "chrome.exe"
            exe.write_text("")
            with mock.patch.object(autobuy, "WIN_CHROME_PATHS", [str(exe)]):
                self.assertEqual(str(exe), autobuy.windows_chrome())

    def test_skips_unexpanded_variables(self):
        """非 Windows 上 %LOCALAPPDATA% 展不开，别拿它去 stat。"""
        with mock.patch.object(autobuy, "WIN_CHROME_PATHS",
                               [r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"]):
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(autobuy.windows_chrome())

    def test_lists_both_native_and_wsl_paths(self):
        joined = " ".join(autobuy.WIN_CHROME_PATHS)
        self.assertIn(r"C:\Program Files\Google", joined)
        self.assertIn("/mnt/c/Program Files/Google", joined)


class UserProfileTests(unittest.TestCase):
    def test_reads_env_on_native_windows(self):
        """原生 Windows 上直接读 %USERPROFILE%，不该再去起 cmd.exe。"""
        with mock.patch.object(os, "name", "nt"), \
             mock.patch.dict(os.environ, {"USERPROFILE": r"C:\Users\me"}), \
             mock.patch.object(autobuy.subprocess, "run",
                               side_effect=AssertionError("不该调 cmd.exe")):
            self.assertEqual(r"C:\Users\me", autobuy.windows_userprofile())

    def test_falls_back_to_cmd_exe_under_wsl(self):
        done = mock.Mock(stdout="C:\\Users\\me\r\n")
        with mock.patch.object(os, "name", "posix"), \
             mock.patch.object(autobuy.subprocess, "run", return_value=done) as run:
            self.assertEqual("C:\\Users\\me", autobuy.windows_userprofile())
        self.assertIn("cmd.exe", run.call_args[0][0])


class PowerShellTests(unittest.TestCase):
    def test_native_windows_uses_bare_name(self):
        with mock.patch.object(os, "name", "nt"), \
             mock.patch.object(notify.shutil, "which",
                               side_effect=lambda n: r"C:\ps.exe" if n == "powershell" else None):
            self.assertEqual(r"C:\ps.exe", notify._powershell())

    def test_wsl_uses_exe_suffix(self):
        with mock.patch.object(os, "name", "posix"), \
             mock.patch.object(notify, "_is_wsl", return_value=True), \
             mock.patch.object(notify.shutil, "which",
                               side_effect=lambda n: "/ps" if n == "powershell.exe" else None):
            self.assertEqual("/ps", notify._powershell())

    def test_plain_linux_has_none(self):
        with mock.patch.object(os, "name", "posix"), \
             mock.patch.object(notify, "_is_wsl", return_value=False):
            self.assertIsNone(notify._powershell())

    def test_desktop_falls_back_to_notify_send_on_linux(self):
        with mock.patch.object(notify, "_powershell", return_value=None), \
             mock.patch.object(notify.shutil, "which", return_value="/usr/bin/notify-send"), \
             mock.patch.object(notify.subprocess, "run") as run:
            notify.Desktop({}).send("标题", "正文")
        self.assertEqual("notify-send", run.call_args[0][0][0])


class OpenUrlTests(unittest.TestCase):
    def test_native_windows_uses_startfile(self):
        """原生 Windows 既没有 wslview 也没有 xdg-open，只有 os.startfile。"""
        with mock.patch.object(os, "name", "nt"), \
             mock.patch.object(os, "startfile", create=True) as start, \
             mock.patch.object(notify.subprocess, "Popen",
                               side_effect=AssertionError("不该走 opener")):
            notify.open_in_browser("https://www.apple.com.cn/shop/bag")
        start.assert_called_once_with("https://www.apple.com.cn/shop/bag")


class PyCmdTests(unittest.TestCase):
    def test_hint_matches_the_platform(self):
        """Windows 的 venv 在 Scripts\\，提示里照抄 bin/ 就是让人白跑一趟。"""
        with mock.patch.object(os, "name", "nt"):
            importlib.reload(hunter)
            self.assertEqual(r".venv\Scripts\python", hunter.PY_CMD)
        with mock.patch.object(os, "name", "posix"):
            importlib.reload(hunter)
            self.assertEqual(".venv/bin/python", hunter.PY_CMD)
        importlib.reload(hunter)          # 还原成当前平台的值


if __name__ == "__main__":
    unittest.main()
