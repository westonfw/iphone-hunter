"""探针：只盯，不买。

跟买手彻底分开的理由，是这两样东西的性质正好相反：

            盯                              买
  要账号    不要（pickup 是公开接口）        要
  能并行    能，按出口 IP 横向扩             不能，一个账号一次一发
  被拦了    这个 IP 静默 90~300 秒           整条链路停摆
  稀缺      出口 IP，可以加                  **Apple ID**

揉在一起的代价是双向的：买手被 541 拖进静默期就跟着失明（2026-09-20 本机 60
分钟、另一台 129 分钟），而盯又抢走了本该留给重试的请求预算。

拆开之后探针可以随便加——一台旧笔记本、一个手机热点、第二个网关，每加一个就
少一分失明。它们不需要账号，所以加探针的成本跟加账号完全不是一回事。

**每一轮看到货都广播，不只在状态翻转时广播。** 买手那边拿这个当心跳：一直收到
就是货还在，收不到了就是没了——它自己不轮询，这是它唯一的库存来源。
"""

from __future__ import annotations

import time

from hunter.apple import PICKUP_PATH, Stock
from hunter.monitor import StockWatcher

from .bus import DEFAULT_PORT, Sender, Sighting, bus_key
from .link import wall_of


class Sensor(StockWatcher):
    """一个只盯不买的探针。

    强制关掉 autobuy——探针连浏览器都不该开。想让同一份部署既盯又买，用
    `hunter2 watch`（LinkedWatcher），不要用这个。
    """

    def __init__(self, cfg: dict, root, sprint: bool = False, log=None):
        cfg = dict(cfg, autobuy=dict(cfg.get("autobuy") or {}, enabled=False))
        super().__init__(cfg, root, sprint=sprint, **({"log": log} if log else {}))
        link = dict(cfg.get("link") or {})
        self.bus_id = str(link.get("id") or "").strip() or "sensor"
        key = bus_key()
        self.sender = None
        if not key:
            self.log("[总线] 没配 HUNTER_BUS_KEY——探针发出去也没人收，先生成密钥")
            return
        peers = tuple(str(p) for p in (link.get("peers") or ()) if p)
        self.sender = Sender(key=key, src=self.bus_id,
                             port=int(link.get("port") or DEFAULT_PORT),
                             peers=peers, log=self.log)
        self.log(f"[探针] 我是 {self.bus_id}，只盯不买，"
                 f"对端 {'、'.join(peers) or '广播'}")

    def _check_pickup(self, part: str, stores) -> None:
        # 先照常做原来那一套（写状态、发提醒、打日志），再把看到的货喊出去
        super()._check_pickup(part, stores)
        if not self.sender:
            return
        sel = [s for s in stores
               if not self.only_stores or s.store_number.upper() in self.only_stores]
        live = [s for s in sel if s.state is Stock.AVAILABLE]
        if not live:
            return
        # 用接口真正返回的那一刻，不是现在——中间还隔着解析和这一圈循环
        observed = getattr(self.client, "observed_at", {}).get(
            PICKUP_PATH, time.monotonic())
        at = wall_of(observed)
        sent = 0
        for s in live:
            try:
                sent += self.sender.send(Sighting(
                    part=part, store=s.store_number.upper(),
                    name=s.store_name or s.store_number, at=at, src=self.bus_id))
            except Exception as e:
                self.log(f"[探针] 广播失败：{type(e).__name__}: {e}")
        if sent:
            self.log(f"[探针] 已广播 {part} 在 "
                     f"{'、'.join(s.store_name or s.store_number for s in live)} 有货")

    def loop(self) -> None:
        try:
            super().loop()
        finally:
            if self.sender:
                self.sender.close()
