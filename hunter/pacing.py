"""请求节奏控制：让长时间监控不被 Apple 的边缘节点掐掉。

固定间隔是最好认的机器特征——就算加了 ±30% 抖动，请求时刻仍然一轮一格，
把时间戳画出来一眼就是机器。这里用四件事叠起来换掉它：

  1. 泊松间隔      —— 间隔服从指数分布（无记忆），形状跟人的点击流一致
  2. 令牌桶预算    —— 每小时请求数有硬上限。真正决定「能盯多久」的是它，不是间隔
  3. AIMD 拥塞控制 —— 被拦一次速率减半，之后每成功一轮慢慢加回来（照抄 TCP）
  4. 冷热时段      —— 平时省着打，只在会放货的时段全速

第 3 点是原来最缺的：老逻辑一次成功就把 fail_streak 清零、立刻满速冲回去，
于是「拦截 → 退避 → 满速 → 再拦截」来回震荡，越撞越黑。
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
    clock: object = time.monotonic
    sleeper: object = time.sleep
    calendar: object = datetime.now
    log: object = print

    def __post_init__(self):
        self.scale = 1.0
        self.blocks = 0
        self.bucket = TokenBucket(self.budget_per_hour, self.burst, self.clock)
        self.retry_after = 0.0
        self._max_scale = max(1.0, self.max_interval / max(self.base_interval, 0.1))

    # ---------- 反馈 ----------

    def on_ok(self) -> None:
        """成功一轮：加性恢复。不清零，慢慢爬回去。"""
        self.scale = max(1.0, self.scale - self.recover_step)

    def on_blocked(self, retry_after: float = 0.0) -> None:
        """被拦一轮：乘性退让。"""
        self.blocks += 1
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
        log=log,
        **kw,
    )
