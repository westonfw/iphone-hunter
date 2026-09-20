"""子程序的报到：开一个转发口，然后周期性地告诉主程序「我在，口在这儿」。

**把自己的出口 IP 借出去**是这个结构的核心收益。主程序手上有几条出口，巡检
的合并速率就是几倍；而每多一个买手，就同时多一个账号和一个出口 IP——两种稀缺
资源一起涨，还不用买代理。

为什么是周期广播而不是「启动时报一次」：

* 主程序可能后启动，也可能重启。报一次的话它重启之后就再也不知道有谁在了。
* 这同时是子程序的心跳。主程序据此摘掉掉线的出口——把请求发给一台已经不在的
  机器，等于白白浪费一轮。

标了 `link.same_exit_as_master` 的子程序**不开转发口**：借它的口出去等于绕回
主程序自己的网关，白搭一跳，而一个没人用的监听口就是白送的攻击面。它照样报到
（`proxy_port=0`），这样主程序的日志里看得见它活着。
"""

from __future__ import annotations

import threading
import time

from .bus import DEFAULT_PORT, Enlist, Sender, bus_key
from .proxy import ForwardProxy

#: 多久报到一次。主程序那边 60 秒判掉线，20 秒等于要连着漏三次才会被摘。
EVERY = 20.0


class Enlister:
    """子程序这一侧的全部对外接口：一个转发口 + 一条心跳。"""

    def __init__(self, cfg: dict, bus_id: str, log=print):
        link = dict(cfg.get("link") or {})
        self.log, self.bus_id = log, bus_id
        self.direct = bool(link.get("same_exit_as_master", False))
        self.every = max(5.0, float(link.get("enlist_every", EVERY) or EVERY))
        self.port = int(link.get("port") or DEFAULT_PORT)
        peers = tuple(str(p) for p in (link.get("peers") or ()) if p)
        self.key = bus_key()
        self.proxy_port = 0
        self.proxy = None
        if not self.direct:
            self.proxy = ForwardProxy(
                self.key, port=int(link.get("proxy_port") or 0),
                host=str(link.get("proxy_host") or "0.0.0.0"), log=log)
        self.sender = Sender(key=self.key, src=bus_id, port=self.port,
                             peers=peers, log=log)
        self.thread = None
        self.closed = False
        self._tick = threading.Event()
        self.sent = 0

    def start(self) -> int:
        """开口、开始报到。返回转发口端口（同出口标记下是 0）。"""
        if not self.key:
            self.log("[报到] 没有 HUNTER_BUS_KEY，不报到也不开转发口")
            return 0
        if self.proxy is not None:
            self.proxy_port = self.proxy.start()
        else:
            self.log("[报到] 标了跟主程序同出口，不开转发口"
                     "（借自己的网关绕一圈没有意义），只报个到")
        self.thread = threading.Thread(target=self._run, name="enlist", daemon=True)
        self.thread.start()
        return self.proxy_port

    def beat(self) -> int:
        """报一次到。**不带自己的 IP**——主程序从 UDP 包的源地址取，那个才准。"""
        try:
            n = self.sender.send(Enlist(
                id=self.bus_id, proxy_port=self.proxy_port, direct=self.direct,
                at=time.time(), src=self.bus_id))
        except Exception as e:
            self.log(f"[报到] 发不出去：{type(e).__name__}: {e}")
            return 0
        self.sent += n
        return n

    def _run(self) -> None:
        first = True
        while not self.closed:
            n = self.beat()
            if first:
                first = False
                self.log(f"[报到] {self.bus_id} 每 {self.every:.0f}s 向主程序报到"
                         + (f"，转发口 :{self.proxy_port}" if self.proxy_port
                            else "（同出口，无转发口）")
                         + ("" if n else "——但一个对端都没发出去，检查网络"))
            self._tick.wait(self.every)

    def close(self, timeout: float = 2.0) -> None:
        self.closed = True
        self._tick.set()
        if self.thread is not None:
            self.thread.join(timeout)
            self.thread = None
        if self.proxy is not None:
            self.proxy.close()
        self.sender.close()

    def describe(self) -> str:
        if self.proxy is None:
            return "同出口，未开转发口"
        return (f"转发口 :{self.proxy_port}，已转 {self.proxy.served} 条、"
                f"拒 {self.proxy.refused} 条")
