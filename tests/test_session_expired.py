"""「操作超时」：`https://www.apple.com.cn/shop/sorry/session_expired`。

这一页的来路写在结账页模型里（2026-09-17 的 HAR，`checkout.session` 那一节）：

    "session": {"d": {"alertMs": "60000", "interactionMs": "300000",
                      "expiredUrl": "https://www.apple.com.cn/shop/sorry/session_expired",
                      "canExtend": true, "ttl": "1199729",
                      "extendSessionUrl": "/shop/checkoutx/session?_a=extendSessionUrl&…"}}

也就是**结账会话 5 分钟没有交互就作废**（过期前 60 秒先弹一次提醒，会话总寿命
ttl ≈ 20 分钟）。作废之后整页被扔到那个 sorry 页。

为什么必须单独认这一态：它跟别的失败**处置方式相反**。
  * 「这一步没生效」→ 重新加载页面接着走；
  * 「登录墙」→ 就地登录；
  * **「会话过期」→ 那一页上连登录框都没有，在上面点什么、发什么包都一样，
    只能先离开、重新登录、从购物袋重来。**
把它混进前两者里，表现就是「每一步都 200 但一步也不推进」或者「反复点同一个
按钮」——日志看着像代码坏了。
"""

import unittest

from hunter.checkout import SORRY_EXPIRED, is_session_expired, is_sign_in
from hunter.fastpath import EXPIRED_URL, FastCheckout, SessionExpired
from test_fastpath import CONTACT, FUL, FakePage, placer, resp

EXPIRED = "https://www.apple.com.cn/shop/sorry/session_expired"


class RecogniseTests(unittest.TestCase):
    def test_the_url_user_reported(self):
        self.assertTrue(is_session_expired(EXPIRED))

    def test_relative_and_secure_host_forms(self):
        """服务端在响应体里给的是相对路径，页面 URL 又可能挂在 secureN 上。"""
        for u in ("/shop/sorry/session_expired",
                  "https://secure6.www.apple.com.cn/shop/sorry/session_expired",
                  "HTTPS://WWW.APPLE.COM.CN/SHOP/SORRY/SESSION_EXPIRED"):
            self.assertTrue(is_session_expired(u), u)

    def test_other_sorry_pages_count_too(self):
        """`/shop/sorry/` 底下都是「这条路作废了」，处置方式一样。"""
        self.assertTrue(is_session_expired("https://www.apple.com.cn/shop/sorry/anything"))

    def test_normal_pages_are_not_expired(self):
        for u in ("https://www.apple.com.cn/shop/bag",
                  "https://secure6.www.apple.com.cn/shop/checkout?_s=Review",
                  "https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJT84CH/A",
                  "", None):
            self.assertFalse(is_session_expired(u), u)

    def test_it_is_not_the_same_as_a_login_wall(self):
        """两者都要「重新登录」，但登录页上能就地登、超时页上不能——
        所以判定必须分开，否则会在 sorry 页上找登录框然后报「登录失败」。"""
        self.assertFalse(is_sign_in(EXPIRED))
        self.assertTrue(is_session_expired(EXPIRED))
        signin = "https://secure6.www.apple.com.cn/shop/signIn"
        self.assertTrue(is_sign_in(signin))
        self.assertFalse(is_session_expired(signin))

    def test_constants_agree(self):
        """两个模块各有一份常量，别让它们漂移。"""
        self.assertEqual(SORRY_EXPIRED, EXPIRED_URL)
        self.assertTrue(is_session_expired(EXPIRED_URL))


def expired_reply():
    """会话过期时服务端照样回 HTTP 200，跳转写在响应体里。"""
    return {"status": 200, "json": {"head": {"status": 302, "data": {
        "url": "/shop/sorry/session_expired"}}, "body": {}}}


class FastPathTests(unittest.TestCase):
    """发包这条路：每一步都可能收到「去超时页」。"""

    def test_raises_session_expired_not_stalled(self):
        """报 Stalled（「响应里没有 xxx 这一节」）会让人去查 Apple 改了什么结构，
        而真相是会话早就没了。"""
        page = FakePage([expired_reply()])
        fc = placer()
        fc.stk = "TOKEN"
        with self.assertRaises(SessionExpired):
            fc.step1_pickup(page)

    def test_it_stops_the_whole_run_with_a_clear_reason(self):
        page = FakePage([FUL, expired_reply()])
        ok, stage, detail = placer().run(page)
        self.assertFalse(ok)
        self.assertIn("会话已过期", stage)
        self.assertIn("重新登录", detail)

    def test_it_does_not_keep_posting_after_that(self):
        """会话作废之后每多发一个包都是白撞——而撞的正是会返 541 的那族端点。"""
        page = FakePage([FUL, expired_reply(), FUL, CONTACT])
        placer().run(page)
        self.assertEqual(2, len(page.calls), "过期之后还在继续发包")

    def test_expiry_on_a_later_step_is_caught_too(self):
        page = FakePage([FUL, FUL, expired_reply()])
        ok, stage, _ = placer().run(page)
        self.assertFalse(ok)
        self.assertIn("会话已过期", stage)

    def test_a_real_stall_is_still_reported_as_a_stall(self):
        """别把普通的「没推进」也说成会话过期——那会让人白跑一趟重新登录。"""
        page = FakePage([FUL, resp("somethingElse")])
        ok, stage, _ = placer().run(page)
        self.assertFalse(ok)
        self.assertIn("没生效", stage)


class BagPathTests(unittest.TestCase):
    """从购物袋接口进结账那一步，也会被指到超时页。"""

    class Page:
        url = "https://www.apple.com.cn/shop/bag"

        def __init__(self, checkout_url):
            self._checkout = checkout_url

        def evaluate(self, js, arg=None):
            if isinstance(arg, list):          # JS_BAG_TO_CHECKOUT
                return {"status": 200, "url": self._checkout, "head": 302}
            return {"stk": "TOKEN", "count": 1, "skus": ["MJT84CH/A"],  # JS_CART_STATE
                    "qty": [1], "origin": "https://www.apple.com.cn"}

    def test_bag_interface_pointing_at_sorry_raises(self):
        """报「购物袋接口没给出结账地址」会让人去查购物袋和令牌，全是白查。"""
        from hunter.fastpath import bag_to_checkout
        page = self.Page("https://www.apple.com.cn/shop/sorry/session_expired")
        with self.assertRaises(SessionExpired):
            bag_to_checkout(page, want_part="MJT84CH/A", want_qty=1,
                            want_origin="https://www.apple.com.cn",
                            log=lambda *a: None)

    def test_normal_checkout_url_still_works(self):
        from hunter.fastpath import bag_to_checkout
        page = self.Page("https://secure6.www.apple.com.cn/shop/checkout")
        got = bag_to_checkout(page, want_part="MJT84CH/A", want_qty=1,
                              want_origin="https://www.apple.com.cn",
                              log=lambda *a: None)
        self.assertIn("/shop/checkout", got)


class WizardTests(unittest.TestCase):
    """点页面那条路：向导循环里必须认出来并停手。"""

    def test_loop_stops_and_flags_it(self):
        from hunter.checkout import OrderPlacer
        p = OrderPlacer(store_numbers=["R581"], id_last4="0000",
                        log=lambda *a: None)

        class Page:
            url = EXPIRED

            def wait_for_timeout(self, _ms):
                pass

        # snapshot 是在真实页面上跑 JS 的，这里直接喂它的产物
        import hunter.checkout as C
        real = C.snapshot
        C.snapshot = lambda page: {"url": EXPIRED, "expired": True, "key": ""}
        try:
            ok, stage, detail, order = p.place(Page(), 0.0)
        finally:
            C.snapshot = real
        self.assertFalse(ok)
        self.assertIn("会话已过期", stage)
        self.assertTrue(p.session_expired, "上层要靠这个标记去重新登录")
        self.assertIn("interactionMs", detail)


class RecoverTests(unittest.TestCase):
    """撞上之后要真的重新登录，而且这一态**必须可重试**（订单肯定没建）。"""

    def buyer(self, signed_after_login):
        from hunter.autobuy import AutoBuy
        ab = AutoBuy.__new__(AutoBuy)
        ab.log = lambda *a: None
        ab.region = "cn"
        ab.timeout = 1000
        ab.signed_in = True
        ab.warmed = False
        ab.calls = []

        def preflight(page):
            ab.calls.append("login")
            ab.signed_in = signed_after_login
            return "已登录" if signed_after_login else "⚠️ 未登录且跳不到登录页"

        ab._preflight_login = preflight
        return ab

    class Page:
        url = EXPIRED

        def __init__(self):
            self.gotos = []

        def goto(self, url, **kw):
            self.gotos.append(url)
            self.url = url

        def wait_for_timeout(self, _ms):
            pass

    def test_it_logs_in_again_and_goes_back_to_the_bag(self):
        ab, page = self.buyer(True), self.Page()
        note = ab._recover_session(page)
        self.assertEqual("", note, "恢复成功时要返回空串让上层继续")
        self.assertEqual(["login"], ab.calls)
        self.assertTrue(any("/shop/bag" in u for u in page.gotos),
                        "重新登录之后要回购物袋重新走")

    def test_it_says_so_when_login_fails(self):
        ab, page = self.buyer(False), self.Page()
        note = ab._recover_session(page)
        self.assertIn("没能重新登录", note)
        self.assertIn("手动登录", note)

    def test_expiry_stays_retriable(self):
        """跟「下单结果不明」正好相反：会话过期时订单肯定没建，该重试。
        这里锁的是「别把 no_retry 立起来」。"""
        from hunter.autobuy import AutoBuy

        class FakePlacer:
            no_retry = False
            session_expired = True

        ab = AutoBuy.__new__(AutoBuy)
        ab.order_placed = False
        r = ab._wrap(FakePlacer(), EXPIRED, False, "⚠️ 结账会话已过期", "要重登", "")
        self.assertTrue(r.retriable)
        self.assertFalse(ab.order_placed)


if __name__ == "__main__":
    unittest.main()
