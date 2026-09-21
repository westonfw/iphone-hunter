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
    #: **硬下限**：被拦或者连不上之后算出来的那个时刻，冲刺也不许越过它。
    #: 越过去就是拿着冲刺顶限流打，而那正是 541 续期的原因。
    floor_at: float = 0.0
    permanent: bool = False
    #: 从 config 里配的代理出口。跟直连一样永不过期，跟买手借来的口不同：
    #: 它不靠报到维持，也不会被报到顶掉。
    configured: bool = False
    #: 代理背后是多个 IP、自己轮换。标了它就不做流控：请求回来立刻发下一个，
    #: 541 不深退避（换 IP 就好）。见 ExitPool.done/blocked。
    rotating: bool = False
    #: 连续「连不上/网络错误」的次数。用来做升级退避：第一次多半是瞬时抖动，
    #: 只晾几秒；连着失败才逐步拉长（那才像代理真挂了）。打通一次就清零。
    net_fails: int = 0
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


def configured_exits(raw, log=print) -> list[tuple[str, str, bool]]:
    """把 `link.exits` 解析成 [(id, 代理地址, 是否多IP轮换)]。

    两种写法都认：

        "exits": ["http://user:pass@1.2.3.4:8080", "socks5://5.6.7.8:1080"]
        "exits": [{"id": "pool", "proxy": "http://1.2.3.4:8080", "rotating": true}]

    `rotating`（别名 `multi_ip`）标一条代理**背后是多个 IP、代理自己轮换**：
    这时主程序对它**不做流控**——一个请求回来立刻发下一个（代理中转本身有
    耗时，天然就是节奏），被 541 也不深退避（下一个请求就换了 IP）。只有代理
    自己挂了/连不上才退避。默认 false，当成单 IP、照常限速和熔断。

    没写 scheme 的按 http 代理处理；没给 id 的按顺序叫 proxy1、proxy2……
    `direct` 是内置直连的名字，配了就跳过并说出来——顶掉它主程序会没有兜底。
    坏条目一律跳过、不炸：一条配错不该让主程序起不来，别的出口还要干活。
    """
    out: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for n, item in enumerate(raw or (), start=1):
        rotating = False
        if isinstance(item, str):
            who, proxy = "", item
        elif isinstance(item, dict):
            who = str(item.get("id") or item.get("name") or "").strip()
            proxy = str(item.get("proxy") or item.get("url") or "")
            rotating = bool(item.get("rotating") or item.get("multi_ip"))
        else:
            log(f"[出口] link.exits 第 {n} 条看不懂（{type(item).__name__}），跳过")
            continue
        proxy = proxy.strip()
        if not proxy:
            log(f"[出口] link.exits 第 {n} 条没有代理地址，跳过")
            continue
        if "://" not in proxy:
            proxy = "http://" + proxy
        who = who or f"proxy{n}"
        if who == "direct":
            log("[出口] link.exits 里的出口不能叫 direct，那是内置直连的名字，跳过")
            continue
        if who in seen:
            log(f"[出口] link.exits 里 {who} 重复了，只留第一条")
            continue
        seen.add(who)
        out.append((who, proxy, rotating))
    return out


#: 轮换代理的熔断配置：冷却 0 秒 = 被 541 不静默。一条 541 只是背后 200 个 IP
#: 里的某一个被限了，下一个请求本就换了 IP，静默整条代理反而把好 IP 全废了。
#: 代理自己挂了那种网络错误由 scout.poll → pool.network_failed 单独管（升级退避）。
ROTATING_BREAKER = {"cooldowns": (0.0,), "heal_after": 1.0}


class ExitPool:
    """主程序的所有出口。线程安全——注册来自总线线程，取用来自巡检线程。"""

    def __init__(self, cfg: dict, log=print, clock=time.monotonic, stale: float = STALE,
                 sprint: bool = False):
        self.cfg, self.log, self.clock, self.stale = cfg, log, clock, stale
        self.sprint = sprint
        self._lock = threading.Lock()
        self._exits: dict[str, Exit] = {}
        #: 标了同出口的子程序。它们永远进不了 _exits，所以不能拿「在不在池子里」
        #: 当「说过没说过」——那样每 20 秒一次报到就刷一行，一天四千多行。
        self._same: set[str] = set()
        #: 因为 use_buyer=false 而被谢绝过的子程序。同样只说一次。
        self._declined: set[str] = set()
        self._turn = 0
        link = dict(cfg.get("link") or {})
        #: 要不要借买手的转发口。配了自己的代理池、或者买手跟主程序同一个
        #: 网关时可以关掉——关掉之后买手照样报到、照样收信号，只是不当出口。
        #: 要不要借买手的转发口。配了自己的代理池、或者买手跟主程序同一个
        #: 网关时可以关掉——关掉之后买手照样报到、照样收信号，只是不当出口。
        #: 老配置写的是 use_buyer_exits，兼容着读。
        self.borrow = bool(link.get("use_buyer", link.get("use_buyer_exits", True)))
        #: 要不要用本机直连出口。想让库存请求全走干净代理、彻底不碰本机 IP 时
        #: 关掉它——Akamai 那 10 秒降级是按出口 IP 判的，本机一旦被盯上，
        #: 留着它反而把脏 IP 混进巡检。关掉后必须有别的出口（代理或买手），
        #: 否则主程序没有任何口可用。
        self.use_direct = bool(link.get("use_direct", True))
        # 直连默认在池子里：就算一个子程序都没上线，主程序也得能干活。
        # 但 use_direct=false 时不建它——那是「只走干净出口」的明确要求。
        if self.use_direct:
            d = self._make("direct", None, self.clock())
            d.permanent = True
            self._exits["direct"] = d
        # 配置里的代理出口：主程序自己的口，不依赖任何买手在不在线。
        # 每条各有各的会话、熔断、节奏，跟借来的口一视同仁。
        self._configured: set[str] = set()
        for who, proxy, rotating in configured_exits(link.get("exits"), log=self.log):
            e = self._make(who, proxy, self.clock(), rotating=rotating)
            e.permanent = e.configured = True
            self._exits[who] = e
            self._configured.add(who)
        if self._configured:
            self._rebalance()
            self.log(f"[出口] 配置了 {len(self._configured)} 条代理出口："
                     f"{'、'.join(sorted(self._configured))}")
        # **关了直连就必须有替代出口，否则主程序等于瞎子。**
        if not self.use_direct:
            if not self._configured and not self.borrow:
                raise SystemExit(
                    "link 里 use_direct=false、use_buyer=false，又没配 link.exits"
                    "——主程序没有任何出口可用，无法刷库存")
            if not self._configured:
                self.log("[出口] ⚠️ use_direct=false 且没配 link.exits：主程序现在"
                         "一条出口都没有，要等买手报到（use_buyer=true）后才开始刷。"
                         "买手都没上线的这段时间是全盲的。")

    def _make(self, who: str, proxy: str | None, now: float,
              rotating: bool = False) -> Exit:
        pacer = build_pacer(self.cfg, sprint=self.sprint, log=lambda *a: None)
        # **绝不把 pacer.acquire 挂到 client.before_request 上。** 那个函数是同步
        # 睡眠：令牌不够时它会在请求里原地睡，而这个进程只有一个巡检线程——一条
        # 出口等预算，所有出口跟着停，心跳也发不出去（实测能一口气堵 120 秒，
        # 早就超过买手 90 秒的失联阈值了）。预算改由调度器兑现：每轮打完
        # `done(e, cost)` 把真实用量记账，`next_delay` 自然会把下一次推到
        # 预算允许的时刻，而等待发生在**挑出口之前**，不占着别人的位置。
        # 轮换出口立刻就绪（due_at=now），不参与错峰——它本来就是能多快多快。
        return Exit(id=who, proxy=proxy, client=self._client(proxy, rotating=rotating),
                    pacer=pacer, rotating=rotating,
                    due_at=now if rotating else self._spread(now, pacer), seen_at=now)

    def _period(self) -> float:
        """错峰用的周期：取**最快**那条出口的目标间隔。

        原来取的是 dict 里第一条（永远是直连）。直连被 541 退避到 4 倍时，这个
        周期跟着变成 120s，_nudge 的最小间距也跟着放大，把别的健康出口越推越远。
        """
        ts = [e.pacer.target() for e in self._exits.values() if e.pacer]
        return max(1.0, min(ts)) if ts else 30.0

    def _rebalance(self) -> None:
        """出口数变了，把冲刺的分摊倍数同步给每一条。调用方必须已经持有 _lock。

        合并提速靠错峰，不靠每条单独超速：n 条出口时单条冲刺 min_interval×n，
        错开之后合并仍是 min_interval。见 Pacer.boost_share。
        """
        n = max(1, len(self._exits))
        for e in self._exits.values():
            if e.pacer:
                e.pacer.boost_share = float(n)

    def _spread(self, now: float, pacer) -> float:
        """新出口插进日程表里**最空的那段**，别跟现有的撞在一起。

        不做这件事的话，两条出口会从同一时刻起步，合并速率跟单条一样——
        加了等于白加。这正是 2026-09-20 两台独立跑时的毛病（合并中位 24.4s
        而不是 15s，7% 的巡检是同时打的）。
        """
        period = max(1.0, pacer.target() if pacer else 30.0)
        # **窗口从「现在」算起，只看未来一个周期。** 从现有出口的排期往后接的话，
        # 一条退避到 300 秒后的旧出口会把新来的健康出口一起推到 315 秒后——它
        # 明明立刻就能跑。退避那条根本不在窗口里，也就不该参与错峰。
        due = sorted(t for t in (e.due_at for e in self._exits.values())
                     if now <= t <= now + period)
        if not due:
            return now
        pts = [now] + due + [now + period]
        best, gap = now, -1.0
        for a, b in zip(pts, pts[1:]):
            if b - a > gap:
                best, gap = a + (b - a) / 2, b - a
        return best

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

    def _client(self, proxy: str | None, rotating: bool = False) -> AppleClient:
        return AppleClient(
            region=self.cfg.get("region", "cn"),
            timeout=int(self.cfg.get("timeout", 15)),
            proxy=proxy,
            breaker=dict(ROTATING_BREAKER) if rotating else breaker_settings(self.cfg),
        )

    # ---------- 注册 ----------

    def enlist(self, who: str, ip: str, port: int, direct: bool) -> None:
        """一个子程序报到了。

        `direct=True` 是人在子程序的 config 里标的「我跟主程序同一个出口 IP」——
        那样借它的口出去等于绕回自己，白搭一跳，所以不加进池子，只记一下它活着。
        """
        now = self.clock()
        if who == "direct":
            # 内置那条直连叫这个名字，而且是池子里唯一不会过期的一条。让子程序
            # 顶掉它的话，它的 permanent 也一起没了——那个子程序一掉线，池子里
            # 可能一条出口都不剩，主程序整个停摆。
            self.log("[出口] 拒绝：link.id 不能叫 direct，那是内置直连的名字，"
                     "顶掉它会把主程序自己的出口也弄丢")
            return
        if who in self._configured:
            self.log(f"[出口] 拒绝：{who} 是 link.exits 里配好的代理出口的名字，"
                     f"子程序另起一个 link.id")
            return
        if not self.borrow:
            if who not in self._declined:
                self._declined.add(who)
                self.log(f"[出口] {who} 报到，但 link.use_buyer=false，"
                         f"不借它的口（信号照收）")
            return
        with self._lock:
            if direct:
                if self._exits.pop(who, None) is not None:
                    self._rebalance()        # 之前借过口、现在改标同出口了
                if who not in self._same:
                    self._same.add(who)
                    self.log(f"[出口] {who} 报到，但标了跟主程序同出口，"
                             f"不单独加一条")
                return
            self._same.discard(who)
            proxy = self._proxy_url(ip, port)
            cur = self._exits.get(who)
            if cur is not None and cur.proxy == proxy:
                cur.seen_at = now           # 心跳，位置没变
                return
            self._exits[who] = self._make(who, proxy, now)
            self._rebalance()
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
            if gone:
                self._rebalance()
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

    def _renew(self, e: Exit) -> None:
        """丢掉这条出口当前的连接，下一次请求会新开一条——对轮换代理就是换 IP。

        调用方必须已持有 _lock。renew 失败不致命：大不了这一轮还复用旧连接，
        下一轮再试，别让一次 renew 异常把整条巡检打断。
        """
        c = e.client
        if c is None:
            return
        try:
            c.renew_session()
        except Exception as ex:
            self.log(f"[出口] {e.id} 换连接失败（{type(ex).__name__}），下一轮再试")

    def done(self, e: Exit, cost: float = 1.0) -> None:
        """一条出口刚打完，记账 + 排下一次，顺便跟别人岔开。

        `cost` 是这一轮**真的发了几个请求**（一轮可能拆成好几批）。先扣掉，
        再让 next_delay 拿同样的估计去排下一次——预算就是这么兑现的，而且全程
        不阻塞：等待体现为 due_at 往后推，不是在请求里原地睡。
        """
        with self._lock:
            if e.rotating:
                # 不做流控：请求回来立刻可以发下一个。代理中转本身有耗时，
                # 单线程顺序发，天然就是节奏，不需要预算也不需要错峰。
                #
                # **每轮换一条新连接。** 轮换代理是「每个 CONNECT 换一个 IP」，
                # 而 curl_cffi 的 Session 默认 keep-alive——不换连接的话，几百个
                # 请求会全挤在第一条隧道、钉死同一个 IP，照样被 541（2026-09-21
                # 实测：复用连接发 421 次全 541，而一次性 curl 每次新连接全 200）。
                # renew_session 丢掉当前连接、默认不换身份，下一轮就走新隧道、新 IP。
                self._renew(e)
                e.due_at = self.clock()
                return
            if e.pacer:
                e.pacer.spend(max(0.0, cost))
                delay = e.pacer.next_delay(max(1.0, cost))
            else:
                delay = 30.0
            e.due_at = self._nudge(e, self.clock() + delay)

    def blocked(self, e: Exit, retry_after: float = 0.0, cost: float = 1.0) -> None:
        """这条出口被拦了：退避记在**它自己**头上，别的出口照常跑。

        被拦之前发出去的那几个请求照样要记账——它们是真的打出去了。
        """
        if e.rotating:
            # 轮换出口不做 AIMD。retry_after 直接用：pickup 的 541 通常没给
            # Retry-After（=0，立刻换个 IP 再发）；而代理自己挂了那种网络错误，
            # scout.poll 传的是 FAIL_COOLDOWN（30s），照它退避、别捶一个死代理。
            # 被拦更要换连接：不换的话下一发还钉在这个已经 541 的 IP 上。
            with self._lock:
                self._renew(e)
                e.due_at = self.clock() + max(0.0, retry_after)
            return
        if e.pacer:
            e.pacer.on_blocked(retry_after)
        self.done(e, cost)
        # 记下这次退避到哪儿：后面看到货冲刺时要拿它当下限，不能把三十秒的
        # 故障冷却压成八秒——那是拿着冲刺去顶限流
        e.floor_at = max(e.floor_at, e.due_at)

    def network_failed(self, e: Exit, base: float, cap: float) -> float:
        """出口连不上/网络错误：算这次该晾多久（升级退避），并记连击次数。

        第一次只晾 base（大概率是瞬时抖动，尤其轮换代理某个后端 IP 坏一下），
        连着失败才 base、2·base、4·base…封顶 cap。这样单条代理偶尔抖一下不会把
        主程序全盲一整个 30 秒，而代理真挂了也会逐步退避、不猛捶。

        只算值、记次数，不动 due_at——退避照旧由调用方交给 pool.blocked 兑现
        （轮换出口直接排 due_at，普通出口走节奏器），跟返回固定值时同一条路。
        打通一次 net_fails 在 pool.ok 里清零。
        """
        e.net_fails += 1
        return min(float(base) * (2 ** (e.net_fails - 1)), float(cap))

    def ok(self, e: Exit) -> None:
        if e.pacer:
            e.pacer.on_ok()
        e.floor_at = 0.0        # 打通了，那次退避的下限不再成立
        e.net_fails = 0         # 连不上的连击也清零

    def boost(self, seconds: float = 0.0) -> None:
        """看到货了：所有出口一起冲刺。

        **光改节奏器是不够的，排期也得往前挪。** 备用出口的 due_at 可能是按
        75 秒的常规间隔排的，只把目标间隔压到 4 秒的话它还要等满那 75 秒——
        冲刺对它完全没生效，当前这条被拦时也顶不上来。
        """
        now = self.clock()
        with self._lock:
            self._rebalance()
            for e in self._exits.values():
                if not e.pacer:
                    continue
                e.pacer.boost(seconds)
                # 提前，但**不许越过硬等待**：预算还差多少令牌、退避排到了哪儿、
                # 熔断还剩多久，三样都是真的要等的。冲刺只压缩「常规间隔」那部分。
                floor = max(now + e.pacer.bucket.wait_for(1), e.floor_at,
                            now + e.cooldown(""))
                e.due_at = max(min(e.due_at, now + e.pacer.target()), floor)

    def all_blocked(self, path: str) -> float:
        """所有出口都在熔断静默里的话，返回最早那条还要等多久；否则 0。

        这是「全盲」的判据，跟「都没到点」不一样：后者几秒就过去，前者可能是
        几分钟——2026-09-20 23:32 起两条出口轮流被 541 封，20 分钟一轮没打成，
        而日志里只有零散的「被拦」，没有一行说「现在一条能用的都没有」。
        """
        with self._lock:
            lefts = [e.cooldown(path) for e in self._exits.values() if e.client]
        if not lefts or any(x <= 0 for x in lefts):
            return 0.0
        return min(lefts)

    def soonest(self, path: str) -> float:
        """最早能用的那条还要等多久。用来决定主循环睡多久。"""
        now = self.clock()
        with self._lock:
            waits = [max(e.cooldown(path), e.due_at - now)
                     for e in self._exits.values() if e.client]
        # 空池（use_direct=false 且买手还没上线）时 min([]) 会炸——
        # 返回一个正数让主循环按 TICK 空转着等，别崩。
        return max(0.0, min(waits)) if waits else 1.0

    def describe(self) -> str:
        with self._lock:
            bits = [f"{e.id}"
                    f"{'(直连)' if e.direct else '(代理·多IP)' if e.rotating else '(代理)' if e.configured else ''}"
                    f"×{e.used}" for e in self._exits.values()]
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
