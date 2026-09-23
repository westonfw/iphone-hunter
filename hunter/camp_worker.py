"""守株待兔的常驻线程：停在结账页蹲着，反复打 search，命中就下单。

跟 PurchaseWorker 的区别是「进结账之后」那一段：
  * PurchaseWorker：收到一次有货信号 → 冷启动走完六步 → 结束。链条 15~17s，
    比 Pro Max 的放货窗口（~11~15s）还长，search 读库存那一刻窗口常已关。
  * CampWorker：**提前上膛**到结账第一步、常驻不退，把链条砍到只剩 search 一步。
    空闲时像人一样 8~15s 打一发续着会话；放货信号一来立刻插一发；某发 search
    撞进窗口就走完下单。会话有 20 分钟 TTL，蹲够一段就重建。

单账号一次只能蹲一个型号（购物袋是账号级的、一次一台），所以蹲哪个由配置/
优先级定；那个型号的放货信号才敲醒 search。别的型号放货这个蹲守用不上。

Playwright 只能单线程用，所以整段（预热、蹲守、下单、关闭）都在这一个线程里。
"""
from __future__ import annotations

import threading
import time

from .autobuy import BuyResult


class CampWorker:
    #: 被拦后的静默阶梯（秒），按连续被拦次数取档（第 1 次只看 retry_after）。
    COOLDOWNS = (120.0, 240.0, 300.0)

    def __init__(self, buyer, report, *, url: str,
                 in_stock_numbers=None, cadence: float = 8.0,
                 idle_cadence: float = 180.0, hot_seconds: float = 25.0,
                 session_seconds: float = 1080.0, rebuild_pause: float = 3.0,
                 log=print, clock=time.monotonic):
        self.buyer = buyer          # AutoBuy
        self.report = report
        self.url = url
        self.in_stock_numbers = list(in_stock_numbers or [])
        #: 主程序报货后热档密打的间隔。像人一样，别打成 541。
        self.cadence = max(1.0, float(cadence))
        #: 冷档（主程序安静）间隔。也一直打 search，只是慢些——目的是把会话**保温**：
        #: 凉的 search 一发 20s（2026-09-22 起两个买手实测，之前 8~10s），隔几分钟
        #: 打一发的会话下一发才是 1~3s。只续期不 search 保不住热（15fd1bc 试过，
        #: 09-23 13:53 放货信号 1s 内打出去、17.6s 才回）。但 90s 一发 92 分钟
        #: 68 个 POST 就 541（09-23 16:11），180s 一发实测仍是 1~3s，就用 180。
        #: 也顺带把 interactionMs 压着不弹「还在吗」。
        self.idle_cadence = max(self.cadence, float(idle_cadence))
        #: 收到放货信号后热档持续多久，之后没有新信号就回冷档。
        self.hot_seconds = max(0.0, float(hot_seconds))
        #: 一段会话最多蹲多久，必须 < 20 分钟 TTL。
        self.session_seconds = max(30.0, float(session_seconds))
        #: 一段结束到重新上膛之间的喘息，避免出错时空转打满 CPU。
        self.rebuild_pause = max(0.0, float(rebuild_pause))
        self.log, self.clock = log, clock
        from .autobuy import _part_of
        #: 蹲的是哪个 part。买手把所有 sighting 都喂过来，只有这个型号的才敲醒。
        self.part = _part_of(url)
        self.closed = False
        self.halted = False
        self.thread = None
        #: 连续「进不了结账入口」（541）的次数，攒到 5 次说一句。
        self._fails = 0
        #: 放货信号：收到所蹲型号的 sighting 就 set()，让蹲守里的 search 立刻打。
        self.wake = threading.Event()
        # Playwright 归这个线程，跟 PurchaseWorker 一样的约定
        self.buyer.cancelled = lambda: self.closed
        self.buyer.managed = True

    # ---------- 外部接口 ----------

    def signal(self) -> None:
        """所蹲型号放货了：敲醒蹲守，让它立刻打一发 search。"""
        self.wake.set()

    # 买手把所有型号的库存信号都喂给 worker（PurchaseWorker 的接口）。蹲守只关心
    # 自己蹲的那个型号：它有货就敲醒 search，别的型号一概忽略。做成 no-op 兼容接口，
    # 买手那边就不用为两种 worker 分叉。

    def observe(self, part, offers, unavailable=(), definitive=True) -> None:
        if part == self.part and offers:
            self.signal()

    def restock(self, part) -> None:
        # 卖空又补货：如果补的正是所蹲型号，也敲一下（信号可能就跟在后面）。
        if part == self.part:
            self.signal()

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="camp", daemon=True)
            self.thread.start()

    def close(self, timeout: float = 5.0):
        self.closed = True
        self.wake.set()   # 把在 wake.wait 里睡着的蹲守踹醒，好尽快退出
        if self.thread:
            self.thread.join(timeout)
            if self.thread.is_alive():
                self.log("[蹲守] 正在结束当前请求；提交记录会保留，重启不会重复提交")

    # ---------- 主循环 ----------

    def _run(self):
        self.log(f"[蹲守] 开始守株待兔：{self.url}"
                 f"（主程序报货时 {self.cadence:.0f}s 一发，安静时 {self.idle_cadence:.0f}s "
                 f"一发保持会话热，{self.session_seconds / 60:.0f} 分钟重建）")
        try:
            while not self.closed:
                try:
                    result = self.buyer.camp(
                        self.url, self.in_stock_numbers,
                        wake=self.wake, stop=lambda: self.closed,
                        cadence=self.cadence, idle_cadence=self.idle_cadence,
                        hot_seconds=self.hot_seconds,
                        session_seconds=self.session_seconds)
                except Exception as e:
                    result = BuyResult(False, "蹲守流程异常", self.url,
                                       f"{type(e).__name__}: {e}")
                if self.closed:
                    break

                if getattr(result, "rebuild", False):
                    # 会话到期/蹲够了：不推送、不停，重新上膛接着蹲。
                    self._fails = 0
                    self.log(f"[蹲守] {result.detail or '重建会话'}")
                    self._pause(self.rebuild_pause)
                    continue

                # **被 541 / 上膛被拦：静默冷却，不推送。** camp 反复探 checkoutx，
                # 541 是常态噪音，不是要通知人的结论；关键是别 3 秒一撞把它捶深。
                back = float(getattr(result, "retry_after", 0.0) or 0.0)
                if back > 0 and not result.ok:
                    self._fails += 1
                    # 连击加档：跟主程序的熔断器一个思路（README「被拦之后」），
                    # 静默期里探一次就续一次，越探越出不来，所以越连击停得越久。
                    # 第一次按对方给的 retry_after 来，连击才上阶梯。
                    ladder = (self.COOLDOWNS[min(self._fails, len(self.COOLDOWNS)) - 1]
                              if self._fails > 1 else 0.0)
                    pause = max(self.rebuild_pause, back, ladder)
                    self.log(f"[蹲守] {result.stage}——静默冷却 {pause:.0f}s 再重新上膛"
                             f"（连续第 {self._fails} 次）")
                    # 一直进不去就说一句：多半是这个 IP/账号在 checkoutx 上被限了。
                    if self._fails == 5:
                        self.report(result, self._title(), self.url)
                        self.log("[蹲守] ⚠️ 连着 5 次进不了结账入口——这个出口 IP 或"
                                 "账号可能在 checkoutx 上被限，蹲守暂时无从下手")
                    self._pause(pause)
                    continue

                # 到这儿才是真结论（成单 / 结果不明 / 配置死局）：报出来。
                self._fails = 0
                self.report(result, self._title(), self.url)

                if result.quota_done or getattr(self.buyer, "halt_for_human", False):
                    self.halted = True
                    self.log(f"[蹲守] {result.detail or '收工'}")
                    break
                self._pause(self.rebuild_pause)
        finally:
            try:
                self.buyer.stop()   # 只在创建 Playwright 的线程上关闭。
            except Exception as e:
                self.log(f"[蹲守] 关闭浏览器出错：{type(e).__name__}: {e}")

    def _title(self) -> str:
        from .autobuy import _part_of
        return _part_of(self.url) or self.url

    def _pause(self, seconds: float) -> None:
        end = self.clock() + max(0.0, seconds)
        while not self.closed and self.clock() < end:
            time.sleep(min(0.25, end - self.clock()))
