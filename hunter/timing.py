"""抢购链路的分段计时。

为什么单独做一个：09-19 复盘时我们靠日志时间戳去减，推出来的结论后来被证明
是错的（把「下单要 60s」当成「窗口必须有 60s」）。真正该盯的是**从看到货到
拿到 signKey 用了几秒**——库存是在第 2 步被签名令牌锁住的，之后的四步再慢
也不影响成败。没有这个数，任何优化都只能靠感觉判断有没有用。

起点用的是**库存观察时刻**（Apple 接口返回那一刻），不是进程开始干活的时刻：
监控轮询间隔、队列等待也算在窗口里，藏起来只会让数字好看。
"""

from __future__ import annotations

import time

#: 到这一步就算「抢到了」——第 2 步的响应里带服务端签发的 timeSlotId/signKey，
#: 拿到它库存就锁住了。实测 09-18 那单拿到之后又花了 50s 才提交，照样成功。
CLAIM = "signKey"


class Stages:
    """按名字记录里程碑，全部相对同一个起点。线程内使用，不加锁。"""

    def __init__(self, origin: float | None = None, clock=time.monotonic):
        self.clock = clock
        self.origin = self.clock() if origin is None else origin
        self.marks: list[tuple[str, float]] = []

    def mark(self, name: str) -> float:
        """记一个里程碑，返回距起点的秒数。"""
        t = self.clock() - self.origin
        self.marks.append((name, t))
        return t

    def at(self, name: str) -> float | None:
        """某个里程碑距起点几秒。没记过返回 None。"""
        return next((t for n, t in self.marks if n == name), None)

    @property
    def claimed(self) -> float | None:
        """到拿住库存用了几秒。None = 这一单没走到那一步。"""
        return self.at(CLAIM)

    def summary(self) -> str:
        """一行拆解：总计 + 每段增量。放货复盘只看这一行就够。"""
        if not self.marks:
            return "没有记录到任何阶段"
        claim = self.claimed
        head = (f"到拿住库存 {claim:.1f}s" if claim is not None
                else f"没拿住库存（走到 {self.marks[-1][0]}，{self.marks[-1][1]:.1f}s）")
        prev, bits = 0.0, []
        for name, t in self.marks:
            bits.append(f"{name} {t - prev:.1f}s")
            prev = t
        return f"{head}｜{' → '.join(bits)}"
