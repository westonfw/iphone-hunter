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
#:
#: 2 —— 快照从 `PART@STORE` 改成按型号分组的 `PART:R581,R359`。**这个必须升版本**：
#: 旧的解析规则按 `@` 切，遇到新格式会切不出东西，于是解析成「空快照」而
#: `saw_at` 照旧有效——旧买手会据此判「所有型号都没货」，掐掉正在进行的下单。
#: 升了版本之后旧程序直接丢弃整条消息，买手收不到心跳就会报「主程序失联」：
#: 吵，但看得见，而且不会误刹。
VERSION = 2

#: 允许的时钟偏差。超过它说明两台机器没对时，那 observed 就不可信了——
#: 宁可丢掉这条信号，也不能拿一个「来自未来」的时刻去算库存新鲜度。
MAX_SKEW = 5.0
#: 比这还旧的信号直接丢。放货窗口本身才十几秒，迟到的没有意义。
MAX_AGE = 120.0

#: 快照最多带这么多条 "PART@STORE"。8 个型号 × 12 家店也才 96 条，正常远够用；
#: 超了就说明配置大得离谱，那时候宁可宣布「这一轮不算看清」让买手放行，也不能
#: 发一份残缺的快照出去当完整的用。
MAX_STOCK = 256

#: 一个 UDP 包最多收这么多。收小了的话大包会被截断，而截断的 JSON 连解析都
#: 过不去——心跳跟着一起丢，于是买手报「主程序失联」，而主程序好好地在跑。
MAX_PACKET = 65535

#: **一个包最多发这么多字节。**
#:
#: 2026-09-20 本机实测：超过 1472 字节的 UDP 包**静默消失**——发送端不报错、
#: 接收端什么都收不到。`lo` 的 MTU 写着 65536，但路径上有一段 1500 的限制，
#: 分片直接被丢。1472 = 1500 − 20（IP 头）− 8（UDP 头），是标准以太网的数，
#: 两台机器之间同理。
#:
#: 这件事最难查的地方在于它长得跟「主程序挂了」一模一样：买手收不到心跳、
#: 推送「主程序失联」，而主程序好好地在跑，日志里一切正常。所以留足余量，
#: 并且**超了一定要报出来**，绝不静默丢。
MAX_PAYLOAD = 1200

#: 「这是我自己广播的」。UDP 广播会回到本机，主程序既发又收，不挡掉的话
#: 每一轮都会在日志里拒绝自己一次。这不是错误，所以收端见到它一声不吭。
ECHO = "自己发的"
#: 种类不在这个接收方要收的范围里（买手收到别的买手的 enlist 就是这种）。
#: 跟 ECHO 一样是「按设计就该忽略」的，不是错误——静默跳过，别刷屏。
WRONG_KIND = "种类不收"


def wall_of(mono: float, now_mono=None, now_wall=None) -> float:
    """把单调时钟的时刻换算成墙上时钟。**跨机器只能传墙上时钟。**"""
    now_mono = time.monotonic() if now_mono is None else now_mono
    now_wall = time.time() if now_wall is None else now_wall
    return now_wall - (now_mono - mono)


def mono_of(wall: float, now_mono=None, now_wall=None) -> float:
    """反过来：别人的墙上时刻换算成本机的单调时刻。

    两台机器的墙上时钟差多少，这里就偏多少——所以收端必须挡掉偏差大的包，
    不然 candidate_max_age 会拿一个错的年龄去判断新鲜度。

    **换算之后不要拿来比先后。** 每次调用都用当时的 monotonic/time 差去折算，
    两次调用之间的抖动会让同一个墙上时刻换出不同的结果。判先后一律用墙上时刻
    本身（见 hunter2.buyer.stock_live）。
    """
    now_mono = time.monotonic() if now_mono is None else now_mono
    now_wall = time.time() if now_wall is None else now_wall
    return now_mono - (now_wall - wall)


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

    **`saw_at` 跟 `at` 是两件事，别混。** `at` 只说「进程还活着」；`saw_at` 说
    「我这一轮把该看的型号全都看清了」——查询失败、返回 UNKNOWN、出口全在熔断
    静默里，都不算看清。买手的急刹只能踩在 `saw_at` 上：拿「进程还活着」当
    「确实没货」，会在接口抖动或者被限流的时候掐掉正在进行的下单，而那恰恰是
    最不该刹车的时刻。0 表示「我还没看清过」。

    **`stock` 是那一轮的完整快照，不是增量。** 没有它的话，「有货」只能靠
    seen 消息表达，而买手就只能把「这一轮没收到 seen」当成「没货了」——UDP
    根本不保证送达，丢一个包就会撤销一批还有效的候选，然后掐掉下单。带上快照
    之后，「还有没有货」由快照本身回答，跟某个包丢没丢无关；买手还能拿它把
    丢掉的候选补回来。

    **`parts` 是这份快照的范围。** 快照只对它盯的型号完整；对别的型号它什么
    都没说。不带范围的话，一个只盯 Q 的探针发来的快照会把还有货的 P 一起撤掉
    ——它根本没查过 P。

    **`gone` 是每个型号的售罄计数。** 心跳十秒一条，而「有货→没货→又有货」
    可能整个发生在两条心跳之间：中间那份「没货」的快照被后一份覆盖，买手就
    永远等不到那条「明确无货」，于是重试次数和判死标记清不掉，货明明有却不再
    重试。计数是累计的，丢几条包也能靠差值看出来中间卖空过。
    """

    id: str
    at: float = 0.0
    exits: int = 0
    round_no: int = 0
    src: str = ""
    #: 最后一轮「所有型号都拿到确定结论」的墙上时刻。0 = 没看清过。
    saw_at: float = 0.0
    #: 那一轮的完整有货快照，每项是 "PART:R581,R359"（按型号分组，省掉重复的
    #: 型号编号——一个包只有一千多字节可用，摊开写的话八个型号十二家店就超了）。
    #: 空 = 一家都没货。
    stock: tuple = ()
    #: 这份快照的**范围**：发送方盯的型号。快照只对它们完整。
    parts: tuple = ()
    #: 范围的另一半：发送方盯的门店。空 = 附近全部，对哪家店都算数。
    #: 两个探针盯同一个型号、不同门店时，少了这一半它们会互相撤掉对方的货。
    stores: tuple = ()
    #: 每个型号的累计售罄次数，每项是 "PART:N"。
    gone: tuple = ()

    def payload(self) -> dict:
        return {"v": VERSION, "kind": "alive", "id": self.id,
                "exits": int(self.exits), "round_no": int(self.round_no),
                "at": round(self.at, 3), "saw_at": round(self.saw_at, 3),
                "stock": list(self.stock), "parts": list(self.parts),
                "stores": list(self.stores), "gone": list(self.gone),
                "src": self.src}

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
            saw = float(d.get("saw_at") or 0)
        except (TypeError, ValueError):
            return None
        # 「看清的时刻」在未来是不可能的，那只能是配错或者伪造。当成没看清，
        # 宁可不刹车——刹错的代价是掐掉一次真实下单。
        if saw > at:
            saw = 0.0
        # **「没带快照」和「空快照」是两回事。** 空快照是「这一轮一家都没货」，
        # 没带是「这个发送端还不会发快照」——老版本只发 saw_at，它的「没货」靠
        # 的是「你没收到 seen」，而那正是我们刚判定不可靠的东西。把它当空快照，
        # 新买手会被一个老探针清空候选。所以没带就连 saw_at 一起作废：这一轮
        # 不算看清，急刹放行。混部署因此只是不刹车，不会误刹。
        raw, scope = d.get("stock"), d.get("parts")
        blind = cls(id=who, at=at, exits=exits, round_no=rnd, saw_at=0.0,
                    src=str(d.get("src") or who))
        if raw is None or scope is None:
            return blind
        if not isinstance(raw, list) or not isinstance(scope, list):
            return None
        stock = []
        for x in raw:
            part, sep, tail = str(x).partition(":")
            part = part.strip().upper()
            if not sep or not part:
                continue
            stores = [y.strip().upper() for y in tail.split(",") if y.strip()]
            if stores:
                stock.append(f"{part}:{','.join(stores)}")
        parts = tuple(str(x).strip().upper() for x in scope if str(x).strip())
        shops = d.get("stores")
        if shops is not None and not isinstance(shops, list):
            return None
        stores = tuple(str(x).strip().upper() for x in (shops or [])
                       if str(x).strip())
        if len(stock) > MAX_STOCK or len(parts) > MAX_STOCK:
            # 装不下就不能再说「这是完整快照」——宁可让买手判不准而放行
            return blind
        gone = {}
        for x in (d.get("gone") or []):
            part, sep, n = str(x).rpartition(":")
            if sep and part.strip() and n.isdigit():
                gone[part.strip().upper()] = int(n)
        return cls(id=who, at=at, exits=exits, round_no=rnd, saw_at=saw,
                   stock=tuple(stock), parts=parts, stores=stores,
                   gone=tuple(f"{k}:{v}" for k, v in sorted(gone.items())),
                   src=str(d.get("src") or who))


@dataclass(frozen=True)
class Enlist:
    """子程序报到：我在，我的转发口在这个端口上。

    **不带自己的 IP。** 主程序从 UDP 包的源地址取——子程序不用去猜自己在内网里
    叫什么，多网卡、容器、WSL 这些场景下那个猜测经常是错的。

    `direct=True` 表示这台跟主程序同一个出口 IP，借它的口出去等于绕回自己，
    白搭一跳。这个由人在 config 里标（`link.same_exit_as_master`）——子程序
    自己问不出公网 IP，而为了问它去连第三方回显服务，代价比收益大。标了它的
    子程序**根本不开转发口**（一个没人用的监听口就是白送的攻击面），所以这种
    报到的 `proxy_port` 是 0，意思是「我在，但没有口借给你」。
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
        direct = bool(d.get("direct"))
        # 0 只在「同出口、不开转发口」时成立；其余一律是配错或者伪造
        if not who or not (0 <= port < 65536) or (port == 0 and not direct):
            return None
        return cls(id=who, proxy_port=port, direct=direct,
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
    #: 自己的 link.id。填了就静默丢掉自己广播回来的包——既发又收的程序
    #: （主程序、watch）否则会把自己的信号当外来的再处理一遍。
    me: str = ""

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
        if self.me and str(body.get("src") or "") == self.me:
            return None, ECHO       # 广播回到本机，不是错误，别刷屏
        kind = str(body.get("kind") or "")
        if kind not in self.kinds:
            return None, WRONG_KIND
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


def dest_of(peer: str, port: int) -> tuple:
    """把一条 peers 配置解析成 (地址, 端口)。

    支持 `192.168.1.9` 和 `192.168.1.9:48712` 两种写法。**后一种是同一台机器上
    跑好几份部署时唯一能用的写法**：单播 UDP 只会投给其中一个 socket（几个进程
    用 SO_REUSEADDR 绑同一个端口时，Linux 实测全进后启动的那个），于是别的进程
    一条消息都收不到，而且不报任何错。广播不存在这个问题——每个 socket 都拿到
    一份副本，所以默认就是广播。
    """
    host, sep, tail = str(peer or "").rpartition(":")
    if sep and tail.isdigit() and host:
        return host, int(tail)
    return str(peer), port


class Sender:
    """往局域网广播信号。丢包不重试——探针下一轮还会再发。"""

    def __init__(self, key: bytes, src: str, port: int = DEFAULT_PORT,
                 peers: tuple = (), log=print):
        self.key, self.src, self.port, self.log = key, src, port, log
        #: 显式对端清单。广播被交换机或防火墙拦掉时用得上。
        #: 每项可以带端口（`192.168.1.9:48712`），同机多进程时必须带。
        self.peers = tuple(peers) or (DEFAULT_ADDR,)
        self.sock = None

    def open(self) -> None:
        if self.sock is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock = s

    def oversize(self, msg) -> bool:
        """这条消息大到会在路上静默消失吗。**发之前问一句。**"""
        return bool(self.key) and len(encode(msg, self.key)) > MAX_PAYLOAD

    def shrink(self, msg):
        """把一条超限的心跳一层层剥到发得出去为止。

        只摘掉快照是不够的：型号一多，光是售罄计数就能把包顶过上限，于是降级
        后的消息照样被拒发——买手还是收不到心跳，还是会误报「主程序失联」。
        剥的顺序按「丢了最不可惜」排：快照 → 售罄计数 → 范围。到最后只剩一句
        「我还活着」，那也比整条消失强。
        """
        import dataclasses
        for drop in ({"saw_at": 0.0, "stock": ()}, {"gone": ()}, {"parts": (),
                                                                  "stores": ()}):
            if not self.oversize(msg):
                return msg
            msg = dataclasses.replace(msg, **drop)
        return msg

    def send(self, sighting: Sighting) -> int:
        """发出去，返回成功投递的对端数。没有密钥就直接不发。"""
        if not self.key:
            return 0
        self.open()
        data = encode(sighting, self.key)
        if len(data) > MAX_PAYLOAD:
            # 硬拦在这儿：发出去也是白发，而且不会有任何错误提示——那正是最难
            # 查的一类故障（长得跟「对端挂了」一模一样）。
            self.log(f"[总线] 这条消息 {len(data)} 字节，超过 {MAX_PAYLOAD} "
                     f"就会在路上静默消失，不发了")
            return 0
        ok = 0
        for peer in self.peers:
            try:
                self.sock.sendto(data, dest_of(peer, self.port))
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
                 kinds: tuple = ("seen",), me: str = ""):
        self.decoder = Decoder(key=key, allow=tuple(allow), clock=clock,
                               kinds=tuple(kinds), me=str(me or ""))
        self.on_sighting, self.port, self.log = on_sighting, port, log
        self.sock = None
        self.thread = None
        self.closed = False
        #: 同一个理由不重复刷屏——密钥配错的话会每个包都拒一次。
        self._last_reason = ""
        self.taken = self.refused = self.echoed = self.skipped = 0

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
                raw, addr = self.sock.recvfrom(MAX_PACKET)
            except socket.timeout:
                continue
            except OSError:
                return
            s, why = self.decoder.decode(raw)
            if why == ECHO:
                self.echoed += 1
                continue
            if why == WRONG_KIND:
                # 按设计就不收的种类（如买手收到别的买手的 enlist）。静默跳过：
                # 正常的 seen/alive 夹在中间会把 _last_reason 去重重置，不静默的话
                # 每条 enlist 都刷一行。
                self.skipped += 1
                continue
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
