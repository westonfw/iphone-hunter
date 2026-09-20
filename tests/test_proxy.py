"""子程序的转发口。

它在局域网上监听、替别人转发流量——写松一点就是个开放中继。所以这里几乎全是
安全测试：没凭据不行、连别处不行、明文方法不行。
"""
import socket
import threading
import unittest
from unittest.mock import Mock

from hunter2.proxy import (ALLOW_HOSTS, ForwardProxy, PROXY_USER, _allowed,
                           _expected_auth, proxy_url)

KEY = b'proxy-test-key'


class AllowlistTests(unittest.TestCase):
    def test_apple_on_443_is_allowed(self):
        for host in ('www.apple.com.cn', 'secure7.www.apple.com.cn', 'apple.com.cn'):
            self.assertTrue(_allowed(host, 443), host)

    def test_anything_else_is_not(self):
        for host in ('example.com', 'evil.test', 'apple.com.cn.evil.test',
                     'notapple.com.cn', '', '127.0.0.1'):
            self.assertFalse(_allowed(host, 443), host)

    def test_other_ports_are_not(self):
        """80 上没有我们要的东西，开着只会多一个面。"""
        for port in (80, 22, 8080, 0, -1):
            self.assertFalse(_allowed('www.apple.com.cn', port), port)

    def test_case_and_trailing_dot_do_not_sneak_through(self):
        self.assertTrue(_allowed('WWW.Apple.COM.CN', 443))
        self.assertTrue(_allowed('www.apple.com.cn.', 443))


class WireTests(unittest.TestCase):
    """真开一个口，用真 socket 打它。"""

    def setUp(self):
        self.p = ForwardProxy(key=KEY, port=0, host='127.0.0.1',
                              log=lambda *a: None)
        self.port = self.p.start()
        self.addCleanup(self.p.close)

    def talk(self, raw: bytes, read: int = 256) -> bytes:
        s = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        try:
            s.sendall(raw)
            s.settimeout(5)
            return s.recv(read)
        finally:
            s.close()

    def connect_req(self, target='www.apple.com.cn:443', auth=None) -> bytes:
        head = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
        if auth is not None:
            head += f"Proxy-Authorization: {auth}\r\n"
        return (head + "\r\n").encode()

    def test_no_credentials_is_refused(self):
        self.assertIn(b'407', self.talk(self.connect_req()))

    def test_wrong_credentials_are_refused(self):
        bad = _expected_auth(b'some-other-key')
        self.assertIn(b'407', self.talk(self.connect_req(auth=bad)))

    def test_a_non_connect_method_is_refused(self):
        """明文转发会让这里变成能改写请求的中间人。"""
        raw = (f"GET http://www.apple.com.cn/ HTTP/1.1\r\n"
               f"Proxy-Authorization: {_expected_auth(KEY)}\r\n\r\n").encode()
        self.assertIn(b'405', self.talk(raw))

    def test_a_host_outside_the_allowlist_is_refused(self):
        got = self.talk(self.connect_req('evil.test:443', _expected_auth(KEY)))
        self.assertIn(b'403', got)

    def test_a_lookalike_host_is_refused(self):
        got = self.talk(self.connect_req('apple.com.cn.evil.test:443',
                                         _expected_auth(KEY)))
        self.assertIn(b'403', got)

    def test_a_non_443_port_is_refused(self):
        got = self.talk(self.connect_req('www.apple.com.cn:8080',
                                         _expected_auth(KEY)))
        self.assertIn(b'403', got)

    def test_a_bad_port_never_crashes_the_listener(self):
        self.assertIn(b'400', self.talk(self.connect_req('www.apple.com.cn:abc',
                                                         _expected_auth(KEY))))
        # 还活着
        self.assertIn(b'407', self.talk(self.connect_req()))

    def test_garbage_never_crashes_the_listener(self):
        for raw in (b'\r\n\r\n', b'\xff\xfe\r\n\r\n', b'CONNECT\r\n\r\n'):
            self.talk(raw)
        self.assertIn(b'407', self.talk(self.connect_req()))

    def test_an_oversized_head_is_dropped(self):
        raw = b'CONNECT x:443 HTTP/1.1\r\nX: ' + b'a' * 20000 + b'\r\n\r\n'
        s = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        try:
            s.sendall(raw)
            s.settimeout(5)
            s.recv(64)            # 400 或直接断开，两者都行
        except OSError:
            pass
        finally:
            s.close()
        self.assertIn(b'407', self.talk(self.connect_req()))

    def test_it_tunnels_to_an_allowed_target(self):
        """白名单内的目标要真的能连通——用一个假的「Apple」验证隧道本身。"""
        import hunter2.proxy as mod
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(('127.0.0.1', 0))
        srv.listen(1)
        up_port = srv.getsockname()[1]
        self.addCleanup(srv.close)

        got = []

        def serve():
            c, _ = srv.accept()
            got.append(c.recv(16))
            c.sendall(b'PONG')
            c.close()

        threading.Thread(target=serve, daemon=True).start()
        # 临时把白名单和端口指向这个假上游
        old_h, old_p = mod.ALLOW_HOSTS, mod.ALLOW_PORTS
        mod.ALLOW_HOSTS, mod.ALLOW_PORTS = ('127.0.0.1',), (up_port,)
        self.addCleanup(lambda: setattr(mod, 'ALLOW_HOSTS', old_h))
        self.addCleanup(lambda: setattr(mod, 'ALLOW_PORTS', old_p))

        s = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        try:
            s.sendall(self.connect_req(f'127.0.0.1:{up_port}', _expected_auth(KEY)))
            s.settimeout(5)
            self.assertIn(b'200', s.recv(128))
            s.sendall(b'PING')
            self.assertEqual(b'PONG', s.recv(16))
        finally:
            s.close()
        self.assertEqual([b'PING'], got)


class SetupTests(unittest.TestCase):
    def test_it_refuses_to_start_without_a_key(self):
        """没密钥就开口子 = 局域网开放中继。"""
        with self.assertRaises(RuntimeError):
            ForwardProxy(key=b'', log=lambda *a: None).start()

    def test_the_proxy_url_carries_the_credentials(self):
        u = proxy_url('192.168.1.5', 48712, b'k/e y')
        self.assertTrue(u.startswith(f'http://{PROXY_USER}:'))
        self.assertIn('192.168.1.5:48712', u)
        self.assertNotIn(' ', u)            # 密钥里的特殊字符要转义

    def test_the_allowlist_is_apple_only(self):
        self.assertTrue(all('apple' in d or 'icloud' in d for d in ALLOW_HOSTS))


if __name__ == '__main__':
    unittest.main()
