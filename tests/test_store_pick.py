import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hunter.autobuy import AutoBuy


class InStockStoreRoutingTests(unittest.TestCase):
    """放货的那家店必须真的被选中。

    2026-09-16 静安放货，程序却去试了五角场/南京东路/浦东（三家都没货）。
    两处各有一个 bug，这两组用例分别钉住它们。
    """

    def make(self, **cfg):
        c = {"enabled": True, "pickup_store_numbers": ["R581", "R359", "R389"]}
        c.update(cfg)
        self.logs = []
        return AutoBuy(c, Path("/tmp"), log=self.logs.append)

    # ---------- bug 1：白名单静默吃掉有货门店 ----------

    def test_display_names_do_not_exclude_a_stocked_store(self):
        ab = self.make(pickup_stores=["五角场", "南京东路"])
        out = ab.store_candidates(["静安"])
        self.assertEqual(["静安", "五角场", "南京东路"], out)
        self.assertFalse(any("本单不会去" in m for m in self.logs))

    def test_stocked_store_goes_first_when_whitelisted(self):
        ab = self.make(pickup_stores=["五角场", "南京东路", "静安"])
        self.assertEqual(["静安", "五角场", "南京东路"], ab.store_candidates(["静安"]))
        self.assertEqual([], [m for m in self.logs if "⚠️" in m])

    def test_empty_whitelist_means_anything_in_stock(self):
        ab = self.make(pickup_stores=[])
        self.assertEqual(["静安"], ab.store_candidates(["静安"]))

    # ---------- bug 2：门店编号没传到快车道 ----------

    def test_fast_path_gets_the_stocked_store_number_first(self):
        """快车道用 store_numbers[0] 发包，所以有货那家的编号必须排第一。

        原来这里传的是 in_stock（门店名），而 OrderPlacer 用 R\\d+ 过滤，
        名字被整个丢掉——于是永远拿配置里的第一家 R581。
        """
        ab = self.make(pickup_stores=[])
        seen = {}
        ab._drive = lambda *a, **kw: seen.update(kw)
        ab.warmed = True
        ab._ctx = object()
        ab._page = type("P", (), {"url": "", "is_closed": lambda s: False})()

        ab.fire("", in_stock=["静安"], in_stock_numbers=["R678"])

        self.assertEqual(["R678"], seen["in_stock_numbers"])
        # 模拟 _drive 里那行拼接，确认有货的排在配置前面
        merged = [s for s in (seen["in_stock_numbers"] + ab.pickup_store_numbers) if s]
        self.assertEqual("R678", merged[0])


if __name__ == "__main__":
    unittest.main()


class StoreLimitTests(unittest.TestCase):
    """换店不许越过配置的门店。

    用户的原话是「不放开门店，别的门店太远了，为了个机器跑那么远不值当」。
    换店的候选来自**结账侧**的库存——那是附近十几家店，比我们配的多得多。
    不设边界的话，首选排不上时会安静地换到一家没配过的店，一路把单下掉，
    等发现时人已经要跑去另一个区取货了。
    """

    @staticmethod
    def _search(avail):
        return {'retailStores': [
            {'storeId': sid,
             'availability': {'availableNowForAllLines': ok,
                              'storeAvailability': '店内取货' if ok else '目前不可取货'}}
            for sid, ok in avail]}

    def fc(self, **kw):
        from hunter.fastpath import FastCheckout
        base = dict(store='R581', stores=['R581'], id_last4='1234',
                    last_name='张', first_name='三', log=Mock())
        base.update(kw)
        return FastCheckout(**base)

    def run_pick(self, fc, avail, good=()):
        from hunter.fastpath import Stalled
        seen = []
        data = self._search(avail)

        def step2(page):
            seen.append(fc.store)
            return data

        fc.step2_store = step2
        fc.take_slot = lambda d: ({} if fc.store in good
                                  else (_ for _ in ()).throw(Stalled('排不上')))
        try:
            fc.select_store(Mock())
        except Stalled:
            pass
        return seen

    def test_it_will_not_wander_outside_the_configured_stores(self):
        """**这是那条 P1。** 只配了 R581，首店排不上，结账说 R359 有货——不许去。"""
        fc = self.fc(allow=['R581'])
        seen = self.run_pick(fc, [('R581', True), ('R359', True)], good=('R359',))
        self.assertEqual(['R581'], seen, f'跑到配置外的门店去了：{seen}')
        self.assertNotEqual('R359', fc.store_used)

    def test_without_a_limit_it_still_rotates_freely(self):
        """没配过门店 = 人没有限制过，那才允许换到结账说有货的任何一家。"""
        fc = self.fc(allow=[])
        seen = self.run_pick(fc, [('R581', True), ('R359', True)], good=('R359',))
        self.assertEqual(['R581', 'R359'], seen)
        self.assertEqual('R359', fc.store_used)

    def test_a_configured_store_outside_the_preference_list_is_still_allowed(self):
        """配了却当时没货的门店，结账说有货就该去——它在边界里。"""
        fc = self.fc(stores=['R581'], allow=['R581', 'R448'])
        seen = self.run_pick(fc, [('R581', True), ('R448', True), ('R359', True)],
                             good=('R448',))
        self.assertEqual(['R581', 'R448'], seen)
        self.assertEqual('R448', fc.store_used)

    def test_the_preference_order_is_kept_inside_the_limit(self):
        fc = self.fc(stores=['R581', 'R448'], allow=['R581', 'R448', 'R359'])
        seen = self.run_pick(fc, [('R581', True), ('R448', True), ('R359', True)],
                             good=('R359',))
        self.assertEqual(['R581', 'R448', 'R359'], seen)

    def test_it_says_which_stores_it_refused(self):
        fc = self.fc(allow=['R581'])
        self.run_pick(fc, [('R581', True), ('R359', True)], good=('R359',))
        self.assertTrue(any('不在配置的门店里' in str(c)
                            for c in fc.log.call_args_list),
                        '安静地不去，等于让人以为那家店也没货')

    def test_a_first_store_outside_the_limit_is_corrected(self):
        """首选门店越界是上游传参出了问题，不能闷着头按它下单。"""
        fc = self.fc(store='R359', stores=['R359', 'R581'], allow=['R581'])
        self.assertEqual('R581', fc.store)
        self.assertEqual(['R581'], fc.stores)


class StoreLimitPlumbingTests(unittest.TestCase):
    """边界是从 config 一路传下来的，中间断一节就等于没设。"""

    def test_the_order_placer_passes_it_on(self):
        from hunter.checkout import OrderPlacer
        op = OrderPlacer(store_numbers=['R581'], allow_stores=['R581', 'R448'])
        self.assertEqual(['R581', 'R448'], op.allow_stores)

    def test_the_configured_list_is_the_boundary(self):
        """偏好顺序和硬边界是同一份名单，不是两个概念。"""
        import inspect
        from hunter.autobuy import AutoBuy
        src = inspect.getsource(AutoBuy)
        self.assertIn('allow_stores=self.pickup_store_numbers', src)
        self.assertNotIn('allow_store_numbers', src)


class OneStoreListTests(unittest.TestCase):
    """门店配置只有一份名单，一个含义：只盯这几家，也只在这几家买。

    用户的原话：「我只从那几个店铺里面买，那也就只需要盯那几个店铺，别的店铺
    就算放货跟我也没关系。」拆成「盯的」和「买的」两份是没有意义的——放货是每家
    门店各自独立的，A 店有货完全不说明 B 店有货。
    """

    def test_pickup_stores_is_the_list(self):
        from hunter.autobuy import stores_of
        self.assertEqual(['R581'], stores_of({'pickup': {'stores': ['R581']}}))

    def test_empty_means_no_limit(self):
        from hunter.autobuy import stores_of
        self.assertEqual([], stores_of({'pickup': {}, 'autobuy': {}}))

    def test_the_old_key_still_works_on_its_own(self):
        """老配置只写了 autobuy.pickup_store_numbers，不能因为改名就静默失效。"""
        from hunter.autobuy import stores_of
        self.assertEqual(['R448'], stores_of(
            {'autobuy': {'pickup_store_numbers': ['R448']}}))

    def test_the_documented_key_wins(self):
        from hunter.autobuy import stores_of
        self.assertEqual(['R581'], stores_of(
            {'pickup': {'stores': ['R581']},
             'autobuy': {'pickup_store_numbers': ['R448']}}))

    def test_numbers_are_normalised(self):
        from hunter.autobuy import stores_of
        self.assertEqual(['R581'], stores_of({'pickup': {'stores': [' r581 ']}}))

    def test_watching_and_buying_come_from_the_same_call(self):
        """这两处要是各读各的配置，名单迟早会分叉。"""
        import inspect
        from hunter2.buyer import Buyer
        src = inspect.getsource(Buyer.__init__)
        self.assertIn('stores_of(cfg)', src)
        self.assertIn('ab["pickup_store_numbers"] = self.only_stores', src)

    def test_the_watcher_limits_buying_to_what_it_watches(self):
        import inspect
        from hunter.monitor import BaseWatcher
        self.assertIn('stores_of(cfg)', inspect.getsource(BaseWatcher.__init__))

    def test_the_doctor_shows_one_list(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch as _patch
        from hunter2.__main__ import cmd_doctor
        cfg = {'link': {'id': 'a'}, 'pickup': {'stores': ['R581']}, 'autobuy': {}}
        buf = io.StringIO()
        with _patch('hunter2.__main__.load_config', return_value=cfg), \
             _patch('hunter2.__main__.bus_key', return_value=b'k'), \
             redirect_stdout(buf):
            cmd_doctor(None)
        self.assertIn('R581', buf.getvalue())
        self.assertIn('盯的就是买的', buf.getvalue())


class BoundaryIsAppliedEverywhereTests(unittest.TestCase):
    """边界要套在整张候选表上，而且每个入口都得传。

    只过滤「额外候选」是不够的：轮换是从偏好表里挑的，表里留着越界的门店就照样
    会换过去——首选排不上时就是这么溜出去的。
    """

    def fc(self, **kw):
        from hunter.fastpath import FastCheckout
        base = dict(store='R581', stores=['R581'], id_last4='1',
                    last_name='张', first_name='三', log=Mock())
        base.update(kw)
        return FastCheckout(**base)

    def test_the_preference_list_itself_is_filtered(self):
        """**这是那条 P2。** stores 里留着 R359，allow 只有 R581。"""
        fc = self.fc(stores=['R581', 'R359'], allow=['R581'])
        self.assertEqual(['R581'], fc.stores, '越界的门店还留在候选表里')

    def test_no_legal_candidate_is_refused_not_papered_over(self):
        """一家合法的都不剩时留一个越界的顶上，等于把配置当建议。"""
        with self.assertRaises(ValueError) as cm:
            self.fc(store='R359', stores=['R359'], allow=['R581'])
        self.assertIn('R581', str(cm.exception))

    def test_it_says_which_ones_it_dropped(self):
        fc = self.fc(stores=['R581', 'R359'], allow=['R581'])
        self.assertTrue(any('R359' in str(c) for c in fc.log.call_args_list))

    def test_no_limit_leaves_the_list_alone(self):
        fc = self.fc(stores=['R581', 'R359'], allow=[])
        self.assertEqual(['R581', 'R359'], fc.stores)

    def test_the_order_placer_filters_before_building(self):
        """边界在这儿就兑现，别留到换店那一步。"""
        from hunter.checkout import OrderPlacer
        op = OrderPlacer(store_numbers=['R359', 'R581'], allow_stores=['R581'])
        self.assertEqual(['R581'], op.store_numbers)

    def test_the_order_placer_without_a_limit_keeps_everything(self):
        from hunter.checkout import OrderPlacer
        op = OrderPlacer(store_numbers=['R359', 'R581'])
        self.assertEqual(['R359', 'R581'], op.store_numbers)

    def test_the_cli_fastpath_passes_the_limit(self):
        """**这是那条 P1。** 这个入口带 --confirm 是会真下单的。"""
        import inspect
        from hunter.__main__ import cmd_fastpath
        src = inspect.getsource(cmd_fastpath)
        self.assertIn('allow=allow', src)
        self.assertIn('stores_of(cfg)', src)

    def test_the_cli_named_store_becomes_the_whole_limit(self):
        """--store 是人当场点名的，那家就是全部——跟 `buy --store` 一个规矩。"""
        import inspect
        from hunter.__main__ import cmd_fastpath
        self.assertIn('allow = [store] if args.store else stores',
                      inspect.getsource(cmd_fastpath))


class SearchRefireTests(unittest.TestCase):
    """search 头一枪返回「全不可取」时再打一枪。

    放货那一刻 Apple 的 fulfillment search 有时还没把新库存 populate，而监控的
    公开接口已经看到了——2026-09-20 三次失败全是这个形状（同一秒监控在报有货）。
    dunhuo 的经验也是「早退通常 2 枪」：第一枪空手不代表没货。
    """

    def _search(self, avail):
        return {'retailStores': [
            {'storeId': sid, 'availability': {'availableNowForAllLines': ok,
                                              'storeAvailability': '店内取货' if ok else '目前不可取货'}}
            for sid, ok in avail]}

    def fc(self, still_live=None, retries=1, **kw):
        from hunter.fastpath import FastCheckout
        base = dict(store='R581', stores=['R581'], id_last4='1', last_name='张',
                    first_name='三', log=Mock(), still_live=still_live,
                    search_retries=retries, search_retry_wait=0.0)
        base.update(kw)
        return FastCheckout(**base)

    def drive(self, fc, seq, good=()):
        """seq 是每次 step2 返回的 avail 列表；take_slot 只在 good 门店成。"""
        from hunter.fastpath import Stalled
        calls = {'n': 0}

        def step2(page):
            i = min(calls['n'], len(seq) - 1); calls['n'] += 1
            return self._search(seq[i])

        def take_slot(d):
            live = {sid for sid, ok, _ in fc.store_availability(d) if ok}
            if fc.store in good and fc.store in live:
                return {}
            raise Stalled('排不上')

        fc.step2_store = step2
        fc.take_slot = take_slot
        try:
            fc.select_store(Mock())
        except Stalled:
            pass
        return calls['n']

    def test_all_unavailable_fires_a_second_search(self):
        fc = self.fc(retries=1)
        n = self.drive(fc, [[('R581', False)], [('R581', False)]])
        self.assertEqual(2, n, '全不可取时没有再打一枪')

    def test_the_second_search_can_win(self):
        """第二枪 populate 出来了，就该正常下这一单。"""
        fc = self.fc(retries=1)
        n = self.drive(fc, [[('R581', False)], [('R581', True)]], good=('R581',))
        self.assertEqual(2, n)
        self.assertEqual('R581', fc.store_used)

    def test_a_known_gone_model_does_not_refire(self):
        """监控明说没货了，就别再白烧一枪 10 秒的 search。"""
        fc = self.fc(still_live=lambda: False, retries=1)
        n = self.drive(fc, [[('R581', False)], [('R581', False)]])
        self.assertEqual(1, n, '监控说没货了还在重打')

    def test_a_still_live_model_does_refire(self):
        fc = self.fc(still_live=lambda: True, retries=2)
        n = self.drive(fc, [[('R581', False)]] * 3)
        self.assertEqual(3, n)

    def test_retries_are_bounded(self):
        fc = self.fc(retries=1)
        n = self.drive(fc, [[('R581', False)]] * 5)
        self.assertEqual(2, n, '重试没有被 search_retries 封住')

    def test_first_shot_with_stock_does_not_refire(self):
        """首枪就有货，一枪就够。"""
        fc = self.fc(retries=1)
        n = self.drive(fc, [[('R581', True)]], good=('R581',))
        self.assertEqual(1, n)

    def test_no_refire_when_disabled(self):
        fc = self.fc(retries=0)
        n = self.drive(fc, [[('R581', False)], [('R581', False)]])
        self.assertEqual(1, n)


class FastAddFetchTests(unittest.TestCase):
    """快加购走页面内 fetch，不再 page.goto 等产品页 DOM。

    page.goto 要等重定向后的产品页 domcontentloaded，实测 1.2~4.3 秒、最慢 28 秒；
    加购是服务端在那个 GET 上就做完的，用不上返回的 HTML。fetch 只等请求出门。
    """

    def buyer(self):
        from types import SimpleNamespace
        from hunter.autobuy import AutoBuy
        ab = AutoBuy.__new__(AutoBuy)
        ab.timeout = 15000
        ab.region = 'cn'
        ab.log = Mock()
        return ab

    def test_it_uses_the_in_page_fetch_not_navigation(self):
        ab = self.buyer()
        page = Mock()
        with patch('hunter.fastpath.atb_add_fetch') as fetch, \
             patch('hunter.fastpath.prepare_bag',
                   return_value={'ok': True, 'kept': True, 'state': {'x': 1}}):
            ok, st, known = ab._fast_add(page, 'https://x/p', 'MJ/A', 'tok')
        self.assertTrue(ok and known)
        fetch.assert_called_once()
        page.goto.assert_not_called()

    def test_it_falls_back_to_navigation_when_fetch_cannot_fire(self):
        ab = self.buyer()
        page = Mock()
        with patch('hunter.fastpath.atb_add_fetch', side_effect=RuntimeError('boom')), \
             patch('hunter.fastpath.prepare_bag',
                   return_value={'ok': True, 'kept': True, 'state': None}):
            ok, _, _ = ab._fast_add(page, 'https://x/p', 'MJ/A', 'tok')
        self.assertTrue(ok)
        page.goto.assert_called_once()

    def test_verification_still_decides_success(self):
        """fetch 发出去不等于进袋——一律以 prepare_bag 复核为准。"""
        ab = self.buyer()
        with patch('hunter.fastpath.atb_add_fetch'), \
             patch('hunter.fastpath.prepare_bag',
                   return_value={'ok': True, 'kept': False}):
            ok, st, known = ab._fast_add(Mock(), 'https://x/p', 'MJ/A', 'tok')
        self.assertFalse(ok)
        self.assertTrue(known)
        self.assertIsNone(st)
