"""买手：只买，不盯。

它一个请求都不花在巡检上——所有配额、所有注意力都留给下单。库存信息完全来自
总线，由探针供给。

这么分的收益，2026-09-20 的日志算得出来：

* 买手不再自己打 pickup-message，就不会被那个端点的 541 拖进 90~300 秒的静默期
  （那天本机 60 分钟、另一台 129 分钟全是这么丢的）。
* 探针可以按出口 IP 随便加，而买手只能按账号加——两者解耦之后各自按各自的
  稀缺资源扩。

**保活比联机模式更要紧。** 买手不巡检、不加载任何页面，登录态、结账那道登录墙、
购物袋干净与否，全靠 `PurchaseWorker` 里那圈 preflight 维持——那是它唯一的会话
来源。而且它没有库存日志可看，登录掉了不会有任何别的迹象，只能靠推送叫人
（所以登录失败一律 wake=True，穿透免打扰）。

**心跳即库存。** 探针每一轮看到货都会广播，所以「一直收到」= 货还在，「收不到了」
= 没了。买手的急刹（stock_live）就建立在这个心跳上，而且必须分清三种状态：

    收到过这个型号且很新鲜   → 货还在，照常打
    最近有探针在说话，但没提它 → 没货了，刹车
    一个探针都联系不上       → **我瞎了**，不是没货——绝不刹车

第三种是关键：探针全挂的时候刹车等于自断手脚，而那恰恰是最该老实往下走的时候。
"""

from __future__ import annotations

import threading
import time

from hunter.apple import AppleClient
from hunter.autobuy import AutoBuy
from hunter.monitor import watch_items
from hunter.notify import AsyncBroadcaster, Broadcaster
from hunter.purchase_worker import Offer, PurchaseWorker

from .bus import DEFAULT_PORT, Alive, Receiver, Sighting, bus_key
from .link import mono_of

#: 多久没收到这个型号的心跳就当它没货了。探针一轮 4~8 秒（冲刺时），
#: 15 秒等于连着三四轮没听见。
FRESH = 15.0
#: 一个探针都联系不上多久，就认定自己瞎了、不再踩刹车。
BLIND_AFTER = 45.0
#: 主程序多久没心跳就报警。它每轮都发，所以 90 秒等于连着漏好几次。
MASTER_STALE = 90.0


def _p(*a) -> None:
    print(*a, flush=True)


class Buyer:
    """一个账号一个买手。部署单元是目录，所以这里只管一个账号。"""

    def __init__(self, cfg: dict, root, log=_p):
        self.cfg, self.log = cfg, log
        items = watch_items(cfg)
        if not items:
            raise SystemExit("config.json 里没有启用的监控条目，买手不知道该买什么")
        self.parts = [i["part"] for i in items]
        self.slug_of = {i["part"]: i.get("model_slug", "") for i in items}
        self.note_of = {i["part"]: i.get("note", "") for i in items}

        pk = cfg.get("pickup") or {}
        self.only_stores = [str(s).upper() for s in (pk.get("stores") or [])]

        ab = dict(cfg.get("autobuy") or {})
        ab.setdefault("region", cfg.get("region", "cn"))
        if not ab.get("enabled"):
            raise SystemExit("autobuy.enabled 是 false——买手没有事情可做")
        # 只拿它拼购买页地址，一个请求都不发
        self.urls = AppleClient(region=ab["region"], timeout=int(cfg.get("timeout", 15)))
        self.autobuy = AutoBuy(ab, root, log=log)
        self.bc = AsyncBroadcaster(Broadcaster.from_config(cfg, log=log))

        link = dict(cfg.get("link") or {})
        self.bus_id = str(link.get("id") or "").strip() or "buyer"
        self.offset = int(link.get("buyer_offset") or 0)
        self.port = int(link.get("port") or DEFAULT_PORT)
        self.allow = tuple(str(p) for p in (link.get("allow") or ()) if p)

        self.fresh = float(ab.get("stock_fresh_seconds", FRESH) or FRESH)
        self.blind_after = float(link.get("blind_after", BLIND_AFTER) or BLIND_AFTER)
        self.master_stale = float(link.get("master_stale", MASTER_STALE) or MASTER_STALE)

        self.lock = threading.Lock()
        #: 每个型号最后一次听到「有货」的本机单调时刻
        self.heard: dict[str, float] = {}
        #: 最后一次收到**任何**探针消息的时刻——用来分清「没货」和「我瞎了」
        self.any_signal = 0.0
        #: 最后一次收到主程序心跳的时刻，和它当时报的出口数
        self.last_alive, self.master_exits, self.master_id = 0.0, 0, ""
        #: 已经就失联报过警了，别每轮刷一遍
        self._warned_missing = False

        self.worker = PurchaseWorker(
            self.autobuy, self._report,
            max_age=ab.get("candidate_max_age", 90),
            max_attempts=ab.get("max_attempts_per_stock", 2),
            retry_delay=ab.get("retry_delay", 15),
            probe_url=self._buy_url(self.parts[0]) if self.parts else "",
            log=log)
        # 覆盖掉 worker 自带的那个（它是按本机轮询写的，而买手不轮询）
        self.autobuy.stock_live = self.stock_live
        self.receiver = None

    # ---------- 库存来自总线 ----------

    def _buy_url(self, part: str) -> str:
        slug = self.slug_of.get(part, "")
        return (self.urls.buy_url(slug, part) if slug
                else self.urls.base + "/shop/buy-iphone")

    def heard_alive(self, a: Alive, ip: str = "") -> None:
        """主程序的心跳。

        它同时是「我没瞎」的证据：没货的时候不会有 seen 消息，光看 seen 的话
        买手会在每一个安静的时段都以为自己失明。
        """
        now = time.monotonic()
        with self.lock:
            first = not self.last_alive
            back = self._warned_missing
            self.last_alive, self.master_exits = now, a.exits
            self.master_id = a.id
            self._warned_missing = False
        if first:
            self.log(f"[买手] 主程序 {a.id} 在线，{a.exits} 条出口")
        elif back:
            self.log(f"[买手] 主程序 {a.id} 回来了")
            self.bc.send("✅ 主程序恢复", f"{a.id} 又在发心跳了，{a.exits} 条出口",
                         "", critical=True)

    def on_bus(self, msg, ip: str = "") -> None:
        """总线上来的东西分流。"""
        if isinstance(msg, Alive):
            self.heard_alive(msg, ip)
        else:
            self.heard_of(msg, ip)

    def check_master(self) -> None:
        """主程序失联就叫醒人。

        按约定不做保险丝——买手不会自己去巡检兜底。所以这条报警是唯一的出路：
        主程序挂了而没人知道的话，看着一切正常，放货那一刻才发现根本没人在盯。
        """
        now = time.monotonic()
        with self.lock:
            last, warned, who = self.last_alive, self._warned_missing, self.master_id
            if not last or warned or now - last <= self.master_stale:
                return
            self._warned_missing = True
        gone = now - last
        self.log(f"[买手] ⚠️ 主程序 {who} 已经 {gone:.0f}s 没有心跳了")
        self.bc.send("⚠️ 主程序失联", 
                     f"{who} 已经 {gone:.0f} 秒没心跳。买手不会自己巡检，"
                     f"现在等于没人在盯库存——去看看那台机器。",
                     "", critical=True, wake=True)

    def heard_of(self, s: Sighting, ip: str = "") -> None:
        """探针说某个型号在某店有货。（ip 是包的源地址，买手用不上；主程序靠它认出子程序）

        **「探针还活着」要在过滤之前记。** 哪怕报的是我们不买的型号，它也证明
        了探针在说话——而那正是急刹敢不敢踩的前提。记在过滤之后的话，只盯一个
        子集的买手会一直以为自己瞎了，刹车永远不生效。
        """
        with self.lock:
            self.any_signal = time.monotonic()
        if s.part not in self.parts:
            return
        if self.only_stores and s.store not in self.only_stores:
            return
        observed = mono_of(s.at)
        with self.lock:
            self.heard[s.part] = max(self.heard.get(s.part, 0.0), observed)
        pri = ((self.parts.index(s.part) + self.offset) % max(1, len(self.parts)),
               len(self.only_stores or ()))
        self.worker.observe(s.part, [Offer(
            s.part, s.store, s.name or s.store, self._buy_url(s.part),
            self.note_of.get(s.part) or s.part, observed, pri)])

    def stock_live(self, part: str):
        """急刹的依据。True / False / None（None = 说不好，别刹）。

        三种状态必须分清，尤其是第三种——探针全挂时刹车等于自断手脚。
        """
        now = time.monotonic()
        with self.lock:
            last, any_signal = self.heard.get(part, 0.0), self.any_signal
            alive = self.last_alive
        if last and now - last <= self.fresh:
            return True
        # 「我还看得见」的证据，优先用主程序的心跳：没货的时候不会有 seen 消息，
        # 光看 seen 的话，每一个安静的时段都会被误判成失明。
        if alive and now - alive <= self.master_stale:
            return False         # 主程序在刷，只是没提这个型号
        if not any_signal or now - any_signal > self.blind_after:
            return None          # 谁都联系不上，这是失明，不是没货
        return False

    # ---------- 结果 ----------

    def _report(self, result, title: str, url: str) -> None:
        if result.ok and (result.order_id or result.order_created):
            pay = (self.cfg.get("autobuy") or {}).get("payment_method") or "扫码"
            self.bc.send(
                f"💳 去付款！{result.order_id or '待付款订单已创建'}",
                f"{title}\n{pay}，请尽快扫码支付。\n{result.detail}",
                result.url or url, critical=True, wake=True)
            return
        self.bc.send(f"{'✅' if result.ok else '⚠️'} 自动下单：{result.stage}",
                     result.detail or "详见终端", result.url or url,
                     critical=True,
                     wake=self.autobuy.order_placed or result.wake)

    # ---------- 跑 ----------

    def loop(self) -> None:
        key = bus_key()
        if not key:
            raise SystemExit(
                "买手不自己巡检，库存全靠总线——没有 HUNTER_BUS_KEY 就等于瞎子。"
                "跑 `python -m hunter2 key` 生成一个，所有部署共用同一个。")
        self.receiver = Receiver(key=key, on_sighting=self.on_bus, port=self.port,
                                 allow=self.allow, log=self.log,
                                 kinds=("seen", "alive"))
        self.receiver.start()
        self.worker.start()
        self.log(f"[买手] {self.bus_id} 就位：不巡检，只等探针的信号（Ctrl+C 停止）")
        try:
            while True:
                time.sleep(1)
                self.check_master()
                if self.worker.halted:
                    self.log("[买手] 已停止接单（买够了或有待核对的订单）")
                    break
        except KeyboardInterrupt:
            self.log("\n[买手] 收到停止信号")
        finally:
            self.receiver.close()
            self.worker.close()
            self.bc.close()
