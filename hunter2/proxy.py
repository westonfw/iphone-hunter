"""子程序身上的转发口：让主程序借这台机器的出口 IP 发请求。

**这是整个系统里最容易写出安全漏洞的一块。** 它在局域网上监听、替别人转发
流量——写松一点就是一个开放中继，同网段任何设备都能拿它当跳板。所以这里有
三道闸，缺一不可：

1. **必须带凭据。** 用的是总线那把共享密钥（HUNTER_BUS_KEY），没有它一律 407。
   密钥不对的话连不上——跟总线同一套信任边界，不额外引入一个。
2. **只许连 Apple。** CONNECT 的目标必须落在白名单域内，端口必须是 443。
   别的一律 403，连一个字节都不转。
3. **只转 CONNECT。** 不支持普通的 HTTP 代理方法（GET/POST 那种明文转发），
   那会让它变成一个能改写请求的中间人。

比较慢的那条路（TLS 在两端之间直通）反而是对的：主程序和 Apple 之间是端到端
加密的，子程序只看得见字节流，看不见 cookie 也改不了请求。
"""

from __future__ import annotations

import base64
import selectors
import socket
import threading

#: 只允许连到这些域（及其子域）。这不是「建议」，是硬闸。
ALLOW_HOSTS = ("apple.com.cn", "apple.com", "icloud.com.cn")
#: 只允许 443。80 上没有我们要的东西，开着只会多一个面。
ALLOW_PORTS = (443,)

#: 代理凭据里的用户名，固定；密码是总线密钥。
PROXY_USER = "hunter"

_BUF = 65536


def proxy_url(host: str, port: int, key: bytes) -> str:
    """拼出主程序那边要用的代理地址（带凭据）。"""
    from urllib.parse import quote
    return (f"http://{PROXY_USER}:{quote(key.decode('utf-8', 'replace'), safe='')}"
            f"@{host}:{port}")


def _allowed(host: str, port: int) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    if port not in ALLOW_PORTS or not h:
        return False
    return any(h == d or h.endswith("." + d) for d in ALLOW_HOSTS)


def _expected_auth(key: bytes) -> str:
    raw = f"{PROXY_USER}:{key.decode('utf-8', 'replace')}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _read_head(sock: socket.socket, limit: int = 8192) -> bytes:
    """读到请求头结束为止。超过上限就当它是恶意的，直接断。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        if len(buf) > limit:
            return b""
        try:
            chunk = sock.recv(1024)
        except OSError:
            return b""
        if not chunk:
            return b""
        buf += chunk
    return buf


def _pipe(a: socket.socket, b: socket.socket) -> None:
    """两个方向对转，任一端关掉就收摊。"""
    sel = selectors.DefaultSelector()
    try:
        sel.register(a, selectors.EVENT_READ, b)
        sel.register(b, selectors.EVENT_READ, a)
        while True:
            # 注意别写成 for...else：那个 else 在循环**正常结束**时就会执行，
            # 于是第一轮转发完就把隧道关了，每个真实请求都断在半路。
            events = sel.select(timeout=60)
            if not events:
                return          # 闲置超时，收摊
            for key, _ in events:
                try:
                    data = key.fileobj.recv(_BUF)
                except OSError:
                    return
                if not data:
                    return      # 一端关了，另一端也没意义了
                try:
                    key.data.sendall(data)
                except OSError:
                    return
    finally:
        sel.close()


class ForwardProxy:
    """只转发到 Apple 的 CONNECT 代理。子程序起一个，主程序借它出去。"""

    def __init__(self, key: bytes, port: int = 0, host: str = "0.0.0.0",
                 log=print, max_conns: int = 32):
        self.key, self.host, self.want_port = key, host, port
        self.log, self.max_conns = log, max_conns
        self.sock = None
        self.thread = None
        self.closed = False
        self.port = 0
        self.served = self.refused = 0
        self._live = 0
        self._lock = threading.Lock()
        self._last_refusal = ""

    def start(self) -> int:
        """开始监听，返回真实端口（port=0 时由系统分配）。"""
        if not self.key:
            raise RuntimeError("没有 HUNTER_BUS_KEY，转发口不能不设防地开出去")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.want_port))
        s.listen(16)
        s.settimeout(0.5)
        self.sock = s
        self.port = s.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, name="fwd", daemon=True)
        self.thread.start()
        self.log(f"[转发] 已在 :{self.port} 监听，只转 CONNECT 到 "
                 f"{'、'.join(ALLOW_HOSTS)}:443，且必须带凭据")
        return self.port

    def _serve(self) -> None:
        while not self.closed:
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                too_many = self._live >= self.max_conns
                if not too_many:
                    self._live += 1
            if too_many:
                self._refuse(conn, "503 Service Unavailable", "并发连接太多")
                continue
            threading.Thread(target=self._one, args=(conn, addr),
                             daemon=True).start()

    def _refuse(self, conn: socket.socket, status: str, why: str,
                extra: str = "") -> None:
        self.refused += 1
        if why != self._last_refusal:
            self._last_refusal = why
            self.log(f"[转发] 拒绝：{why}")
        try:
            conn.sendall(f"HTTP/1.1 {status}\r\n{extra}"
                         f"Content-Length: 0\r\nConnection: close\r\n\r\n".encode())
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    def _one(self, conn: socket.socket, addr) -> None:
        try:
            conn.settimeout(30)
            head = _read_head(conn)
            if not head:
                return self._refuse(conn, "400 Bad Request", "请求头读不完整")
            lines = head.split(b"\r\n")
            first = lines[0].decode("latin-1", "replace").split()
            if len(first) < 2 or first[0].upper() != "CONNECT":
                # 只支持 CONNECT。明文转发会让这里变成能改写请求的中间人。
                return self._refuse(conn, "405 Method Not Allowed",
                                    f"只接受 CONNECT，收到 {first[:1]}")

            want = _expected_auth(self.key)
            got = ""
            for ln in lines[1:]:
                name, _, val = ln.decode("latin-1", "replace").partition(":")
                if name.strip().lower() == "proxy-authorization":
                    got = val.strip()
            import hmac as _h
            if not _h.compare_digest(got, want):
                return self._refuse(conn, "407 Proxy Authentication Required",
                                    "凭据不对（密钥不一致，或者有人在蹭）",
                                    'Proxy-Authenticate: Basic realm="hunter"\r\n')

            host, _, port_s = first[1].rpartition(":")
            try:
                port = int(port_s)
            except ValueError:
                return self._refuse(conn, "400 Bad Request", "目标端口不是数字")
            if not _allowed(host, port):
                return self._refuse(conn, "403 Forbidden",
                                    f"目标 {host}:{port} 不在白名单里")

            try:
                up = socket.create_connection((host, port), timeout=15)
            except OSError as e:
                return self._refuse(conn, "502 Bad Gateway",
                                    f"连不上 {host}：{type(e).__name__}")
            self.served += 1
            try:
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                conn.settimeout(None)
                up.settimeout(None)
                _pipe(conn, up)
            finally:
                for s in (conn, up):
                    try:
                        s.close()
                    except OSError:
                        pass
        finally:
            with self._lock:
                self._live -= 1

    def close(self, timeout: float = 2.0) -> None:
        self.closed = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.thread is not None:
            self.thread.join(timeout)
            self.thread = None
