"""请求节奏控制：让长时间监控不被 Apple 的边缘节点掐掉。

固定间隔是最好认的机器特征——就算加了 ±30% 抖动，请求时刻仍然一轮一格，
把时间戳画出来一眼就是机器。这里用四件事叠起来换掉它：

  1. 泊松间隔      —— 间隔服从指数分布（无记忆），形状跟人的点击流一致
  2. 令牌桶预算    —— 每小时请求数有硬上限。真正决定「能盯多久」的是它，不是间隔
  3. AIMD 拥塞控制 —— 被拦一次速率减半，之后每成功一轮慢慢加回来（照抄 TCP）
  4. 冷热时段      —— 平时省着打，只在会放货的时段全速

第 3 点是原来最缺的：老逻辑一次成功就把 fail_streak 清零、立刻满速冲回去，
于是「拦截 → 退避 → 满速 → 再拦截」来回震荡，越撞越黑。

第 5 件事后来才加：**Breaker**（按端点熔断）。AIMD 管的是「平时该多快」，
它管的是「已经被拦了该怎么办」——答案不是慢，是**静默**，而且只静默出事的
那个端点。理由见 Breaker 的文档。
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
    """每小时 N 个请求的硬上限，允许攒出一小段突发。

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

    def trip(self) -> float:
        """这个端点被拦了。返回这次要静默多久。"""
        t = self.clock()
        # 用 trips 而不是 last_block 的真假来判断「之前拦过没有」——单调时钟
        # 从 0 起步时 last_block 正好是 0.0，拿它当标志会静默失效。
        if self.trips and t - self.last_block > self.heal_after:
            self.tier = -1          # 上次被拦已经很久了，不算连击
        self.tier = min(self.tier + 1, len(self.cooldowns) - 1)
        self.last_block = t
        self.trips += 1
        self.open_until = t + self.cooldowns[self.tier]
        return self.cooldowns[self.tier]

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
    clock: object = time.monotonic
    sleeper: object = time.sleep
    calendar: object = datetime.now
    log: object = print

    def __post_init__(self):
        self.scale = 1.0
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

    def spend(self, requests: float) -> None:
        """这一轮实际发了几个请求，记进预算。"""
        self.bucket.take(requests)

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
        return min(max(t, self.min_interval), self.max_interval)

    #: 最短间隔占目标的比例。不能取 0：真出现 0.2s 的连发反而像脚本。
    FLOOR = 0.35
    #: 最长间隔占目标的比例，防止偶尔抽出一个超长空窗把放货错过去。
    CEIL = 4.0

    def next_delay(self, next_cost: float = 1.0) -> float:
        target = self.target()
        # 平移指数分布：均值正好是 target，无记忆性，画出来跟人的点击流同形。
        #
        # 注意别用「先抽指数再 clamp 到下界」——指数分布有近 30% 的样本落在
        # 0.35 倍以下，clamp 会把它们全压成同一个值，等于又造出一个固定节拍。
        # 平移之后下界处没有堆积，上界靠重抽（概率 <0.5%）也不堆。
        floor = target * self.FLOOR
        for _ in range(8):
            delay = floor + random.expovariate(1.0 / (target - floor))
            if delay <= target * self.CEIL:
                break
        else:
            delay = target * self.CEIL
        # 预算不够就多等——这一条决定了长跑能跑多久
        delay = max(delay, self.bucket.wait_for(next_cost))
        if self.retry_after:
            delay = max(delay, self.retry_after)
            self.retry_after = 0.0
        return min(max(delay, self.min_interval), self.max_interval)

    def sleep(self, next_cost: float = 1.0) -> float:
        d = self.next_delay(next_cost)
        self.sleeper(d)
        return d

    def describe(self) -> str:
        bits = [f"目标 {self.target():.0f}s"]
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
