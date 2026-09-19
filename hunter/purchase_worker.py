"""一个线程拥有全部 Playwright 对象，库存线程只递交最新观察。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .autobuy import BuyResult


@dataclass(frozen=True)
class Offer:
    part: str
    store: str
    name: str
    url: str
    title: str
    observed: float
    priority: tuple = ()

    @property
    def key(self):
        return self.part, self.store


class PurchaseWorker:
    def __init__(self, buyer, report, *, max_age=30, max_attempts=2,
                 retry_delay=15, warm_url='', log=print, clock=time.monotonic):
        self.buyer, self.report, self.log = buyer, report, log
        self.max_age = max(1, float(max_age))
        self.max_attempts = max(1, int(max_attempts))
        self.retry_delay = max(1, float(retry_delay))
        self.warm_url, self.clock = warm_url, clock
        self.offers = {}
        self.attempts = {}
        self.epochs = {}
        self.cv = threading.Condition()
        self.closed = False
        self.halted = False
        self.cooldown_until = 0.0
        self.thread = None
        self.buyer.cancelled = lambda: self.closed
        self.buyer.managed = True

    def observe(self, part, offers, unavailable=()):
        with self.cv:
            # 未知/缺失门店撤回候选，但不当成一次新的放货。
            self.offers = {k: v for k, v in self.offers.items() if k[0] != part}
            for store in unavailable:
                key = part, store
                self.attempts.pop(key, None)
                self.epochs[key] = self.epochs.get(key, 0) + 1
            for offer in offers:
                self.offers[offer.key] = offer
            self.cv.notify_all()

    def _next(self):
        now = self.clock()
        if now < self.cooldown_until:
            return None
        for offer in sorted(self.offers.values(), key=lambda o: o.priority):
            count, after = self.attempts.get(offer.key, (0, -1))
            if (now - offer.observed <= self.max_age and count < self.max_attempts
                    and offer.observed > after and (count == 0 or now >= after + self.retry_delay)):
                return offer
        return None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name='purchase', daemon=True)
            self.thread.start()

    def close(self, timeout=5):
        with self.cv:
            self.closed = True
            self.cv.notify_all()
        if self.thread:
            self.thread.join(timeout)
            if self.thread.is_alive():
                self.log('[购买] 正在结束当前请求；提交记录会保留，重启不会重复提交')

    def _cool_if_blocked(self, error):
        from .fastpath import Blocked
        if not isinstance(error, Blocked):
            return 0.0
        delay = max(120, error.retry_after)
        self.cooldown_until = max(self.cooldown_until, self.clock() + delay)
        return delay

    def _preflight(self):
        if not getattr(self.buyer, 'cfg', {}).get('preflight', True):
            return
        try:
            note = self.buyer.prepare()
            if self.buyer.signed_in is not True and note != getattr(self, '_login_note', ''):
                self.report(BuyResult(False, '请提前检查登录', '', note), '购买就绪检查', '')
            self._login_note = note
        except Exception as e:
            self._cool_if_blocked(e)
            from .purchase_guard import PendingOrder
            if isinstance(e, PendingOrder):
                self.buyer.order_placed = self.halted = True
            note = str(e)
            if note != getattr(self, '_login_note', ''):
                self.report(BuyResult(False, '购买就绪检查未通过', '', note), '购买就绪检查', '')
            self._login_note = note

    def _run(self):
        warm_after = 0
        check_interval = max(120, float(getattr(self.buyer, 'cfg', {}).get('preflight_interval', 600)))
        try:
            self._preflight()
            check_after = self.clock() + check_interval
            while True:
                with self.cv:
                    if self.closed:
                        return
                    if self.halted:
                        self.cv.wait(timeout=1)
                        continue
                    offer = self._next()
                    epoch = self.epochs.get(offer.key, 0) if offer else 0
                if offer is None:
                    if self.clock() < self.cooldown_until:
                        with self.cv:
                            if not self.closed:
                                self.cv.wait(timeout=1)
                        continue
                    if self.clock() >= check_after:
                        self._preflight()
                        check_after = self.clock() + check_interval
                        continue
                    if self.warm_url and self.clock() >= warm_after:
                        try:
                            if not self.buyer.warm_alive:
                                self.buyer.warm(self.warm_url)
                        except Exception as e:
                            self._cool_if_blocked(e)
                            self.log(f'[预热] 暂不可用：{type(e).__name__}: {e}')
                        warm_after = self.clock() + 120
                    with self.cv:
                        if not self.closed:
                            self.cv.wait(timeout=1)
                    continue
                started = self.clock()
                self.log(f'[购买] {offer.part}/{offer.store}，库存观察距今 '
                         f'{started - offer.observed:.1f}s')
                try:
                    buy = self.buyer.fire if self.buyer.warm_alive else self.buyer.buy
                    result = buy(offer.url, [offer.name], [offer.store])
                except Exception as e:
                    delay = self._cool_if_blocked(e)
                    result = BuyResult(False, '购买流程异常', offer.url,
                                       f'{type(e).__name__}: {e}',
                                       retriable=not delay, retry_after=delay)
                with self.cv:
                    if self.epochs.get(offer.key, 0) == epoch:
                        count = self.attempts.get(offer.key, (0, 0))[0] + 1
                        if not result.retriable:
                            count = self.max_attempts
                        self.attempts[offer.key] = count, self.clock()
                    self.cooldown_until = max(self.cooldown_until,
                                              self.clock() + result.retry_after)
                    self.halted = bool(result.ok or self.buyer.order_placed)
                self.log(f'[购买] {offer.part}/{offer.store}：{result.stage}，'
                         f'耗时 {self.clock() - started:.1f}s')
                try:
                    self.report(result, offer.title, offer.url)
                except Exception as e:
                    self.log(f'[购买] 结果通知失败：{e}')
        finally:
            self.buyer.stop()  # 只在创建 Playwright 的线程上关闭。
