"""快车道失败必须原地结束；用实际编排和模拟响应覆盖整条调用边界。"""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hunter.autobuy import AutoBuy, BuyResult
from hunter.checkout import OrderPlacer
from hunter.fastpath import Blocked, CartMismatch, FastCheckout, bag_to_checkout, prepare_bag
from hunter.monitor import BaseWatcher
import test_fastpath as fixtures
from test_fastpath import FakePage, HAPPY


class CheckoutOnlyTests(unittest.TestCase):
    def placer(self, **kw):
        return OrderPlacer(store_numbers=['R581'], payment='招商银行',
                           installment_months=24, id_last4='0000', log=lambda *a: None, **kw)

    def test_each_failed_step_stops_without_navigation_or_dom_fallback(self):
        for step in range(6):
            for status in (403, 429, 541, 500):
                with self.subTest(step=step, status=status):
                    page = FakePage(HAPPY[:step] + [{'status': status}] + HAPPY[step+1:])
                    p = self.placer()
                    with patch('hunter.checkout.snapshot', side_effect=AssertionError('DOM fallback')):
                        ok, _, _, _ = p.place(page, 0)
                    self.assertFalse(ok)
                    self.assertEqual(step + 1, len(page.calls))
                    self.assertEqual([], page.gotos)
                    self.assertFalse(p.fast_unknown)
                    if status in (403, 429, 541):
                        self.assertFalse(p.retriable)

    def test_legacy_false_does_not_restore_dom_path(self):
        page = FakePage([{'status': 541}])
        p = self.placer(fast_path=False)
        self.assertFalse(p.place(page, 0)[0])
        self.assertEqual(1, len(page.calls))
        self.assertEqual([], page.gotos)

    def test_stop_at_review_never_submits(self):
        page = FakePage(list(HAPPY))
        p = self.placer(stop_at_review=True)
        ok, stage, _, _ = p.place(page, 0)
        self.assertTrue(ok)
        self.assertIn('Review', stage)
        self.assertEqual(6, len(page.calls))
        self.assertFalse(p.fast_ordered)
        self.assertEqual(1, len(page.gotos))
        self.assertIn('_s=Review', page.url)

    def test_success_survives_order_number_read_failure(self):
        page = FakePage(HAPPY + [fixtures.PlaceOrderTests.PLACED,
                        fixtures.PlaceOrderTests._status('/shop/checkout/thankyou')])
        p = self.placer()
        with patch('hunter.checkout.snapshot', side_effect=RuntimeError('DOM still loading')):
            ok, stage, _, order = p.place(page, 0)
        self.assertTrue(ok)
        self.assertTrue(p.fast_ordered)
        self.assertIn('待付款', stage)
        self.assertEqual('', order)
        self.assertEqual(8, len(page.calls))

    def test_submit_timeout_never_retries_or_navigates(self):
        page = FakePage(HAPPY + [{'status': 0, 'error': 'TimeoutError'}])
        p = self.placer()
        ok, stage, _, _ = p.place(page, 0)
        self.assertFalse(ok)
        self.assertIn('结果不明', stage)
        self.assertTrue(p.no_retry)
        self.assertEqual(7, len(page.calls))
        self.assertEqual([], page.gotos)

    def test_confirmed_order_keeps_payment_url_when_navigation_fails(self):
        page = FakePage(HAPPY + [fixtures.PlaceOrderTests.PLACED,
                        fixtures.PlaceOrderTests._status('/shop/checkout/thankyou')])
        original_goto = page.goto
        def goto(url, **kw):
            if url.endswith('/thankyou'):
                raise RuntimeError('navigation failed')
            original_goto(url, **kw)
        page.goto = goto
        p = self.placer()
        with patch('hunter.checkout.snapshot', return_value={}):
            self.assertTrue(p.place(page, 0)[0])
        self.assertTrue(p.result_url.endswith('/shop/checkout/thankyou'))
        self.assertTrue(page.url.endswith('/shop/checkout/status'))

    def test_unexpected_exception_preserves_submitted_flag(self):
        fc = FastCheckout(store='R581', id_last4='0000', last_name='', first_name='')
        def fail(_):
            fc.submitted = True
            raise RuntimeError('disconnected after submit')
        fc.run = fail
        p = self.placer()
        with patch('hunter.fastpath.FastCheckout', return_value=fc):
            ok, stage, _, _ = p.place(FakePage([]), 0)
        self.assertFalse(ok)
        self.assertTrue(p.no_retry)
        self.assertIn('结果不明', stage)

    def test_missing_store_does_not_touch_page(self):
        p = OrderPlacer(log=lambda *a: None)
        page = FakePage([])
        self.assertFalse(p.place(page, 0)[0])
        self.assertFalse(p.retriable)
        self.assertEqual([], page.calls)
        self.assertEqual([], page.gotos)

    def test_unknown_redirect_is_not_a_definite_rejection(self):
        for url in ('/shop/bag', '/shop/sorry/session_expired', '/shop/signIn', '/error'):
            self.assertTrue(FastCheckout.order_unknown(url))


class CartEntryTests(unittest.TestCase):
    def test_blocked_cart_entry_raises_without_second_request(self):
        for code in (403, 429, 541):
            page = fixtures.BagToCheckoutTests.Page('TOKEN', {'status': code})
            with self.assertRaises(Blocked):
                bag_to_checkout(page, log=lambda *a: None)
            self.assertEqual(2, page.calls)

    def test_cart_login_redirect_is_preserved(self):
        target = 'https://secure8.www.apple.com.cn/shop/signIn?c=aHR0cA'
        page = fixtures.BagToCheckoutTests.Page('TOKEN', {'status': 200, 'url': target})
        self.assertEqual(target, bag_to_checkout(page, log=lambda *a: None))

    def test_unreadable_cart_is_not_empty(self):
        for state in ({'cart': False, 'count': None}, {'cart': True, 'count': 1}):
            state['origin'] = 'https://www.apple.com.cn'
            page = fixtures.PrepareBagTests.Page(state)
            self.assertFalse(prepare_bag(page, log=lambda *a: None)['ok'])
            self.assertEqual([], page.deleted)


class AutoBuyOnlyTests(unittest.TestCase):
    URL = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A'
    CHECKOUT = 'https://secure8.www.apple.com.cn/shop/checkout'

    def setUp(self):
        self.ab = AutoBuy({'fast_path': False, 'pickup_store_numbers': ['R581']},
                          Path('/tmp'), log=lambda *a: None)
        self.page = Mock()
        self.page.url = self.URL
        self.page.goto.side_effect = lambda url, **kw: setattr(self.page, 'url', url)
        self.ab._pick = Mock()
        self.ab._wait_add_button = Mock(return_value=(True, ''))
        self.ab._settle = Mock()
        self.prepare = patch('hunter.fastpath.prepare_bag', return_value={'ok': True})
        self.prepare.start()
        self.addCleanup(self.prepare.stop)
        self.watch = patch('hunter.autobuy.watch_checkout_block', return_value={})
        self.watch.start()
        self.addCleanup(self.watch.stop)

    def drive(self):
        return self.ab._drive(Mock(), self.page, self.URL, False)

    def test_cart_api_failure_never_loads_bag_or_guesses_checkout(self):
        with patch('hunter.fastpath.bag_to_checkout', return_value=''):
            r = self.drive()
        self.assertFalse(r.ok)
        self.assertEqual([self.URL], [c.args[0] for c in self.page.goto.call_args_list])
        self.page.locator.assert_called_once()  # 唯一的点击是产品页加购

    def test_cart_mismatch_stops_before_checkout(self):
        with patch('hunter.fastpath.bag_to_checkout', side_effect=CartMismatch('wrong SKU')):
            r = self.drive()
        self.assertFalse(r.ok)
        self.assertIn('wrong SKU', r.detail)
        self.assertEqual(1, self.page.goto.call_count)

    def test_prepare_failure_never_clicks_add(self):
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': False, 'reason': 'bad cart'}):
            r = self.drive()
        self.assertFalse(r.ok)
        self.page.locator.assert_not_called()

    def test_local_bag_memory_cannot_skip_server_reconciliation(self):
        self.ab.bagged_part = 'MJT84CH/A'
        with patch('hunter.fastpath.prepare_bag', return_value={'ok': False, 'reason': 'unknown'}):
            self.assertFalse(self.drive().ok)
        self.page.locator.assert_not_called()

    def test_necessary_bag_entry_can_establish_session_before_fastpath(self):
        from hunter.fastpath import EntryUnavailable
        self.ab._checkout_via_bag = Mock(return_value=self.page)
        def entry(*args):
            self.page.url = self.CHECKOUT
            return self.page
        self.ab._checkout_via_bag.side_effect = entry
        p = Mock(no_retry=False, retriable=True, fast_ordered=False, blocked=False,
                 result_url=self.CHECKOUT, secure_host='')
        p.place.return_value = (True, 'Review', '', '')
        with patch('hunter.fastpath.bag_to_checkout', side_effect=EntryUnavailable()), \
             patch('hunter.autobuy.OrderPlacer', return_value=p):
            self.assertTrue(self.drive().ok)
        self.ab._checkout_via_bag.assert_called_once()
        p.place.assert_called_once()

    def test_success_returns_final_url_and_created_state(self):
        p = Mock(no_retry=False, retriable=True, fast_ordered=True, secure_host='', result_url='')
        def finish(page, t0):
            page.url = self.CHECKOUT + '/thankyou'
            return True, '已创建待付款订单', '', ''
        p.place.side_effect = finish
        with patch('hunter.fastpath.bag_to_checkout', return_value=self.CHECKOUT), \
             patch('hunter.autobuy.OrderPlacer', return_value=p):
            r = self.drive()
        self.assertTrue(r.ok)
        self.assertTrue(r.order_created)
        self.assertEqual(self.CHECKOUT + '/thankyou', r.url)

    def test_success_without_order_id_still_wakes_user(self):
        w = BaseWatcher.__new__(BaseWatcher)
        w.autobuy = SimpleNamespace(warmed=False, order_placed=True,
            buy=Mock(return_value=BuyResult(True, '已创建待付款订单', self.CHECKOUT + '/thankyou',
                                           order_created=True)))
        w.autobuy_done = False
        w.bc = Mock()
        w.cfg = {}
        w.log = Mock()
        w._report_purchase(w.autobuy.buy.return_value, 'hit', self.URL)
        alerts = [c for c in w.bc.send.call_args_list if c.kwargs.get('wake')]
        self.assertEqual(1, len(alerts))
        self.assertIn('去付款', alerts[0].args[0])


if __name__ == '__main__':
    unittest.main()
