"""离线验证线程边界、持久提交保护与库存调度；不访问 Apple。"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hunter.autobuy import BuyResult
from hunter.apple import Blocked, StorePickup, Stock, _retry_after
from hunter.monitor import StockWatcher, State
from hunter.notify import AsyncBroadcaster
from hunter.pacing import Pacer, Breaker
from hunter.purchase_guard import PurchaseGuard, PendingOrder, PurchaseBusy
from hunter.purchase_worker import Offer, PurchaseWorker
from hunter.fastpath import bag_to_checkout, EntryUnavailable
from test_fastpath import FakePage, HAPPY, placer, resp, FUL, CONTACT, REVIEW


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_lock_blocks_other_owner_and_is_released(self):
        with PurchaseGuard(self.root):
            with self.assertRaises(PurchaseBusy):
                with PurchaseGuard(self.root):
                    pass
        with PurchaseGuard(self.root):
            pass

    def test_process_death_after_submit_blocks_restart(self):
        code = ('from pathlib import Path; import os; '
                'from hunter.purchase_guard import PurchaseGuard; '
                f'g=PurchaseGuard(Path({str(self.root)!r})); g.__enter__(); '
                'g.submitted(store="R001",part="P"); os._exit(0)')
        subprocess.run([sys.executable, '-c', code], check=True)
        with self.assertRaises(PendingOrder):
            with PurchaseGuard(self.root):
                pass
        with PurchaseGuard(self.root, inspect=True) as g:
            self.assertEqual('submitted', g.record['status'])
            g.finish('resolved')
        with PurchaseGuard(self.root):
            pass

    def test_corrupt_journal_never_permits_purchase(self):
        (self.root / 'order-attempt.json').write_text('{broken')
        with self.assertRaises(PendingOrder):
            with PurchaseGuard(self.root):
                pass

    def test_journal_failure_prevents_submit_request(self):
        fc = placer(place_order=True, submit_guard=Mock())
        fc.submit_guard.submitted.side_effect = OSError('disk full')
        page = FakePage(list(HAPPY))
        self.assertFalse(fc.run(page)[0])
        self.assertFalse(fc.submitted)
        self.assertEqual(6, len(page.calls))

    def test_timeout_records_unknown_and_never_reposts(self):
        with PurchaseGuard(self.root) as guard:
            fc = placer(place_order=True, submit_guard=guard)
            page = FakePage(list(HAPPY) + [{'status': 0, 'error': 'timeout'}])
            self.assertFalse(fc.run(page)[0])
            self.assertEqual(7, len(page.calls))
        data = json.loads((self.root / 'order-attempt.json').read_text())
        self.assertEqual('unknown', data['status'])
        with self.assertRaises(PendingOrder):
            with PurchaseGuard(self.root):
                pass

    def test_shutdown_before_submit_never_posts(self):
        fc = placer(place_order=True, cancelled=lambda: True)
        page = FakePage(list(HAPPY))
        self.assertFalse(fc.run(page)[0])
        self.assertFalse(fc.submitted)
        self.assertEqual(6, len(page.calls))


class WorkerTests(unittest.TestCase):
    def make(self, **kw):
        buyer = SimpleNamespace(cfg={'preflight': False}, warm_alive=False, order_placed=False,
                                buy=Mock(), stop=Mock())
        self.t = 100.
        w = PurchaseWorker(buyer, Mock(), clock=lambda: self.t, log=lambda *a: None, **kw)
        return w, buyer

    def offer(self, store='R001', at=100):
        return Offer('P', store, store, 'url', 'title', at)

    def test_unchanged_stock_can_retry_but_requires_new_observation(self):
        w, _ = self.make()
        w.observe('P', [self.offer()])
        self.assertIsNotNone(w._next())
        w.attempts['P'] = (1, 105)
        self.t = 125
        self.assertIsNone(w._next())
        w.observe('P', [self.offer(at=125)])
        self.assertIsNotNone(w._next())
        w.attempts['P'] = (2, 126)
        self.t = 145
        w.observe('P', [self.offer(at=145)])
        self.assertIsNone(w._next())
        w.observe('P', [], ['R001'])
        w.observe('P', [self.offer(at=145)])
        self.assertIsNotNone(w._next())

    def test_unknown_withdraws_candidate_without_resetting_attempts(self):
        w, _ = self.make()
        # attempts 和 tried 在生产里永远是一起写的（_run 里同一个临界区），
        # 固件也得照这个来：试过 R001 才会有 R001 的重试计数。
        w.attempts['P'], w.tried['P'] = (2, 99), {'R001'}
        w.observe('P', [])
        w.observe('P', [self.offer()])
        self.assertIsNone(w._next())

    def test_stale_offer_is_not_purchased(self):
        w, _ = self.make()
        w.observe('P', [self.offer(at=1)])
        self.assertIsNone(w._next())

    def test_slow_purchase_does_not_block_observation_and_next_store_runs(self):
        entered, release, second = threading.Event(), threading.Event(), threading.Event()
        w, buyer = self.make()
        threads = []
        def buy(url, names, stores):
            threads.append(threading.get_ident())
            if stores == ['R001']:
                entered.set()
                release.wait(2)
                return BuyResult(False, 'sold out', url, retriable=False)
            second.set()
            return BuyResult(True, 'review', url)
        buyer.buy.side_effect = buy
        w.observe('P', [self.offer()])
        w.start()
        try:
            self.assertTrue(entered.wait(1))
            w.observe('P', [self.offer(), self.offer('R002')])
            release.set()
            self.assertTrue(second.wait(1))
        finally:
            release.set()
            w.close()
        self.assertEqual(2, buyer.buy.call_count)
        self.assertEqual(1, len(set(threads)))
        self.assertNotEqual(threading.get_ident(), threads[0])
        buyer.stop.assert_called_once()


class NotificationTests(unittest.TestCase):
    def test_payment_notification_is_not_queued_behind_slow_stock_alert(self):
        entered, release, paid = threading.Event(), threading.Event(), threading.Event()
        bc = Mock()
        def send(title, *args, **kw):
            if kw.get('wake'):
                paid.set()
            else:
                entered.set()
                release.wait(2)
        bc.send.side_effect = send
        async_bc = AsyncBroadcaster(bc)
        try:
            async_bc.send('stock')
            self.assertTrue(entered.wait(1))
            async_bc.send('pay', wake=True)
            self.assertTrue(paid.wait(1))
        finally:
            release.set()
            async_bc.close()


class SchedulingTests(unittest.TestCase):
    def test_retry_after_http_date_and_nonfinite(self):
        from email.utils import formatdate
        with patch('hunter.apple.time.time', return_value=1000):
            r = SimpleNamespace(headers={'Retry-After': formatdate(5000, usegmt=True)})
            self.assertEqual(4000, _retry_after(r))
        for value in ['nan', 'inf', 'broken', '-2']:
            self.assertEqual(0, _retry_after(SimpleNamespace(headers={'Retry-After': value})))

    def test_retry_and_budget_are_not_capped_by_polling_maximum(self):
        p = Pacer(max_interval=90, budget_per_hour=1, burst=1, clock=lambda: 0)
        p.on_blocked(2000)
        self.assertGreaterEqual(p.next_delay(), 2000)
        p.spend(1)
        self.assertGreaterEqual(p.next_delay(), 3600)
        b = Breaker(clock=lambda: 0)
        self.assertEqual(2000, b.trip(2000))

    def test_each_request_is_admitted_before_spending(self):
        clock = [0.]
        sleeps = []
        def sleep(n):
            sleeps.append(n)
            clock[0] += n
        p = Pacer(budget_per_hour=360, burst=1, clock=lambda: clock[0], sleeper=sleep)
        p.acquire()
        self.assertEqual([], sleeps)
        p.acquire()
        self.assertEqual([10], sleeps)
        self.assertEqual(0, p.bucket.tokens)

    def test_available_batch_is_processed_before_later_failure_or_optional_query(self):
        w = StockWatcher.__new__(StockWatcher)
        w.round_no = 0
        w.pickup_on = True
        w.location = 'zip'
        w.part_groups = {'a': ['A'], 'b': ['B']}
        w.avail_every = 6
        w.log = Mock()
        calls = []
        def pickup(parts, **kw):
            calls.append(parts[0])
            if parts == ['B']:
                raise Blocked('blocked')
            return {'A': ['stock']}
        w.client = SimpleNamespace(pickup=pickup, availability=Mock(side_effect=OSError()))
        w._check_pickup = lambda p, s: calls.append('process ' + p)
        with self.assertRaises(Blocked):
            w.run()
        self.assertEqual(['A', 'process A', 'B'], calls)


class PaymentAndEntryTests(unittest.TestCase):
    def test_plain_payment_omits_installment_field(self):
        banks = resp('billing', {'options': [{'labelImageAlt': '支付宝', 'value': 'alipay'}]})
        page = FakePage([FUL, FUL, CONTACT, banks, resp('billing'), REVIEW])
        fc = placer(payment_label='支付宝', installment_months=0)
        self.assertTrue(fc.run(page)[0])
        self.assertNotIn('selectInstallmentOption', page.calls[-1]['body'])

    def test_missing_requested_months_never_changes_payment_terms(self):
        page = FakePage(list(HAPPY))
        self.assertFalse(placer(installment_months=36).run(page)[0])
        self.assertEqual(5, len(page.calls))

    def test_empty_label_never_matches_payment(self):
        self.assertEqual('', placer().find_billing_option({'labelImageAlt': '', 'value': 'wrong'}))

    def test_verified_cart_without_token_allows_necessary_web_entry(self):
        page = Mock()
        page.evaluate.return_value = {'count': 1, 'qty': [1], 'skus': ['P'],
                                      'origin': 'https://www.apple.com.cn'}
        with self.assertRaises(EntryUnavailable):
            bag_to_checkout(page, want_part='P', log=lambda *a: None)
        self.assertEqual(1, page.evaluate.call_count)

    def test_unverified_cart_without_token_never_allows_web_entry(self):
        page = Mock()
        page.evaluate.return_value = {'count': 1, 'skus': ['P']}
        self.assertEqual('', bag_to_checkout(page, want_part='P', log=lambda *a: None))


if __name__ == '__main__':
    unittest.main()

class MonitorCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        w = StockWatcher.__new__(StockWatcher)
        w.state = State(Path(self.tmp.name) / 'state.json')
        w.cfg = {'autobuy': {'pickup_store_numbers': ['R001']}}
        w.note_of, w.only_stores, w.parts = {}, [], ['P']
        w.autobuy = SimpleNamespace(pickup_stores=[])
        w.client = SimpleNamespace(observed_at={})
        w.purchase_worker = Mock()
        w._buy_url = lambda p: 'url'
        w.hit, w.log = Mock(), Mock()
        self.w = w

    def store(self, number='R001', state=Stock.AVAILABLE):
        return StorePickup('P', number, number, state=state,
                           reason='unknown' if state is Stock.UNKNOWN else '')

    def test_unchanged_stock_updates_candidates_without_duplicate_notification(self):
        self.w._check_pickup('P', [self.store()])
        self.w._check_pickup('P', [self.store()])
        self.assertEqual(2, self.w.purchase_worker.observe.call_count)
        self.assertEqual(1, self.w.hit.call_count)

    def test_unknown_store_does_not_erase_previous_available_state(self):
        self.w.state.set('pickup:P', ['R001'])
        self.w._check_pickup('P', [self.store(state=Stock.UNKNOWN),
                                  self.store('R002', Stock.UNAVAILABLE)])
        self.assertEqual(['R001'], self.w.state.get('pickup:P'))
        self.assertEqual([], self.w.purchase_worker.observe.call_args.args[1])

    def test_only_allowed_stores_become_purchase_candidates(self):
        self.w._check_pickup('P', [self.store(), self.store('R002')])
        offers = self.w.purchase_worker.observe.call_args.args[1]
        self.assertEqual(['R001'], [o.store for o in offers])

    def test_response_age_is_carried_to_purchase_scheduler(self):
        from hunter.apple import PICKUP_PATH
        self.w.client.observed_at[PICKUP_PATH] = 12.5
        self.w._check_pickup('P', [self.store()])
        self.assertEqual(12.5, self.w.purchase_worker.observe.call_args.args[1][0].observed)


class AdditionalBoundaries(unittest.TestCase):
    def test_business_error_with_expected_section_still_stops(self):
        response = resp('fulfillment')
        response['json']['head']['status'] = 409
        page = FakePage([response])
        self.assertFalse(placer().run(page)[0])
        self.assertEqual(1, len(page.calls))

    def test_card_with_ambiguous_label_is_not_selected(self):
        options = {'options': [{'labelImageAlt': '支付宝分期', 'value': 'installments1'},
                               {'labelImageAlt': '支付宝支付', 'value': 'alipay'}]}
        self.assertEqual('', placer(payment_label='支付宝').find_billing_option(options))

    def test_cart_quantity_missing_never_creates_checkout_session(self):
        page = Mock()
        page.evaluate.return_value = {'stk': 'token', 'count': 1, 'skus': ['P']}
        from hunter.fastpath import CartMismatch
        with self.assertRaises(CartMismatch):
            bag_to_checkout(page, want_part='P', log=lambda *a: None)
        self.assertEqual(1, page.evaluate.call_count)

    def test_checkout_block_cools_down_all_candidates(self):
        buyer = SimpleNamespace(order_placed=False)
        w = PurchaseWorker(buyer, Mock(), clock=lambda: 100)
        w.observe('P', [Offer('P', 'R001', 'store', 'url', 'title', 100)])
        w.cooldown_until = 220
        self.assertIsNone(w._next())


class CheckoutLifecycleTests(unittest.TestCase):
    def test_checkout_retry_after_reaches_purchase_result(self):
        from hunter.autobuy import AutoBuy
        from hunter.checkout import OrderPlacer
        p = OrderPlacer(store_numbers=['R001'], log=lambda *a: None)
        page = FakePage([{'status': 429, 'retry_after': '1800'}])
        outcome = p.place(page, 0)
        ab = AutoBuy({}, Path('/tmp'), log=lambda *a: None)
        result = ab._wrap(p, page.url, *outcome)
        self.assertEqual(1800, result.retry_after)
        self.assertFalse(result.retriable)

    def test_checkout_response_hooks_are_removed_at_end_of_attempt(self):
        from hunter.checkout import watch_checkout_block
        page = Mock()
        ctx = Mock(pages=[page])
        state = watch_checkout_block(page, ctx=ctx)
        state['close']()
        page.remove_listener.assert_called_once_with('response', page.on.call_args.args[1])
        ctx.remove_listener.assert_called_once_with('page', ctx.on.call_args.args[1])
