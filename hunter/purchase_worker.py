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
                 retry_delay=15, warm_url='', probe_url='', log=print,
                 clock=time.monotonic):
        self.buyer, self.report, self.log = buyer, report, log
        self.max_age = max(1, float(max_age))
        #: 一轮放货里同一型号最多打几次。**0 = 不限**——货一没监控就不再报，
        #: 候选随之消失，_next 自然停手（2026-09-20 07:27 实测：结账说没货的
        #: 那一刻，监控 6 秒前就已经改口了，不存在「打空炮」的窗口）。
        #: 真正没救的失败由 retriable=False 直接判死，不靠次数兜。
        self.max_attempts = max(0, int(max_attempts))
        self.retry_delay = max(1, float(retry_delay))
        self.warm_url, self.clock = warm_url, clock
        #: 结账预热拿哪个型号探路。跟 warm_url 分开：预热产品页可以关着，
        #: 结账预热照样要有一个型号可用。
        self.probe_url = probe_url or warm_url
        self.offers = {}
        self.attempts = {}
        self.epochs = {}
        #: 每个型号已经试过的门店。冒出一家没试过的 = 新机会，重试计数清零。
        self.tried = {}
        #: 每个型号**最后一次被真正查过**的时刻。只有查过才敢说「没货」——
        #: 查询失败/熔断时根本不调 observe，那时候「看不到」只是「没去看」。
        self.polled = {}
        #: 这一轮已经判死的型号（result.fatal，比如页面明写售罄、配置不对）。
        #: **不含被限流**——那是全局的，由 cooldown_until 管，冷却过了还要接着打。
        #: 跟次数无关，
        #: 不限次数时它就是唯一的刹车。卖光再补货时跟着 attempts 一起清。
        self.burned = set()
        self.cv = threading.Condition()
        self.closed = False
        self.halted = False
        self.cooldown_until = 0.0
        self.thread = None
        self.buyer.cancelled = lambda: self.closed
        self.buyer.managed = True
        # 让买家在发结账请求之前能回头问一句「货还在吗」
        self.buyer.stock_live = self.stock_live

    def observe(self, part, offers, unavailable=()):
        with self.cv:
            prev = {k[1] for k in self.offers if k[0] == part}
            self.offers = {k: v for k, v in self.offers.items() if k[0] != part}
            for offer in offers:
                self.offers[offer.key] = offer
            self.polled[part] = self.clock()      # 这一轮真的查过它了
            live = {o.store for o in offers}
            tried = self.tried.get(part, set())

            if unavailable and prev and not live:
                # **明确**报无货：这一轮卖完了，下次放货算全新的一轮。
                # 注意跟「结果未知」区分——未知只是撤回候选，不能当成新一轮，
                # 否则接口抖一下就把重试次数刷没了。
                self.attempts.pop(part, None)
                self.tried.pop(part, None)
                self.burned.discard(part)
                self.epochs[part] = self.epochs.get(part, 0) + 1
            elif live - prev - tried:
                # 冒出一家没试过的门店：重试计数清零，而且换 epoch——正在跑的
                # 那一单是拿旧门店表打的，它的结论管不了这家新店。
                fresh = "、".join(sorted(live - prev - tried))
                self.attempts.pop(part, None)
                self.burned.discard(part)
                self.epochs[part] = self.epochs.get(part, 0) + 1
                self.log(f'[购买] {part} 新增有货门店 {fresh}，重试次数重新计')
            self.cv.notify_all()

    #: 「刚刚查过」的时限。超过它就当没查过——常规巡检间隔 30~45 秒，拿那么旧的
    #: 读数去踩刹车会误杀真放货。放货期间冲刺会把间隔压到 4~6 秒（2026-09-20
    #: 实测 6~8 秒），所以真正要刹车的那一刻，信息一定是新鲜的。
    FRESH_SECONDS = 15.0

    def stock_live(self, part) -> bool | None:
        """监控还看不看得到这个型号的货。True/False/None（None = 说不好）。

        **只有「刚刚查过、而且没查到」才返回 False。** 查询失败、熔断静默、
        或者上一次查询已经是半分钟前——这些情况下「看不到」只等于「没去看」，
        一律返回 None 让买家照常往下走。宁可白跑一趟，也不能误杀一次真放货。

        这是给买家用的急刹：明知没货还发 search，赔的是 10.8 秒、十几个
        checkoutx 请求，以及那之后大概率的一次掉线。
        """
        with self.cv:
            if any(k[0] == part for k in self.offers):
                return True
            last = self.polled.get(part)
        fresh = float(getattr(self.buyer, 'cfg', {}).get(
            'stock_fresh_seconds', self.FRESH_SECONDS) or self.FRESH_SECONDS)
        if last is None or self.clock() - last > fresh:
            return None
        return False

    def _targets(self, part, now) -> list:
        """这个型号当前所有还新鲜的门店候选，按优先级排。

        全都交给买家，让它在**同一个结账会话**里换店（一次 search 就够），
        而不是回到这里重来一整轮。
        """
        return sorted((o for k, o in self.offers.items()
                       if k[0] == part and now - o.observed <= self.max_age),
                      key=lambda o: o.priority)

    def _eligible(self, offer, now) -> bool:
        """这个型号现在能不能打：观察还新鲜、还有重试次数、也过了重试间隔。

        计数按**型号**记，不按门店——一轮尝试内部就会把有货的几家挨个试过，
        再按门店计数等于把同一轮重复算好几次。
        """
        if offer.part in self.burned:
            return False
        count, after = self.attempts.get(offer.part, (0, -1))
        if self.max_attempts and count >= self.max_attempts:
            return False
        return (now - offer.observed <= self.max_age
                and offer.observed > after
                and (count == 0 or now >= after + self.retry_delay))

    def _next(self):
        """挑下一个要打的目标：按优先级取第一个还能打的。

        **不锁型号。** 曾经锁过——理由是换型号要清袋、重加购、重进结账，代价
        15~30 秒。2026-09-19 查明加购其实是一个带 atbtoken 的 GET（388ms）之后，
        换型号的净代价降到 **0.4 秒**，那个理由就没了。

        反过来锁定还有害：失败最常见的原因是「拿不到取货时段」，而 select_store
        已经在一次尝试里把该型号所有有货门店都试过了。既然这个型号全军覆没，
        再守着它重试大概率还是同样结果，隔壁型号反而可能下得了单。

        留下来的是跟锁无关的那几样：按**型号**计重试次数（一次尝试内部就把门店
        试遍了，按门店计会重复计数）、一次把所有有货门店交给买家、新门店给新机会。
        """
        now = self.clock()
        if now < self.cooldown_until:
            return None
        for offer in sorted(self.offers.values(), key=lambda o: o.priority):
            if self._eligible(offer, now):
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
            note = self.buyer.prepare(self.probe_url)
            if self.buyer.signed_in is not True and note != getattr(self, '_login_note', ''):
                # wake=True：登录掉了必须当场叫醒。凌晨掉线、早上才发现，
                # 等于整晚白挂，而重登要人过双重认证，脚本代不了劳。
                self.report(BuyResult(False, '请提前检查登录', '', note, wake=True),
                            '购买就绪检查', '')
            self._login_note = note
            self._warn_login_expiry()
        except Exception as e:
            self._cool_if_blocked(e)
            from .purchase_guard import PendingOrder, QuotaReached
            if isinstance(e, QuotaReached):
                self.halted = True
                self.log(f'[购买] {e}')
                return
            if isinstance(e, PendingOrder):
                self.buyer.order_placed = self.buyer.halt_for_human = True
                self.halted = True
            note = str(e)
            if note != getattr(self, '_login_note', ''):
                self.report(BuyResult(False, '购买就绪检查未通过', '', note, wake=True),
                            '购买就绪检查', '')
            self._login_note = note

    def _warn_login_expiry(self):
        """idmsa 的 DES 凭证 15 天到期，到期必须人工过双重认证。

        所以别等它掉——掉的那一刻可能正好是放货前一小时。剩余天数低于
        `login_warn_days` 就提前喊一次，让人挑个有空的时候重登。读的是 cookie
        自己的到期时间，零请求。
        """
        getter = getattr(self.buyer, 'login_days_left', None)
        days = getter() if callable(getter) else None
        if days is None:
            return
        limit = float(getattr(self.buyer, 'cfg', {}).get('login_warn_days', 3))
        if limit <= 0 or days > limit:
            self._expiry_warned = False
            return
        if getattr(self, '_expiry_warned', False):
            return
        self._expiry_warned = True
        self.report(BuyResult(
            False, '登录快到期了', '',
            f'登录凭证还有 {days:.1f} 天到期。到期要人工过双重认证，脚本代不了劳'
            f'——挑个有空的时候手动重登一次，别等放货那天。', wake=True),
            '购买就绪检查', '')

    def _run(self):
        warm_after = 0
        # 默认 300 秒。实测（2026-09-20，两台机器各自独立的 cookie jar）登录态
        # 大约每 2 小时掉一次，而且是**绝对寿命**——我们每 10 分钟就有真实导航，
        # 续不动它。掉线到下一次保活之间就是裸奔窗口，600 秒太长：07:20 掉线、
        # 07:27 放货，正好撞上，白付 21 秒的登录。
        check_interval = max(120, float(getattr(self.buyer, 'cfg', {}).get('preflight_interval', 300)))
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
                    epoch = self.epochs.get(offer.part, 0) if offer else 0
                    # 把这个型号**所有**有货的门店一起交出去，让买家在一个结账
                    # 会话里换店。原来只传命中的那一家，另外几家有货的门店压根
                    # 没机会试——2026-09-19 22:46 四家同时有货，只试了一家。
                    live = self._targets(offer.part, self.clock()) if offer else []
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
                stores = [o.store for o in live] or [offer.store]
                names = [o.name for o in live] or [offer.name]
                self.log(f'[购买] {offer.part} @ {"、".join(stores)}，库存观察距今 '
                         f'{started - offer.observed:.1f}s')
                try:
                    buy = self.buyer.fire if self.buyer.warm_alive else self.buyer.buy
                    result = buy(offer.url, names, stores)
                except Exception as e:
                    delay = self._cool_if_blocked(e)
                    result = BuyResult(False, '购买流程异常', offer.url,
                                       f'{type(e).__name__}: {e}',
                                       retriable=not delay, retry_after=delay)
                if result.quota_done:
                    # 买够了：收工，不是故障。
                    with self.cv:
                        self.halted = True
                    self.log(f'[购买] {result.detail or "已达到订单上限，停止抢购"}')
                    self.report(result, offer.title, offer.url)
                    continue
                with self.cv:
                    self.tried[offer.part] = self.tried.get(offer.part, set()) | set(stores)
                    if self.epochs.get(offer.part, 0) == epoch:
                        if result.ok:
                            # 成一单**不算一次重试**——重试次数是给失败用的，
                            # 买满为止由 max_orders 的配额说了算。计数清零，但
                            # 仍然要等一次新观察（after 照记），别拿旧观察连打。
                            self.attempts[offer.part] = 0, self.clock()
                        else:
                            count = self.attempts.get(offer.part, (0, 0))[0] + 1
                            if result.fatal:
                                # 只有「这个型号没救了」才判死。被限流不算——
                                # 那是全局的，冷却过了还得接着打这个型号。
                                self.burned.add(offer.part)
                                self.log(f'[购买] {offer.part} 这一轮判死：'
                                         f'{result.stage}')
                            self.attempts[offer.part] = count, self.clock()
                    self.cooldown_until = max(self.cooldown_until,
                                              self.clock() + result.retry_after)
                    # 买够了才停。成功一单只是「还差几台」——真正的上限由
                    # PurchaseGuard 的配额（max_orders）说了算，下一单进不了
                    # 守卫就会抛 QuotaReached，那时才收工。
                    # 用 order_placed 当停止信号是错的：它成单就置 True，于是
                    # 一单之后全线停摆，max_orders 大于 1 永远不生效。
                    self.halted = bool(getattr(self.buyer, 'halt_for_human', False))
                self.log(f'[购买] {offer.part}：{result.stage}，'
                         f'耗时 {self.clock() - started:.1f}s')
                # 一单打完立刻补一次保活，别等下一个间隔。
                # 2026-09-19/20 的统计：6 次购买尝试里有 4 次在 1 分钟内跟着一次
                # 掉线（基线掉线率约 1 次/小时，随机撞上的概率 2.5%，不是巧合）。
                # 结账预热本身不引起掉线（同期 75 次，零相关），差别在于它不跑六步
                # ——所以多半是六步或它的失败收尾把会话作废了。掉了不马上修，下一次
                # 放货就得自己付那 20 秒登录。
                check_after = self.clock()
                try:
                    self.report(result, offer.title, offer.url)
                except Exception as e:
                    self.log(f'[购买] 结果通知失败：{e}')
        finally:
            self.buyer.stop()  # 只在创建 Playwright 的线程上关闭。
