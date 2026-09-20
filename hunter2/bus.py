"""局域网信号总线：探针把「看到货了」广播出去，买手收到就开火。

**这条总线能触发花钱的操作**，所以每条消息都必须签名。局域网不是可信边界——
一个被攻陷的摄像头、一台访客手机，都在同一个广播域里。没有签名的话，任何人
往这个端口发一个包就能让买手去下单。

设计上的几条硬规矩：

1. **只传「看到了」，不传「没货了」。** 对端的「没货」可能只是它自己的接口抖动
   或者熔断静默——拿别人的失明当真相去踩刹车，会误杀真放货。刹车只认自己看到的。
2. **观察时刻用发送方的**，不是收到的时刻。不然 candidate_max_age 会把早就过期
   的信号当成新鲜的。跨机器就意味着要对时，所以时钟偏差要显式检查、显式报警。
3. **UDP 广播，丢了就丢了。** 探针每轮都会重发，补一次最多晚几秒；而 TCP 的连接
   建立和重传在放货那几秒里是纯负担。
4. **重放要挡住。** 签名只证明「这是我们的人发的」，挡不住有人把一个旧包重发一万遍。
"""

from __future__ import annotations

import hmac
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from hashlib import sha256

#: 共享密钥从环境变量读，**不从 config.json 读**——跟 Apple 密码同一个理由：
#: config.json 会被备份、同步盘、误提交带出去。
KEY_ENV = "HUNTER_BUS_KEY"

DEFAULT_PORT = 48711
#: 广播地址。同一网段的所有机器都能收到，不用配对端清单。
DEFAULT_ADDR = "255.255.255.255"

#: 消息格式版本。改了字段含义就要加，收到不认识的版本直接丢。
VERSION = 1

#: 允许的时钟偏差。超过它说明两台机器没对时，那 observed 就不可信了——
#: 宁可丢掉这条信号，也不能拿一个「来自未来」的时刻去算库存新鲜度。
MAX_SKEW = 5.0
#: 比这还旧的信号直接丢。放货窗口本身才十几秒，迟到的没有意义。
MAX_AGE = 120.0


def bus_key() -> bytes:
    """取共享密钥。没配就返回空——调用方据此决定是禁用总线还是报错。"""
    return (os.environ.get(KEY_ENV) or "").encode("utf-8")


def _canon(payload: dict) -> bytes:
    """签名用的规范化形式。键排序、无空格，两端必须完全一致。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sign(payload: dict, key: bytes) -> str:
    return hmac.new(key, _canon(payload), sha256).hexdigest()


def verify(payload: dict, mac: str, key: bytes) -> bool:
    """**必须用 compare_digest**：普通的 == 会按字节短路，泄漏出正确前缀的长度。"""
    try:
        return hmac.compare_digest(sign(payload, key), str(mac or ""))
    except Exception:
        return False


@dataclass(frozen=True)
class Alive:
    """主程序的心跳。

    **没有它，主程序挂了没人知道。** 没货的时候本来就没有 seen 消息，所以买手
    分不清「主程序死了」和「只是没放货」——那正是最危险的状态：看着一切正常，
    放货那一刻才发现根本没人在盯。

    按约定不做保险丝（买手不会自己去巡检兜底），所以这条心跳是唯一的报警来源，
    断了就必须叫醒人。
    """

    id: str
    at: float = 0.0
    exits: int = 0
    round_no: int = 0
    src: str = ""

    def payload(self) -> dict:
        return {"v": VERSION, "kind": "alive", "id": self.id,
                "exits": int(self.exits), "round_no": int(self.round_no),
                "at": round(self.at, 3), "src": self.src}

    @classmethod
    def parse(cls, d: dict) -> "Alive | None":
        if not isinstance(d, dict) or d.get("v") != VERSION or d.get("kind") != "alive":
            return None
        who = str(d.get("id") or "").strip()
        if not who:
            return None
        try:
            at = float(d.get("at") or 0)
            exits = int(d.get("exits") or 0)
            rnd = int(d.get("round_no") or 0)
        except (TypeError, ValueError):
            return None
        return cls(id=who, at=at, exits=exits, round_no=rnd,
                   src=str(d.get("src") or who))


@dataclass(frozen=True)
class Enlist:
    """子程序报到：我在，我的转发口在这个端口上。

    **不带自己的 IP。** 主程序从 UDP 包的源地址取——子程序不用去猜自己在内网里
    叫什么，多网卡、容器、WSL 这些场景下那个猜测经常是错的。

    `direct=True` 表示这台跟主程序同一个出口 IP，借它的口出去等于绕回自己，
    白搭一跳。这个由人在 config 里标（`link.same_exit_as_master`）——子程序
    自己问不出公网 IP，而为了问它去连第三方回显服务，代价比收益大。
    """

    id: str
    proxy_port: int
    direct: bool = False
    at: float = 0.0
    src: str = ""

    def payload(self) -> dict:
        return {"v": VERSION, "kind": "enlist", "id": self.id,
                "proxy_port": int(self.proxy_port), "direct": bool(self.direct),
                "at": round(self.at, 3), "src": self.src}

    @classmethod
    def parse(cls, d: dict) -> "Enlist | None":
        if not isinstance(d, dict) or d.get("v") != VERSION or d.get("kind") != "enlist":
            return None
        who = str(d.get("id") or "").strip()
        try:
            port = int(d.get("proxy_port") or 0)
            at = float(d.get("at") or 0)
        except (TypeError, ValueError):
            return None
        if not who or not (0 < port < 65536):
            return None
        return cls(id=who, proxy_port=port, direct=bool(d.get("direct")),
                   at=at, src=str(d.get("src") or who))


@dataclass(frozen=True)
class Sighting:
    """一次「某型号在某门店有货」的观察。

    `at` 是**发送方**看到它的墙上时钟（epoch 秒）。用墙上时钟是因为要跨机器；
    代价是两台必须对时，所以收端会检查偏差。
    """

    part: str
    store: str
    name: str = ""
    at: float = 0.0
    src: str = ""

    def payload(self) -> dict:
        return {"v": VERSION, "kind": "seen", "part": self.part, "store": self.store,
                "name": self.name, "at": round(self.at, 3), "src": self.src}

    @classmethod
    def parse(cls, d: dict) -> "Sighting | None":
        if not isinstance(d, dict) or d.get("v") != VERSION or d.get("kind") != "seen":
            return None
        part, store = str(d.get("part") or "").strip(), str(d.get("store") or "").strip()
        if not part or not store:
            return None
        try:
            at = float(d.get("at") or 0)
        except (TypeError, ValueError):
            return None
        return cls(part=part.upper(), store=store.upper(),
                   name=str(d.get("name") or ""), at=at, src=str(d.get("src") or ""))


def encode(s, key: bytes, nonce: str = "") -> bytes:
    """打包成一个待广播的 UDP 载荷。Sighting 和 Enlist 都走这里。"""
    body = s.payload()
    body["nonce"] = nonce or uuid.uuid4().hex
    return json.dumps({"body": body, "mac": sign(body, key)},
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class Seen:
    """见过的 nonce，用来挡重放。按时间淘汰，不会无限长。"""

    def __init__(self, ttl: float = MAX_AGE * 2, clock=time.time):
        self.ttl, self.clock, self._at = ttl, clock, {}

    def fresh(self, nonce: str) -> bool:
        """第一次见返回 True；见过返回 False。"""
        now = self.clock()
        if self._at and len(self._at) > 4096:
            self._at = {n: t for n, t in self._at.items() if now - t <= self.ttl}
        last = self._at.get(nonce)
        if last is not None and now - last <= self.ttl:
            return False
        self._at[nonce] = now
        return True


@dataclass
class Decoder:
    """把收到的字节变成 Sighting，顺便挡掉伪造、重放、过期和时钟跑偏的。

    每一条拒绝都要能说出理由——这条总线会触发下单，静默丢包最难查。
    """

    key: bytes
    clock: object = time.time
    max_skew: float = MAX_SKEW
    max_age: float = MAX_AGE
    seen: Seen = field(default_factory=Seen)
    #: 只收自己人发的；留空表示不限。用来把「另一台机器」和「网上邻居」分开。
    allow: tuple = ()
    #: 只认这几种消息。买手不关心 enlist，主程序两种都要——各自只开自己用得上的，
    #: 能处理的消息种类越少，能出错的地方越少。
    kinds: tuple = ("seen",)

    def decode(self, raw: bytes) -> tuple["Sighting | None", str]:
        """返回 (信号, 拒绝理由)。收下了理由是空串。"""
        if not self.key:
            return None, "没有配 " + KEY_ENV
        try:
            msg = json.loads(raw.decode("utf-8"))
            body, mac = msg["body"], msg["mac"]
        except Exception:
            return None, "不是合法的总线消息"
        if not isinstance(body, dict):
            return None, "消息体不是对象"
        if not verify(body, mac, self.key):
            return None, "签名不对（密钥不一致，或者有人在冒充）"
        nonce = str(body.get("nonce") or "")
        if not nonce:
            return None, "缺 nonce，挡不住重放"
        kind = str(body.get("kind") or "")
        if kind not in self.kinds:
            return None, f"不收 {kind!r} 这种消息"
        s = {"seen": Sighting, "enlist": Enlist, "alive": Alive}[kind].parse(body)
        if s is None:
            return None, "字段不认识（版本不一致？）"
        if self.allow and s.src not in self.allow:
            return None, f"来源 {s.src!r} 不在名单里"
        now = self.clock()
        if s.at - now > self.max_skew:
            return None, (f"时刻来自未来 {s.at - now:.1f}s——两台机器没对时，"
                          f"这条信号的新鲜度不可信")
        if now - s.at > self.max_age:
            return None, f"已经旧了 {now - s.at:.0f}s，放货窗口早过了"
        if not self.seen.fresh(nonce):
            return None, "重放（这个 nonce 见过了）"
        return s, ""


class Sender:
    """往局域网广播信号。丢包不重试——探针下一轮还会再发。"""

    def __init__(self, key: bytes, src: str, port: int = DEFAULT_PORT,
                 peers: tuple = (), log=print):
        self.key, self.src, self.port, self.log = key, src, port, log
        #: 显式对端清单。广播被交换机或防火墙拦掉时用得上。
        self.peers = tuple(peers) or (DEFAULT_ADDR,)
        self.sock = None

    def open(self) -> None:
        if self.sock is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock = s

    def send(self, sighting: Sighting) -> int:
        """发出去，返回成功投递的对端数。没有密钥就直接不发。"""
        if not self.key:
            return 0
        self.open()
        data = encode(sighting, self.key)
        ok = 0
        for peer in self.peers:
            try:
                self.sock.sendto(data, (peer, self.port))
                ok += 1
            except OSError as e:
                self.log(f"[总线] 发给 {peer} 失败：{type(e).__name__}: {e}")
        return ok

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


class Receiver:
    """守着端口收信号，每收到一条合法的就调一次 on_sighting。

    单独一个线程，不碰买手的状态——它只负责把信号递进去。
    """

    def __init__(self, key: bytes, on_sighting, port: int = DEFAULT_PORT,
                 allow: tuple = (), log=print, clock=time.time,
                 kinds: tuple = ("seen",)):
        self.decoder = Decoder(key=key, allow=tuple(allow), clock=clock,
                               kinds=tuple(kinds))
        self.on_sighting, self.port, self.log = on_sighting, port, log
        self.sock = None
        self.thread = None
        self.closed = False
        #: 同一个理由不重复刷屏——密钥配错的话会每个包都拒一次。
        self._last_reason = ""
        self.taken = self.refused = 0

    def start(self) -> None:
        if self.thread is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", self.port))
        s.settimeout(0.5)
        self.sock = s
        self.thread = threading.Thread(target=self._run, name="bus-rx", daemon=True)
        self.thread.start()
        self.log(f"[总线] 正在 :{self.port} 上等信号")

    def _run(self) -> None:
        while not self.closed:
            try:
                raw, addr = self.sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                return
            s, why = self.decoder.decode(raw)
            if s is None:
                self.refused += 1
                if why != self._last_reason:
                    self._last_reason = why
                    self.log(f"[总线] 丢弃来自 {addr[0]} 的包：{why}")
                continue
            self.taken += 1
            self._last_reason = ""
            try:
                # 回调拿得到源地址：enlist 要靠它得知子程序的内网 IP
                self.on_sighting(s, addr[0])
            except Exception as e:
                self.log(f"[总线] 处理信号出错：{type(e).__name__}: {e}")

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
