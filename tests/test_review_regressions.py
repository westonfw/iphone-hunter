"""用户复核指出的入口、预热、门店配置和端点反馈回归。全程离线。"""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hunter.autobuy import AutoBuy
from hunter.fastpath import BLOCK_CODES, Blocked, EntryUnavailable
from hunter.apple import AppleClient, AVAIL_PATH, PICKUP_PATH, Stock, StorePickup
from hunter.monitor import StockWatcher, State
from hunter.pacing import Pacer
from hunter.purchase_worker import Offer, PurchaseWorker


class PurchaseEntryRegressions(unittest.TestCase):
    URL = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A'
    LOGIN = 'https://secure8.www.apple.com.cn/shop/signIn'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ab = AutoBuy({'pickup_store_numbers': ['R581']}, Path(tmp.name), log=Mock())
        self.page = Mock(url=self.URL)
        self.page.goto.side_effect = lambda url, **kw: setattr(self.page, 'url', url)
        self.ab._pick = Mock()
        self.ab._wait_add_button = Mock(return_value=(True, ''))
        self.ab._settle = Mock()
        watch = patch('hunter.autobuy.watch_checkout_block', return_value={})
        watch.start()
        self.addCleanup(watch.stop)

    def drive(self, warm=False):
        return self.ab._drive(Mock(), self.page, None if warm else self.URL, False)

    def assert_blocked(self, result, delay=120):
        self.assertFalse(result.ok)
        self.assertFalse(result.retriable)
        self.assertEqual(delay, result.retry_after)
        finished = threading.Event()
        buyer = SimpleNamespace(cfg={'preflight': False}, warm_alive=False,
                                order_placed=False, buy=Mock(return_value=result), stop=Mock())
        clock = [100.]
        w = PurchaseWorker(buyer, lambda *args: finished.set(), clock=lambda: clock[0], log=Mock())
        w.observe('P', [Offer('P', 'R001', 'store', 'url', 'title', 100)])
        w.start()
        try:
            self.assertTrue(finished.wait(1))
            self.assertEqual(100 + delay, w.cooldown_until)
            clock[0] = 116
            w.observe('P', [Offer('P', 'R002', 'store', 'url', 'title', 116)])
            self.assertIsNone(w._next())
            buyer.buy.assert_called_once()
        finally:
            w.close()

    def test_login_followed_by_web_entry_block_keeps_cooldown(self):
        def signin(page):
            page.url = 'https://www.apple.com.cn/shop/account/home'
            return True, ''
        self.ab._sign_in = Mock(side_effect=signin)
        self.ab._checkout_via_bag = Mock(side_effect=Blocked('429', 600))
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True}), \
             patch('hunter.fastpath.bag_to_checkout', side_effect=[self.LOGIN, EntryUnavailable()]) as entry:
            result = self.drive()
        self.assert_blocked(result, 600)
        self.assertEqual(2, entry.call_count)
        self.ab._checkout_via_bag.assert_called_once()

    def test_initial_web_entry_block_uses_same_cooldown_boundary(self):
        self.ab._checkout_via_bag = Mock(side_effect=Blocked('541'))
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True}), \
             patch('hunter.fastpath.bag_to_checkout', side_effect=EntryUnavailable()):
            self.assert_blocked(self.drive())

    def test_warm_clear_reloads_and_reselects_before_add(self):
        events = Mock()
        events.attach_mock(self.page.goto, 'goto')
        events.attach_mock(self.ab._pick, 'pick')
        events.attach_mock(self.page.locator.return_value.first.click, 'add')
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True, 'removed': 1}), \
             patch('hunter.fastpath.bag_to_checkout', return_value=''):
            self.drive(warm=True)
        self.assertEqual(['goto', 'pick', 'pick', 'add'], [c[0] for c in events.mock_calls])
        self.assertEqual(self.URL, events.mock_calls[0].args[0])
        self.assertEqual(['tradein', 'applecare'], [c.args[1] for c in self.ab._pick.call_args_list])

    def test_warm_kept_cart_does_not_reload_or_add(self):
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True, 'kept': True}), \
             patch('hunter.fastpath.bag_to_checkout', return_value=''):
            self.drive(warm=True)
        self.page.goto.assert_not_called()
        self.ab._pick.assert_not_called()
        self.page.locator.assert_not_called()

    def test_bag_read_blocked_in_both_paths_stops_before_add(self):
        for warm in (False, True):
            for status in BLOCK_CODES:
                with self.subTest(warm=warm, status=status):
                    self.page.url = self.URL
                    self.page.evaluate.return_value = {'status': status, 'retry_after': '300', 'cart': False}
                    with patch('hunter.fastpath.bag_to_checkout') as entry:
                        self.assert_blocked(self.drive(warm), 300)
                    self.page.locator.assert_not_called()
                    entry.assert_not_called()

    def test_bag_delete_blocked_stops_before_add(self):
        for status in BLOCK_CODES:
            with self.subTest(status=status):
                self.page.url = self.URL
                self.page.evaluate.side_effect = [
                    {'stk': 'token', 'cart': True, 'count': 1, 'items': ['item-a'],
                     'skus': ['OTHER'], 'qty': [1], 'origin': 'https://www.apple.com.cn'},
                    {'status': status, 'retry_after': '1800'}]
                self.assert_blocked(self.drive(), 1800)
                self.page.locator.assert_not_called()

    def test_preheat_cart_block_is_not_swallowed(self):
        self.ab.start = Mock()
        self.ab._page = self.page
        self.page.is_closed.return_value = False
        self.ab._ctx = Mock()
        self.ab._preflight_login = Mock(return_value='已登录')
        self.ab.bag_state = Mock(return_value={'items': 1})
        with patch('hunter.autobuy.goto_buy_page', return_value=True), \
             patch('hunter.fastpath.prepare_bag', side_effect=Blocked('429', 300)):
            with self.assertRaises(Blocked):
                self.ab._warm_inner(self.URL)
        self.assertFalse(self.ab.warmed)
        self.ab._ctx.new_page.return_value.close.assert_called_once()


class StoreConfigurationRegressions(unittest.TestCase):
    def test_number_formats_and_display_names_do_not_drop_number_match(self):
        for configured, expected in ((['R581', 'R359'], ['R581', 'R359']),
                                     ('r581, R359', ['R581', 'R359']),
                                     (None, ['R581', 'R359', 'R999'])):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as tmp:
                w = StockWatcher.__new__(StockWatcher)
                from hunter.autobuy import stores_of
                w.cfg = {'autobuy': {'pickup_store_numbers': configured,
                                     'pickup_stores': ['旧店名']}}
                w.autobuy = AutoBuy(w.cfg['autobuy'], Path(tmp), log=Mock())
                # 盯的和买的是同一份名单，都从 stores_of 来
                w.only_stores = stores_of(w.cfg)
                w.parts, w.note_of = ['P'], {}
                w.client = SimpleNamespace(observed_at={})
                w.state = State(Path(tmp) / 'state.json')
                w.purchase_worker, w.hit, w.log = Mock(), Mock(), Mock()
                w._buy_url = lambda _: 'url'
                stores = [StorePickup('P', n, '新门店名称', state=Stock.AVAILABLE)
                          for n in ['R581', 'R359', 'R999']]
                w._check_pickup('P', stores)
                offers = w.purchase_worker.observe.call_args.args[1]
                self.assertEqual(expected, [o.store for o in offers])


class PacerEndpointRegressions(unittest.TestCase):
    def make(self, pickup_on=True, cooling=False):
        w = StockWatcher.__new__(StockWatcher)
        w.round_no, w.avail_every = 0, 1
        w.parts, w.part_groups, w.location = ['PART'], {'g': ['PART']}, '200000'
        w.pickup_on, w.sprint, w.purchase_worker = pickup_on, False, None
        w.log, w.bc = Mock(), Mock()
        w._check_pickup, w._check_buyable = Mock(), Mock()
        w.wait = Mock(side_effect=KeyboardInterrupt)
        w.pacer = Pacer()
        w.pacer.scale = 2
        w.client = AppleClient()
        self.addCleanup(w.client.s.close)
        if cooling:
            w.client.breaker(PICKUP_PATH).trip()
        good = SimpleNamespace(status_code=200, headers={}, content=b'{}', history=[],
                               json=lambda: {'body': {'stores': []}}, raise_for_status=lambda: None)
        blocked = SimpleNamespace(status_code=541, headers={'Retry-After': '300'},
                                  content=b'blocked', history=[])
        w.client.s.get = Mock(side_effect=[good, blocked] if pickup_on and not cooling else [blocked])
        return w

    def test_pickup_success_auxiliary_block_only_trips_auxiliary_breaker(self):
        w = self.make()
        w.loop()
        self.assertEqual(0, w.pacer.blocks)
        self.assertEqual(1.75, w.pacer.scale)
        self.assertFalse(w.client.breaker(AVAIL_PATH).ready())
        self.assertTrue(w.client.breaker(PICKUP_PATH).ready())

    def test_pickup_cooling_and_auxiliary_block_does_not_report_success(self):
        w = self.make(cooling=True)
        w.loop()
        self.assertEqual(0, w.pacer.blocks)
        self.assertEqual(2, w.pacer.scale)
        self.assertFalse(w.client.breaker(AVAIL_PATH).ready())
        w._check_pickup.assert_not_called()

    def test_availability_is_primary_when_pickup_disabled(self):
        w = self.make(pickup_on=False)
        w.loop()
        self.assertEqual(1, w.pacer.blocks)
        self.assertEqual(4, w.pacer.scale)


class LoginVerificationRegressions(unittest.TestCase):
    """「页面说已登录、点进订单却要登录」——判据必须是订单页，不是账号入口。"""

    BASE = 'https://www.apple.com.cn'

    def page(self, landed, dom):
        p = Mock()
        p.url = landed
        p.goto.side_effect = lambda url, **kw: None
        p.evaluate.return_value = dom
        return p

    def test_account_link_alone_is_not_proof_of_login(self):
        # 登出状态下账号入口照样渲染：这正是用户撞到的误判。
        from hunter.checkout import login_state
        st = login_state(self.page('', {
            'acctLink': 'https://secure7.www.apple.com.cn/shop/account/home',
            'signInLink': '', 'signOutLink': '', 'signOutAutom': 0,
            'signOutWords': 0, 'signInWords': 0, 'secureHost': 'secure7',
            'rendered': 4000}))
        self.assertIsNot(True, st['signed_in'])

    def test_sign_out_affordance_is_proof(self):
        from hunter.checkout import login_state
        st = login_state(self.page('', {
            'acctLink': 'https://secure7.www.apple.com.cn/shop/account/home',
            'signInLink': '', 'signOutLink': '/shop/signOut', 'signOutAutom': 0,
            'signOutWords': 1, 'signInWords': 2, 'secureHost': 'secure7',
            'rendered': 9000}))
        self.assertIs(True, st['signed_in'])
        self.assertEqual('secure7', st['secure_host'])

    def test_order_page_bounced_to_idmsa_is_hard_negative(self):
        from hunter.checkout import verify_signed_in
        p = self.page('https://idmsa.apple.com.cn/appleauth/auth/signin', {})
        st = verify_signed_in(p, self.BASE, log=Mock())
        self.assertIs(False, st['signed_in'])
        self.assertIn('/shop/order/list', p.goto.call_args.args[0])

    def test_unreadable_order_page_is_unknown_never_signed_in(self):
        from hunter.checkout import verify_signed_in
        p = self.page('https://www.apple.com.cn/shop/order/list', {
            'acctLink': '', 'signInLink': '', 'signOutLink': '', 'signOutAutom': 0,
            'signOutWords': 0, 'signInWords': 0, 'secureHost': '', 'rendered': 12})
        self.assertIsNone(verify_signed_in(p, self.BASE, log=Mock())['signed_in'])

    def preflight(self, landed_url, goto_raises=False):
        """跑一次兜底登录检查，返回 (AutoBuy, 结论)。"""
        with tempfile.TemporaryDirectory() as tmp:
            ab = AutoBuy({}, Path(tmp), log=Mock())
            ab._settle = Mock()
            ab._sign_in = Mock(return_value=(False, '没配密码'))
            p = Mock()
            p.url = landed_url
            if goto_raises:
                p.goto.side_effect = RuntimeError('boom')
            return ab, ab._preflight_login(p), p

    def test_the_preflight_does_not_open_the_order_page(self):
        """订单页那一趟是多余的：账号页没登录就会跳登录页，那本身就是判据。
        每 5 分钟一轮的保活里，多翻一趟就是把请求数白白翻一倍。"""
        ab, note, p = self.preflight('https://www.apple.com.cn/shop/account/home')
        went = [c.args[0] for c in p.goto.call_args_list]
        self.assertTrue(went, '一次导航都没有')
        self.assertFalse([u for u in went if '/shop/order' in u],
                         f'还在翻订单页：{went}')

    def test_the_account_page_accepting_us_is_proof(self):
        ab, note, _ = self.preflight('https://www.apple.com.cn/shop/account/home')
        self.assertIs(True, ab.signed_in)
        self.assertIn('已登录', note)

    def test_being_bounced_to_sign_in_is_a_hard_negative(self):
        ab, note, _ = self.preflight('https://idmsa.apple.com.cn/appleauth/auth/signin')
        self.assertIs(False, ab.signed_in)
        self.assertNotIn('已登录', note)

    def test_an_unreachable_account_page_is_unknown_never_signed_in(self):
        """**判不准就说判不准**，绝不报「已登录」——那正是上次踩的坑。"""
        ab, note, _ = self.preflight('', goto_raises=True)
        self.assertIsNone(ab.signed_in)
        self.assertNotIn('已登录', note)

    def test_login_expiry_is_read_from_cookie_not_process_uptime(self):
        import time as _t
        with tempfile.TemporaryDirectory() as tmp:
            ab = AutoBuy({}, Path(tmp), log=Mock())
            ab._ctx = Mock()
            ab._ctx.cookies.return_value = [
                {'name': 'DESabc', 'domain': '.idmsa.apple.com.cn',
                 'expires': _t.time() + 2 * 86400},
                {'name': 'as_sfa', 'domain': '.apple.com.cn',
                 'expires': _t.time() + 180 * 86400},
            ]
            self.assertAlmostEqual(2.0, ab.login_days_left(), places=1)

    def test_login_failure_wakes_through_quiet_hours(self):
        worker = PurchaseWorker.__new__(PurchaseWorker)
        worker.report = Mock()
        worker.buyer = SimpleNamespace(cfg={}, signed_in=False,
                                       prepare=lambda: '⚠️ 未登录',
                                       login_days_left=lambda: None)
        worker._preflight()
        self.assertTrue(worker.report.call_args.args[0].wake)


class HotStockRegressions(unittest.TestCase):
    def test_seeing_stock_boosts_polling_but_backoff_still_wins(self):
        clock = [0.0]
        pc = Pacer(base_interval=30, min_interval=4, clock=lambda: clock[0])
        self.assertEqual(30.0, pc.target())
        pc.boost(180)
        self.assertEqual(4.0, pc.target())
        pc.on_blocked()
        self.assertGreater(pc.target(), 4.0)   # 冲刺不许顶着限流打
        clock[0] = 181.0
        self.assertFalse(pc.boosting())

    @staticmethod
    def _search(avail):
        """造一个 search 响应：结账侧的门店库存长 2026-09-19 手录 HAR 那个样子。"""
        return {'retailStores': [
            {'storeId': sid,
             'availability': {'availableNowForAllLines': ok,
                              'storeAvailability': '店内取货' if ok else '目前不可取货'}}
            for sid, ok in avail]}

    def test_rotation_skips_straight_to_the_store_checkout_says_has_stock(self):
        """盲试每家要花 10s/家。结账侧库存第一次 search 就全带回来了，直接挑对的。"""
        from hunter.fastpath import FastCheckout, Stalled
        fc = FastCheckout(store='R001', stores=['R001', 'R002', 'R003'],
                          id_last4='1234', last_name='张', first_name='三', log=Mock())
        seen = []
        avail = self._search([('R001', False), ('R002', False), ('R003', True)])

        def step2(page):
            seen.append(fc.store)
            return avail

        fc.step2_store = step2
        fc.take_slot = lambda data: ({} if fc.store == 'R003'
                                     else (_ for _ in ()).throw(Stalled('排不上')))
        fc.select_store(Mock())
        self.assertEqual(['R001', 'R003'], seen)      # R002 结账说没货，不浪费一次 10s
        self.assertEqual('R003', fc.store_used)

    def test_checkout_saying_every_store_is_out_stops_immediately(self):
        from hunter.fastpath import FastCheckout, Stalled
        fc = FastCheckout(store='R001', stores=['R001', 'R002'],
                          id_last4='1234', last_name='张', first_name='三', log=Mock())
        calls = []
        avail = self._search([('R001', False), ('R002', False)])
        fc.step2_store = lambda page: (calls.append(1), avail)[1]
        fc.take_slot = lambda data: (_ for _ in ()).throw(Stalled('排不上'))
        with self.assertRaises(Stalled) as cm:
            fc.select_store(Mock())
        self.assertIn('目前不可取货', str(cm.exception))
        self.assertEqual(1, len(calls))               # 只发了一次 search

    def test_old_flow_can_still_be_forced_through(self):
        from hunter.fastpath import FastCheckout, Stalled
        fc = FastCheckout(store='R001', stores=['R001'], require_slot=False,
                          id_last4='1234', last_name='张', first_name='三', log=Mock())
        fc.step2_store = lambda page: {}
        fc.take_slot = lambda data: (_ for _ in ()).throw(Stalled('没有时段模块'))
        fc.select_store(Mock())                        # 不抛
        self.assertEqual('R001', fc.store_used)
        self.assertEqual({}, fc.slot)


class CriticalPathRegressions(unittest.TestCase):
    """抢购关键路径上的固定等待必须是条件等待，而且只在真要用页面时才等。"""

    def test_option_wait_returns_as_soon_as_sections_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            ab = AutoBuy({}, Path(tmp), log=Mock())
            page, loc = Mock(), Mock()
            loc.count.side_effect = [0, 0, 3]
            page.locator.return_value = loc
            ab._await_options(page, cap_ms=5000, step_ms=0)
            self.assertEqual(3, loc.count.call_count)          # 出现就走，不睡满
            page.locator.assert_called_once_with(ab.SEL_READY)  # locator 只建一次

    def test_option_wait_never_settles_for_the_add_to_cart_button(self):
        """加购按钮是服务端直出的，一到 domcontentloaded 就在；必选项单选框要等
        JS 水合。拿按钮当就绪判据 = 一秒返回、_pick 选空、按钮一直灰着。
        2026-09-19 22:44 / 22:47 两单就是这么丢的。"""
        self.assertNotIn('add-to-cart', AutoBuy.SEL_READY)
        self.assertEqual(2, AutoBuy.SEL_READY.count('input[type=radio]'))
        self.assertIn('tradein', AutoBuy.SEL_READY)
        self.assertIn('applecare', AutoBuy.SEL_READY)

    def test_option_wait_warns_when_sections_never_hydrate(self):
        """等满了还没水合就必须喊出来，否则 _pick 静默选空，报错指向完全错的方向。"""
        with tempfile.TemporaryDirectory() as tmp:
            log = Mock()
            ab = AutoBuy({}, Path(tmp), log=log)
            page, loc = Mock(), Mock()
            loc.count.return_value = 0
            page.locator.return_value = loc
            ab._await_options(page, cap_ms=0, step_ms=0)
            self.assertTrue(any('单选框' in str(c) for c in log.call_args_list),
                            log.call_args_list)

    def test_option_wait_does_not_spin_when_count_is_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ab = AutoBuy({}, Path(tmp), log=Mock())
            page, loc = Mock(), Mock()
            loc.count.return_value = object()      # 问不出数量
            page.locator.return_value = loc
            ab._await_options(page, cap_ms=5000, step_ms=0)
            self.assertEqual(1, loc.count.call_count)

    def test_bag_wait_returns_as_soon_as_item_lands(self):
        from hunter.fastpath import wait_for_bag_count
        page = Mock()
        page.evaluate.side_effect = [{'count': 0}, {'count': 1}]
        wait_for_bag_count(page, cap_ms=5000, step_ms=0)
        self.assertEqual(2, page.evaluate.call_count)

    def test_bag_wait_gives_up_when_state_is_not_a_model(self):
        from hunter.fastpath import wait_for_bag_count
        page = Mock()
        page.evaluate.return_value = None
        wait_for_bag_count(page, cap_ms=5000, step_ms=0)
        self.assertEqual(1, page.evaluate.call_count)

    def test_preflight_clears_the_bag_so_the_hot_path_has_nothing_to_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            ab = AutoBuy({}, Path(tmp), log=Mock())
            ab.bagged_part = 'MJT84CH/A'
            with patch('hunter.fastpath.prepare_bag',
                       return_value={'ok': True, 'removed': 2}) as prep:
                note = ab.preflight_clear_bag(Mock())
            # want_part 留空 = 无条件清空，别把「袋里正好是上次那台」当成可以留
            self.assertEqual('', prep.call_args.kwargs['want_part'])
            self.assertIn('2', note)
            self.assertEqual('', ab.bagged_part)


if __name__ == '__main__':
    unittest.main()


class TargetChoiceAndQuotaRegressions(unittest.TestCase):
    """怎么挑目标；买够了才收工；结果不明的单绝不再买。"""

    def worker(self, **kw):
        w = PurchaseWorker.__new__(PurchaseWorker)
        w.offers, w.attempts, w.epochs = {}, {}, {}
        w.tried, w.burned, w.polled, w.had = {}, set(), {}, set()
        w.max_age, w.max_attempts, w.retry_delay = 90.0, 2, 15.0
        w.cooldown_until, w.log = 0.0, Mock()
        w.clock = kw.get('clock', lambda: 0.0)
        return w

    @staticmethod
    def offer(part, store, observed=0.0, pri=(0, 0)):
        return Offer(part, store, store, 'url', part, observed, pri)

    def test_a_failed_model_gives_way_to_the_next_one(self):
        """不再死守一个型号。加购变成 388ms 的一个 GET 之后，换型号的净代价只剩
        0.4 秒；而「拿不到取货时段」是型号+门店层面的结论，select_store 已经在
        一次尝试里把该型号所有有货门店试遍了——再守着它重试多半还是同样结果。"""
        now = [0.0]
        w = self.worker(clock=lambda: now[0])
        w.offers = {('A', 'R1'): self.offer('A', 'R1', pri=(0, 0)),
                    ('B', 'R1'): self.offer('B', 'R1', pri=(1, 0))}
        self.assertEqual('A', w._next().part)
        w.attempts['A'] = (1, 0.0)                   # A 刚失败
        now[0] = 5.0                                 # 还没到 A 的重试间隔
        self.assertEqual('B', w._next().part)        # 立刻换 B，不干等

    def test_config_order_decides_who_goes_first(self):
        w = self.worker()
        w.offers = {('B', 'R1'): self.offer('B', 'R1', pri=(1, 0)),
                    ('A', 'R1'): self.offer('A', 'R1', pri=(0, 0))}
        self.assertEqual('A', w._next().part)

    def test_attempts_are_counted_per_model_not_per_store(self):
        """一次尝试内部就把该型号的门店试遍了，按门店计数会把同一轮重复算几次。"""
        w = self.worker()
        w.offers = {('A', 'R1'): self.offer('A', 'R1', pri=(0, 0)),
                    ('A', 'R2'): self.offer('A', 'R2', pri=(0, 1))}
        w.attempts['A'] = (2, 0.0)                   # 型号 A 的次数已用尽
        self.assertIsNone(w._next())                 # R2 不能再给它一次

    def test_all_in_stock_stores_of_one_model_go_out_together(self):
        w = self.worker()
        w.offers = {('A', 'R1'): self.offer('A', 'R1', pri=(0, 0)),
                    ('A', 'R2'): self.offer('A', 'R2', pri=(0, 1)),
                    ('B', 'R9'): self.offer('B', 'R9', pri=(1, 0))}
        self.assertEqual(['R1', 'R2'], [o.store for o in w._targets('A', 0.0)])

    def test_a_brand_new_store_for_a_model_earns_a_fresh_try(self):
        """「R001 售罄」不该判定整个型号没戏——别家店冒出货来是新机会。"""
        w = self.worker()
        w.log = Mock()
        w.cv = __import__('threading').Condition()
        w.attempts['A'], w.tried['A'] = (2, 0.0), {'R1'}
        w.offers = {('A', 'R1'): self.offer('A', 'R1')}
        w.observe('A', [self.offer('A', 'R1'), self.offer('A', 'R2')])
        self.assertNotIn('A', w.attempts)            # 重试次数重新计

    def test_unknown_result_still_blocks_every_further_purchase(self):
        from hunter.purchase_guard import PurchaseGuard, PendingOrder
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with PurchaseGuard(root, max_orders=2) as g:
                g.part = 'A'
                g.submitted(store='R1', part='A')
                g.finish('unknown', 'u')
            with self.assertRaises(PendingOrder):
                with PurchaseGuard(root, max_orders=2):
                    pass

    def test_confirmed_order_counts_and_lets_the_second_one_through(self):
        from hunter.purchase_guard import PurchaseGuard, QuotaReached
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for n in ('A', 'B'):
                with PurchaseGuard(root, max_orders=2) as g:
                    g.submitted(store='R1', part=n)
                    g.finish('confirmed', f'/{n}')
            with self.assertRaises(QuotaReached):
                with PurchaseGuard(root, max_orders=2):
                    pass

    def test_default_quota_is_one_so_two_never_happens_by_accident(self):
        from hunter.purchase_guard import PurchaseGuard, QuotaReached
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with PurchaseGuard(root) as g:
                g.submitted(store='R1', part='A')
                g.finish('confirmed', '/A')
            with self.assertRaises(QuotaReached):
                with PurchaseGuard(root):
                    pass

    def test_confirmed_is_counted_once_even_if_finish_repeats(self):
        from hunter.purchase_guard import PurchaseGuard
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with PurchaseGuard(root, max_orders=2) as g:
                g.submitted(store='R1', part='A')
                g.finish('confirmed', '/A')
                g.finish('confirmed', '/A')
                self.assertEqual(1, len(g.bought))

    def test_resolve_does_not_count_a_phone_unless_told_so(self):
        from hunter.purchase_guard import PurchaseGuard
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with PurchaseGuard(root, max_orders=2) as g:
                g.submitted(store='R1', part='A')
                g.finish('unknown', '/A')
            with PurchaseGuard(root, inspect=True) as g:
                g.resolve()
                self.assertEqual([], g.bought)
            with PurchaseGuard(root, inspect=True) as g:
                g.resolve(bought=True)
                self.assertEqual(1, len(g.bought))

    def test_keepalive_is_never_blocked_by_the_quota(self):
        """买够了之后保活还得继续跑，不然登录态会烂掉。"""
        from hunter.purchase_guard import PurchaseGuard
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with PurchaseGuard(root) as g:
                g.submitted(store='R1', part='A')
                g.finish('confirmed', '/A')
            with PurchaseGuard(root, check_orders=False):
                pass


class SameModelRetryRegressions(unittest.TestCase):
    """同型号重试：袋里还在就别再加载产品页——那是关键路径上最大的一块。"""

    URL = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A'

    def buyer(self, **cfg):
        tmp = tempfile.mkdtemp()
        ab = AutoBuy({'pickup_store_numbers': ['R581'], **cfg}, Path(tmp), log=Mock())
        ab._pick, ab._settle = Mock(), Mock()
        ab._wait_add_button = Mock(return_value=(True, ''))
        ab._await_options = Mock(return_value=0.0)
        return ab

    def page(self):
        p = Mock()
        p.url = self.URL
        p.goto.side_effect = lambda u, **kw: setattr(p, 'url', u)
        return p

    def drive(self, ab, page, bag):
        with patch('hunter.fastpath.prepare_bag', return_value=bag), \
             patch('hunter.fastpath.bag_to_checkout', return_value=''), \
             patch('hunter.autobuy.watch_checkout_block', return_value={}), \
             patch('hunter.fastpath.wait_for_bag_count', return_value=0.0):
            return ab._drive(Mock(), page, self.URL, False)

    def test_first_attempt_goes_straight_to_the_product_page(self):
        """第一次命中不能为了问购物袋多绕一趟——那是最要紧的一次机会。"""
        ab, page = self.buyer(), self.page()
        self.drive(ab, page, {'ok': True, 'kept': False})
        self.assertEqual([self.URL], [c.args[0] for c in page.goto.call_args_list])

    def test_same_model_retry_skips_the_product_page_when_the_bag_still_holds_it(self):
        ab, page = self.buyer(), self.page()
        self.drive(ab, page, {'ok': True, 'kept': False})     # 第一轮，加过购
        page.goto.reset_mock()
        self.drive(ab, page, {'ok': True, 'kept': True})      # 第二轮，袋里还在
        gone = [c.args[0] for c in page.goto.call_args_list]
        self.assertEqual([], gone, '已经在主站、袋里也对，一趟导航都不该有')

    def test_retry_falls_back_to_the_product_page_when_the_bag_is_wrong(self):
        ab, page = self.buyer(), self.page()
        self.drive(ab, page, {'ok': True, 'kept': False})
        page.goto.reset_mock()
        self.drive(ab, page, {'ok': True, 'kept': False, 'removed': 1})
        gone = [c.args[0] for c in page.goto.call_args_list]
        self.assertIn(self.URL, gone)                         # 赌错了就老实加载
        ab._pick.assert_called()                              # 而且必选项要重选

    def test_a_different_model_never_takes_the_fast_path(self):
        """换型号时袋里那台是上一轮的，绝不能赌。"""
        ab, page = self.buyer(), self.page()
        self.drive(ab, page, {'ok': True, 'kept': False})
        page.goto.reset_mock()
        other = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJYA4CH/A'
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True, 'kept': False}), \
             patch('hunter.fastpath.bag_to_checkout', return_value=''), \
             patch('hunter.autobuy.watch_checkout_block', return_value={}), \
             patch('hunter.fastpath.wait_for_bag_count', return_value=0.0):
            ab._drive(Mock(), page, other, False)
        self.assertEqual([other], [c.args[0] for c in page.goto.call_args_list])

    def test_a_stale_attempt_never_takes_the_fast_path(self):
        """隔太久了就别赌——袋子可能早被别处清了，多绕一趟反而更慢。"""
        ab, page = self.buyer(bag_trust_seconds=0), self.page()
        self.drive(ab, page, {'ok': True, 'kept': False})
        page.goto.reset_mock()
        self.drive(ab, page, {'ok': True, 'kept': True})
        self.assertEqual([self.URL], [c.args[0] for c in page.goto.call_args_list])


class BoostBudgetRegressions(unittest.TestCase):
    """看到货就该一直快，而不是快 100 秒然后被预算按回 16 秒。"""

    def pacer(self, boost_budget):
        t = [0.0]
        p = Pacer(base_interval=60, min_interval=4, budget_per_hour=220, burst=20,
                  boost_budget_per_hour=boost_budget, clock=lambda: t[0],
                  sleeper=lambda d: t.__setitem__(0, t[0] + d), log=Mock())
        return p, t

    def poll(self, p, t, horizon=300.0, rounds=400):
        """模拟一波持续放货：每轮都看到货、都发一个请求。"""
        marks = []
        for _ in range(rounds):
            if t[0] >= horizon:
                break
            p.boost()
            p.acquire()
            marks.append(t[0])
            t[0] += p.next_delay()
        return [marks[i] - marks[i - 1] for i in range(1, len(marks))]

    def test_sustained_restock_keeps_the_sprint_cadence(self):
        p, t = self.pacer(900)
        gaps = self.poll(p, t)
        # 4s 的节奏 ↔ 900/h 正好平衡，所以能一直保持
        self.assertLess(max(gaps), 6.0, f'最大间隔 {max(gaps):.1f}s')
        self.assertGreater(len(gaps), 60)

    def test_without_its_own_budget_the_sprint_dies_after_the_burst(self):
        """这一条钉住旧行为，说明这个预算不是可有可无的。"""
        p, t = self.pacer(220)
        gaps = self.poll(p, t)
        self.assertGreater(max(gaps), 15.0)          # 被补充速率按回 16s
        self.assertLess(len(gaps), 45)

    def test_budget_goes_back_to_normal_when_the_sprint_ends(self):
        p, t = self.pacer(900)
        p.boost(60)
        self.assertAlmostEqual(900.0, p.bucket.rate * 3600, places=3)
        t[0] = 61.0
        p.next_delay()
        self.assertAlmostEqual(220.0, p.bucket.rate * 3600, places=3)

    def test_switching_rate_does_not_conjure_tokens(self):
        """换速率前必须按旧速率补到此刻，否则这段时间会按新速率重算。"""
        p, t = self.pacer(900)
        p.bucket.tokens = 0.0
        t[0] = 100.0                                  # 按 220/h 攒了 6.1 个
        p.boost()
        self.assertAlmostEqual(100 * 220 / 3600, p.bucket.tokens, places=2)

    def test_acquire_always_makes_progress(self):
        """浮点误差曾让 acquire 用越来越小的间隔空转、永不收敛。"""
        p, t = self.pacer(220)
        p.bucket.tokens = 0.0
        p.acquire()
        self.assertGreater(t[0], 0.0)
        p.bucket.tokens = 1 - 1e-12                   # 差一丁点儿
        before = t[0]
        p.acquire()
        self.assertGreaterEqual(t[0] - before, 0.0)   # 不死循环就算过


class FastAddToCartRegressions(unittest.TestCase):
    """加购其实是一个 GET，atbtoken 就在 cookie as_atb 里。实测 388ms vs 11~28s。"""

    URL = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A'
    PART = 'MJT84CH/A'

    def ctx(self, value='1.0|MjAyNg|' + 'a' * 40):
        c = Mock()
        c.cookies.return_value = [{'name': 'as_dc', 'value': 'x'},
                                  {'name': 'as_atb', 'value': value}]
        return c

    def buyer(self, **cfg):
        ab = AutoBuy({'pickup_store_numbers': ['R581'], **cfg},
                     Path(tempfile.mkdtemp()), log=Mock())
        ab._pick, ab._settle = Mock(), Mock()
        ab._wait_add_button = Mock(return_value=(True, ''))
        ab._await_options = Mock(return_value=0.0)
        return ab

    def page(self):
        p = Mock()
        p.url = self.URL
        p.goto.side_effect = lambda u, **kw: setattr(p, 'url', u)
        return p

    def drive(self, ab, page, ctx, bags):
        """bags: prepare_bag 依次返回的结果。"""
        with patch('hunter.fastpath.prepare_bag', side_effect=list(bags)), \
             patch('hunter.fastpath.bag_to_checkout', return_value=''), \
             patch('hunter.autobuy.watch_checkout_block', return_value={}), \
             patch('hunter.fastpath.wait_for_bag_count', return_value=0.0):
            return ab._drive(ctx, page, self.URL, False)

    # ---------- URL 与 token ----------

    def test_token_is_the_last_segment_of_the_cookie(self):
        from hunter.fastpath import atb_token
        self.assertEqual('a' * 40, atb_token(self.ctx()))

    def test_missing_cookie_yields_no_token(self):
        from hunter.fastpath import atb_token
        c = Mock(); c.cookies.return_value = [{'name': 'as_dc', 'value': 'x'}]
        self.assertEqual('', atb_token(c))
        self.assertEqual('', atb_token(None))

    def test_unreadable_jar_never_raises(self):
        from hunter.fastpath import atb_token
        c = Mock(); c.cookies.side_effect = RuntimeError('boom')
        self.assertEqual('', atb_token(c))

    def test_url_matches_the_shape_recorded_in_the_har(self):
        from hunter.fastpath import atb_add_url
        u = atb_add_url(self.URL, self.PART, 'tok')
        self.assertIn('/shop/buy-iphone/iphone-18-pro/mjt84ch/a?', u)   # 路径小写
        self.assertIn('product=MJT84CH%2FA', u)                          # 参数大写
        for want in ('purchaseOption=fullPrice', 'acpart=none', 'atbtoken=tok',
                     'igt=true', 'add-to-cart=add-to-cart', 'step=select'):
            self.assertIn(want, u)

    # ---------- 主流程 ----------

    def test_successful_fast_add_never_loads_the_product_page(self):
        ab, page = self.buyer(), self.page()
        self.drive(ab, page, self.ctx(),
                   [{'ok': True, 'kept': False},        # 购物袋页上：空袋
                    {'ok': True, 'kept': True}])        # 快加购后复核：进袋了
        gone = [c.args[0] for c in page.goto.call_args_list]
        # 页面本来就在主站上，连购物袋页都不用去——袋子是 fetch 出来的
        self.assertEqual(1, len(gone), gone)
        self.assertIn('add-to-cart=add-to-cart', gone[0])
        self.assertNotIn(self.URL, gone)                 # 产品页一次都没碰
        ab._pick.assert_not_called()                     # 必选项也不用点
        page.locator.assert_not_called()                 # 更没有点加购按钮

    def test_silent_failure_falls_back_to_the_product_page(self):
        """token 用过一次就作废，再发照样 200、袋子纹丝不动——只能靠复核发现。"""
        ab, page = self.buyer(), self.page()
        self.drive(ab, page, self.ctx(),
                   [{'ok': True, 'kept': False},
                    {'ok': True, 'kept': False}])       # 复核：没进袋
        gone = [c.args[0] for c in page.goto.call_args_list]
        self.assertTrue(any('add-to-cart=add-to-cart' in g for g in gone))
        self.assertIn(self.URL, gone)                    # 老实退回产品页
        ab._pick.assert_called()

    def test_no_token_goes_straight_to_the_product_page(self):
        """读不到 token 就别为了一个用不上的快加购白跑一趟购物袋页。"""
        c = Mock(); c.cookies.return_value = []
        ab, page = self.buyer(), self.page()
        self.drive(ab, page, c, [{'ok': True, 'kept': False}])
        self.assertEqual([self.URL], [x.args[0] for x in page.goto.call_args_list])

    # ---------- 安全闸 ----------

    def test_non_default_options_never_take_the_fast_path(self):
        """purchaseOption=fullPrice / acpart=none 把「不折抵 + 不加 AppleCare」
        写死在了 URL 里。配置不是这两个还走这条路，就是静默买成别的条件。"""
        for cfg in ({'trade_in': '折抵换购'}, {'applecare': 'AppleCare+ 两年'},
                    {'fast_add_to_cart': False}):
            with self.subTest(**cfg):
                ab, page = self.buyer(**cfg), self.page()
                self.drive(ab, page, self.ctx(), [{'ok': True, 'kept': False}])
                gone = [x.args[0] for x in page.goto.call_args_list]
                self.assertFalse([g for g in gone if 'add-to-cart=' in g], gone)
                self.assertIn(self.URL, gone)

    def test_rehearsal_never_adds_anything(self):
        ab, page = self.buyer(), self.page()
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True}):
            ab._drive(self.ctx(), page, self.URL, True)
        gone = [x.args[0] for x in page.goto.call_args_list]
        self.assertFalse([g for g in gone if 'add-to-cart=' in g], gone)


class CheckoutWarmupRegressions(unittest.TestCase):
    """空闲期把结账那道登录墙撞掉。实测墙一次付清、后面全免（跨清袋跨型号）。"""

    URL = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A'
    PART = 'MJT84CH/A'

    def buyer(self, **cfg):
        ab = AutoBuy({'preflight_warm_checkout': True, **cfg},
                     Path(tempfile.mkdtemp()), log=Mock())
        ab._settle, ab._sign_in = Mock(), Mock(return_value=(True, ''))
        ab._enter_checkout = Mock()
        ab._ctx = Mock()
        ab._ctx.cookies.return_value = [{'name': 'as_atb', 'value': '1.0|x|' + 'b' * 40}]
        return ab

    def page(self, url=None):
        p = Mock()
        p.url = url or 'https://secure7.www.apple.com.cn/shop/checkout'
        p.is_closed.return_value = False
        p.goto.side_effect = lambda u, **kw: setattr(p, 'url', u)
        return p

    def warm(self, ab, page, bag, landed=None):
        ab._enter_checkout.return_value = self.page(landed or
                                                    'https://secure7.www.apple.com.cn/shop/checkout')
        with patch('hunter.fastpath.prepare_bag', return_value=bag):
            return ab.warm_checkout_session(ab._ctx, page, self.URL)

    def test_it_adds_enters_checkout_and_clears_the_login_wall(self):
        ab, page = self.buyer(), self.page()
        note = self.warm(ab, page, {'ok': True, 'kept': True},
                         landed='https://secure7.www.apple.com.cn/shop/signIn')
        self.assertIn('撞掉', note)
        ab._sign_in.assert_called_once()
        self.assertTrue(any('add-to-cart=add-to-cart' in c.args[0]
                            for c in page.goto.call_args_list))

    def test_no_wall_is_reported_as_already_ready(self):
        ab, page = self.buyer(), self.page()
        note = self.warm(ab, page, {'ok': True, 'kept': True})
        self.assertIn('本来就没墙', note)
        ab._sign_in.assert_not_called()

    def test_a_failed_probe_add_stops_before_entering_checkout(self):
        ab, page = self.buyer(), self.page()
        note = self.warm(ab, page, {'ok': True, 'kept': False})
        self.assertIn('没进袋', note)
        ab._enter_checkout.assert_not_called()

    def test_no_token_seeds_the_cookie_by_loading_the_product_page(self):
        """全新 profile 没逛过产品页，as_atb 还没种。每轮保活都在这儿放弃的话，
        放货时加购要走 11~28 秒的产品页而不是 388ms 的接口——2026-09-20 机器 B
        就是这么跑了一晚上。空闲期不差这一趟：加载一次产品页把它种上。"""
        ab, page = self.buyer(), self.page()
        ab._settle = Mock()
        calls = {'n': 0}

        def cookies():
            calls['n'] += 1
            return [] if calls['n'] == 1 else [
                {'name': 'as_atb', 'value': '1.0|x|deadbeef', 'domain': '.apple.com.cn'}]

        ab._ctx.cookies.side_effect = cookies
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True, 'kept': True}):
            ab.warm_checkout_session(ab._ctx, page, self.URL)
        self.assertEqual(self.URL, page.goto.call_args_list[0].args[0],
                         '没去加载产品页种 cookie')
        ab._enter_checkout.assert_called_once()

    def test_still_no_token_after_seeding_means_no_warmup(self):
        """种了也没有，那才是真读不到——说清楚，别再往下走。"""
        ab, page = self.buyer(), self.page()
        ab._settle = Mock()
        ab._ctx.cookies.return_value = []
        note = ab.warm_checkout_session(ab._ctx, page, self.URL)
        self.assertIn('atbtoken', note)
        self.assertIn('仍没有', note)
        self.assertEqual(1, page.goto.call_count)       # 只种一次，不反复刷产品页
        ab._enter_checkout.assert_not_called()

    def test_rate_limit_propagates_so_the_cooldown_applies(self):
        """限流不能被吞掉——吞了就会每 10 分钟去续一次封禁。"""
        from hunter.fastpath import Blocked
        ab, page = self.buyer(), self.page()
        ab._enter_checkout.side_effect = Blocked('541', 0)
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True, 'kept': True}):
            with self.assertRaises(Blocked):
                ab.warm_checkout_session(ab._ctx, page, self.URL)

    def test_it_is_off_unless_asked_for(self):
        """它会往购物袋里加东西，默认必须是关的。"""
        self.assertFalse(AutoBuy({}, Path(tempfile.mkdtemp()), log=Mock()).warm_checkout)

    def test_prepare_warms_before_clearing_the_bag(self):
        """顺序要紧：先撞墙（会加一台），再清袋。反过来袋里会留东西。"""
        ab = self.buyer()
        ab._preflight_login = Mock(return_value='已登录')
        ab.signed_in = True
        ab.start = Mock()
        ab._login_page = self.page('https://www.apple.com.cn/shop/bag')
        order = []

        def warm(*a):
            order.append('warm')
            ab.signed_in = True          # 真方法会设它，固件也得设
            return '撞掉了'

        ab.warm_checkout_session = warm
        ab.preflight_clear_bag = Mock(side_effect=lambda *a: order.append('clear') or '清空了')
        ab.prepare(self.URL)
        self.assertEqual(['warm', 'clear'], order)


class CheckoutTabHygieneRegressions(unittest.TestCase):
    """结账页有「5 分钟不操作就超时」的计时器，预热完必须收拾干净。"""

    URL = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A'
    BAG = 'https://www.apple.com.cn/shop/bag'

    def page(self, url):
        p = Mock()
        p.url = url
        p.is_closed.return_value = False
        p.goto.side_effect = lambda u, **kw: setattr(p, 'url', u)
        return p

    def buyer(self, **cfg):
        ab = AutoBuy({'preflight_warm_checkout': True, **cfg},
                     Path(tempfile.mkdtemp()), log=Mock())
        ab._settle, ab._sign_in = Mock(), Mock(return_value=(True, ''))
        ab._ctx = Mock()
        ab._ctx.cookies.return_value = [{'name': 'as_atb', 'value': '1.0|x|' + 'c' * 40}]
        return ab

    def test_a_tab_opened_by_the_bag_path_is_closed_again(self):
        """_checkout_via_bag 会另开一个标签并返回它。不收掉就一直停在结账页上
        滴答，过会儿弹超时——用户会看见。"""
        ab = self.buyer()
        mine = self.page(self.BAG)
        spawned = self.page('https://secure7.www.apple.com.cn/shop/checkout')
        ab._ctx.pages = [mine]

        def enter(ctx, page, part):
            ab._ctx.pages = [mine, spawned]      # 模拟新标签冒出来
            return spawned

        ab._enter_checkout = enter
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True, 'kept': True}):
            ab.warm_checkout_session(ab._ctx, mine, self.URL)
        spawned.close.assert_called_once()
        mine.close.assert_not_called()

    def test_our_own_page_is_taken_off_the_checkout_page(self):
        ab = self.buyer()
        mine = self.page(self.BAG)
        ab._ctx.pages = [mine]
        ab._enter_checkout = lambda ctx, page, part: page   # 同一页导航过去
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True, 'kept': True}):
            ab.warm_checkout_session(ab._ctx, mine, self.URL)
        self.assertEqual(self.BAG, mine.url)      # 最后落在购物袋页，不是结账页

    def test_cleanup_still_happens_when_entering_checkout_blows_up(self):
        ab = self.buyer()
        mine = self.page(self.BAG)
        spawned = self.page('https://secure7.www.apple.com.cn/shop/checkout')
        ab._ctx.pages = [mine]

        def enter(ctx, page, part):
            ab._ctx.pages = [mine, spawned]
            raise RuntimeError('boom')

        ab._enter_checkout = enter
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True, 'kept': True}):
            note = ab.warm_checkout_session(ab._ctx, mine, self.URL)
        self.assertIn('进结账失败', note)
        spawned.close.assert_called_once()

    def test_unreadable_page_list_never_breaks_the_warmup(self):
        ab = self.buyer()
        mine = self.page(self.BAG)
        type(ab._ctx).pages = property(lambda _: (_ for _ in ()).throw(RuntimeError()))
        ab._enter_checkout = lambda ctx, page, part: page
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True, 'kept': True}):
            ab.warm_checkout_session(ab._ctx, mine, self.URL)   # 不抛
        del type(ab._ctx).pages


class OrderPageProbeRegressions(unittest.TestCase):
    """结账预热是比订单页更硬的登录证据——开着它就不该再翻订单页。"""

    URL = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A'

    def buyer(self, **cfg):
        ab = AutoBuy({'preflight_warm_checkout': True, **cfg},
                     Path(tempfile.mkdtemp()), log=Mock())
        ab.start, ab._settle = Mock(), Mock()
        ab.preflight_clear_bag = Mock(return_value='清空了')
        p = Mock(); p.url = 'https://www.apple.com.cn/shop/bag'
        p.is_closed.return_value = False
        ab._login_page, ab._ctx = p, Mock()
        ab._preflight_login = Mock(return_value='订单页探针跑了')
        return ab

    def test_warmup_success_means_the_order_page_is_never_opened(self):
        ab = self.buyer()

        def warm(*a):
            ab.signed_in = True
            return '结账会话已就绪'

        ab.warm_checkout_session = warm
        note = ab.prepare(self.URL)
        ab._preflight_login.assert_not_called()
        self.assertIn('结账会话已就绪', note)

    def test_warmup_saying_not_signed_in_also_skips_the_order_page(self):
        """它已经给出结论了，再去翻订单页问一遍没有意义。"""
        ab = self.buyer()

        def warm(*a):
            ab.signed_in = False
            return '⚠️ 结账登录墙没撞掉'

        ab.warm_checkout_session = warm
        ab.prepare(self.URL)
        ab._preflight_login.assert_not_called()

    def test_indeterminate_warmup_falls_back_to_the_order_page(self):
        """读不到 token / 探路加购没进袋时，订单页是唯一不下单也能问出登录态的办法。"""
        ab = self.buyer()
        ab.warm_checkout_session = Mock(return_value='读不到 atbtoken，跳过结账预热')
        ab.prepare(self.URL)
        ab._preflight_login.assert_called_once()

    def test_warmup_off_keeps_the_order_page_probe(self):
        ab = self.buyer(preflight_warm_checkout=False)
        ab.warm_checkout_session = Mock()
        ab.prepare(self.URL)
        ab.warm_checkout_session.assert_not_called()
        ab._preflight_login.assert_called_once()


class ParkAfterAttemptRegressions(unittest.TestCase):
    """结账页 5 分钟不操作就跳「操作超时」，一单结束别把标签留在那儿。"""

    CHECKOUT = 'https://secure7.www.apple.com.cn/shop/checkout?_s=Fulfillment-init'
    BAG = 'https://www.apple.com.cn/shop/bag'

    def setup(self, url=CHECKOUT, placed=False):
        ab = AutoBuy({}, Path(tempfile.mkdtemp()), log=Mock())
        ab._attempt_order = placed        # 「这一单」可能成单，不是「历史上买过」
        p = Mock(); p.url = url
        p.is_closed.return_value = False
        p.goto.side_effect = lambda u, **kw: setattr(p, 'url', u)
        ab._page = p
        return ab, p

    def test_a_failed_attempt_leaves_the_tab_off_the_checkout_page(self):
        ab, p = self.setup()
        ab._park_after_attempt(p)
        self.assertEqual(self.BAG, p.url)

    def test_a_possible_order_is_never_navigated_away(self):
        """_attempt_order 覆盖「成功」和「结果不明」。那一页上有二维码和订单号。"""
        ab, p = self.setup(placed=True)
        ab._park_after_attempt(p)
        self.assertEqual(self.CHECKOUT, p.url)
        p.goto.assert_not_called()

    def test_an_already_expired_page_is_also_taken_away(self):
        ab, p = self.setup('https://www.apple.com.cn/shop/sorry/session_expired')
        ab._park_after_attempt(p)
        self.assertEqual(self.BAG, p.url)

    def test_later_failures_still_get_parked_after_an_earlier_success(self):
        """买到第一台之后 order_placed 就一直是 True。拿它当判据的话，之后每一次
        失败尝试都会把标签留在结账页上滴答——max_orders>1 时这是常态。"""
        ab, p = self.setup()
        ab.order_placed = True            # 之前买到过
        ab._attempt_order = False         # 但这一单没成
        ab._park_after_attempt(p)
        self.assertEqual(self.BAG, p.url)

    def test_pages_elsewhere_are_left_alone(self):
        ab, p = self.setup('https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A')
        ab._park_after_attempt(p)
        p.goto.assert_not_called()

    def test_a_closed_page_never_raises(self):
        ab, p = self.setup()
        p.is_closed.return_value = True
        ab._park_after_attempt(p)
        p.goto.assert_not_called()


class QuotaIsCumulativeRegressions(unittest.TestCase):
    """已买台数是**累计**的、落盘的，不会自己清——不然重启一次就能再买两台。"""

    def guard(self, root, **kw):
        from hunter.purchase_guard import PurchaseGuard
        return PurchaseGuard(root, **kw)

    def buy_one(self, root, part):
        with self.guard(root, max_orders=2) as g:
            g.submitted(store='R1', part=part)
            g.finish('confirmed', f'/{part}')

    def test_the_count_survives_a_restart(self):
        from hunter.purchase_guard import QuotaReached
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.buy_one(root, 'A')
            self.buy_one(root, 'B')
            # 新进程 = 新的 PurchaseGuard 实例，读的是同一个 order-attempt.json
            with self.assertRaises(QuotaReached):
                with self.guard(root, max_orders=2):
                    pass

    def test_resolve_alone_does_not_clear_the_count(self):
        """--resolve 是「这笔不明的单我核对过了」，不是「让我再买两台」。"""
        from hunter.purchase_guard import QuotaReached
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.buy_one(root, 'A'); self.buy_one(root, 'B')
            with self.guard(root, inspect=True) as g:
                g.resolve()
            with self.assertRaises(QuotaReached):
                with self.guard(root, max_orders=2):
                    pass

    def test_reset_count_starts_a_new_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.buy_one(root, 'A'); self.buy_one(root, 'B')
            with self.guard(root, inspect=True) as g:
                self.assertEqual(2, g.reset_count())
            with self.guard(root, max_orders=2) as g:      # 不抛
                self.assertEqual([], g.bought)

    def test_raising_the_limit_also_lets_more_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.buy_one(root, 'A'); self.buy_one(root, 'B')
            with self.guard(root, max_orders=3):
                pass

    def test_an_unknown_order_does_not_count_but_still_blocks(self):
        from hunter.purchase_guard import PendingOrder
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.guard(root, max_orders=2) as g:
                g.submitted(store='R1', part='A')
                g.finish('unknown', '/A')
                self.assertEqual([], g.bought)             # 不计入
            with self.assertRaises(PendingOrder):          # 但挡住一切
                with self.guard(root, max_orders=2):
                    pass


class RelogThenRewarmRegressions(unittest.TestCase):
    """掉登录补回来之后，那一轮的结账墙还立着——必须立刻补撞，别等下一轮。

    2026-09-20 07:20 掉线、07:27 放货，正好撞在裸奔窗口里，白付 21 秒登录。
    """

    URL = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A'

    def buyer(self, **cfg):
        ab = AutoBuy({'preflight_warm_checkout': True, **cfg},
                     Path(tempfile.mkdtemp()), log=Mock())
        ab.start, ab._settle = Mock(), Mock()
        ab.preflight_clear_bag = Mock(return_value='清空了')
        p = Mock(); p.url = 'https://www.apple.com.cn/shop/bag'
        p.is_closed.return_value = False
        ab._login_page, ab._ctx = p, Mock()
        return ab

    def test_rewarms_right_after_a_successful_relogin(self):
        ab = self.buyer()
        calls = []

        def warm(*a):
            calls.append(ab.signed_in)
            if ab.signed_in is True:          # 补登之后这一次
                return '结账登录墙已撞掉'
            return '探路加购没进袋（原因见随后的登录检查）'

        def relogin(_page):
            ab.signed_in = True
            return '已登录（预热时补登）'

        ab.warm_checkout_session = warm
        ab._preflight_login = relogin
        note = ab.prepare(self.URL)
        self.assertEqual(2, len(calls))                  # 预热跑了两次
        self.assertEqual([None, True], calls)
        self.assertIn('已撞掉', note)

    def test_no_rewarm_when_the_relogin_also_failed(self):
        ab = self.buyer()
        calls = []
        ab.warm_checkout_session = Mock(
            side_effect=lambda *a: calls.append(1) or '探路加购没进袋')

        def fail(_page):
            ab.signed_in = False
            return '⚠️ 未登录'

        ab._preflight_login = fail
        ab.prepare(self.URL)
        self.assertEqual(1, len(calls))                  # 没必要再撞一次

    def test_a_conclusive_warmup_never_triggers_the_order_page(self):
        ab = self.buyer()

        def warm(*a):
            ab.signed_in = True
            return '结账会话已就绪'

        ab.warm_checkout_session = warm
        ab._preflight_login = Mock()
        ab.prepare(self.URL)
        ab._preflight_login.assert_not_called()

    def test_the_failure_note_does_not_guess_the_cause(self):
        """原来咬定「token 多半已用过」，紧跟着的探针又打「⚠️ 没登录」，自相矛盾。"""
        ab = AutoBuy({'preflight_warm_checkout': True},
                     Path(tempfile.mkdtemp()), log=Mock())
        ab._ctx = Mock()
        ab._ctx.cookies.return_value = [{'name': 'as_atb', 'value': '1.0|x|' + 'd' * 40}]
        ab._ctx.pages = []
        page = Mock(); page.url = 'https://www.apple.com.cn/shop/bag'
        page.goto.side_effect = lambda u, **kw: setattr(page, 'url', u)
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True, 'kept': False}):
            note = ab.warm_checkout_session(ab._ctx, page, self.URL)
        self.assertIn('没进袋', note)
        self.assertNotIn('token 多半已用过', note)


class PreflightIntervalRegressions(unittest.TestCase):
    def test_default_is_five_minutes(self):
        """登录态约每 2 小时掉一次且续不动，掉线到下次保活之间就是裸奔窗口。"""
        w = PurchaseWorker.__new__(PurchaseWorker)
        w.buyer = SimpleNamespace(cfg={})
        self.assertEqual(
            300.0, max(120, float(w.buyer.cfg.get('preflight_interval', 300))))

    def test_config_can_still_override(self):
        w = PurchaseWorker.__new__(PurchaseWorker)
        w.buyer = SimpleNamespace(cfg={'preflight_interval': 900})
        self.assertEqual(
            900.0, max(120, float(w.buyer.cfg.get('preflight_interval', 300))))


class MaxOrdersActuallyWorksRegressions(unittest.TestCase):
    """max_orders 大于 1 必须真的能买到第二台。

    曾经不能：_wrap 成功时置 order_placed=True，而 worker 的
    halted = bool(order_placed) 立刻停摆——设 4 的实际行为跟 1 一模一样。
    """

    def worker(self, buyer):
        w = PurchaseWorker.__new__(PurchaseWorker)
        w.buyer, w.halted, w.locked = buyer, False, None
        return w

    def test_a_successful_order_does_not_halt_the_worker(self):
        w = self.worker(SimpleNamespace(order_placed=True, halt_for_human=False))
        w.halted = bool(getattr(w.buyer, 'halt_for_human', False))
        self.assertFalse(w.halted, '成单只是「还差几台」，不该停')

    def test_an_unknown_result_does_halt_the_worker(self):
        w = self.worker(SimpleNamespace(order_placed=True, halt_for_human=True))
        w.halted = bool(getattr(w.buyer, 'halt_for_human', False))
        self.assertTrue(w.halted)

    def test_wrap_marks_success_without_asking_for_a_human(self):
        ab = AutoBuy({}, Path(tempfile.mkdtemp()), log=Mock())
        placer = SimpleNamespace(no_retry=False, retriable=True,
                                 fast_ordered=True, retry_after=0)
        r = ab._wrap(placer, 'u', True, '已创建待付款订单', 'd', 'W123')
        self.assertTrue(r.ok)
        self.assertTrue(ab.order_placed)       # 通知和「别动标签」要用
        self.assertFalse(ab.halt_for_human)    # 但不是停止信号

    def test_wrap_asks_for_a_human_when_the_result_is_unknown(self):
        ab = AutoBuy({}, Path(tempfile.mkdtemp()), log=Mock())
        placer = SimpleNamespace(no_retry=True, retriable=False,
                                 fast_ordered=False, retry_after=0)
        r = ab._wrap(placer, 'u', False, '⚠️ 下单结果不明', 'd', '')
        self.assertFalse(r.retriable)
        self.assertTrue(ab.halt_for_human)

    def test_the_quota_is_what_finally_stops_it(self):
        """买够了由守卫说了算：下一单进不了守卫就抛 QuotaReached。"""
        from hunter.purchase_guard import PurchaseGuard, QuotaReached
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for n in ('A', 'B'):
                with PurchaseGuard(root, max_orders=2) as g:
                    g.submitted(store='R1', part=n)
                    g.finish('confirmed', f'/{n}')
            with self.assertRaises(QuotaReached):
                with PurchaseGuard(root, max_orders=2):
                    pass

    def test_four_orders_go_through_when_asked_for_four(self):
        from hunter.purchase_guard import PurchaseGuard, QuotaReached
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for n in 'ABCD':
                with PurchaseGuard(root, max_orders=4) as g:
                    g.submitted(store='R1', part=n)
                    g.finish('confirmed', f'/{n}')
            with PurchaseGuard(root, inspect=True) as g:
                self.assertEqual(4, len(g.bought))
            with self.assertRaises(QuotaReached):
                with PurchaseGuard(root, max_orders=4):
                    pass


class KeepBuyingWhileStockLastsRegressions(unittest.TestCase):
    """放货期间就该一直买到配额满，重试次数不该成为拦路虎。"""

    def worker(self, max_attempts=2, clock=None):
        w = PurchaseWorker.__new__(PurchaseWorker)
        w.offers, w.attempts, w.epochs = {}, {}, {}
        w.tried, w.burned, w.polled, w.had = {}, set(), {}, set()
        w.max_age, w.max_attempts, w.retry_delay = 90.0, max_attempts, 15.0
        w.cooldown_until, w.log = 0.0, Mock()
        w.clock = clock or (lambda: 0.0)
        return w

    @staticmethod
    def offer(part, store, observed=0.0):
        return Offer(part, store, store, 'url', part, observed, (0, 0))

    def test_a_success_does_not_burn_a_retry(self):
        """买到一台不算「失败重试」，否则 max_orders 还没到就先被次数卡死。"""
        now = [0.0]
        w = self.worker(clock=lambda: now[0])
        w.offers = {('A', 'R1'): self.offer('A', 'R1')}
        w.attempts['A'] = (0, 0.0)                    # 刚成过一单：计数清零、记了时间
        now[0] = 20.0
        self.assertIsNone(w._next())                  # 还得等一次新观察
        w.offers[('A', 'R1')] = self.offer('A', 'R1', observed=19.0)
        self.assertIsNotNone(w._next())               # 新观察来了就接着买

    def test_zero_means_no_attempt_limit(self):
        now = [0.0]
        w = self.worker(max_attempts=0, clock=lambda: now[0])
        for i in range(1, 8):
            w.offers = {('A', 'R1'): self.offer('A', 'R1', observed=now[0])}
            self.assertIsNotNone(w._next(), f'第 {i} 次就被拦了')
            w.attempts['A'] = (i, now[0])
            now[0] += 20.0

    def test_a_hopeless_verdict_still_stops_even_without_a_limit(self):
        """不限次数时，retriable=False 是唯一的刹车——页面明写售罄那种。"""
        now = [0.0]
        w = self.worker(max_attempts=0, clock=lambda: now[0])
        w.offers = {('A', 'R1'): self.offer('A', 'R1')}
        w.burned.add('A')
        self.assertIsNone(w._next())
        now[0] = 60.0
        w.offers = {('A', 'R1'): self.offer('A', 'R1', observed=59.0)}
        self.assertIsNone(w._next())                  # 新观察也救不回来

    def test_a_restock_clears_the_hopeless_verdict(self):
        w = self.worker(max_attempts=0)
        w.cv = __import__('threading').Condition()
        w.burned.add('A')
        w.offers = {('A', 'R1'): self.offer('A', 'R1')}
        w.observe('A', [], ['R1'])                    # 明确报卖光
        self.assertNotIn('A', w.burned)

    def test_a_new_store_also_clears_it(self):
        w = self.worker(max_attempts=0)
        w.cv = __import__('threading').Condition()
        w.burned.add('A')
        w.tried['A'] = {'R1'}
        w.offers = {('A', 'R1'): self.offer('A', 'R1')}
        w.observe('A', [self.offer('A', 'R1'), self.offer('A', 'R2')])
        self.assertNotIn('A', w.burned)

    def test_the_limit_still_works_when_asked_for(self):
        now = [0.0]
        w = self.worker(max_attempts=2, clock=lambda: now[0])
        w.offers = {('A', 'R1'): self.offer('A', 'R1')}
        w.attempts['A'] = (2, 0.0)
        now[0] = 60.0
        w.offers = {('A', 'R1'): self.offer('A', 'R1', observed=59.0)}
        self.assertIsNone(w._next())


class FatalVsBlockedRegressions(unittest.TestCase):
    """「这个型号没救了」和「全局被限流」必须分开。

    限流是 IP/端点级的，跟型号无关。拿 retriable=False 判死型号，等于一次 541
    就把那个型号从这一轮里永久排除——而冷却过了它可能正常得很。
    """

    def placer(self, **kw):
        base = dict(no_retry=False, retriable=True, fast_ordered=False,
                    retry_after=0, blocked=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def buyer(self):
        return AutoBuy({}, Path(tempfile.mkdtemp()), log=Mock())

    def test_being_rate_limited_never_condemns_the_model(self):
        ab = self.buyer()
        r = ab._wrap(self.placer(retriable=False, blocked=True),
                     'u', False, '⚠️ 快车道入口失败', '541', '')
        self.assertFalse(r.retriable)
        self.assertFalse(r.fatal, '限流是全局的，不该判死型号')

    def test_a_config_dead_end_does_condemn_it(self):
        ab = self.buyer()
        r = ab._wrap(self.placer(retriable=False, blocked=False),
                     'u', False, '⚠️ 缺少取货门店编号', 'd', '')
        self.assertTrue(r.fatal)

    def test_a_sold_out_page_condemns_it(self):
        """页面明写售罄：这一轮这个型号真的没了，再打是白烧几十秒。"""
        ab = self.buyer()
        ab._pick, ab._settle = Mock(), Mock()
        ab._await_options = Mock(return_value=0.0)
        ab._wait_add_button = Mock(return_value=(False, 'SOLD_OUT:已售罄'))
        page = Mock()
        page.url = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A'
        page.goto.side_effect = lambda u, **kw: setattr(page, 'url', u)
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True, 'kept': False}), \
             patch('hunter.autobuy.watch_checkout_block', return_value={}):
            r = ab._drive(Mock(), page, page.url, False)
        self.assertTrue(r.fatal)
        self.assertFalse(r.retriable)

    def test_a_successful_order_is_never_fatal(self):
        ab = self.buyer()
        r = ab._wrap(self.placer(fast_ordered=True), 'u', True, '已创建待付款订单', 'd', 'W1')
        self.assertFalse(r.fatal)

    def test_an_unknown_result_is_not_fatal_either(self):
        """它走的是整体停摆（halt_for_human），不该顺手把型号也判死。"""
        ab = self.buyer()
        r = ab._wrap(self.placer(no_retry=True, retriable=False),
                     'u', False, '⚠️ 下单结果不明', 'd', '')
        self.assertFalse(r.fatal)
        self.assertTrue(ab.halt_for_human)

    def test_the_worker_only_burns_on_fatal(self):
        from hunter.autobuy import BuyResult
        w = PurchaseWorker.__new__(PurchaseWorker)
        w.burned, w.log = set(), Mock()
        for res, want in ((BuyResult(False, '限流', retriable=False), False),
                          (BuyResult(False, '售罄', retriable=False, fatal=True), True)):
            w.burned.clear()
            if res.fatal:
                w.burned.add('A')
            self.assertEqual(want, 'A' in w.burned, res.stage)


class SkipTheBagNavigationRegressions(unittest.TestCase):
    """袋子是 fetch("/shop/bag") 问出来的，跟着 location.origin 走、不读 DOM。
    所以只要已经在主站上，那趟导航纯属白烧。

    2026-09-20 09:42 实测：放货瞬间 goto(/shop/bag) 花了 **16 秒**，而页面本来
    就停在 /shop/bag 上（_park_after_attempt 每次失败后就把它带回那儿）。
    36.2 秒的尝试里，这一项占了将近一半。
    """

    URL = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A'
    BAG = 'https://www.apple.com.cn/shop/bag'

    def buyer(self):
        ab = AutoBuy({'pickup_store_numbers': ['R581']},
                     Path(tempfile.mkdtemp()), log=Mock())
        ab._pick, ab._settle = Mock(), Mock()
        ab._wait_add_button = Mock(return_value=(True, ''))
        ab._await_options = Mock(return_value=0.0)
        return ab

    def ctx(self):
        c = Mock()
        c.cookies.return_value = [{'name': 'as_atb', 'value': '1.0|x|' + 'e' * 40}]
        return c

    def page(self, url):
        p = Mock()
        p.url = url
        p.goto.side_effect = lambda u, **kw: setattr(p, 'url', u)
        return p

    def drive(self, ab, page, ctx, bags):
        with patch('hunter.fastpath.prepare_bag', side_effect=list(bags)), \
             patch('hunter.fastpath.bag_to_checkout', return_value=''), \
             patch('hunter.autobuy.watch_checkout_block', return_value={}), \
             patch('hunter.fastpath.wait_for_bag_count', return_value=0.0):
            return ab._drive(ctx, page, self.URL, False)

    def test_already_on_the_main_site_means_no_navigation_at_all(self):
        ab, page = self.buyer(), self.page(self.BAG)
        self.drive(ab, page, self.ctx(), [{'ok': True, 'kept': True}])
        page.goto.assert_not_called()

    def test_any_main_site_page_counts_not_just_the_bag(self):
        ab, page = self.buyer(), self.page('https://www.apple.com.cn/shop/sorry/session_expired')
        self.drive(ab, page, self.ctx(), [{'ok': True, 'kept': True}])
        page.goto.assert_not_called()

    def test_sitting_on_the_checkout_host_still_needs_a_trip_back(self):
        """secureN 上 fetch("/shop/bag") 打的是 secureN，读回来是空的（2026-09-14
        为此中过招，结果袋里两台）。"""
        ab = self.buyer()
        page = self.page('https://secure7.www.apple.com.cn/shop/checkout')
        self.drive(ab, page, self.ctx(), [{'ok': True, 'kept': True}])
        self.assertEqual([self.BAG], [c.args[0] for c in page.goto.call_args_list])

    def test_a_blank_page_also_needs_the_trip(self):
        ab, page = self.buyer(), self.page('about:blank')
        self.drive(ab, page, self.ctx(), [{'ok': True, 'kept': True}])
        self.assertEqual([self.BAG], [c.args[0] for c in page.goto.call_args_list])

    def test_a_lookalike_host_is_not_the_main_site(self):
        ab, page = self.buyer(), self.page('https://www.apple.com.cn.evil.test/shop/bag')
        self.drive(ab, page, self.ctx(), [{'ok': True, 'kept': True}])
        self.assertEqual([self.BAG], [c.args[0] for c in page.goto.call_args_list])


class OneFetchLessRegressions(unittest.TestCase):
    """加购后的复核和进结账读的是同一份购物袋。放货那一刻一次读要 2.5 秒，
    合成一次就是省 2.5 秒——2026-09-20 09:50 那趟总共才 23.1 秒。"""

    URL = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A'
    STATE = {'stk': 'TOK', 'count': 1, 'cart': True, 'skus': ['MJT84CH/A'],
             'qty': [1], 'items': ['item-1'], 'origin': 'https://www.apple.com.cn',
             'checkoutUrl': '/shop/checkout'}

    def test_prepare_bag_hands_the_raw_state_out_when_it_keeps_the_cart(self):
        from hunter.fastpath import prepare_bag
        page = Mock()
        page.evaluate.return_value = dict(self.STATE)
        r = prepare_bag(page, want_part='MJT84CH/A',
                        want_origin='https://www.apple.com.cn', log=lambda *a: None)
        self.assertTrue(r['kept'])
        self.assertEqual(1, r['state']['count'])

    def test_bag_to_checkout_reuses_a_handed_state_without_fetching(self):
        from hunter.fastpath import bag_to_checkout
        page = Mock()
        page.evaluate.side_effect = AssertionError('不该再 fetch 一次')
        bag_to_checkout(page, want_part='MJT84CH/A',
                        want_origin='https://www.apple.com.cn',
                        log=lambda *a: None, state=dict(self.STATE))

    def test_it_still_fetches_when_no_state_is_handed(self):
        from hunter.fastpath import bag_to_checkout
        page = Mock()
        page.evaluate.return_value = dict(self.STATE)
        bag_to_checkout(page, want_part='MJT84CH/A',
                        want_origin='https://www.apple.com.cn', log=lambda *a: None)
        page.evaluate.assert_called()

    def test_an_empty_state_is_not_trusted(self):
        """给了个空壳就该自己重读，别拿它当真。"""
        from hunter.fastpath import bag_to_checkout
        page = Mock()
        page.evaluate.return_value = dict(self.STATE)
        bag_to_checkout(page, want_part='MJT84CH/A',
                        want_origin='https://www.apple.com.cn',
                        log=lambda *a: None, state={})
        page.evaluate.assert_called()

    def test_fast_add_reports_both_the_verdict_and_the_state(self):
        ab = AutoBuy({}, Path(tempfile.mkdtemp()), log=Mock())
        page = Mock()
        page.goto.side_effect = lambda u, **kw: None
        with patch('hunter.fastpath.prepare_bag',
                   return_value={'ok': True, 'kept': True, 'state': dict(self.STATE)}):
            ok, st = ab._fast_add(page, self.URL, 'MJT84CH/A', 'tok')
        self.assertTrue(ok)
        self.assertEqual(1, st['count'])

    def test_a_failed_fast_add_hands_back_nothing(self):
        ab = AutoBuy({}, Path(tempfile.mkdtemp()), log=Mock())
        page = Mock()
        page.goto.side_effect = lambda u, **kw: None
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True, 'kept': False}):
            ok, st = ab._fast_add(page, self.URL, 'MJT84CH/A', 'tok')
        self.assertFalse(ok)
        self.assertIsNone(st)

    def test_the_state_is_used_once_and_dropped(self):
        """登录之后那条路必须重读——那时候袋子和会话都可能变了。"""
        ab = AutoBuy({}, Path(tempfile.mkdtemp()), log=Mock())
        ab._bag_state = dict(self.STATE)
        with patch('hunter.fastpath.bag_to_checkout', return_value='') as b:
            try:
                ab._enter_checkout(Mock(), Mock(), 'MJT84CH/A', state=ab._bag_state)
            except Exception:
                pass
        self.assertIsNone(ab._bag_state)
        self.assertEqual(1, b.call_args.kwargs['state']['count'])


class StallHintsRegressions(unittest.TestCase):
    """2026-09-20 08:57 走到了第 6 步，请求体跟 HAR 逐字段一致却被拒，
    而日志只会说「没有 review 这一节」——下次得能看到服务端的说法。"""

    def hints(self, data, **kw):
        from hunter.fastpath import stall_hints
        return stall_hints(data, **kw)

    def test_it_digs_error_text_out_of_a_nested_response(self):
        got = self.hints({'body': {'checkout': {'billing': {'d': {
            'errorMessage': '所选分期方式当前不可用'}}}}})
        self.assertIn('所选分期方式当前不可用', got)

    def test_it_looks_at_several_key_spellings(self):
        for key in ('error', 'errorMessage', 'errors', 'message',
                    'validationText', 'warningText', 'alertMsg', 'reason'):
            with self.subTest(key=key):
                self.assertIn('坏了', self.hints({'a': {key: '坏了'}}), key)

    def test_it_ignores_unrelated_strings(self):
        self.assertEqual('', self.hints({'title': '结账', 'label': '继续'}))

    def test_whitespace_is_collapsed_and_长文本被截断(self):
        got = self.hints({'errorMessage': '  行一\n\n   行二  ' + 'x' * 300})
        self.assertNotIn('\n', got)
        self.assertLess(len(got), 160)

    def test_duplicates_are_reported_once(self):
        got = self.hints({'a': {'error': '同一句'}, 'b': {'message': '同一句'}})
        self.assertEqual(1, got.count('同一句'))

    def test_it_never_walks_forever_on_a_cycle(self):
        d = {'error': '有错'}
        d['self'] = d
        self.assertIn('有错', self.hints(d))

    def test_a_silent_rejection_says_so(self):
        from hunter.fastpath import FastCheckout, Stalled
        fc = FastCheckout(store='R581', id_last4='0000', last_name='张',
                          first_name='三', log=Mock())
        page = Mock()
        page.evaluate.return_value = {
            'status': 200,
            'json': {'head': {'status': 200},
                     'body': {'checkout': {'billing': {'d': {}}}}}}
        fc.stk = 'TOK'
        with self.assertRaises(Stalled) as cm:
            fc._post(page, '/shop/checkoutx/billing', 'continueFromBillingToReview',
                     'checkout.billing', [])
        self.assertIn('服务端没说为什么', str(cm.exception))

    def test_a_spoken_rejection_is_carried_into_the_error(self):
        from hunter.fastpath import FastCheckout, Stalled
        fc = FastCheckout(store='R581', id_last4='0000', last_name='张',
                          first_name='三', log=Mock())
        page = Mock()
        page.evaluate.return_value = {
            'status': 200,
            'json': {'head': {'status': 200}, 'body': {'checkout': {
                'billing': {'d': {'errorMessage': '分期额度不足'}}}}}}
        fc.stk = 'TOK'
        with self.assertRaises(Stalled) as cm:
            fc._post(page, '/shop/checkoutx/billing', 'continueFromBillingToReview',
                     'checkout.billing', [])
        self.assertIn('分期额度不足', str(cm.exception))


class AbortWhenGoneRegressions(unittest.TestCase):
    """明知必输就别发 search。

    2026-09-20 两台机器 17 次 search，10 次是在监控已经打出「N 家门店均无货」
    之后 1~7 秒才发的——10 次全输。每次赔 10.8 秒、十几个 checkoutx 请求，
    还跟「尝试之后一分钟内掉线」强相关（6 次里 4 次）。
    """

    URL = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A'
    PART = 'MJT84CH/A'

    def buyer(self, live, **cfg):
        ab = AutoBuy({'pickup_store_numbers': ['R581'], **cfg},
                     Path(tempfile.mkdtemp()), log=Mock())
        ab._pick, ab._settle = Mock(), Mock()
        ab._wait_add_button = Mock(return_value=(True, ''))
        ab._await_options = Mock(return_value=0.0)
        landed = Mock()
        landed.url = 'https://secure7.www.apple.com.cn/shop/checkout'
        ab._enter_checkout = Mock(return_value=landed)
        ab.stock_live = live
        return ab

    def page(self):
        p = Mock()
        p.url = 'https://www.apple.com.cn/shop/bag'
        p.goto.side_effect = lambda u, **kw: setattr(p, 'url', u)
        return p

    def drive(self, ab, page):
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': True, 'kept': True}), \
             patch('hunter.autobuy.watch_checkout_block', return_value={}), \
             patch('hunter.fastpath.wait_for_bag_count', return_value=0.0):
            return ab._drive(Mock(), page, self.URL, False)

    def test_it_stops_before_entering_checkout_when_the_stock_is_gone(self):
        ab, page = self.buyer(lambda part: False), self.page()
        r = self.drive(ab, page)
        self.assertFalse(r.ok)
        self.assertIn('没进结账', r.stage)
        ab._enter_checkout.assert_not_called()
        self.assertTrue(r.retriable, '货可能再回来，这不是判死')

    def test_it_goes_ahead_when_the_stock_is_still_there(self):
        ab, page = self.buyer(lambda part: True), self.page()
        self.drive(ab, page)
        ab._enter_checkout.assert_called_once()

    def test_an_unanswerable_probe_never_blocks(self):
        """问不出来就照常走——宁可白跑，也不能因为自己判断失误错过真放货。"""
        for probe in (None, lambda part: None,
                      lambda part: (_ for _ in ()).throw(RuntimeError())):
            with self.subTest(probe=probe):
                ab, page = self.buyer(probe), self.page()
                self.drive(ab, page)
                ab._enter_checkout.assert_called_once()

    def test_the_brake_can_be_turned_off(self):
        ab, page = self.buyer(lambda part: False, abort_when_gone=False), self.page()
        self.drive(ab, page)
        ab._enter_checkout.assert_called_once()

    def test_it_asks_about_the_part_being_bought(self):
        asked = []
        ab, page = self.buyer(lambda part: asked.append(part) or True), self.page()
        self.drive(ab, page)
        self.assertEqual([self.PART], asked[:1])

    def worker(self, now=0.0):
        w = PurchaseWorker.__new__(PurchaseWorker)
        w.cv = __import__('threading').Condition()
        w.offers, w.polled = {}, {}
        w.clock = lambda: now
        return w

    def test_the_worker_says_live_while_an_offer_stands(self):
        w = self.worker()
        w.offers = {('A', 'R1'): Offer('A', 'R1', 'R1', 'u', 'A', 0.0, (0, 0))}
        self.assertTrue(w.stock_live('A'))

    def test_gone_only_counts_when_it_was_just_looked_at(self):
        """常规巡检 30~45 秒一轮，拿那么旧的读数踩刹车会误杀真放货。"""
        w = self.worker(now=100.0)
        w.polled['A'] = 95.0                  # 5 秒前查过，没查到
        self.assertFalse(w.stock_live('A'))
        w.polled['A'] = 70.0                  # 30 秒前查的，不算数
        self.assertIsNone(w.stock_live('A'))

    def test_never_polled_means_dont_know(self):
        """查询失败/熔断静默时根本不调 observe——「看不到」只等于「没去看」。"""
        self.assertIsNone(self.worker().stock_live('A'))

    def test_observe_records_that_it_looked(self):
        w = self.worker(now=42.0)
        w.log, w.tried, w.attempts, w.epochs, w.burned = Mock(), {}, {}, {}, set()
        w.observe('A', [])
        self.assertEqual(42.0, w.polled['A'])


class StaleBagRegressions(unittest.TestCase):
    """购物袋状态**永远重新读**，不吃当前页面的 DOM，也不吃 HTTP 缓存。

    2026-09-20 23:14 实测：23:12 保活刚把袋子清空，23:14 下单却又「删了 1 件」——
    因为 _park_after_attempt 把页面停在 /shop/bag 上十几分钟不动，而读袋子有条
    「已经在购物袋页上就直接 parse innerHTML」的捷径，它不看页面是什么时候加载的。
    读到的是十几分钟前的快照：去删一个早就不存在的条目，然后加购，最后袋里到底
    有什么谁也不知道。袋子一脏，结账的 availableNowForAllLines 对每家店都是假，
    整单死在「所有门店不可取货」上——而货其实好好的。
    """

    def test_the_dom_shortcut_is_gone(self):
        from hunter import fastpath
        self.assertNotIn('if (here.stk && here.cart) return here',
                         fastpath.JS_CART_STATE,
                         '又能读到停了十几分钟的页面了')

    def test_it_always_refetches(self):
        from hunter import fastpath
        self.assertIn('fetch("/shop/bag"', fastpath.JS_CART_STATE)

    def test_the_read_never_comes_from_cache(self):
        """「袋子现在装了什么」读到缓存副本，等于读到过去。"""
        from hunter import fastpath
        self.assertIn('cache: "no-store"', fastpath.JS_CART_STATE)

    def test_the_cleared_items_are_named(self):
        """只打条数的话，「真读到 1 条」和「读到旧快照里的 1 条」长得一模一样。"""
        import inspect
        from hunter import fastpath
        src = inspect.getsource(fastpath.prepare_bag)
        self.assertIn("'、'.join(skus)", src)

    def test_the_bag_is_cleared_even_when_login_is_unknown(self):
        """登录判不准正是袋子最可能脏的时候——预热跑不起来多半就是加购没进袋。"""
        import inspect
        from hunter.autobuy import AutoBuy
        src = inspect.getsource(AutoBuy.prepare)
        self.assertNotIn('self.signed_in is True and self.clear_bag', src)
        self.assertIn('if self.clear_bag and self.preclear_bag', src)


class SoldOutVerdictRegressions(unittest.TestCase):
    """结账侧「全部门店不可取」不能被翻译成「没货了」。

    availableNowForAllLines 是「袋里所有条目在这家店都有货」：袋子里多了一条时
    每家店都报不可取，而货可能好好的。2026-09-20 三次失败全是这个形状。
    """

    def test_the_verdict_names_both_possibilities(self):
        from hunter.fastpath import FastCheckout, Stalled
        fc = FastCheckout(store='R581', stores=['R581'], id_last4='1',
                          last_name='张', first_name='三', log=Mock())
        avail = {'retailStores': [
            {'storeId': 'R581', 'availability': {'availableNowForAllLines': False,
                                                 'storeAvailability': '目前不可取货'}}]}
        fc.step2_store = lambda page: avail
        fc.take_slot = lambda d: (_ for _ in ()).throw(Stalled('排不上'))
        with self.assertRaises(Stalled) as cm:
            fc.select_store(Mock())
        msg = str(cm.exception)
        self.assertIn('不止一件', msg)
        self.assertNotIn('已经没货了', msg)
