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
from hunter.pacing import breaker_settings

#: 多久没报到就认为这个子程序不在了。子程序每 20 秒报一次，60 秒等于连着漏三次。
STALE = 60.0


@dataclass
class Exit:
    """一条出口。`proxy` 为空表示直连（走主程序自己的网关）。"""

    id: str
    proxy: str | None = None
    client: AppleClient | None = None
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
        self._exits["direct"] = Exit(id="direct", proxy=None,
                                     client=self._client(None), permanent=True)

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
            self._exits[who] = Exit(id=who, proxy=proxy,
                                    client=self._client(proxy), seen_at=now)
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
        """轮到谁了。全都在熔断里就返回 None——那时候该等，不该硬打。"""
        self.prune()
        with self._lock:
            pool = list(self._exits.values())
            if not pool:
                return None
            n = len(pool)
            for i in range(n):
                e = pool[(self._turn + i) % n]
                if e.ready(path):
                    self._turn = (self._turn + i + 1) % n
                    e.used += 1
                    return e
            return None

    def soonest(self, path: str) -> float:
        """全在熔断时，最早能用的那条还要等多久。"""
        with self._lock:
            waits = [e.cooldown(path) for e in self._exits.values() if e.client]
        return min(waits) if waits else 0.0

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
