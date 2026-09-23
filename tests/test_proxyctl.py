"""买手被 541 之后让固定口换出口 IP。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from hunter.autobuy import AutoBuy, BuyResult
from hunter.camp_worker import CampWorker
from hunter.proxyctl import ProxyControl, ProxyControlError


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass


def fake_opener(payloads):
    calls = []
    opener = Mock()
    def open_(req, timeout=0):
        calls.append(req)
        return _Resp(json.dumps(payloads.pop(0)).encode())
    opener.open.side_effect = open_
    return opener, calls


class ControlTests(unittest.TestCase):
    def test_rotate_hits_the_sticky_port_with_basic_auth_from_the_url(self):
        opener, calls = fake_opener([{"ip": "2.2.2.2", "previous": "1.1.1.1", "rotated": True, "pool": 2}])
        with patch("urllib.request.build_opener", return_value=opener):
            got = ProxyControl("http://hunter:pw@203.0.113.1:8081").rotate()
        self.assertEqual("2.2.2.2", got["ip"])
        self.assertEqual("http://203.0.113.1:8081/rotate", calls[0].full_url)
        self.assertTrue(calls[0].get_header("Authorization").startswith("Basic "))

    def test_bad_answer_is_an_error(self):
        opener, _ = fake_opener([{"nope": 1}])
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ProxyControlError):
                ProxyControl("http://203.0.113.1:8081").current()


class SwitchExitTests(unittest.TestCase):
    def ab(self, logs, proxy="http://203.0.113.1:8081"):
        return AutoBuy({"pickup_store_numbers": ["R581"], "proxy": proxy},
                       Path("/tmp"), log=logs.append)

    def test_switch_exit_returns_the_new_ip(self):
        logs = []
        ab = self.ab(logs)
        ab.proxyctl = Mock(); ab.proxyctl.rotate.return_value = {"ip": "2.2.2.2", "previous": "1.1.1.1", "rotated": True, "pool": 2}
        self.assertEqual("2.2.2.2", ab.switch_exit("被 541"))
        self.assertTrue(any("出口 IP 已换" in x for x in logs), logs)

    def test_switch_exit_without_a_spare_ip_or_without_a_proxy_returns_empty(self):
        logs = []
        ab = self.ab(logs)
        ab.proxyctl = Mock(); ab.proxyctl.rotate.return_value = {"ip": "1.1.1.1", "previous": "1.1.1.1", "rotated": False, "pool": 1}
        self.assertEqual("", ab.switch_exit())
        plain = AutoBuy({"pickup_store_numbers": ["R581"]}, Path("/tmp"), log=logs.append)
        self.assertEqual("", plain.switch_exit())

    def test_switch_exit_failure_falls_back_to_the_cooldown(self):
        logs = []
        ab = self.ab(logs)
        ab.proxyctl = Mock(); ab.proxyctl.rotate.side_effect = ProxyControlError("/rotate 返回 407")
        self.assertEqual("", ab.switch_exit())
        self.assertTrue(any("换出口 IP 失败" in x for x in logs), logs)


class CampWorkerSwitchTests(unittest.TestCase):
    def test_blocked_with_a_switched_exit_rebuilds_at_once(self):
        from test_camp_worker import FakeBuyer, URL
        seq = [BuyResult(False, "⚠️ 上膛被拦", URL, retriable=False, retry_after=120.0),
               BuyResult(True, "ok", URL, quota_done=True)]
        b = FakeBuyer(seq)
        b.switch_exit = lambda why="": "2.2.2.2"
        naps = []
        w = CampWorker(b, lambda *a: None, url=URL, rebuild_pause=3.0, log=lambda *a: None)
        w._pause = lambda s: naps.append(s)
        w._run()
        self.assertEqual([3.0], naps[:1])
        self.assertNotIn(120.0, naps)
        self.assertEqual(0, w._fails)

    def test_blocked_without_a_proxy_keeps_the_old_cooldown(self):
        from test_camp_worker import FakeBuyer, URL
        seq = [BuyResult(False, "⚠️ 上膛被拦", URL, retriable=False, retry_after=120.0),
               BuyResult(True, "ok", URL, quota_done=True)]
        b = FakeBuyer(seq)
        naps = []
        w = CampWorker(b, lambda *a: None, url=URL, rebuild_pause=0.0, log=lambda *a: None)
        w._pause = lambda s: naps.append(s)
        w._run()
        self.assertIn(120.0, naps)


if __name__ == "__main__":
    unittest.main()


class CredentialDecodingTests(unittest.TestCase):
    def test_percent_encoded_password_is_decoded_before_basic_auth(self):
        import base64
        c = ProxyControl("http://hunter:Nw%40x@203.0.113.1:8081")
        self.assertEqual("Basic " + base64.b64encode(b"hunter:Nw@x").decode(), c._auth)
        from hunter.autobuy import playwright_proxy
        self.assertEqual({"server": "http://203.0.113.1:8081", "username": "hunter", "password": "Nw@x"},
                         playwright_proxy("http://hunter:Nw%40x@203.0.113.1:8081"))


class BlockedBeforeArmingTests(unittest.TestCase):
    """541 发生在上膛之前（页面被扔到 /shop/404、读不到 stk）也要按被拦报，
    这样 CampWorker 才会走静默冷却 / 换出口 IP，而不是 30 秒一次地重撞。"""

    def test_stk_failure_with_a_541_hit_becomes_a_blocked_result(self):
        blocked = {"hits": [{"status": 541, "url": "x", "retry_after": 0.0}], "retry_after": 0.0}
        r = AutoBuy._blocked_before_arming((False, "⚠️ 读不到 x-aos-stk", "不在结账页上"), blocked, "u")
        self.assertIsNotNone(r)
        self.assertFalse(r.ok)
        self.assertEqual(120, r.retry_after)
        self.assertIn("被限流", r.stage)

    def test_other_outcomes_are_left_alone(self):
        blocked = {"hits": [{"status": 541, "url": "x", "retry_after": 0.0}], "retry_after": 0.0}
        self.assertIsNone(AutoBuy._blocked_before_arming((False, "rebuild", "到点"), blocked, "u"))
        self.assertIsNone(AutoBuy._blocked_before_arming((True, "✅", ""), blocked, "u"))
        self.assertIsNone(AutoBuy._blocked_before_arming(
            (False, "⚠️ 读不到 x-aos-stk", ""), {"hits": [], "retry_after": 0.0}, "u"))
