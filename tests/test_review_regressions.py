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
                w.cfg = {'autobuy': {'pickup_store_numbers': configured, 'pickup_stores': ['旧店名']}}
                w.autobuy = AutoBuy(w.cfg['autobuy'], Path(tmp), log=Mock())
                w.parts, w.only_stores, w.note_of = ['P'], [], {}
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
