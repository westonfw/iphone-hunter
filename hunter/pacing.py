"""监控节奏：有界抖动、逐请求令牌预算、端点冷却及逐步恢复。

抖动用于分散请求时刻，不表示真人行为，也不能保证避免限流。
令牌桶允许 burst 突发；长期平均速率受 budget_per_hour 约束。
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

_WINDOW = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*[-~]\s*(\d{1,2}):(\d{2})\s*$")


def parse_window(text: str) -> tuple[int, int]:
    """把 "07:50-09:30" 解析成一天中的分钟数区间。支持跨零点（23:00-01:00）。"""
    m = _WINDOW.match(text)
    if not m:
        raise ValueError(f"时段格式不对：{text!r}，应该像 07:50-09:30")
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if not (0 <= h1 < 24 and 0 <= h2 < 24 and m1 < 60 and m2 < 60):
        raise ValueError(f"时段超出范围：{text!r}")
    return h1 * 60 + m1, h2 * 60 + m2


def in_window(minute: int, window: tuple[int, int]) -> bool:
    start, end = window
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end  # 跨零点


class TokenBucket:
    """长期平均每小时 N 个请求，允许 burst 个请求的突发。

    只算账不睡觉——要等多久由调用方决定，这样才好测。
    """

    def __init__(self, per_hour: float, burst: float, clock=time.monotonic):
        self.rate = max(per_hour, 1.0) / 3600.0  # 每秒补多少令牌
        self.burst = max(burst, 1.0)
        self.clock = clock
        self.tokens = self.burst
        self.last = clock()

    def _refill(self) -> None:
        t = self.clock()
        self.tokens = min(self.burst, self.tokens + (t - self.last) * self.rate)
        self.last = t

    def set_rate(self, per_hour: float) -> None:
        """换补充速率。**先按旧速率补满到此刻**再换，否则这段时间会按新速率
        重算，等于凭空多给（或少给）令牌。"""
        self._refill()
        self.rate = max(per_hour, 1.0) / 3600.0

    def take(self, n: float) -> None:
        """记账：已经发了 n 个请求。允许透支，透支的部分由 wait_for 还回来。"""
        self._refill()
        self.tokens -= n

    def wait_for(self, n: float) -> float:
        """再发 n 个请求前还得等几秒。0 表示现在就能发。"""
        self._refill()
        short = n - self.tokens
        return max(0.0, short / self.rate) if short > 0 else 0.0


@dataclass
class Breaker:
    """按端点熔断：被拦之后**彻底不碰**这个端点，而不是降速接着打。

    为什么不是指数退避
    ------------------
    2026-09-15 的日志把这件事说死了：26 次 541 全部落在 `retail/pickup-message`
    上（同期 `availability-message` 89 次请求一次没被拦），而其中 10 段不重启
    也自己恢复了，恢复间隔中位数只有 79 秒、最短 24 秒。

    但退避的语义是「间隔翻倍、照样打」——静默期里每一次探测都在给封禁续期。
    那天唯一没自愈的一段正是被探了 6 次、一路退到 900s，26 分钟没爬出来；
    同一时刻换个进程发一模一样的请求（同 IP、同参数、连 UA 都同）却是 200。

    所以这里只有三挡固定静默期、封顶 5 分钟，而且静默期内**一个包都不发**。
    连击档位靠 heal_after 自己过期，不靠成功次数去磨。
    """

    #: 第 1/2/3+ 次被拦分别静默多久。数据说 90s 就够绝大多数情况了。
    cooldowns: tuple[float, ...] = (90.0, 180.0, 300.0)
    #: 这么久没被拦，连击档位归零——再被拦是新的一次，不是上一轮的延续。
    heal_after: float = 600.0
    clock: object = time.monotonic

    def __post_init__(self):
        self.open_until = 0.0
        self.tier = -1
        self.last_block = 0.0
        self.trips = 0

    def left(self) -> float:
        """还要静默几秒。0 表示现在可以发。"""
        return max(0.0, self.open_until - self.clock())

    def ready(self) -> bool:
        return self.left() <= 0.0

    def trip(self, retry_after: float = 0.0) -> float:
        """这个端点被拦了。返回这次要静默多久。"""
        t = self.clock()
        # 用 trips 而不是 last_block 的真假来判断「之前拦过没有」——单调时钟
        # 从 0 起步时 last_block 正好是 0.0，拿它当标志会静默失效。
        if self.trips and t - self.last_block > self.heal_after:
            self.tier = -1          # 上次被拦已经很久了，不算连击
        self.tier = min(self.tier + 1, len(self.cooldowns) - 1)
        self.last_block = t
        self.trips += 1
        delay = max(self.cooldowns[self.tier], retry_after)
        self.open_until = max(self.open_until, t + delay)
        return self.open_until - t

    def ok(self) -> None:
        """这个端点通了。

        立刻恢复满速，不做渐进恢复：封禁是端点级的，它一通就说明已经过期，
        再慢慢爬只是白白错过放货。连击档位另算——它只由时间清零。
        """
        self.open_until = 0.0
        if self.trips and self.clock() - self.last_block > self.heal_after:
            self.tier = -1


@dataclass
class Pacer:
    """一轮监控之间该睡多久，由它说了算。"""

    base_interval: float = 30.0
    min_interval: float = 4.0
    max_interval: float = 900.0
    budget_per_hour: float = 150.0
    burst: float = 20.0
    hot_windows: list[tuple[int, int]] = field(default_factory=list)
    cold_multiplier: float = 5.0
    recover_step: float = 0.25   # 每成功一轮，拥塞倍率减多少
    block_factor: float = 2.0    # 每被拦一次，拥塞倍率乘多少
    #: 拥塞倍率的硬上限。原来只受 max_interval/base_interval 约束（=30），
    #: 撞穿之后 30s 的巡检变成 900s，等于放货那一刻程序是聋的。真正的等待
    #: 交给 Breaker 去做，Pacer 只需要「明显慢一点」，8 倍足够。
    max_scale: float = 8.0
    #: 这么久没被拦就把倍率直接归 1，不再一轮一轮磨。见 on_ok。
    heal_after: float = 600.0
    #: 看到有货之后，压到 min_interval 冲刺多久。0 = 关掉。见 boost。
    boost_seconds: float = 180.0
    #: 冲刺期间的每小时预算。常规预算（220/h ≈ 16.4s 一次）会把冲刺掐死：
    #: 2026-09-19 22:43 那波实测，冲刺目标 4s，burst 的 20 个令牌只撑了 105 秒，
    #: 之后就被补充速率按在 14~17s——而那时候货还在，正是最该快的时候。
    boost_budget_per_hour: float = 900.0
    #: 冲刺的**分摊倍数**，给多出口用：合并提速靠错峰，不靠每条单独超速。
    #:
    #: 2026-09-20 23:11 那一程的教训：两条出口各自冲到 4~5s，23:17 起先后被
    #: 541 封死，从 23:32 到停机 20 分钟一轮都没打成——「零失明」反而变成了
    #: 全盲。541 按（出口 IP + 端点）独立判，单条 4s 就是过线，几条出口一起
    #: 过线就一起死。设成出口数 n 之后：单条冲刺 min_interval×n（两条各 8s，
    #: 回到实测能活的区间），错峰之后合并仍是 min_interval——快的部分留住了，
    #: 危险的部分摊薄了。冲刺预算同样除以 n：boost_budget 是全池的量。
    boost_share: float = 1.0
    clock: object = time.monotonic
    sleeper: object = time.sleep
    calendar: object = datetime.now
    log: object = print

    def __post_init__(self):
        self.scale = 1.0
        self.boost_until = 0.0
        self._boosted_budget = False
        self.blocks = 0
        self.bucket = TokenBucket(self.budget_per_hour, self.burst, self.clock)
        self.retry_after = 0.0
        self.last_block_at = 0.0
        self._max_scale = max(1.0, min(self.max_interval / max(self.base_interval, 0.1),
                                       self.max_scale))

    # ---------- 反馈 ----------

    def on_ok(self) -> None:
        """成功一轮：加性恢复，外加一条时间兜底。

        光靠 recover_step 每轮减 0.25 是不够的：从上限（原来是 ×30）爬回满速要
        116 个成功轮次，一轮又被退避拉长到几百秒——实际就是**一天都回不来**。
        这正是「关掉重开就好了」的全部真相：重启唯一做的事就是把这个倍率归 1。

        所以加一条：超过 heal_after 没再被拦，说明上一次封禁早过期了，直接满速。
        """
        if (self.scale > 1.0 and self.blocks
                and self.clock() - self.last_block_at > self.heal_after):
            self.scale = 1.0
            return
        self.scale = max(1.0, self.scale - self.recover_step)

    def on_blocked(self, retry_after: float = 0.0) -> None:
        """被拦一轮：乘性退让。"""
        self.blocks += 1
        self.last_block_at = self.clock()
        self.scale = min(self._max_scale, self.scale * self.block_factor)
        self.retry_after = max(self.retry_after, retry_after)

    def boost(self, seconds: float = 0.0) -> float:
        """看到货了：临时压到 min_interval 冲刺一会儿。返回冲刺到什么时候。

        补货捡漏最难的一段就在这里：`hot_windows` 要求你提前知道几点放货，而补货
        压根不挑时间。真正可靠的信号是「刚刚有一个型号从无货变有货」——那一刻
        大概率还有后续，也可能第一单没抢到要马上再来，所以这几分钟值得全速。

        只降间隔、不动预算：令牌桶照常扣，撞上 budget_per_hour 时 next_delay 还是
        会把人按住。所以冲刺花的是**余额**，不会变成无限刷。
        """
        span = self.boost_seconds if seconds <= 0 else seconds
        if span <= 0:
            return self.boost_until
        self.boost_until = max(self.boost_until, self.clock() + span)
        self._sync_budget()
        return self.boost_until

    def boosting(self) -> bool:
        return self.clock() < self.boost_until

    def _sync_budget(self) -> None:
        """冲刺期间把令牌桶切到冲刺预算，结束后切回来。

        只降间隔不给预算等于没降：桶一空，next_delay 里的 bucket.wait_for
        会把人按在补充速率上，冲刺目标再低也没用。
        """
        want = self.boosting()
        if want == self._boosted_budget:
            return
        self._boosted_budget = want
        share = max(1.0, self.boost_share)
        self.bucket.set_rate(self.boost_budget_per_hour / share if want
                             else self.budget_per_hour)
        if not want:
            self.log(f"[节奏] 冲刺结束，预算回到 {self.budget_per_hour:.0f} 次/小时")

    def spend(self, requests: float) -> None:
        """这一轮实际发了几个请求，记进预算。"""
        self.bucket.take(requests)

    #: 等令牌时每次至少睡这么久。浮点误差会让 tokens 永远差一丁点儿到不了 1，
    #: 于是 acquire 用越来越小的间隔空转、永不收敛（确定性时钟下直接死循环，
    #: 真实时钟靠 time.sleep 的毫秒粒度才蹭过去）。睡够一个下限就一定往前走。
    MIN_SLEEP = 0.05

    def acquire(self) -> None:
        """在每次实际发送请求之前等待并扣除一个令牌。"""
        while True:
            delay = self.bucket.wait_for(1)
            if delay <= 0:
                self.bucket.take(1)
                return
            self.sleeper(min(max(delay, self.MIN_SLEEP), 30))

    # ---------- 决策 ----------

    def is_hot(self) -> bool:
        if not self.hot_windows:
            return True  # 没配热时段就全天等价对待，靠预算兜底
        minute = self.calendar().hour * 60 + self.calendar().minute
        return any(in_window(minute, w) for w in self.hot_windows)

    def target(self) -> float:
        """当前这一刻的目标平均间隔。"""
        t = self.base_interval * self.scale
        if not self.is_hot():
            t *= self.cold_multiplier
        if self.boosting():
            # 退避优先于冲刺：被拦过就老实按退避后的间隔走，别拿冲刺去顶限流。
            # 冲刺下限按 boost_share 摊薄——多出口时合并提速靠错峰，单条不超速。
            lo = self.min_interval * max(1.0, self.boost_share)
            t = min(t, max(lo * self.scale, lo))
        return min(max(t, self.min_interval), self.max_interval)

    FLOOR = 0.8
    CEIL = 1.2

    def next_delay(self, next_cost: float = 1.0) -> float:
        self._sync_budget()
        target = self.target()
        delay = min(max(random.uniform(target * self.FLOOR, target * self.CEIL),
                        self.min_interval), self.max_interval)
        # max_interval 只限制普通轮询间隔，不能截短服务端或预算要求的等待。
        delay = max(delay, self.bucket.wait_for(next_cost), self.retry_after)
        self.retry_after = 0.0
        return delay

    def sleep(self, next_cost: float = 1.0) -> float:
        d = self.next_delay(next_cost)
        self.sleeper(d)
        return d

    def describe(self) -> str:
        bits = [f"目标 {self.target():.0f}s"]
        if self.boosting():
            bits.append(f"冲刺 {self.boost_until - self.clock():.0f}s")
        if self.scale > 1.0:
            bits.append(f"退避 ×{self.scale:.2f}")
        if not self.is_hot():
            bits.append("冷时段")
        bits.append(f"余额 {self.bucket.tokens:.0f}/{self.bucket.burst:.0f}")
        return "，".join(bits)


def build_pacer(cfg: dict, sprint: bool = False, log=print, **kw) -> Pacer:
    """从 config.json 里那堆键装出一个 Pacer，兼容老的 poll_interval / sprint_interval。"""
    pc = dict(cfg.get("pacing") or {})
    fallback = float(cfg.get("sprint_interval", 5) if sprint else cfg.get("poll_interval", 30))
    base = float(pc.get("sprint_interval", fallback) if sprint else pc.get("base_interval", fallback))

    windows: list[tuple[int, int]] = []
    for text in (pc.get("hot_windows") or []):
        try:
            windows.append(parse_window(str(text)))
        except ValueError as e:
            log(f"[节奏] 忽略无法解析的时段：{e}")

    return Pacer(
        base_interval=base,
        min_interval=float(pc.get("min_interval", 4)),
        max_interval=float(pc.get("max_interval", 900)),
        # 冲刺是开卖前十分钟的短跑，配额给足；常规监控要跑一整天，才需要省
        budget_per_hour=float(pc.get("sprint_budget_per_hour", 900) if sprint
                              else pc.get("budget_per_hour", 150)),
        burst=float(pc.get("burst", 20)),
        hot_windows=[] if sprint else windows,   # 冲刺时无视冷热，你人就在旁边等
        cold_multiplier=float(pc.get("cold_multiplier", 5)),
        recover_step=float(pc.get("recover_step", 0.25)),
        max_scale=float(pc.get("max_scale", 8)),
        heal_after=float(pc.get("heal_after", 600)),
        boost_seconds=float(pc.get("boost_seconds", 180)),
        boost_budget_per_hour=float(pc.get("boost_budget_per_hour",
                                           pc.get("sprint_budget_per_hour", 900))),
        log=log,
        **kw,
    )


def breaker_settings(cfg: dict) -> dict:
    """从 config.json 的 pacing 段里取熔断参数，交给 AppleClient 建 Breaker。"""
    pc = dict(cfg.get("pacing") or {})
    cd = pc.get("cooldowns")
    kw: dict = {}
    if cd:
        try:
            kw["cooldowns"] = tuple(float(x) for x in cd) or Breaker.cooldowns
        except (TypeError, ValueError):
            pass
    if pc.get("heal_after") is not None:
        kw["heal_after"] = float(pc["heal_after"])
    return kw
