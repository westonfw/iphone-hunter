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


class CheckoutProxyTests(unittest.TestCase):
    """autobuy.proxy：结账只把 Apple 的域指到固定 IP 口，其余直连。"""

    def test_pac_sends_apple_to_the_proxy_and_the_rest_direct(self):
        from hunter.autobuy import proxy_pac
        pac = proxy_pac("http://203.0.113.11:8081")
        self.assertIn('return "PROXY 203.0.113.11:8081"', pac)
        self.assertIn('return "DIRECT"', pac)
        for d in ("apple.com.cn", "cdn-apple.com", "mzstatic.com"):
            self.assertIn(f'dnsDomainIs(host, ".{d}")', pac)

    def test_pac_rejects_a_url_without_a_port(self):
        from hunter.autobuy import proxy_pac
        with self.assertRaises(ValueError):
            proxy_pac("http://203.0.113.11")

    def test_launch_command_carries_the_pac_only_when_configured(self):
        import base64
        from hunter.autobuy import debug_chrome_cmd
        plain = debug_chrome_cmd("chrome", 9222, "/p")
        self.assertFalse(any(a.startswith("--proxy") for a in plain))
        cmd = debug_chrome_cmd("chrome", 9222, "/p", proxy="http://203.0.113.11:8081")
        flag = next(a for a in cmd if a.startswith("--proxy-pac-url=data:"))
        pac = base64.b64decode(flag.split("base64,", 1)[1]).decode()
        self.assertIn("PROXY 203.0.113.11:8081", pac)
        self.assertFalse(any(a.startswith("--proxy-server") for a in cmd))

    def test_fallback_launch_passes_the_proxy_to_playwright(self):
        ab = AutoBuy({"mode": "profile", "pickup_store_numbers": ["R581"],
                      "proxy": "http://u:pw@203.0.113.11:8081"},
                     Path("/tmp"), log=lambda *a: None)
        pw = Mock()
        pw.chromium.launch_persistent_context.return_value = "ctx"
        ab._launch(pw)
        kw = pw.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual({"server": "http://203.0.113.11:8081", "username": "u", "password": "pw"},
                         kw["proxy"])

    def test_attached_chrome_without_the_flag_is_called_out(self):
        logs = []
        ab = AutoBuy({"pickup_store_numbers": ["R581"], "proxy": "http://203.0.113.11:8081"},
                     Path("/tmp"), log=logs.append)
        ctx = Mock()
        page = ctx.new_page.return_value
        page.locator.return_value.inner_text.return_value = "chrome --remote-debugging-port=9222"
        ab._check_proxy_flag(ctx)
        self.assertTrue(any("没带代理参数" in x for x in logs), logs)
        page.locator.return_value.inner_text.return_value = "chrome --proxy-pac-url=data:x"
        logs.clear()
        ab._check_proxy_flag(ctx)
        self.assertTrue(any("结账走代理" in x for x in logs), logs)


class ProxyCredentialTests(unittest.TestCase):
    def test_percent_encoded_password_is_decoded(self):
        from hunter.autobuy import playwright_proxy
        self.assertEqual({"server": "http://203.0.113.1:8081", "username": "hunter", "password": "Nw@x"},
                         playwright_proxy("http://hunter:Nw%40x@203.0.113.1:8081"))
