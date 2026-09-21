"""购物袋卫生：绝不在读不清袋子的时候再加一台，保活探路也别叠在遗留条目上。

2026-09-20 三次「删了 2 件」的真相：失败那一单把目标留在袋里，保活的探路加购
直接叠上去，prepare_bag 再把两条一起删掉、报「没进袋」，外层误判登录、白付一次
登录。另一条同族的路：快加购之后袋子读不到，按「没进袋」退回产品页再点一次，
袋里就是两台同型号。
"""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hunter.autobuy import AutoBuy

URL = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A'
EMPTY = {'ok': True, 'kept': False, 'removed': 0}
KEPT = {'ok': True, 'kept': True, 'removed': 0, 'state': {'count': 1}}
BLIND = {'ok': False, 'reason': '读购物袋状态失败：Error'}


def autobuy(**cfg):
    base = {'pickup_store_numbers': ['R581'], 'preflight_warm_checkout': True}
    base.update(cfg)
    return AutoBuy(base, Path('/tmp'), log=lambda *a: None)


class FastAddTests(unittest.TestCase):
    def setUp(self):
        self.ab = autobuy()
        self.page = Mock()
        self.page.url = URL
        self.page.evaluate.return_value = {'ok': True}

    def test_unreadable_bag_is_reread_once(self):
        with patch('hunter.fastpath.prepare_bag', side_effect=[BLIND, KEPT]) as pb:
            ok, state, known = self.ab._fast_add(self.page, URL, 'MJT84CH/A', 'tok')
        self.assertTrue(ok and known)
        self.assertEqual({'count': 1}, state)
        self.assertEqual(2, pb.call_count)

    def test_still_unreadable_means_unknown_not_missing(self):
        with patch('hunter.fastpath.prepare_bag', side_effect=[BLIND, BLIND]):
            ok, state, known = self.ab._fast_add(self.page, URL, 'MJT84CH/A', 'tok')
        self.assertFalse(ok)
        self.assertFalse(known)
        self.assertIsNone(state)

    def test_confirmed_miss_is_still_a_miss(self):
        with patch('hunter.fastpath.prepare_bag', side_effect=[EMPTY]) as pb:
            ok, _, known = self.ab._fast_add(self.page, URL, 'MJT84CH/A', 'tok')
        self.assertFalse(ok)
        self.assertTrue(known)
        self.assertEqual(1, pb.call_count)


class DriveTests(unittest.TestCase):
    """快加购发出去了、袋子读不到：整单停手，绝不退回产品页再点加购。"""

    def setUp(self):
        self.ab = autobuy()
        self.page = Mock()
        self.page.url = URL
        self.page.evaluate.return_value = {'ok': True}
        self.page.goto.side_effect = lambda url, **kw: setattr(self.page, 'url', url)
        self.ab._pick = Mock()
        self.ab._settle = Mock()
        self.ab._wait_add_button = Mock(return_value=(True, ''))
        for target in ('hunter.autobuy.watch_checkout_block',):
            p = patch(target, return_value={})
            p.start()
            self.addCleanup(p.stop)

    def test_blind_after_fast_add_stops_without_a_second_add(self):
        # 第 1 次：开场清袋（空）；第 2、3 次：快加购后的复核，两次都读不到
        with patch('hunter.fastpath.atb_token', return_value='tok'), \
             patch('hunter.fastpath.prepare_bag', side_effect=[EMPTY, BLIND, BLIND]), \
             patch('hunter.fastpath.bag_to_checkout') as entry:
            r = self.ab._drive(Mock(), self.page, URL, False)
        self.assertFalse(r.ok)
        self.assertIn('没敢再加', r.stage)
        self.assertTrue(r.retriable)
        self.page.locator.assert_not_called()           # 没退回产品页点加购
        self.assertNotIn(URL, [c.args[0] for c in self.page.goto.call_args_list])
        entry.assert_not_called()                       # 也没带着不明的袋子进结账

    def test_confirmed_miss_still_falls_back_to_the_product_page(self):
        with patch('hunter.fastpath.atb_token', return_value='tok'), \
             patch('hunter.fastpath.prepare_bag', side_effect=[EMPTY, EMPTY]), \
             patch('hunter.fastpath.bag_to_checkout', return_value=''), \
             patch('hunter.fastpath.wait_for_bag_count', return_value=100.0):
            self.ab._drive(Mock(), self.page, URL, False)
        self.assertIn(URL, [c.args[0] for c in self.page.goto.call_args_list])
        self.page.locator.return_value.first.click.assert_called_once()


class WarmProbeTests(unittest.TestCase):
    """保活探路：先看袋里有什么，别把探路型号叠在上一单的遗留上。"""

    CHECKOUT = 'https://secure7.www.apple.com.cn/shop/checkout?_s=Fulfillment-init'

    def setUp(self):
        self.ab = autobuy()
        self.ab._settle = Mock()
        self.page = Mock()
        self.page.url = 'https://www.apple.com.cn/shop/bag'
        self.ctx = Mock()
        self.ctx.pages = []
        self.ab._await_options = Mock(return_value=0.0)
        self.ab._pick = Mock()
        self.ab._wait_add_button = Mock(return_value=(False, '加购按钮一直是灰的'))
        landed = Mock()
        landed.url = self.CHECKOUT
        self.ab._enter_checkout = Mock(return_value=landed)

    def adds(self):
        return [c.args[0] for c in self.page.goto.call_args_list
                if 'add-to-cart' in c.args[0]]

    def warm(self, bags):
        with patch('hunter.fastpath.atb_token', return_value='tok'), \
             patch('hunter.fastpath.prepare_bag', side_effect=list(bags)) as pb:
            note = self.ab.warm_checkout_session(self.ctx, self.page, URL)
        return note, pb

    def test_leftover_probe_part_is_reused_without_adding(self):
        note, pb = self.warm([KEPT])
        self.assertEqual([], self.adds())
        self.assertEqual(1, pb.call_count)
        self.assertIn('结账会话已就绪', note)
        self.assertTrue(self.ab.signed_in)

    def test_leftover_other_part_is_cleared_before_the_probe_add(self):
        cleared = {'ok': True, 'kept': False, 'removed': 1}
        note, pb = self.warm([cleared, KEPT])
        self.assertEqual(1, len(self.adds()))
        # 第一次是清场，第二次是加购后的复核
        self.assertEqual(2, pb.call_count)
        self.assertIn('结账会话已就绪', note)

    def test_unreadable_bag_still_goes_through_the_add(self):
        # 页面还在 about:blank 上时读不到袋子：照旧加购，靠复核兜底
        note, pb = self.warm([BLIND, KEPT])
        self.assertEqual(1, len(self.adds()))
        self.assertIn('结账会话已就绪', note)

    def test_probe_that_never_lands_is_reported_not_stacked(self):
        # 接口加购没进袋（EMPTY, EMPTY）→ 退产品页，灰按钮点不了 → 报没进袋。
        # 关键：接口那一枪只发了一次（adds()==1），没有在遗留上叠第二次。
        with patch('hunter.fastpath.wait_for_bag_count', return_value=0.0):
            note, _ = self.warm([EMPTY, EMPTY])
        self.assertEqual(1, len(self.adds()))
        self.assertIn('探路加购没进袋', note)
        self.ab._enter_checkout.assert_not_called()


if __name__ == '__main__':
    unittest.main()


class WarmProbeFallbackTests(unittest.TestCase):
    """接口加购没进袋时，退产品页真点一次——这才让结账墙每轮都能被提前撞掉。

    2026-09-20 机器 B 每轮预热都卡在「token 多半已用过」，于是每次真放货都当场
    登录 8~31s。产品页兜底既保证探路商品进袋、进得了结账去撞墙，又种回新鲜的
    as_atb 让下一轮接口加购也能用。
    """

    URL = 'https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A'

    def buyer(self):
        ab = autobuy()
        ab._settle = Mock()
        ab._await_options = Mock(return_value=0.0)
        ab._pick = Mock()
        ab._wait_add_button = Mock(return_value=(True, ''))
        ab._sign_in = Mock(return_value=(True, ''))
        landed = Mock(); landed.url = 'https://secure7.www.apple.com.cn/shop/signIn'
        ab._enter_checkout = Mock(return_value=landed)
        return ab

    def page(self):
        p = Mock()
        p.url = 'https://www.apple.com.cn/shop/bag'
        p.goto.side_effect = lambda u, **kw: setattr(p, 'url', u)
        return p

    def test_stale_token_falls_back_to_product_page_and_lands(self):
        ab, page, ctx = self.buyer(), self.page(), Mock()
        ctx.pages = []
        MISS = {'ok': True, 'kept': False, 'removed': 0}   # 空袋 + 接口加购后仍空
        KEPT = {'ok': True, 'kept': True}                  # 产品页点加购后进袋
        with patch('hunter.fastpath.atb_token', return_value='stale'), \
             patch('hunter.fastpath.wait_for_bag_count', return_value=100.0), \
             patch('hunter.fastpath.prepare_bag', side_effect=[MISS, MISS, KEPT]):
            note = ab.warm_checkout_session(ctx, page, self.URL)
        # 退回了产品页并点了加购
        self.assertIn(self.URL, [c.args[0] for c in page.goto.call_args_list])
        ab._wait_add_button.assert_called_once()
        page.locator.return_value.first.click.assert_called_once()
        # 撞墙成功
        self.assertIn('撞掉', note)
        ab._enter_checkout.assert_called_once()

    def test_product_page_add_still_missing_reports_not_landed(self):
        ab, page, ctx = self.buyer(), self.page(), Mock()
        ctx.pages = []
        MISS = {'ok': True, 'kept': False}
        with patch('hunter.fastpath.atb_token', return_value='stale'), \
             patch('hunter.fastpath.wait_for_bag_count', return_value=100.0), \
             patch('hunter.fastpath.prepare_bag', side_effect=[MISS, MISS, MISS]):
            note = ab.warm_checkout_session(ctx, page, self.URL)
        self.assertIn('探路加购没进袋', note)
        ab._enter_checkout.assert_not_called()   # 没进袋就不进结账

    def test_gray_add_button_reports_not_landed(self):
        ab, page, ctx = self.buyer(), self.page(), Mock()
        ctx.pages = []
        ab._wait_add_button = Mock(return_value=(False, '加购按钮一直是灰的'))
        MISS = {'ok': True, 'kept': False}
        with patch('hunter.fastpath.atb_token', return_value='stale'), \
             patch('hunter.fastpath.prepare_bag', side_effect=[MISS, MISS]):
            note = ab.warm_checkout_session(ctx, page, self.URL)
        self.assertIn('探路加购没进袋', note)
        page.locator.return_value.first.click.assert_not_called()
