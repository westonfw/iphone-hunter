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
