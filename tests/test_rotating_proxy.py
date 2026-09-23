"""tools/rotating-proxy.py：固定口（结账用）和免密来源。

固定口永远从同一个 IP 出站、连不上也不换；它的 IP 退出轮换池；--allow 的来源免密。
"""
import importlib.util
import os
import sys
import unittest
from unittest.mock import patch, Mock

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "rotating-proxy.py")
_spec = importlib.util.spec_from_file_location("rotating_proxy", _PATH)
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)


import tempfile
from pathlib import Path


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "buyer-a.txt").write_text("203.0.113.11  # 当前\n203.0.113.12\n", encoding="utf-8")
        (self.dir / "buyer-b.txt").write_text("203.0.113.21\n", encoding="utf-8")

    def test_buyer_maps_port_to_the_ips_in_its_file(self):
        got = rp.parse_buyers(["8081=buyer-a.txt", "8082=buyer-b.txt"], self.dir)
        self.assertEqual({8081: ["203.0.113.11", "203.0.113.12"], 8082: ["203.0.113.21"]},
                         {k: v[1] for k, v in got.items()})
        self.assertEqual("buyer-a.txt", got[8081][0].name)

    def test_buyer_rejects_garbage_missing_empty_and_duplicate_ports(self):
        with self.assertRaises(ValueError):
            rp.parse_buyers(["8081:buyer-a.txt"], self.dir)
        with self.assertRaises(ValueError):
            rp.parse_buyers(["8081=nope.txt"], self.dir)
        (self.dir / "empty.txt").write_text("# 空\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            rp.parse_buyers(["8081=empty.txt"], self.dir)
        with self.assertRaises(ValueError):
            rp.parse_buyers(["8081=buyer-a.txt", "8081=buyer-b.txt"], self.dir)

    def test_two_accounts_may_not_share_an_ip(self):
        (self.dir / "buyer-c.txt").write_text("203.0.113.12\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            rp.parse_buyers(["8081=buyer-a.txt", "8083=buyer-c.txt"], self.dir)

    def test_monitor_pool_and_buyer_files_must_be_disjoint(self):
        buyers = rp.parse_buyers(["8081=buyer-a.txt"], self.dir)
        self.assertEqual(["203.0.113.12"],
                         rp.check_disjoint(["9.9.9.9", "203.0.113.12"], buyers))
        self.assertEqual([], rp.check_disjoint(["9.9.9.9"], buyers))

    def test_allow_takes_ips_and_cidrs(self):
        allow = rp.parse_allow(["203.0.113.5", "198.51.100.0/24,192.0.2.7"])
        self.assertTrue(rp.client_allowed("203.0.113.5", allow))
        self.assertTrue(rp.client_allowed("198.51.100.200", allow))
        self.assertFalse(rp.client_allowed("203.0.113.6", allow))
        self.assertFalse(rp.client_allowed("garbage", allow))

    def test_browser_asset_domains_are_allowed(self):
        self.assertTrue(rp.host_allowed("store.storeimages.cdn-apple.com", 443))
        self.assertTrue(rp.host_allowed("is1-ssl.mzstatic.com", 443))
        self.assertFalse(rp.host_allowed("www.google.com", 443))
        self.assertFalse(rp.host_allowed("www.apple.com.cn", 80))


class PinnedTests(unittest.TestCase):
    def test_pinned_port_never_switches_ip(self):
        srv = rp.RotatingProxy(["203.0.113.11"], "u", "p", port=8081, pinned=True,
                               log=lambda *a: None)
        tried = []
        def boom(addr, timeout, source_address):
            tried.append(source_address[0])
            raise OSError("down")
        with patch.object(rp.socket, "create_connection", boom):
            up, info = srv._dial("www.apple.com.cn", 443)
        self.assertIsNone(up)
        self.assertEqual(["203.0.113.11"] * rp.CONNECT_TRIES, tried)

    def test_rotating_port_tries_different_ips(self):
        srv = rp.RotatingProxy(["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4"], "u", "p",
                               log=lambda *a: None)
        tried = []
        def boom(addr, timeout, source_address):
            tried.append(source_address[0])
            raise OSError("down")
        with patch.object(rp.socket, "create_connection", boom):
            srv._dial("www.apple.com.cn", 443)
        self.assertEqual(rp.CONNECT_TRIES, len(set(tried)))

    def test_allowed_peer_skips_auth_others_do_not(self):
        allow = rp.parse_allow(["203.0.113.0/24"])
        srv = rp.RotatingProxy(["1.1.1.1"], "u", "p", allow=allow, log=lambda *a: None)
        conn = Mock(); conn.getpeername.return_value = ("203.0.113.9", 5555)
        self.assertTrue(srv._peer_allowed(conn))
        conn.getpeername.return_value = ("198.51.100.9", 5555)
        self.assertFalse(srv._peer_allowed(conn))
        bare = rp.RotatingProxy(["1.1.1.1"], "u", "p", log=lambda *a: None)
        conn.getpeername.return_value = ("203.0.113.9", 5555)
        self.assertFalse(bare._peer_allowed(conn))


if __name__ == "__main__":
    unittest.main()


class RotateTests(unittest.TestCase):
    def srv(self, ips):
        return rp.RotatingProxy(ips, "u", "p", port=8081, pinned=True, log=lambda *a: None)

    def test_rotate_prefers_never_burned_then_least_recently_burned(self):
        s = self.srv(["a", "b", "c"])
        self.assertEqual(("a", "b"), s.rotate())
        self.assertEqual(("b", "c"), s.rotate())
        self.assertEqual(("c", "a"), s.rotate())     # a 是烧得最早的
        self.assertEqual("a", s.current_ip())

    def test_single_ip_port_cannot_rotate(self):
        s = self.srv(["a"])
        self.assertEqual(("a", "a"), s.rotate())

    def test_rotate_tears_down_live_tunnels(self):
        s = self.srv(["a", "b"])
        t1, t2 = Mock(), Mock()
        s._tunnels.update((t1, t2))
        s.rotate()
        t1.shutdown.assert_called_once(); t2.shutdown.assert_called_once()

    def _conn(self, request: bytes, peer="203.0.113.9"):
        conn = Mock()
        chunks = [request, b""]
        conn.recv.side_effect = lambda n: chunks.pop(0)
        conn.getpeername.return_value = (peer, 1234)
        return conn

    def test_control_endpoints_answer_json_for_allowed_peers(self):
        s = rp.RotatingProxy(["a", "b"], "u", "p", port=8081, pinned=True,
                             allow=rp.parse_allow(["203.0.113.0/24"]), log=lambda *a: None)
        conn = self._conn(b"GET /ip HTTP/1.1\r\nHost: x\r\n\r\n")
        s._handle(conn)
        out = b"".join(c.args[0] for c in conn.sendall.call_args_list)
        self.assertIn(b"200 OK", out); self.assertIn(b'"ip": "a"', out)
        conn = self._conn(b"GET /rotate HTTP/1.1\r\nHost: x\r\n\r\n")
        s._handle(conn)
        out = b"".join(c.args[0] for c in conn.sendall.call_args_list)
        self.assertIn(b'"ip": "b"', out); self.assertIn(b'"rotated": true', out)

    def test_control_needs_auth_from_other_peers_and_accepts_authorization_header(self):
        s = self.srv(["a", "b"])
        conn = self._conn(b"GET /rotate HTTP/1.1\r\nHost: x\r\n\r\n")
        s._handle(conn)
        out = b"".join(c.args[0] for c in conn.sendall.call_args_list)
        self.assertIn(b"407", out)
        self.assertEqual("a", s.current_ip())
        import base64
        tok = base64.b64encode(b"u:p").decode()
        conn = self._conn(f"GET /rotate HTTP/1.1\r\nAuthorization: Basic {tok}\r\n\r\n".encode())
        s._handle(conn)
        self.assertEqual("b", s.current_ip())

    def test_rotating_port_has_no_control_endpoint(self):
        s = rp.RotatingProxy(["a", "b"], "u", "p", allow=rp.parse_allow(["203.0.113.9"]),
                             log=lambda *a: None)
        conn = self._conn(b"GET /rotate HTTP/1.1\r\n\r\n")
        s._handle(conn)
        out = b"".join(c.args[0] for c in conn.sendall.call_args_list)
        self.assertIn(b"404", out)
