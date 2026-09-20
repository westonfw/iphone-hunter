"""出口池：主程序手上所有能发请求的路径，轮着用。

一条出口 = 一个 `AppleClient`。**每条各有各的会话、各有各的熔断**——541 是按
「出口 IP + 端点」判的，一条被拉黑跟另一条没关系。这正是「零失明」的来源：
某条进了 90~300 秒静默期，跳过它用下一条，巡检节奏一点不受影响。

子程序的出口是活的：它报到就加进来，很久不吭声就摘掉。摘掉不是删掉——它可能
只是重启中，下次报到会原样回来。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from hunter.apple import AppleClient
from hunter.pacing import breaker_settings, build_pacer

#: 多久没报到就认为这个子程序不在了。子程序每 20 秒报一次，60 秒等于连着漏三次。
STALE = 60.0


@dataclass
class Exit:
    """一条出口。`proxy` 为空表示直连（走主程序自己的网关）。"""

    id: str
    proxy: str | None = None
    client: AppleClient | None = None
    #: **每条出口自己的节奏器。** 预算和退避都是按「出口 IP」算的——Apple 就是
    #: 这么判的，所以我们也这么记。合并速率因此自动是 N 倍：两条出口各自 30 秒
    #: 一轮，系统看到的就是 15 秒一轮，而每个 IP 仍然只打 30 秒一次。
    pacer: object = None
    #: 下一次轮到它的时刻。主循环按这个挑，天然错开，不需要任何相位协调。
    due_at: float = 0.0
    #: 最后一次听到子程序报到。直连这条永远不过期。
    seen_at: float = 0.0
    permanent: bool = False
    used: int = 0

    @property
    def direct(self) -> bool:
        return not self.proxy

    def ready(self, path: str) -> bool:
        """这条路现在能不能发。被熔断的返回 False。"""
        c = self.client
        if c is None:
            return False
        br = c.breakers.get(path)
        return br is None or br.ready()

    def cooldown(self, path: str) -> float:
        c = self.client
        br = c.breakers.get(path) if c else None
        return br.left() if br else 0.0


class ExitPool:
    """主程序的所有出口。线程安全——注册来自总线线程，取用来自巡检线程。"""

    def __init__(self, cfg: dict, log=print, clock=time.monotonic, stale: float = STALE):
        self.cfg, self.log, self.clock, self.stale = cfg, log, clock, stale
        self._lock = threading.Lock()
        self._exits: dict[str, Exit] = {}
        self._turn = 0
        # 直连永远在池子里：就算一个子程序都没上线，主程序也得能干活
        d = self._make("direct", None, self.clock())
        d.permanent = True
        self._exits["direct"] = d

    def _make(self, who: str, proxy: str | None, now: float) -> Exit:
        pacer = build_pacer(self.cfg, log=lambda *a: None)
        return Exit(id=who, proxy=proxy, client=self._client(proxy),
                    pacer=pacer, due_at=self._spread(now, pacer), seen_at=now)

    def _period(self) -> float:
        for e in self._exits.values():
            if e.pacer:
                return max(1.0, e.pacer.target())
        return 30.0

    def _spread(self, now: float, pacer) -> float:
        """新出口插进日程表里**最空的那段**，别跟现有的撞在一起。

        不做这件事的话，两条出口会从同一时刻起步，合并速率跟单条一样——
        加了等于白加。这正是 2026-09-20 两台独立跑时的毛病（合并中位 24.4s
        而不是 15s，7% 的巡检是同时打的）。
        """
        period = max(1.0, pacer.target() if pacer else 30.0)
        due = sorted(e.due_at for e in self._exits.values())
        if not due:
            return now
        # 把未来一个周期内的日程摊开，找最大的空档，插在它中间
        pts = [t for t in due if t >= now] or [due[0] + period]
        pts = pts + [pts[0] + period]
        best, gap = pts[0], -1.0
        for a, b in zip(pts, pts[1:]):
            if b - a > gap:
                best, gap = a + (b - a) / 2, b - a
        return max(now, best)

    def _nudge(self, e: Exit, want: float) -> float:
        """排下一次时跟别人保持距离。

        抖动是个随机游走，放着不管的话几条出口迟早会飘到一起去。看见挨太近就
        推开半格——一次、有界，不会把节奏搞乱。
        """
        n = max(1, len(self._exits))
        keep = self._period() / n * 0.5
        near = [abs(want - o.due_at) for o in self._exits.values() if o is not e]
        if near and min(near) < keep:
            return want + keep
        return want

    def _client(self, proxy: str | None) -> AppleClient:
        return AppleClient(
            region=self.cfg.get("region", "cn"),
            timeout=int(self.cfg.get("timeout", 15)),
            proxy=proxy,
            breaker=breaker_settings(self.cfg),
        )

    # ---------- 注册 ----------

    def enlist(self, who: str, ip: str, port: int, direct: bool) -> None:
        """一个子程序报到了。

        `direct=True` 是人在子程序的 config 里标的「我跟主程序同一个出口 IP」——
        那样借它的口出去等于绕回自己，白搭一跳，所以不加进池子，只记一下它活着。
        """
        now = self.clock()
        with self._lock:
            if direct:
                if who not in self._exits:
                    self.log(f"[出口] {who} 报到，但标了跟主程序同出口，不单独加一条")
                return
            proxy = self._proxy_url(ip, port)
            cur = self._exits.get(who)
            if cur is not None and cur.proxy == proxy:
                cur.seen_at = now           # 心跳，位置没变
                return
            self._exits[who] = self._make(who, proxy, now)
            self.log(f"[出口] {who} 加入（{ip}:{port}），"
                     f"现在共 {len(self._exits)} 条")

    def _proxy_url(self, ip: str, port: int) -> str:
        from hunter2.bus import bus_key
        from hunter2.proxy import proxy_url
        return proxy_url(ip, port, bus_key())

    def prune(self) -> list[str]:
        """摘掉很久没报到的。返回摘掉的 id。"""
        now, gone = self.clock(), []
        with self._lock:
            for who, e in list(self._exits.items()):
                if e.permanent or now - e.seen_at <= self.stale:
                    continue
                self._exits.pop(who, None)
                gone.append(who)
        for who in gone:
            self.log(f"[出口] {who} 掉线了（{self.stale:.0f}s 没报到），先摘掉")
        return gone

    # ---------- 取用 ----------

    def pick(self, path: str) -> Exit | None:
        """现在轮到哪条出口。没有到点的就返回 None（调用方去 sleep 一小会儿）。

        挑的依据是每条自己的 due_at，所以**相位天然错开**：两条各 30 秒一轮，
        它们会自己岔开成 15 秒。不需要分时隙、不需要对时、不会漂移——这正是
        把 N 个出口收进一个进程换来的东西。
        """
        self.prune()
        now = self.clock()
        with self._lock:
            due = [e for e in self._exits.values() if e.due_at <= now and e.ready(path)]
            if not due:
                return None
            e = min(due, key=lambda x: x.due_at)
            e.used += 1
            return e

    def done(self, e: Exit, cost: float = 1.0) -> None:
        """一条出口刚打完，按它自己的节奏器排下一次，顺便跟别人岔开。"""
        with self._lock:
            delay = e.pacer.next_delay(cost) if e.pacer else 30.0
            e.due_at = self._nudge(e, self.clock() + delay)

    def blocked(self, e: Exit, retry_after: float = 0.0) -> None:
        """这条出口被拦了：退避记在**它自己**头上，别的出口照常跑。"""
        if e.pacer:
            e.pacer.on_blocked(retry_after)
        self.done(e)

    def ok(self, e: Exit) -> None:
        if e.pacer:
            e.pacer.on_ok()

    def boost(self, seconds: float = 0.0) -> None:
        """看到货了：所有出口一起冲刺。"""
        with self._lock:
            for e in self._exits.values():
                if e.pacer:
                    e.pacer.boost(seconds)

    def soonest(self, path: str) -> float:
        """最早能用的那条还要等多久。用来决定主循环睡多久。"""
        now = self.clock()
        with self._lock:
            waits = [max(e.cooldown(path), e.due_at - now)
                     for e in self._exits.values() if e.client]
        return max(0.0, min(waits)) if waits else 0.0

    def describe(self) -> str:
        with self._lock:
            bits = [f"{e.id}{'(直连)' if e.direct else ''}×{e.used}"
                    for e in self._exits.values()]
        return "、".join(bits) or "（空）"

    def close(self) -> None:
        with self._lock:
            for e in self._exits.values():
                s = getattr(e.client, "s", None)
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass
            self._exits.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._exits)
