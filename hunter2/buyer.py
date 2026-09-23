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

**库存以心跳里的快照为准，不以「有没有收到 seen」为准。** seen 是低延迟的那条
路——看到货立刻喊，买手立刻开火；但它走的是 UDP，丢了就是丢了。拿「这一轮没
收到 seen」当「没货了」，等于让一个丢包去撤销一批还有效的候选，然后掐掉下单。
所以心跳里带一份**完整快照**（`Alive.stock`），「还有没有货」由快照回答：

    快照里有它                    → 货还在，照常打（丢了的候选还能从快照补回来）
    主程序**看清了**，而快照里没它 → 确实没货，刹车
    没有新鲜的「看清了」           → **说不好**，绝不刹车

第三种是关键。**「主程序还活着」不等于「主程序看清了」**：查询失败、接口返回
UNKNOWN、所有出口都在 541 静默期里，进程都照样在发心跳。拿心跳当「确实没货」，
等于在接口抖动和被限流的时候掐掉自己的下单，而那恰恰是最该老实往下走的时候。
"""

from __future__ import annotations

import dataclasses
import threading
import time

from hunter.apple import AppleClient
from hunter.autobuy import AutoBuy, stores_of
from hunter.monitor import watch_items
from hunter.notify import AsyncBroadcaster, Broadcaster
from hunter.purchase_worker import Offer, PurchaseWorker

from .bus import DEFAULT_PORT, Alive, Receiver, Sighting, bus_key, mono_of
from .enlist import Enlister

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

        self.only_stores = stores_of(cfg)

        ab = dict(cfg.get("autobuy") or {})
        ab.setdefault("region", cfg.get("region", "cn"))
        ab["pickup_store_numbers"] = self.only_stores
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
        self.max_age = float(ab.get("candidate_max_age", 90) or 90)
        self.blind_after = float(link.get("blind_after", BLIND_AFTER) or BLIND_AFTER)
        self.master_stale = float(link.get("master_stale", MASTER_STALE) or MASTER_STALE)

        self.lock = threading.Lock()
        #: 每个型号最后一次听到「有货」的本机单调时刻（只用来算新鲜度）
        self.heard: dict[str, float] = {}
        #: 同上，但存的是**发送方自己的**墙上时刻。判先后只能用它：mono_of 每次
        #: 换算都拿当时的 monotonic/time 差去折算，两次调用之间的抖动会让同一个
        #: 时刻换出不同的结果——于是 clear > last 在两者本来相等时也会成立，
        #: 然后清空候选、急刹。本地回放 300 次，134 次误删了刚收到的候选。
        self.heard_at: dict[str, float] = {}
        #: 每个型号当前**所有**有货门店：{part: {store: Offer}}。
        #: 信号是一家一条来的，而 worker.observe 是整体替换——一条一调的话
        #: 只有最后一家活下来，另外几家一次都不会被试。
        self.live: dict[str, dict] = {}
        #: 最后一次收到**任何**总线消息的时刻。只用来打日志——它证明不了
        #: 「确实没货」，当过刹车依据是错的（见 stock_live）。
        self.any_signal = 0.0
        #: 最后一次收到主程序心跳的时刻，和它当时报的出口数
        self.last_alive, self.master_exits, self.master_id = 0.0, 0, ""
        #: 完整读数，**按 (型号, 门店) 存**：{(型号, 门店): (本机单调时刻, 墙上时刻)}。
        #: 门店那一维用 `"*"` 表示「这个来源盯附近全部，对哪家店都算数」。
        #:
        #: 按型号存一个是不够的：只查 R359 的探针发来的快照会给整个型号盖上
        #: 时间戳，之后一条稍早的 R581 有货消息就会被当成过时的丢掉——而那个
        #: 探针根本没查过 R581。
        self.clear: dict = {}
        #: 每条有货候选的**发送方墙上时刻**，按 (型号, 门店) 存。按型号存一个的
        #: 话，多批次查询里后一批的一条新 seen 会把整个型号挡住，另外几家门店
        #: 就再也对不齐了。
        self.seen_at: dict = {}
        #: 每个来源报的累计售罄次数，按 (来源, 型号) 存。**按型号存一个是错的**：
        #: 两个探针各报各的 P:5 和 P:2，交替到达时每一次都像「又卖空了一次」，
        #: 于是重试上限被反复清空。计数只在同一个来源内部才有可比性。
        self.gone_seen: dict = {}
        #: 每个来源最后一条被采纳的心跳的发出时刻。UDP 不保证顺序，5→4→5 这种
        #: 乱序会把基线改成 4，下一条正常心跳就成了「又卖空一次」的假信号。
        self.gone_at: dict = {}
        #: 已经就失联报过警了，别每轮刷一遍
        self._warned_missing = False
        #: 已经就「主程序在跑但一直没看清」提过一次了
        self._warned_dim = False
        #: 买手起来的时刻。**从没连上过主程序也要报警**：密钥不一致、网络不通、
        #: 主程序压根没启动——这几种情况下 last_alive 永远是 0，光看「多久没心跳」
        #: 的话可以一声不吭地空等一整天。
        self.started = time.monotonic()
        #: 起来多久还没听见主程序就报警。留一段宽限期：正常的启动顺序是主程序
        #: 先起，但两边差个几十秒是常事，那时候报警是噪音。
        self.first_wait = max(self.master_stale, 120.0)

        #: 守株待兔模式：不再收到信号才冷启动，而是常驻蹲在结账页反复打 search。
        #: 单账号一次只能蹲一个型号（购物袋是账号级的），所以蹲哪个由 buyer_offset
        #: 在启用型号里挑一个。放货信号只对所蹲的那个型号有意义。
        camp_cfg = dict(ab.get("camp") or {})
        self.camp_enabled = bool(camp_cfg.get("enabled"))
        self.camp_part = (self.parts[self.offset % len(self.parts)]
                          if self.parts else "")
        if self.camp_enabled:
            from hunter.camp_worker import CampWorker
            self.worker = CampWorker(
                self.autobuy, self._report,
                url=self._buy_url(self.camp_part),
                in_stock_numbers=self.only_stores,
                cadence=float(camp_cfg.get("cadence", 8)),
                idle_cadence=float(camp_cfg.get("idle_cadence", 120)),
                hot_seconds=float(camp_cfg.get("hot_seconds", 25)),
                session_seconds=float(camp_cfg.get("session_seconds", 1080)),
                log=log)
            self.log(f"[买手] 守株待兔模式：蹲 {self.note_of.get(self.camp_part) or self.camp_part}"
                     f"（{self.camp_part}），信号来了立刻打 search")
        else:
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
        #: 买手同时是主程序的一条出口：它开一个转发口，把自己的出口 IP 借出去。
        #: 每加一个买手就同时多一个账号和一个出口 IP——两种稀缺资源一起涨。
        self.enlister = Enlister(cfg, self.bus_id, log=log)

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
            # saw_at 是墙上时钟，换算成本机的单调时刻只为算新鲜度；判先后用的
            # 是墙上时刻本身（见 heard_at 的注释）。0 = 这一轮没看清，那就保留
            # 上一次的，别让它倒退。
            resets = self._note_gone(a)
            updates = self._reconcile(a) if a.saw_at else []
        for part, stores in resets:
            # **只重置重试状态，不动候选。** 用 observe(part, []) 来做的话，
            # 一条没带快照的降级心跳会把候选清空，而后面没有任何东西来恢复
            # 它们——急刹说有货，队列却拿不出候选，买手就那么站着。
            self.worker.restock(part)
            self.log(f"[买手] {part} 在 {'、'.join(stores)} 卖空过一轮，"
                     f"重试次数和判死标记清零")
        for part, offers, gone in updates:
            # 必须在锁外面调：observe 要拿 worker 自己的锁。
            # 上面那圈重置已经先跑过了——顺序反了的话，刚补回来的候选会被紧接着
            # 的那条「明确无货」抹掉。
            if offers or gone:
                self.worker.observe(part, offers, unavailable=gone)
        if back:
            # 报过警就得有个下文，哪怕这是**第一次**连上——「连不上主程序」那条
            # 警报正是在从没连上的情况下发的，只在「曾经连上过」时才报恢复的话，
            # 它会一直挂在那儿没有结果。
            what = "恢复" if first is False else "连上了"
            self.log(f"[买手] 主程序 {a.id} {what}")
            self.bc.send(f"✅ 主程序{what}", f"{a.id} 在发心跳了，{a.exits} 条出口",
                         "", critical=True)
        elif first:
            self.log(f"[买手] 主程序 {a.id} 在线，{a.exits} 条出口")

    ANY = "*"

    def _clear_of(self, part: str, store: str) -> tuple:
        """这家门店最近一次被看清是什么时候。调用方必须已经持有 self.lock。"""
        a = self.clear.get((part, store)) or (0.0, 0.0)
        b = self.clear.get((part, self.ANY)) or (0.0, 0.0)
        return a if a[1] >= b[1] else b

    def _note_gone(self, a: Alive) -> list:
        """记下每个来源报的售罄计数，返回需要重置的 (型号, 门店)。

        **跟快照分开做。** 启动那条心跳带着 `P:0` 但没有 saw_at，挂在快照处理
        里的话基线根本记不下来；等真的「有货→售罄→补货」发生完，收到 P:1 时
        又会被当成第一次接触，于是判死状态解不开。

        重置只作用在**这个来源盯的门店**上：只监控 R359 的探针报售罄，跟我们
        在 R581 的失败次数没有任何关系。
        """
        src = a.src or a.id
        if a.at < self.gone_at.get(src, 0.0):
            return []                 # 迟到的旧包，计数不能倒着走
        self.gone_at[src] = a.at
        shops = set(a.stores)
        out = []
        for x in a.gone:
            part, _, n = x.rpartition(":")
            if not n.isdigit() or part not in self.parts:
                continue
            cur, key = int(n), (src, part)
            was = self.gone_seen.get(key)
            self.gone_seen[key] = cur
            if was is None:
                continue          # 第一次听它说话，只记基线
            if cur < was:
                # 消息比上一条新（上面查过了）却报了更小的数：对端重启了，
                # 重新取基线，不当成「又卖空一次」
                continue
            if cur == was:
                continue
            mine = set(self.live.get(part) or ())
            hit = sorted(mine & shops) if shops else sorted(mine)
            if hit or not mine:
                out.append((part, hit or [part]))
        return out

    def _reconcile(self, a: Alive) -> list:
        """拿这一轮的完整快照对齐候选。调用方必须已经持有 self.lock。

        两件事一起做，因为它们是同一件事的两面：

        * **补回来。** 快照里有、而我们手上没有的，是 seen 包丢了。UDP 不保证
          送达，丢一个包不该让我们错过一次放货——从快照直接把候选建出来。
        * **撤下去。** 快照里没有、而我们手上有的，才是真的卖完了。这一步非做
          不可：重试次数和「判死」标记都靠 `PurchaseWorker.observe` 那条「明确
          无货」分支来清，不回灌的话，同一家店二次补货时型号还停在上一轮的判死
          状态里——`stock_live` 已经是 True，`_next()` 却一直返回 None。

        判据只用快照，不用「这一轮有没有收到 seen」：后者是个丢包就会翻转的
        东西，拿它撤销候选等于让网络抖动去掐自己的单。
        """
        at = mono_of(a.saw_at)
        want: dict[str, set] = {}
        for x in a.stock:
            part, _, tail = x.partition(":")
            if part not in self.parts:
                continue
            for store in tail.split(","):
                if store and (not self.only_stores or store in self.only_stores):
                    want.setdefault(part, set()).add(store)

        # **只碰发送方盯过的型号和门店。** 它没查过的，这份快照什么都没说——
        # 两个探针盯同一个型号、不同门店时，少了门店这一半它们会互相撤货。
        scope = {p for p in a.parts if p in self.parts}
        shops = set(a.stores)
        out = []
        for part in (scope or set(want)) | (set(want) & set(self.live)):
            # **过时与否要一家一家判。** 一刀切的话，一条覆盖 R359+R581 的旧
            # 快照会因为「R581 还没人查过」而整条被放行，于是它顺带把已经售罄
            # 的 R359 又加回来。
            def fresh_for(st, _p=part, _s=a.saw_at):
                return _s >= self._clear_of(_p, st)[1]

            covers = sorted(shops) if shops else [self.ANY]
            if not any(fresh_for(c) for c in covers):
                continue                      # 每一家都有更新的读数了，整条过时
            for c in covers:
                if fresh_for(c):
                    self.clear[(part, c)] = (at, a.saw_at)
            cur = self.live.setdefault(part, {})
            # 只收那些「这条消息确实比我们已知的更新」的门店
            keep = {st for st in want.get(part, set())
                    if fresh_for(st if shops else self.ANY) and fresh_for(st)}
            # **按门店比时刻，不按型号。** 一条新 seen 只能保住它那家门店；
            # 按型号挡的话，多批次查询里后一批的一条新 seen 会让整个型号跳过
            # 对齐，另外几家门店就再也校不准了。
            gone, stale = [], []
            for store in sorted(cur):
                if store in keep:
                    continue
                if shops and store not in shops:
                    stale.append(store)       # 这家不在它的范围里，它没查过
                elif self.seen_at.get((part, store), 0.0) > a.saw_at:
                    stale.append(store)       # 这家有更新的 seen，快照管不了它
                elif not fresh_for(store):
                    stale.append(store)       # 这家已经有更新的读数了
                else:
                    gone.append(store)
            for store in gone:
                del cur[store]
                self.seen_at.pop((part, store), None)
            added = sorted(keep - set(cur))
            # **每一轮快照都要把留下来的候选也刷新一遍。** 只给新门店建 Offer
            # 的话，门店集合不变时观察时刻就一直停在第一次——过了
            # candidate_max_age（默认 90 秒）worker 那边的候选全部过期，
            # `_next()` 返回 None，而 stock_live 还在说 True：货一直有，重试却
            # 停了，而且没有任何一条日志会提这件事。
            for store in keep:
                old = cur.get(store)
                # **只往前，不往后。** 刚收到的 seen 可能比这份快照还新（一轮
                # 拆成几批时就是这样）；无条件盖回去的话，候选会被改成九十秒前
                # 的观察，队列判它过期而急刹还说有货——买不了也停不下来。
                when = max(at, old.observed) if old else at
                cur[store] = self._offer(part, store,
                                         old.name if old else store, when)
                self.seen_at[(part, store)] = max(
                    self.seen_at.get((part, store), 0.0), a.saw_at)
            if keep:
                self.heard[part] = max(self.heard.get(part, 0.0), at)
                self.heard_at[part] = max(self.heard_at.get(part, 0.0), a.saw_at)
            if added:
                self.log(f"[买手] {part} 在 {'、'.join(added)} 有货"
                         f"（从快照补的，seen 包没到）")
            if not gone and not keep:
                continue
            offers = sorted(cur.values(), key=lambda o: (o.priority, o.store))
            out.append((part, offers, gone))
        return out

    def _offer(self, part: str, store: str, name: str, observed: float) -> Offer:
        # 门店的名次用**它在配置里的下标**。全给同一个数的话，后面那层排序只能
        # 按编号字典序来——配了 [R581, R359] 结果先打 R359，而人写的顺序就是
        # 「近的排前面」。
        rank = (self.only_stores.index(store)
                if store in self.only_stores else len(self.only_stores or ()))
        pri = ((self.parts.index(part) + self.offset) % max(1, len(self.parts)),
               rank)
        return Offer(part, store, name or store, self._buy_url(part),
                     self.note_of.get(part) or part, observed, pri)

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
            if warned:
                return
            if not last:
                # **从没连上过也要报。** 密钥不一致、网络不通、主程序压根没起来，
                # 这几种情况下「多久没心跳」永远算不出来，光看它就会一声不吭地
                # 空等一整天。宽限期是给正常的启动顺序留的。
                if now - self.started <= self.first_wait:
                    return
                self._warned_missing = True
                waited = now - self.started
                self.log(f"[买手] ⚠️ 起来 {waited:.0f}s 了，一条主程序心跳都没收到")
                self.bc.send(
                    "⚠️ 连不上主程序",
                    f"买手起来 {waited:.0f} 秒，一条心跳都没收到。买手不会自己"
                    f"巡检，现在等于没人在盯库存。检查：主程序起了没、"
                    f"HUNTER_BUS_KEY 两边一不一样、link.port 通不通。",
                    "", critical=True, wake=True)
                return
            if now - last <= self.master_stale:
                return
            self._warned_missing = True
        gone = now - last
        self.log(f"[买手] ⚠️ 主程序 {who} 已经 {gone:.0f}s 没有心跳了")
        self.bc.send("⚠️ 主程序失联", 
                     f"{who} 已经 {gone:.0f} 秒没心跳。买手不会自己巡检，"
                     f"现在等于没人在盯库存——去看看那台机器。",
                     "", critical=True, wake=True)

    def check_sight(self) -> None:
        """主程序在跑，但很久没「看清」过一轮——出口全卡在熔断里，或者接口在抖。

        这不报警（它不一定是故障，而且急刹本来就会因此放行），但要在日志里说出来：
        否则这段时间看着完全正常，而实际上没有任何一个型号有确定的读数。
        """
        now = time.monotonic()
        with self.lock:
            alive, warned = self.last_alive, self._warned_dim
            clear = max((c for c, _ in self.clear.values()), default=0.0)
            if not alive or now - alive > self.master_stale:
                return                      # 那是失联，另一条路管
            dim = not clear or now - clear > self.blind_after
            self._warned_dim = dim
        if dim and not warned:
            self.log(f"[买手] 主程序还在发心跳，但已经 "
                     f"{(now - clear) if clear else now:.0f}s 没有一轮完整的读数了"
                     f"（出口都在静默期？接口在抖？）——急刹这段时间一律放行")
        elif warned and not dim:
            self.log("[买手] 主程序的读数恢复完整了")

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
        offer = self._offer(s.part, s.store, s.name, observed)
        with self.lock:
            # **迟到的旧消息不能复活已经撤销的候选。** 新快照已经明确说过这家
            # 门店没货了，晚几秒才到的 seen 描述的是更早的时刻——放它进来，
            # 急刹判「无货」而队列里偏偏排着它，买手会去打一个明知没有的门店。
            ceiling = self._clear_of(s.part, s.store)[1]
            if ceiling and s.at <= ceiling and s.store not in (
                    self.live.get(s.part) or {}):
                self.log(f"[买手] 忽略迟到的 {s.part}@{s.store}"
                         f"（比最近一轮完整读数还早 {ceiling - s.at:.1f}s）")
                return
            self.heard[s.part] = max(self.heard.get(s.part, 0.0), observed)
            self.heard_at[s.part] = max(self.heard_at.get(s.part, 0.0), s.at)
            self.seen_at[(s.part, s.store)] = max(
                self.seen_at.get((s.part, s.store), 0.0), s.at)
            # **攒起来再一次性交。** observe 是整体替换某个型号的候选，一家一调
            # 等于后一家把前一家挤掉——四家同时放货时只会试最后那一家。
            cur = self.live.setdefault(s.part, {})
            old = cur.get(s.store)
            if old is None or offer.observed >= old.observed:
                cur[s.store] = offer
            elif old.name == old.store and offer.name != offer.store:
                # 旧的那条是从快照补的，只有编号没有店名。时刻上它更新，留着；
                # 但店名要换过来——通知里写「R581」而不是「五角场」，人得自己
                # 去查那是哪家店。
                cur[s.store] = dataclasses.replace(old, name=offer.name)
            cutoff = time.monotonic() - self.max_age
            for store, o in list(cur.items()):
                if o.observed < cutoff:
                    del cur[store]
            batch = sorted(cur.values(), key=lambda o: (o.priority, o.store))
        self.worker.observe(s.part, batch)

    def stock_live(self, part: str):
        """急刹的依据。True / False / None（None = 说不好，别刹）。

        **只有「刚刚看清了、而且没提这个型号」才是没货。** 「主程序还活着」不算：
        查询失败、接口返回 UNKNOWN、所有出口都在 541 静默期里，进程照样在发心跳，
        而那几种情况下刹车会掐掉一次真实的下单。所以这里认的是心跳里的 `saw_at`，
        不是心跳本身。

        **比的是时序，不只是新鲜度。** 两个时刻都在同一个时钟域里：完整读数比
        有货观察新，才说明「后来查过、没了」；反过来有货观察更新的话，那条读数
        管不了它。只比各自新不新鲜的话，一轮拆成几批、每批隔十几秒时，两个时刻
        会一起变旧，而先失效的是有货观察——于是刹车踩在一个从没被查成无货的
        型号上。

        判不准的时候一律放行（返回 None）。方向是有意选的：刹错的代价是丢掉一台，
        不刹错的代价只是多花十几秒和几个请求。
        """
        now = time.monotonic()
        with self.lock:
            last_at = self.heard_at.get(part, 0.0)
            # 候选就是过滤过的（只含我们买的型号和去得了的门店），直接看它，
            # 急刹和候选就不可能再各判各的
            in_snapshot = bool(self.live.get(part))
            # **要我们去得了的每一家都被看清过**，取其中最弱的那条。只查了
            # 一家的探针说「没货」，不能代表另一家。
            need = self.only_stores or [self.ANY]
            clear, clear_at = min((self._clear_of(part, x) for x in need),
                                  key=lambda x: x[1])
            last = self.heard.get(part, 0.0)
        if clear_at and now - clear <= self.fresh:
            # 有新鲜的完整快照：它说了算，不管 seen 包丢没丢
            if in_snapshot:
                return True
            if clear_at >= last_at:
                return False     # 有货之后又完整查过一轮，快照里没有它
        if last and now - last <= self.fresh:
            return True
        return None              # 没有足够新鲜的结论，说不好，别刹

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
        # me= 挡掉自己报到广播的回声：UDP 广播会回到本机，而买手不收 enlist，
        # 不挡的话日志里每 20 秒被自己拒绝一次。
        self.receiver = Receiver(key=key, on_sighting=self.on_bus, port=self.port,
                                 allow=self.allow, log=self.log,
                                 kinds=("seen", "alive"), me=self.bus_id)
        self.receiver.start()
        self.enlister.start()
        self.worker.start()
        self.log(f"[买手] {self.bus_id} 就位：不巡检，只等主程序的信号"
                 f"（{self.enlister.describe()}，Ctrl+C 停止）")
        try:
            while True:
                time.sleep(1)
                self.check_master()
                self.check_sight()
                if self.worker.halted:
                    self.log("[买手] 已停止接单（买够了或有待核对的订单）")
                    break
        except KeyboardInterrupt:
            self.log("\n[买手] 收到停止信号")
        finally:
            self.enlister.close()
            self.receiver.close()
            self.worker.close()
            self.bc.close()
