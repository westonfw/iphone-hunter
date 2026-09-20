"""主程序的主循环：轮转所有出口刷库存，看到货就广播。

一轮 = 挑一条到点的出口 → 用它打一遍 pickup-message → 把看到的货喊出去。
挑哪条完全由 `ExitPool` 决定，所以这里没有任何相位协调的代码——那正是把 N 个
出口收进一个进程换来的东西。

三件事必须每轮都做：

1. **看到货就广播，每轮都广播**，不只在状态翻转时广播。买手拿这个当心跳：
   一直收到 = 货还在，收不到了 = 没了。它自己不轮询，这是它唯一的库存来源。
2. **发心跳**（`Alive`）。没货的时候本来就没有 seen 消息，不发心跳的话买手
   分不清「主程序死了」和「只是没放货」。
3. **收报到**（`Enlist`）。子程序上线就多一条出口，掉线就摘掉。

主程序是单点。按约定**不做保险丝**——买手不会自己巡检兜底，挂了就靠那条心跳
断掉来叫人。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from hunter.apple import (MAX_PARTS_PER_QUERY, PICKUP_PATH, Blocked, CoolingDown,
                          Stock)
from hunter.autobuy import stores_of
from hunter.monitor import State, now, watch_items
from hunter.notify import AsyncBroadcaster, Broadcaster
from hunter2.bus import (DEFAULT_PORT, Alive, Enlist, Receiver, Sender,
                         Sighting, bus_key, wall_of)

from .exits import ExitPool

#: 没有出口到点时，主循环的空转粒度。比它更细没有意义（一轮请求本身就要几百毫秒），
#: 比它更粗会把冲刺时的 4 秒间隔磨钝。
TICK = 0.25

#: 一条出口连不上（超时、DNS、代理挂了）之后先晾多久。
#:
#: 这个值存在的理由是**别让一条坏出口拖住好出口**：一轮拆成六批，每批连接超时
#: 15 秒，就是 90 秒里一个请求都没发出去，而调度器要等整轮结束才能换人。所以
#: 网络故障一出现就立刻中止这一轮，把这条出口晾一会儿，让别的出口顶上。
FAIL_COOLDOWN = 30.0


def _p(*a) -> None:
    print(*a, flush=True)


class Scout:
    """一个进程，N 条出口，只盯不买。"""

    def __init__(self, cfg: dict, root: Path, sprint: bool = False, log=_p):
        self.cfg, self.log = cfg, log
        items = watch_items(cfg)
        if not items:
            raise SystemExit("config.json 里没有启用的监控条目，主程序不知道该盯什么")
        self.parts = [i["part"] for i in items]
        self.slug_of = {i["part"]: i.get("model_slug", "") for i in items}
        self.note_of = {i["part"]: i.get("note", "") for i in items}
        # 保留 request_group：同一组的型号可以塞进一个请求，分组是为了把
        # 「一次查太多会被拒」的那些拆开。
        self.groups: dict[str, list[str]] = {}
        for it in items:
            self.groups.setdefault(it.get("request_group") or "default", []).append(
                it["part"])

        pk = cfg.get("pickup") or {}
        self.location = str(pk.get("location", "")).strip()
        if not self.location:
            raise SystemExit("主程序只盯门店取货，config.pickup.location 必须填邮编")
        # 盯的就是买的：放货每家独立，盯一家不打算买的店只是白烧预算
        self.only_stores = stores_of(cfg)

        link = dict(cfg.get("link") or {})
        self.bus_id = str(link.get("id") or "").strip() or "scout"
        self.port = int(link.get("port") or DEFAULT_PORT)
        peers = tuple(str(p) for p in (link.get("peers") or ()) if p)
        allow = tuple(str(p) for p in (link.get("allow") or ()) if p)

        key = bus_key()
        if not key:
            raise SystemExit(
                "主程序的产出就是广播出去的库存信号，没有 HUNTER_BUS_KEY 等于白跑。"
                "跑 `python -m hunter2 key` 生成一个，所有部署共用同一个。")
        self.sender = Sender(key=key, src=self.bus_id, port=self.port,
                             peers=peers, log=log)
        # 只收 enlist：主程序不需要别人告诉它有货——它自己就是眼睛。
        # me= 挡掉自己广播回来的包（UDP 广播会回到本机）。
        self.receiver = Receiver(key=key, on_sighting=self.on_bus, port=self.port,
                                 allow=allow, log=log, kinds=("enlist",),
                                 me=self.bus_id)
        self.pool = ExitPool(cfg, log=log, sprint=sprint)
        self.bc = AsyncBroadcaster(Broadcaster.from_config(cfg, log=log))
        # **不跟 watch 共用 state.json。** 同一个目录里两个程序写同一份状态，
        # 是一条不会报错的互相覆盖路径：一边刚记下「R359 有货」，另一边一轮
        # 无货就把它抹了，于是货回来时谁都不再推送。
        self.state = State(root / "scout-state.json")
        self.round_no = 0
        self.shouted = 0
        self.last_beat = 0.0
        #: 最后一轮「所有型号都拿到确定结论」的墙上时刻。买手的急刹只认这个，
        #: 所以宁可保守：这一轮但凡有一个型号没读准，就不往前推。
        #: 最近一轮完整读数：`(墙上时刻, {(型号, 门店)})`。
        #:
        #: **两样东西必须一起换掉。** 心跳跑在另一个线程上，分两次赋值的话它可能
        #: 正好读到「新时刻 + 上一轮的快照」——那个组合从来没有存在过，而买手会
        #: 照着它撤销刚收到的候选。整体换一个不可变的元组，读写各是一次属性操作，
        #: 中间插不进去。
        self.reading: tuple = (0.0, frozenset())
        #: 每个型号累计卖空过几次。心跳十秒一条，而「有货→没货→又有货」可能
        #: 整个发生在两条心跳之间——中间那份「没货」的快照会被后一份盖掉，买手
        #: 就永远等不到那条「明确无货」，重试次数和判死标记清不掉，货明明有却
        #: 不再重试。计数是累计的，丢几条包也能靠差值看出来中间卖空过。
        self.gone: dict = {}
        self.reqs = 0
        #: 广播可能来自巡检线程和心跳线程两处，sendto 本身安全，开 socket 不是
        self._out = threading.Lock()
        self._beating = None
        self._stop = threading.Event()
        #: 心跳间隔。轮询间隔可能被退避拉长到几分钟，心跳不能跟着一起稀——
        #: 那样买手会在一次正常的退避里误判主程序死了。
        self.beat_every = max(2.0, float(link.get("beat_every", 10) or 10))
        #: 全部出口都被封着的起点，0 = 现在不是。持续超过 blind_after 就报出来。
        #: 「零失明」是这套结构的承诺；承诺兑现不了的时候不能悄悄的。
        self._blind_since = 0.0
        self._warned_blind = False
        self.blind_after = max(10.0, float(link.get("blind_after", 45) or 45))

    # ---------- 子程序报到 ----------

    def on_bus(self, msg, ip: str = "") -> None:
        """总线上唯一会进来的东西：子程序的报到。

        **IP 取的是 UDP 包的源地址**，不是消息里写的——子程序对自己内网地址的
        猜测在多网卡 / 容器 / WSL 下经常是错的。
        """
        if isinstance(msg, Enlist):
            self.pool.enlist(msg.id, ip, msg.proxy_port, msg.direct)

    # ---------- 一轮 ----------

    def poll(self, e) -> tuple[bool, float | None]:
        """用一条出口刷一遍库存。返回 (有没有真打成, 被拦了要退避多久)。

        每一批结果立刻处理：后面的分组失败，不能把前面已经发现的货丢掉。

        顺带记两件事：这一轮真的发了几个请求（`self.reqs`，给预算记账用），
        以及**是不是把所有型号都读准了**（`self.saw_at`，买手的急刹只认它）。
        """
        ok = False
        self.reqs = 0
        clear, live = set(), set()
        oldest = 0.0
        for parts in self.groups.values():
            for start in range(0, len(parts), MAX_PARTS_PER_QUERY):
                batch = parts[start:start + MAX_PARTS_PER_QUERY]
                try:
                    self.reqs += 1
                    got = e.client.pickup(batch, location=self.location)
                except CoolingDown as ex:
                    self.reqs -= 1           # 静默期里一个包都没发
                    self.log(f"[{now()}] {e.id}：{ex}")
                    return ok, None          # 这条在静默期，换下一条
                except Blocked as ex:
                    self.log(f"[{now()}] {e.id} 被拦：{ex}")
                    return ok, float(getattr(ex, "retry_after", 0.0) or 0.0)
                except Exception as ex:
                    # **立刻中止这一轮。** 接着用同一条坏出口打剩下几批，等于让
                    # 每批各超时一次；调度器要等整轮结束才能换人，六批就是 90 秒
                    # 一个请求都发不出去，而心跳照常、看着一切正常。
                    self.log(f"[{now()}] {e.id} 门店查询失败：{type(ex).__name__}: {ex}"
                             f"，这一轮停在这儿，先晾它 {FAIL_COOLDOWN:.0f}s")
                    return ok, FAIL_COOLDOWN
                at = wall_of(getattr(e.client, "observed_at", {}).get(
                    PICKUP_PATH, time.monotonic()))
                oldest = at if not oldest else min(oldest, at)
                ok = True
                for part in batch:
                    good, ready = self._saw(part, got.get(part) or [], e)
                    live |= {(part, s.store_number.upper()) for s in ready}
                    if good:
                        clear.add(part)
        if clear >= set(self.parts):
            # 只有**整轮都读准了**才敢说「我看清了」。少读准一个型号就不推进：
            # 买手会拿这个时刻当「确实没货」去掐掉下单，宁可让它放行。
            #
            # 时刻取**最早那一批**，不是「现在」。一轮拆成几批、每批隔十几秒的话，
            # 用轮末时间等于把三十秒前的读数包装成「刚刚看清的」——买手会拿它去
            # 盖掉一条同样是三十秒前的有货观察，然后中止一次根本没被查成无货的
            # 下单。取最早那批就自动保守：整轮太慢时这个时刻自己就过期了，
            # 买手判不准，放行。
            if oldest >= self.saw_at:
                had = {p for p, _ in self.stock}
                for part in had - {p for p, _ in live}:
                    self.gone[part] = self.gone.get(part, 0) + 1
                self.reading = (oldest, frozenset(live))
        return ok, None

    @property
    def saw_at(self) -> float:
        return self.reading[0]

    @property
    def stock(self) -> frozenset:
        return self.reading[1]

    def _saw(self, part: str, stores, e) -> bool:
        """一个型号的门店结果：先广播，再决定要不要推送给人。

        顺序是有意的——广播先走，推送那一路要访问网络、要读写状态文件，买手
        等不起。

        返回 (这个型号读准了没有, 有货的门店)。读不准的几种情况一律返回 False，
        它们会让整轮的 `saw_at` 不推进——**那正是急刹不该生效的时候**。
        """
        label = self.note_of.get(part) or part
        if self.only_stores:
            stores = [s for s in stores if s.store_number.upper() in self.only_stores]
            if not stores:
                self.log(f"[{now()}] {label}: 指定门店没出现在结果里，检查 pickup.stores")
                return False, []
        if not stores:
            self.log(f"[{now()}] {label}: 门店结果为空，保留上次状态")
            return False, []

        unknown = [s for s in stores if s.state is Stock.UNKNOWN]
        if unknown and len(unknown) == len(stores):
            # 全未知绝不当成无货：那会让买手以为货没了而踩急刹
            self.log(f"[{now()}] {label}: 门店库存未知 — {unknown[0].reason}")
            return False, []

        ready = [s for s in stores if s.state is Stock.AVAILABLE]
        if ready:
            # 用接口真正返回的那一刻，不是现在——中间还隔着解析和这一圈循环
            observed = getattr(e.client, "observed_at", {}).get(
                PICKUP_PATH, time.monotonic())
            self._shout(part, ready, wall_of(observed))
            # 看到货：**所有**出口一起冲刺。第一单没抢到时，后面几分钟最值钱。
            self.pool.boost()
            names = "、".join(s.store_name or s.store_number for s in ready[:6])
            more = f" 等 {len(ready)} 家" if len(ready) > 6 else ""
            self.log(f"[{now()}] {label}: 🏬 {names}{more} 可取货（{e.id}）")
        else:
            self.log(f"[{now()}] {label}: {len(stores)} 家门店均无货"
                     + (f"（另有 {len(unknown)} 家状态未知）" if unknown else "")
                     + f"（{e.id}）")

        self._tell_human(part, label, stores, ready)
        # 有未知的门店就不算读准：那几家可能正好是放货的那几家。
        # **配置的门店少回来一家也不算。** 接口只回了 R581、没回 R359 的话，
        # R359 的状态是「不知道」而不是「没货」——算作读准的话，上一轮在 R359
        # 看到的货会被这一轮撤掉，而它可能还好好地在那儿。
        seen = {s.store_number.upper() for s in stores
                if s.state is not Stock.UNKNOWN}
        missing = [x for x in self.only_stores if x not in seen]
        if missing:
            self.log(f"[{now()}] {label}: {'、'.join(missing)} 这一轮没返回，"
                     f"当作未知（不推进「看清了」）")
        return (not unknown and not missing), ready

    def _shout(self, part: str, ready, at: float) -> None:
        for s in ready:
            try:
                with self._out:
                    self.shouted += self.sender.send(Sighting(
                        part=part, store=s.store_number.upper(),
                        name=s.store_name or s.store_number, at=at,
                        src=self.bus_id))
            except Exception as ex:
                self.log(f"[主程序] 广播失败：{type(ex).__name__}: {ex}")

    def _tell_human(self, part: str, label: str, stores, ready) -> None:
        """变化才推送。每轮都推的话，一次补货能把人手机刷爆。"""
        prev = self.state.get(f"pickup:{part}")
        first_run = prev is None
        prev = prev or []
        # 这一轮没看到的门店保留上次状态：结果里缺了一家不等于那家没货了
        observed = {s.store_number for s in stores if s.state is not Stock.UNKNOWN}
        cur = sorted({s.store_number for s in ready} | (set(prev) - observed))
        if cur == prev:
            return
        fresh = [s for s in ready if s.store_number not in prev]
        if fresh:
            lines = [f"{s.store_name}（{s.city}）{s.quote}" for s in fresh[:8]]
            # 首轮就有货也要说，但要标清楚是「启动时就有」而不是「刚刚放货」
            head = "🏬 启动时已有货" if first_run else "🚨 刚放货"
            self.bc.send(f"{head}：{label}", "\n".join(lines),
                         self._buy_url(part), critical=True)
        self.state.set(f"pickup:{part}", cur)

    def _buy_url(self, part: str) -> str:
        client = next(iter(self.pool._exits.values())).client
        slug = self.slug_of.get(part, "")
        return client.buy_url(slug, part) if slug else client.base + "/shop/buy-iphone"

    # ---------- 全盲 ----------

    def check_blind(self) -> None:
        """所有出口都在熔断静默里，而且持续了一阵子：说出来。

        2026-09-20 23:32 起两条出口轮流被 541 封，到停机 20 分钟一轮没打成，
        期间还真放了一次货。日志里只有零散的「被拦」，没有一行把这个状态点破——
        人看着心跳正常、买手也不报警，以为一切都好。
        """
        left = self.pool.all_blocked(PICKUP_PATH)
        now = time.monotonic()
        if left <= 0:
            if self._warned_blind:
                self.log("[主程序] ✅ 出口恢复了，重新在盯")
                self.bc.send("✅ 主程序恢复巡检", "至少一条出口已经解封", "",
                             critical=True)
            self._blind_since, self._warned_blind = 0.0, False
            return
        if not self._blind_since:
            self._blind_since = now
            return
        if self._warned_blind or now - self._blind_since < self.blind_after:
            return
        self._warned_blind = True
        n = len(self.pool)
        self.log(f"[主程序] ⚠️ {n} 条出口全在熔断静默里，已经 "
                 f"{now - self._blind_since:.0f}s 没打成一轮，最早解封还要 {left:.0f}s")
        self.bc.send("⚠️ 主程序全盲",
                     f"{n} 条出口全被 541 封着，{now - self._blind_since:.0f} 秒没打成"
                     f"一轮。这段时间放货看不见。最早解封还要 {left:.0f} 秒。",
                     "", critical=True)

    # ---------- 心跳 ----------

    def beat(self, force: bool = False) -> None:
        """告诉买手「我还在」，外加「我最后一次看清是什么时候」。

        **跟巡检彻底解耦，跑在自己的线程上。** 一轮巡检可能拆成好几批请求，
        任何一批卡在超时上都会把整轮拖到几十秒；心跳要是搭在巡检线程上，一次
        慢查询就能让买手以为主程序死了，然后白白叫醒人。

        `saw_at` 跟 `at` 是两件事：前者说「我看清了」，后者只说「我还活着」。
        买手的急刹只能踩在前者上（见 hunter2.buyer.stock_live）。
        """
        t = time.monotonic()
        if not force and t - self.last_beat < self.beat_every:
            return
        self.last_beat = t
        # 消息在 try 外面造：包在里面的话，少一个字段这种错会被当成「发不出去」
        # 吞掉，而心跳看着一直在发
        # 一次读出来，别分两次取——分开取就又回到了「新时刻配旧快照」
        saw_at, stock = self.reading
        by_part = {}
        for part, st in stock:
            by_part.setdefault(part, []).append(st)
        snap = tuple(f"{k}:{','.join(sorted(v))}" for k, v in sorted(by_part.items()))
        scope = tuple(self.parts)
        # **每个型号都要给一个数，哪怕是 0。** 只发卖空过的那些，买手第一次
        # 收到 P:1 时没有基线可比，就不知道那是「刚卖空」还是「本来就是 1」，
        # 于是第一次「售罄→补货」永远恢复不了重试。
        gone = tuple(f"{p}:{self.gone.get(p, 0)}" for p in self.parts)
        msg = Alive(id=self.bus_id, at=time.time(), exits=len(self.pool),
                    round_no=self.round_no, saw_at=saw_at, stock=snap,
                    parts=scope, stores=tuple(self.only_stores), gone=gone,
                    src=self.bus_id)
        small = self.sender.shrink(msg)
        if small is not msg:
            self.log(f"[主程序] 心跳装不进一个 UDP 包（{len(scope)} 个型号），"
                     f"已降级：{'不带快照' if not small.saw_at else ''}"
                     f"{'、不带售罄计数' if not small.gone else ''}")
        msg = small
        try:
            with self._out:
                self.sender.send(msg)
        except OSError as ex:
            self.log(f"[主程序] 心跳发不出去：{type(ex).__name__}: {ex}")

    def _beat_loop(self) -> None:
        while not self._stop.is_set():
            self.beat()
            # 用 Event.wait 而不是 sleep：停的时候立刻就能退出来
            self._stop.wait(min(self.beat_every, 1.0))

    # ---------- 跑 ----------

    def loop(self) -> None:
        self.receiver.start()
        self.beat(force=True)
        self._beating = threading.Thread(target=self._beat_loop, name="beat",
                                         daemon=True)
        self._beating.start()
        self.log(f"[主程序] {self.bus_id} 就位：{len(self.parts)} 个型号，"
                 f"出口 {self.pool.describe()}，端口 {self.port}（Ctrl+C 停止）")
        try:
            while True:
                e = self.pool.pick(PICKUP_PATH)
                if e is None:
                    # 所有出口要么没到点、要么在熔断静默里。等待发生在这儿，
                    # 不在请求里——预算和退避都体现为 due_at，谁也不挡着谁。
                    self.check_blind()
                    time.sleep(min(max(self.pool.soonest(PICKUP_PATH), 0.0), TICK))
                    continue
                self.check_blind()
                self.round_no += 1
                ok, retry_after = self.poll(e)
                if retry_after is not None:
                    self.pool.blocked(e, retry_after, self.reqs)
                else:
                    if ok:
                        self.pool.ok(e)
                    self.pool.done(e, self.reqs)
        except KeyboardInterrupt:
            self.log(f"\n[主程序] 收到停止信号（{self.round_no} 轮，"
                     f"广播 {self.shouted} 条，出口 {self.pool.describe()}）")
        finally:
            self._stop.set()
            if self._beating is not None:
                self._beating.join(2.0)
            self.receiver.close()
            self.sender.close()
            self.pool.close()
            self.bc.close()
