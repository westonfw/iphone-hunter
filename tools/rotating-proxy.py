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

结账也走它：一个账号一个固定口（--buyer）
----------------------------------------
结账（买手的 Chrome）需要**固定一个 IP 全程不变**——中途换 IP 会立刻被 Akamai 判
会话作废，所以不能走轮换口。每个账号单独一个口、单独一个 IP 文件：

    --buyer 8081=buyer-a.txt --buyer 8082=buyer-b.txt --buyer 8083=buyer-c.txt

文件一行一个 IP，第一行是当前用的，后面是备用。每个口只在自己文件里的 IP 之间换，
不会自己换。**账号之间、账号和 ips.txt（监控池）之间的 IP 不能重叠**，重叠拒绝启动：
结账会话绑 IP，两个账号走同一个 IP 就是同一个 IP 上开两条结账链路；541 也按 IP 记，
一个账号撞的会连累另一个。当前 IP 连不上不会换别的顶上——换了等于把会话作废——
只回 502 并在日志里喊。

买手被 541 之后可以主动换：对固定口发 `GET /rotate`（免密来源或带 Basic 鉴权），
它把当前 IP 标为「烧过」、切到最久没烧过的那个、**并掐断这个口上所有在转的连接**
（Chrome 会复用已建好的隧道，不掐断的话新请求还从旧 IP 走）。`GET /ip` 看当前。
买手拿到新 IP 立刻重建会话，不必再干等 120~300 秒的静默期。

Chrome 的 --proxy-server / PAC 带不了密码，弹框要人手点，所以固定口通常配
`--allow 买手机器的公网IP`（可重复、可写 CIDR）让那几台免密；轮换口照旧要密码。
买手那边：config.json 的 `autobuy.proxy` 填 `http://这台的IP:8081`，`hunter connect
--launch` 起 Chrome 时只把 Apple 的域名指到这个口，其余流量直连。

它**不做**什么
--------------
* 不轮换 TLS 指纹、不碰请求内容——TLS 在客户端和 Apple 之间端到端，这台只看字节流。

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
import ipaddress
import os
import random
import selectors
import socket
import sys
import threading
import time
from pathlib import Path

#: 只放行这些域（含子域）和端口。库存接口在 www.apple.com.cn:443；结账页的静态
#: 资源、Apple ID 登录框、Apple Pay 脚本在 cdn-apple.com / mzstatic.com 上，
#: 买手的 Chrome 走固定口时得放行，否则结账页残缺。
ALLOW_HOSTS = ("apple.com.cn", "apple.com", "icloud.com.cn", "cdn-apple.com",
               "mzstatic.com")
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


def parse_allow(items) -> list:
    """--allow 的值：IP 或 CIDR，一个都解析不了就报错，别静默放行。"""
    out = []
    for raw in items or []:
        for piece in str(raw).split(","):
            piece = piece.strip()
            if piece:
                out.append(ipaddress.ip_network(piece, strict=False))
    return out


def client_allowed(addr: str, allow) -> bool:
    """来源地址在免密名单里吗。"""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in allow)


def parse_buyers(items, base: Path) -> dict[int, tuple[Path, list[str]]]:
    """--buyer PORT=文件：固定口 → (它的 IP 文件, 文件里的 IP)。

    相对路径相对 base（默认脚本所在目录）。端口重复、文件不存在、文件为空、
    IP 写错、两个账号的 IP 重叠，一律报错拒绝启动——结账会话绑 IP，两个账号共用
    一个 IP 是风控最爱看的形状，这种错不能静默放过。
    """
    out: dict[int, tuple[Path, list[str]]] = {}
    owner: dict[str, int] = {}
    for raw in items or []:
        port_s, _, file_s = str(raw).partition("=")
        try:
            port = int(port_s)
        except ValueError:
            raise ValueError(f"--buyer 要写成 端口=IP文件，看不懂：{raw!r}")
        if not file_s.strip():
            raise ValueError(f"--buyer {port} 没给 IP 文件")
        if port in out:
            raise ValueError(f"--buyer 端口 {port} 给了两次")
        path = Path(file_s.strip())
        if not path.is_absolute():
            path = base / path
        if not path.exists():
            raise ValueError(f"--buyer {port} 的 IP 文件不存在：{path}")
        ips = load_ips(path)
        if not ips:
            raise ValueError(f"--buyer {port} 的 IP 文件是空的：{path}")
        for ip in ips:
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                raise ValueError(f"{path} 里有一行不是 IP：{ip!r}")
            if ip in owner:
                raise ValueError(f"IP {ip} 同时出现在端口 {owner[ip]} 和 {port} 的文件里——"
                                 f"一个账号一套 IP，不能共用")
            owner[ip] = port
        out[port] = (path, ips)
    return out


def check_disjoint(pool: list[str], buyers: dict[int, tuple[Path, list[str]]]) -> list[str]:
    """监控池和买手文件里重叠的 IP。scout 在 pickup-message 上撞出的 541 是按 IP
    记的，不能连累买手在 checkoutx 上的那个 IP，所以重叠直接拒绝启动。"""
    taken = {ip for _, lst in buyers.values() for ip in lst}
    return [ip for ip in pool if ip in taken]


class RotatingProxy:
    def __init__(self, ips: list[str], user: str, password: str, *,
                 host: str = "0.0.0.0", port: int = 8080, max_conns: int = 128,
                 pinned: bool = False, allow=None, label: str = "", log=_p):
        if not ips:
            raise ValueError("IP 池是空的——ips.txt 里一个能用的 IP 都没有")
        self.ips = ips
        #: 日志里怎么称呼这个口（买手的 IP 文件名），空就只用端口号。
        self.label = label
        #: 固定口：只用当前那一个，连不上也不自己换——换了等于把结账会话作废。
        #: 换只能由买手在被 541 之后主动 GET /rotate。
        self.pinned = bool(pinned)
        self._cur = 0
        #: 固定口里每个 IP 上次被「烧」（被 541 后换掉）的时刻，换的时候挑最久没烧的。
        self._burned: dict[str, float] = {}
        #: 这个口上正在转的连接，换 IP 时全掐断。
        self._tunnels: set[socket.socket] = set()
        #: 免密的来源网段（Chrome 的 PAC/--proxy-server 带不了密码）。
        self.allow = list(allow or [])
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

    # ---------- 固定口：当前 IP / 主动换 ----------

    def current_ip(self) -> str:
        with self._lock:
            return self.ips[self._cur]

    def rotate(self) -> tuple[str, str]:
        """买手被 541 了：把当前 IP 标为烧过，切到最久没烧过的那个，掐断在转的连接。
        返回 (旧 IP, 新 IP)。只有一个 IP 的固定口换不了，返回 (ip, ip)。"""
        with self._lock:
            old = self.ips[self._cur]
            self._burned[old] = time.time()
            others = [i for i in range(len(self.ips)) if i != self._cur]
            if others:
                self._cur = min(others, key=lambda i: self._burned.get(self.ips[i], 0.0))
            new = self.ips[self._cur]
            tunnels = list(self._tunnels)
        for s in tunnels:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return old, new

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
        """连上游，最多换 CONNECT_TRIES 个源 IP。返回 (socket 或 None, 用的IP)。

        固定口不换 IP：同一个 IP 再试 CONNECT_TRIES 次，还不行就是它坏了。
        """
        order = [self.current_ip()] * CONNECT_TRIES if self.pinned else self._pick_order()
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
            if len(first) < 2:
                return self._refuse(conn, "400 Bad Request")

            # 鉴权：常量时间比较，别给时序侧信道。控制请求（GET /ip、/rotate）
            # 认 Authorization 或 Proxy-Authorization 都行。
            got = ""
            for ln in text.split("\r\n")[1:]:
                name, _, val = ln.partition(":")
                if name.strip().lower() in ("proxy-authorization", "authorization"):
                    got = val.strip()
                    break
            if not hmac.compare_digest(got, self._auth) and not self._peer_allowed(conn):
                return self._refuse(
                    conn, "407 Proxy Authentication Required",
                    'Proxy-Authenticate: Basic realm="rotproxy"\r\n')

            if first[0].upper() == "GET":
                return self._control(conn, first[1])
            if first[0].upper() != "CONNECT":
                return self._refuse(conn, "405 Method Not Allowed")

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
                if self.pinned:
                    self.log(f"[proxy:{self.port}] ⚠️ 固定口的出站 IP {self.current_ip()} "
                             f"连 {host} 失败（试了 {CONNECT_TRIES} 次，不换 IP）：{info}")
                else:
                    self.log(f"[proxy:{self.port}] 连 {host} 失败，试了 {CONNECT_TRIES} 个源 IP：{info}")
                return self._refuse(conn, "502 Bad Gateway")

            with self._lock:
                self.served += 1
                self._tunnels.update((conn, up))
            try:
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            except OSError:
                return
            self._pipe(conn, up)
        finally:
            with self._lock:
                self._tunnels.discard(conn)
                if up is not None:
                    self._tunnels.discard(up)
            for s in (conn, up):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def _control(self, conn: socket.socket, path: str) -> None:
        """固定口的控制接口：GET /ip 看当前出站 IP，GET /rotate 换一个。轮换口没有。"""
        path = path.split("?", 1)[0]
        if not self.pinned or path not in ("/ip", "/rotate"):
            return self._refuse(conn, "404 Not Found")
        if path == "/rotate":
            old, new = self.rotate()
            rotated = old != new
            self.log(f"[proxy:{self.port}] 买手要求换出口：{old} → {new}"
                     + ("" if rotated else "（这个口只有一个 IP，换不了）"))
            body = (f'{{"port": {self.port}, "ip": "{new}", "previous": "{old}", '
                    f'"rotated": {"true" if rotated else "false"}, "pool": {len(self.ips)}}}')
        else:
            body = f'{{"port": {self.port}, "ip": "{self.current_ip()}", "pool": {len(self.ips)}}}'
        raw = body.encode()
        try:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         + f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode()
                         + raw)
        except OSError:
            pass

    def _peer_allowed(self, conn: socket.socket) -> bool:
        if not self.allow:
            return False
        try:
            addr = conn.getpeername()[0]
        except OSError:
            return False
        return client_allowed(addr, self.allow)

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

    def listen(self) -> None:
        """先把端口绑上。所有口都绑成功再开始服务——绑不上时报的是哪个端口被占，
        而不是让固定口的线程先跑起来、轮换口最后才撞一串 traceback。"""
        if self._srv is not None:
            return
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, self.port))
        except OSError as e:
            srv.close()
            raise OSError(f"端口 {self.host}:{self.port} 绑不上（{e.strerror or e}）——"
                          f"另一份代理还在跑？看 ss -ltnp | grep :{self.port} 和 "
                          f"systemctl status rotating-proxy") from e
        srv.listen(512)
        srv.settimeout(1.0)
        self._srv = srv

    def serve(self) -> None:
        self.listen()
        srv = self._srv
        what = (f"固定口{'（' + self.label + '）' if self.label else ''}，出站 IP {self.ips[0]}"
                + (f"（备用 {len(self.ips) - 1} 个，GET /rotate 换）" if len(self.ips) > 1 else
                   "（没有备用，被 541 只能干等）")
                if self.pinned else f"轮换口，IP 池 {len(self.ips)} 个")
        free = f"，{len(self.allow)} 个网段免密" if self.allow else ""
        self.log(f"[proxy:{self.port}] 监听 {self.host}:{self.port}，{what}，"
                 f"并发上限 {self.max_conns}{free}，只转 CONNECT 到 "
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
                self.log(f"[proxy:{self.port}] 近 5 分钟：转发 {d[0]}、拒 {d[1]}、"
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
    ap.add_argument("--ips", default="", help="监控库存用的轮换 IP 池文件，一行一个，默认脚本同目录的 ips.txt")
    ap.add_argument("--user", default="hunter", help="Basic 用户名，默认 hunter")
    ap.add_argument("--max-conns", type=int, default=128, help="并发上限，默认 128")
    ap.add_argument("--buyer", action="append", default=[], metavar="PORT=FILE",
                    help="一个账号一个固定口：这个端口只用 FILE 里的 IP（一行一个，第一行当前、"
                         "其余备用，GET /rotate 换下一个）。可重复；文件之间、文件和 --ips 之间不能重叠")
    ap.add_argument("--allow", action="append", default=[], metavar="IP|CIDR",
                    help="免密的来源地址（买手机器的公网 IP）。Chrome 的代理设置带不了密码。可重复")
    args = ap.parse_args(argv)

    password = os.environ.get("ROTPROXY_PASS", "")
    if not password:
        print("请用环境变量 ROTPROXY_PASS 设置密码（别写进文件）：\n"
              "  export ROTPROXY_PASS='你的强密码'", file=sys.stderr)
        return 2

    here = Path(__file__).resolve().parent
    ips_path = Path(args.ips) if args.ips else here / "ips.txt"
    if not ips_path.exists():
        print(f"监控 IP 池文件不存在：{ips_path}\n一行一个 IP，只写确实挂在本机、能出网的。",
              file=sys.stderr)
        return 2
    pool = load_ips(ips_path)
    if not pool:
        print(f"{ips_path} 里没有可用 IP。", file=sys.stderr)
        return 2
    try:
        buyers = parse_buyers(args.buyer, here)
        allow = parse_allow(args.allow)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if args.port in buyers:
        print(f"--buyer 不能用轮换口自己的端口 {args.port}", file=sys.stderr)
        return 2
    overlap = check_disjoint(pool, buyers)
    if overlap:
        print(f"这些 IP 同时在监控池 {ips_path.name} 和买手文件里：{'、'.join(overlap)}——"
              f"监控撞出的 541 会连累结账，两边不能共用", file=sys.stderr)
        return 2

    servers = [RotatingProxy(pool, args.user, password, host=args.host, port=args.port,
                             max_conns=args.max_conns, allow=allow)]
    for port, (path, lst) in sorted(buyers.items()):
        servers.append(RotatingProxy(lst, args.user, password, host=args.host, port=port,
                                     max_conns=args.max_conns, pinned=True, allow=allow,
                                     label=path.name))
    try:
        for srv in servers:
            srv.listen()
    except OSError as e:
        print(str(e), file=sys.stderr)
        return 2
    threads = [threading.Thread(target=srv.serve, daemon=True) for srv in servers[1:]]
    for t in threads:
        t.start()
    try:
        servers[0].serve()
    except KeyboardInterrupt:
        for srv in servers:
            srv.stop()
        print("\n[proxy] 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
