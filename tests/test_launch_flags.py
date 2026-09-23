"""兜底模式（探测不到你的 Chrome、由 Playwright 自己启动）不能带自动化标记。

挂到用户自己的 Chrome 那条路实测（2026-09-23）navigator.webdriver 为 false、
没有横幅；Playwright 自己启动的默认带 --enable-automation，两样都会有。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from pathlib import Path
from unittest.mock import Mock

from hunter.autobuy import AutoBuy


class FallbackLaunchFlagsTests(unittest.TestCase):
    def test_fallback_launch_strips_the_automation_flags(self):
        ab = AutoBuy({"mode": "profile", "pickup_store_numbers": ["R581"]},
                     Path("/tmp"), log=lambda *a: None)
        pw = Mock()
        pw.chromium.launch_persistent_context.return_value = "ctx"
        ctx, attached = ab._launch(pw)
        self.assertEqual(("ctx", False), (ctx, attached))
        kw = pw.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(["--enable-automation"], kw["ignore_default_args"])
        self.assertIn("--disable-blink-features=AutomationControlled", kw["args"])


if __name__ == "__main__":
    unittest.main()
