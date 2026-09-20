"""把现有的 watch 循环接到信号总线上：自己看到的广播出去，别人看到的收进来。

部署单元是**目录**：拷一份代码、改一份 config，就是一个新部署。所以这里不管
「一个进程跑几个账号」——一台机器要跑几个账号，就放几份拷贝，订单记录、浏览器
profile、日志天然各是各的。

接上总线之后，N 份部署互相是对方的眼睛：

* **补盲。** 单份部署今天有 9~20% 的时间在 541 熔断静默里，那段时间完全看不见
  放货。别人的信号正好补上。
* **抢早。** N 个探针取最快的那个，检测延迟从「自己的轮询间隔」变成「最快那个
  的轮询间隔」。
* **分工。** 2026-09-20 08:56 有 8 个型号同时放货，而所有部署都会按 watch 的配置
  顺序从第一个打起——撞在同一个型号上。用 `buyer_offset` 把优先级轮转开，各打各的。

**只接收「看到了」。** 别人的「没货」可能只是它自己接口抖动或熔断静默，拿来当
真相会误杀——所以刹车永远只认自己看到的（见 hunter.purchase_worker.stock_live）。
"""

from __future__ import annotations

import dataclasses
import time

from hunter.monitor import StockWatcher
from hunter.purchase_worker import Offer

from .bus import DEFAULT_PORT, Receiver, Sender, Sighting, bus_key


def wall_of(mono: float, now_mono=None, now_wall=None) -> float:
    """把单调时钟的时刻换算成墙上时钟。跨机器只能传墙上时钟。"""
    now_mono = time.monotonic() if now_mono is None else now_mono
    now_wall = time.time() if now_wall is None else now_wall
    return now_wall - (now_mono - mono)


def mono_of(wall: float, now_mono=None, now_wall=None) -> float:
    """反过来：别人的墙上时刻换算成本机的单调时刻。

    两台机器的墙上时钟差多少，这里就偏多少——所以总线那边必须挡掉偏差大的包，
    不然 candidate_max_age 会拿一个错的年龄去判断新鲜度。
    """
    now_mono = time.monotonic() if now_mono is None else now_mono
    now_wall = time.time() if now_wall is None else now_wall
    return now_mono - (now_wall - wall)


def rotate(offer: Offer, offset: int, span: int) -> Offer:
    """按部署的编号轮转型号优先级，让几份部署不要都去打同一个型号。

    priority 的第一项是型号在 watch 列表里的下标。加上 offset 再取模，等于把
    每份部署的「第一顺位」错开——8 个型号同时放货时，offset=0 打第 1 个、
    offset=1 打第 2 个，而不是一起挤在第 1 个上。
    """
    if not offset or span <= 0 or not offer.priority:
        return offer
    pri = list(offer.priority)
    try:
        pri[0] = (int(pri[0]) + int(offset)) % int(span)
    except (TypeError, ValueError):
        return offer
    return dataclasses.replace(offer, priority=tuple(pri))


class LinkedWatcher(StockWatcher):
    """StockWatcher + 一条局域网总线。除此之外行为完全一致。

    没配 `HUNTER_BUS_KEY` 就退化成普通的 StockWatcher——总线不是必需品，
    单机也要能跑。
    """

    def __init__(self, cfg: dict, root, sprint: bool = False, log=None):
        super().__init__(cfg, root, sprint=sprint, **({"log": log} if log else {}))
        link = dict(cfg.get("link") or {})
        self.bus_id = str(link.get("id") or "").strip() or "buyer"
        self.offset = int(link.get("buyer_offset") or 0)
        self.port = int(link.get("port") or DEFAULT_PORT)
        peers = tuple(str(p) for p in (link.get("peers") or ()) if p)
        allow = tuple(str(p) for p in (link.get("allow") or ()) if p)

        key = bus_key()
        self.sender = self.receiver = None
        if not key:
            self.log("[总线] 没配 HUNTER_BUS_KEY，本次不联机（单机照常工作）")
            return
        if not self.purchase_worker:
            self.log("[总线] 自动下单没开，只广播不接收")
        self.sender = Sender(key=key, src=self.bus_id, port=self.port,
                             peers=peers, log=self.log)
        self._wrap_observe()
        if self.purchase_worker:
            self.receiver = Receiver(key=key, on_sighting=self._heard,
                                     port=self.port, allow=allow, log=self.log)
        self.log(f"[总线] 我是 {self.bus_id}，偏移 {self.offset}，"
                 f"端口 {self.port}，对端 {'、'.join(peers) or '广播'}")

    # ---------- 出 ----------

    def _wrap_observe(self) -> None:
        """在 worker.observe 外面套一层：喂给自己的同时广播出去。

        套在这里而不是改 monitor：hunter 那边一行都不用动，两套可以并行跑。
        """
        worker = self.purchase_worker
        if worker is None:
            return
        # **必须留住原始的那个**：_heard 收到别人的信号后要喂给买手，如果喂的是
        # 包过的版本，就会把刚收到的再广播一遍——A→B→A→B 无限回声。
        inner = self._feed = worker.observe

        def observe(part, offers, unavailable=()):
            offers = [rotate(o, self.offset, len(self.parts)) for o in offers]
            inner(part, offers, unavailable)
            self._shout(offers)

        worker.observe = observe

    def _shout(self, offers) -> None:
        if not self.sender or not offers:
            return
        now_m, now_w = time.monotonic(), time.time()
        for o in offers:
            try:
                self.sender.send(Sighting(
                    part=o.part, store=o.store, name=o.name,
                    at=wall_of(o.observed, now_m, now_w), src=self.bus_id))
            except Exception as e:
                self.log(f"[总线] 广播失败：{type(e).__name__}: {e}")

    # ---------- 入 ----------

    def _heard(self, s: Sighting) -> None:
        """别人看到货了。构造成本地的 Offer 喂给买手。

        **不碰 pacer 的冲刺**：冲刺是给自己的轮询提速用的，而这条信号已经到手了，
        再提速只会多烧配额。冲刺仍然由本机自己看到货时触发。
        """
        worker = self.purchase_worker
        if worker is None:
            return
        part = s.part
        if part not in self.parts:
            return                      # 对端在盯我们不买的型号
        if self.only_stores and s.store not in self.only_stores:
            return                      # 那家店我们不去
        observed = mono_of(s.at)
        pri = ((self.parts.index(part) + self.offset) % max(1, len(self.parts)),
               len(self.only_stores or ()))
        offer = Offer(part, s.store, s.name or s.store, self._buy_url(part),
                      self.note_of.get(part) or part, observed, pri)
        age = time.monotonic() - observed
        self.log(f"[总线] {s.src} 报 {part}@{s.store} 有货（{age:.1f}s 前看到的）")
        # 只递「有货」，不递「没货」——别人的失明不是真相。
        # 走 _feed（原始的 observe），不走被包过的那个，否则会把它再广播回去。
        feed = getattr(self, "_feed", None) or worker.observe
        feed(part, [offer])

    # ---------- 生命周期 ----------

    def loop(self) -> None:
        if self.receiver:
            self.receiver.start()
        try:
            super().loop()
        finally:
            if self.receiver:
                self.receiver.close()
            if self.sender:
                self.sender.close()
