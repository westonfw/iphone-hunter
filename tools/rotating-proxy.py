#!/usr/bin/env python3
"""多 IP 轮换出站代理：每个连接从本机 IP 池里挑一个当源地址出站。

给 iphone-hunter 的主程序（scout）刷库存用。scout 那边把这台配成一条
`{"rotating": true}` 的出口，就能「只走这台代理、单线程尽快刷」，而每个请求
落在不同的公网 IP 上，摊薄 Apple 对 (IP + 端点) 的累计限流。

它做什么
--------
* 只当 HTTP CONNECT 代理（`curl_cffi` 的 http 代理走的就是 CONNECT），不做明文转发
  ——明文转发会让它变成能改写请求的中间人。
* 每条连接**随机挑一个源 IP** 绑到出站 socket 上（`source_address`）。
* **单个坏 IP 内部重试**：选中的 IP 连不上就换一个再试，连试几个都不行才回 502。
  这条最重要：不这么做的话，200 个 IP 里坏一个就会让 scout 那条轮换出口误退避
  30 秒（它把 CONNECT 失败当成代理挂了）。
* 并发封顶、Basic 鉴权（常量时间比较）、只放行 Apple 的域和 443 端口。

它**不做**什么
--------------
* 不轮换 TLS 指纹、不碰请求内容——TLS 在客户端和 Apple 之间端到端，这台只看字节流。
* 不做粘性会话。结账（买手）需要**固定** IP 全程不变，别用这台轮换代理跑结账
  ——中途换 IP 会立刻被 Akamai 判会话作废。这台只喂库存监控。

怎么用
------
1. 把要用的公网 IP 一行一个写进 ips.txt（跟本脚本同目录，或用 --ips 指定）。
   只写**确实挂在本机网卡、且能出网**的 IP：
       ip -4 addr show | awk '/inet /{print $2}' | cut -d/ -f1
       curl --interface <IP> -s -o /dev/null -w '%{http_code}\n' https://www.apple.com.cn/
2. 设个强密码（环境变量，别写进文件）：
       export ROTPROXY_PASS='你的强密码'
3. 跑：
       python3 tools/rotating-proxy.py --port 8080 --user hunter
4. scout 的 config.json：
       "link": {
         "exits": [{"id": "pool",
                    "proxy": "http://hunter:你的强密码@这台的IP:8080",
                    "rotating": true}],
         "use_direct": false, "use_buyer": false
       }

长期跑用 systemd，见同目录 rotating-proxy.service。
"""
from __future__ import annotations

import argparse
import base64
import hmac
import os
import random
import selectors
import socket
import sys
import threading
import time
from pathlib import Path

#: 只放行这些域（含子域）和端口。库存接口在 www.apple.com.cn:443。
ALLOW_HOSTS = ("apple.com.cn", "apple.com", "icloud.com.cn")
ALLOW_PORTS = (443,)

#: 选中的源 IP 连不上时，最多换几个再试。200 个 IP 里坏一两个是常态，
#: 内部消化掉，别让客户端看见——那会误触发它的 30 秒故障退避。
CONNECT_TRIES = 3
#: 连一个上游的超时。放货高峰 Apple 可能慢，但连接建立不该等太久。
CONNECT_TIMEOUT = 8.0
#: 读 CONNECT 请求头的超时。连上不发数据的连接不能白占线程。
HEAD_TIMEOUT = 15.0
#: 一次 CONNECT 请求头最多这么大，超了就是不怀好意。
HEAD_MAX = 8192
#: 转发缓冲。
BUF = 65536


def _p(*a) -> None:
    """行缓冲的 print：systemd/journal 下也能实时看到日志。"""
    print(*a, flush=True)


def load_ips(path: Path) -> list[str]:
    ips = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ips.append(line)
    # 去重但保序
    seen: set[str] = set()
    out = []
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def host_allowed(host: str, port: int) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    if port not in ALLOW_PORTS or not h:
        return False
    return any(h == d or h.endswith("." + d) for d in ALLOW_HOSTS)


class RotatingProxy:
    def __init__(self, ips: list[str], user: str, password: str, *,
                 host: str = "0.0.0.0", port: int = 8080, max_conns: int = 128,
                 log=_p):
        if not ips:
            raise ValueError("IP 池是空的——ips.txt 里一个能用的 IP 都没有")
        self.ips = ips
        self.host, self.port = host, port
        self.max_conns = max(1, int(max_conns))
        self.log = log
        self._auth = "Basic " + base64.b64encode(
            f"{user}:{password}".encode()).decode()
        self._sem = threading.BoundedSemaphore(self.max_conns)
        self._live = 0
        self._lock = threading.Lock()
        # 计数，供定期汇报
        self.served = self.refused = self.bad_ip_retries = 0
        self._srv: socket.socket | None = None
        self._stop = False
        # 轮流起点，避免每次都从同一个 IP 开始试
        self._cursor = 0

    # ---------- 出站：随机源 IP + 坏 IP 内部重试 ----------

    def _pick_order(self) -> list[str]:
        """这次连接按什么顺序试 IP：随机一个起点，然后依次往后绕。

        纯随机每次独立取，坏 IP 会被反复撞上；随机起点 + 顺序遍历保证
        `CONNECT_TRIES` 次不会重复试同一个，也不会永远漏掉某些 IP。
        """
        n = len(self.ips)
        start = random.randrange(n)
        return [self.ips[(start + i) % n] for i in range(n)]

    def _dial(self, host: str, port: int) -> tuple[socket.socket | None, str]:
        """连上游，最多换 CONNECT_TRIES 个源 IP。返回 (socket 或 None, 用的IP)。"""
        order = self._pick_order()
        last = ""
        for i, src in enumerate(order[:CONNECT_TRIES]):
            try:
                up = socket.create_connection(
                    (host, port), timeout=CONNECT_TIMEOUT, source_address=(src, 0))
                up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if i:
                    with self._lock:
                        self.bad_ip_retries += 1
                return up, src
            except OSError as e:
                last = f"{type(e).__name__}: {e}"
                continue
        return None, last

    # ---------- 单条连接 ----------

    def _read_head(self, conn: socket.socket) -> bytes:
        conn.settimeout(HEAD_TIMEOUT)
        head = b""
        while b"\r\n\r\n" not in head:
            if len(head) > HEAD_MAX:
                return b""
            try:
                chunk = conn.recv(4096)
            except OSError:
                return b""
            if not chunk:
                return b""
            head += chunk
        return head

    def _refuse(self, conn: socket.socket, status: str, extra: str = "") -> None:
        with self._lock:
            self.refused += 1
        try:
            conn.sendall(f"HTTP/1.1 {status}\r\n{extra}"
                         f"Content-Length: 0\r\nConnection: close\r\n\r\n".encode())
        except OSError:
            pass

    def _handle(self, conn: socket.socket) -> None:
        up = None
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            head = self._read_head(conn)
            if not head:
                return self._refuse(conn, "400 Bad Request")
            text = head.decode("latin-1", "replace")
            first = text.split("\r\n", 1)[0].split()
            if len(first) < 2 or first[0].upper() != "CONNECT":
                return self._refuse(conn, "405 Method Not Allowed")

            # 鉴权：常量时间比较，别给时序侧信道
            got = ""
            for ln in text.split("\r\n")[1:]:
                name, _, val = ln.partition(":")
                if name.strip().lower() == "proxy-authorization":
                    got = val.strip()
                    break
            if not hmac.compare_digest(got, self._auth):
                return self._refuse(
                    conn, "407 Proxy Authentication Required",
                    'Proxy-Authenticate: Basic realm="rotproxy"\r\n')

            host, _, port_s = first[1].rpartition(":")
            try:
                port = int(port_s)
            except ValueError:
                return self._refuse(conn, "400 Bad Request")
            if not host_allowed(host, port):
                return self._refuse(conn, "403 Forbidden")

            up, info = self._dial(host, port)
            if up is None:
                # 连试几个源 IP 都连不上：多半是这几个 IP 都坏了，或上游不通。
                # 回 502，让客户端换个时机再来（scout 会退避一下）。
                self.log(f"[proxy] 连 {host} 失败，试了 {CONNECT_TRIES} 个源 IP：{info}")
                return self._refuse(conn, "502 Bad Gateway")

            with self._lock:
                self.served += 1
            try:
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            except OSError:
                return
            self._pipe(conn, up)
        finally:
            for s in (conn, up):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def _pipe(self, a: socket.socket, b: socket.socket) -> None:
        """双向转发到任意一端关闭。用 selectors 一个线程管两个方向，
        省掉「每条连接两个线程」里那多出来的一个。"""
        a.settimeout(None)
        b.settimeout(None)
        sel = selectors.DefaultSelector()
        sel.register(a, selectors.EVENT_READ, b)
        sel.register(b, selectors.EVENT_READ, a)
        try:
            while not self._stop:
                events = sel.select(timeout=1.0)
                if not events:
                    continue
                for key, _ in events:
                    src, dst = key.fileobj, key.data
                    try:
                        data = src.recv(BUF)
                    except OSError:
                        return
                    if not data:
                        return
                    try:
                        dst.sendall(data)
                    except OSError:
                        return
        finally:
            sel.close()

    # ---------- 服务器 ----------

    def serve(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(512)
        srv.settimeout(1.0)
        self._srv = srv
        self.log(f"[proxy] 监听 {self.host}:{self.port}，IP 池 {len(self.ips)} 个，"
                 f"并发上限 {self.max_conns}，只转 CONNECT 到 "
                 f"{'、'.join(ALLOW_HOSTS)}:443")
        threading.Thread(target=self._report_loop, daemon=True).start()
        try:
            while not self._stop:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not self._sem.acquire(blocking=False):
                    # 并发满了：直接拒，别排队占内存
                    self._refuse(conn, "503 Service Unavailable")
                    try:
                        conn.close()
                    except OSError:
                        pass
                    continue
                threading.Thread(target=self._run_one, args=(conn,),
                                 daemon=True).start()
        finally:
            try:
                srv.close()
            except OSError:
                pass

    def _run_one(self, conn: socket.socket) -> None:
        with self._lock:
            self._live += 1
        try:
            self._handle(conn)
        finally:
            with self._lock:
                self._live -= 1
            self._sem.release()

    def _report_loop(self) -> None:
        last = (0, 0, 0)
        while not self._stop:
            time.sleep(300)
            with self._lock:
                cur = (self.served, self.refused, self.bad_ip_retries)
                live = self._live
            d = tuple(c - p for c, p in zip(cur, last))
            last = cur
            if any(d):
                self.log(f"[proxy] 近 5 分钟：转发 {d[0]}、拒 {d[1]}、"
                         f"坏 IP 换用 {d[2]}（当前在连 {live}，累计转 {cur[0]}）")

    def stop(self) -> None:
        self._stop = True
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="多 IP 轮换出站 CONNECT 代理")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    ap.add_argument("--port", type=int, default=8080, help="监听端口，默认 8080")
    ap.add_argument("--ips", default="", help="IP 池文件，默认脚本同目录的 ips.txt")
    ap.add_argument("--user", default="hunter", help="Basic 用户名，默认 hunter")
    ap.add_argument("--max-conns", type=int, default=128, help="并发上限，默认 128")
    args = ap.parse_args(argv)

    password = os.environ.get("ROTPROXY_PASS", "")
    if not password:
        print("请用环境变量 ROTPROXY_PASS 设置密码（别写进文件）：\n"
              "  export ROTPROXY_PASS='你的强密码'", file=sys.stderr)
        return 2

    ips_path = Path(args.ips) if args.ips else Path(__file__).with_name("ips.txt")
    if not ips_path.exists():
        print(f"IP 池文件不存在：{ips_path}\n一行一个 IP，只写确实挂在本机、能出网的。",
              file=sys.stderr)
        return 2
    ips = load_ips(ips_path)
    if not ips:
        print(f"{ips_path} 里没有可用 IP。", file=sys.stderr)
        return 2

    proxy = RotatingProxy(ips, args.user, password, host=args.host, port=args.port,
                          max_conns=args.max_conns)
    try:
        proxy.serve()
    except KeyboardInterrupt:
        proxy.stop()
        print("\n[proxy] 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
