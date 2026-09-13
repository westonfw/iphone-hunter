import unittest
from unittest import mock

from hunter.checkout import BANK_INSTALLMENT, CARD_PAY, SCAN_PAY, card_would_charge

# 2026-09-13 从真实 Billing 页读到的选项，顺序即 DOM 顺序
BILLING_PAGE = [
    "支付宝 ALIPAY", "微信支付",
    "花呗分期", "微信分付", "招商银行", "中国建设银行", "中国工商银行",
    "通过支付宝选择更多银行",
    "信用卡\nVisa, Mastercard",
]


def first_click(needles):
    """复刻 JS_CLICK_TEXT：按 DOM 顺序取第一个命中任意关键词的元素。"""
    for text in BILLING_PAGE:
        if any(n in text for n in needles):
            return text
    return None


class BankInstallmentTests(unittest.TestCase):
    def test_bank_installment_counts_as_scan_pay(self):
        for bank in BANK_INSTALLMENT:
            self.assertIn(bank, SCAN_PAY, f"{bank} 走支付宝通道，是待付款不是即时扣款")

    def test_real_credit_card_is_still_blocked(self):
        for needle in ("信用卡", "Visa", "Mastercard"):
            self.assertIn(needle, CARD_PAY)
            self.assertNotIn(needle, SCAN_PAY)

    def test_cmb_does_not_trip_the_charge_guard(self):
        snap = {"compact": "".join(BILLING_PAGE)}
        self.assertFalse(card_would_charge(snap, "招商银行"))

    def test_visa_path_still_trips_the_guard(self):
        # 页面上同时有支付宝时守卫会放行，所以单独构造只有卡的快照
        self.assertTrue(card_would_charge({"compact": "信用卡 Visa, Mastercard"}, "信用卡"))


class PaymentClickOrderTests(unittest.TestCase):
    """选付款方式时的 DOM 顺序陷阱。"""

    def test_combined_needles_would_pick_alipay_instead(self):
        # 回归：把想选的和通用名单一起传，命中的是排最前的支付宝
        got = first_click(("招商银行",) + tuple(SCAN_PAY))
        self.assertEqual("支付宝 ALIPAY", got)

    def test_specific_first_then_fallback_picks_the_bank(self):
        # 现在的做法：先单独试指定的那个
        got = first_click(("招商银行",)) or first_click(SCAN_PAY)
        self.assertEqual("招商银行", got)

    def test_fallback_still_works_when_bank_absent(self):
        got = first_click(("交通银行",)) or first_click(SCAN_PAY)
        self.assertEqual("支付宝 ALIPAY", got)


if __name__ == "__main__":
    unittest.main()


class OrderCreatedTests(unittest.TestCase):
    """「订单创建成功」的判定。误报的代价是不再重试 = 没抢到货。"""

    def test_payment_method_name_alone_is_not_proof(self):
        # 回归：「掌上生活」是招商银行 App 的名字，在 Billing 页选付款方式时
        # 就印在页面上。据此判定会在一单没下的时候报成功。
        from hunter.checkout import looks_unpaid
        billing = {"unpaid": False, "thank": False, "order": "",
                   "continueVisible": "检查订单"}
        self.assertFalse(looks_unpaid(billing))

    def test_still_in_wizard_is_not_proof(self):
        from hunter.checkout import looks_unpaid
        self.assertFalse(looks_unpaid(
            {"unpaid": True, "continueVisible": "检查订单", "order": ""}))

    def test_order_number_is_proof(self):
        from hunter.checkout import looks_unpaid
        self.assertTrue(looks_unpaid({"order": "W1234567890"}))

    def test_thankyou_url_is_proof(self):
        from hunter.checkout import looks_unpaid
        self.assertTrue(looks_unpaid({"thank": True}))

    def test_pay_text_alone_is_never_proof(self):
        # 翻页中间态：按钮短暂消失，光靠文案会误报成已下单
        from hunter.checkout import looks_unpaid
        self.assertFalse(looks_unpaid({"unpaid": True, "continueVisible": ""}))


class StepDetectionTests(unittest.TestCase):
    def test_button_text_maps_to_step(self):
        from hunter.checkout import step_from_button
        for text, key in (("继续填写取货详情", "fulfillment"),
                          ("继续选择付款方式", "pickupcontact"),
                          ("检查订单", "billing"),
                          ("现在下单", "review")):
            self.assertEqual(key, step_from_button(text))

    def test_unknown_button_yields_nothing(self):
        from hunter.checkout import step_from_button
        self.assertEqual("", step_from_button("送货与取货常见问题解答"))


class ReviewPageTests(unittest.TestCase):
    """Review 页实测（2026-09-13，secure7 _s=Review）。"""

    def test_place_order_button_is_recognised(self):
        from hunter.checkout import step_from_button
        # 回归：按钮实际叫「立即下单」，代码原来只找「现在下单/确认下单」
        self.assertEqual("review", step_from_button("立即下单"))

    def test_review_wording_is_not_mistaken_for_a_placed_order(self):
        from hunter.checkout import looks_unpaid
        # 回归：Review 页有「取货日期待付款完成后确定」，裸「待付款」会误判成已下单。
        # unpaid 由页面正则算出，这里模拟修好之后的取值：Review 页不该置位。
        review = {"unpaid": False, "thank": False, "order": "",
                  "continueVisible": "立即下单"}
        self.assertFalse(looks_unpaid(review))


class CheckoutHostTests(unittest.TestCase):
    """secureN 主机不能写死：登录一次换一台，不同账号也不一样。"""

    def test_extracts_secure_host(self):
        from hunter.checkout import secure_host_of
        self.assertEqual("secure7.www.apple.com.cn",
                         secure_host_of("https://secure7.www.apple.com.cn/shop/checkout?_s=Review"))

    def test_plain_host_is_not_a_secure_host(self):
        from hunter.checkout import secure_host_of
        self.assertEqual("", secure_host_of("https://www.apple.com.cn/shop/bag"))
        self.assertEqual("", secure_host_of(""))

    def test_open_tab_host_wins(self):
        from hunter.checkout import checkout_candidates
        got = checkout_candidates("cn", seen_urls=[
            "https://www.apple.com.cn/shop/bag",
            "https://secure7.www.apple.com.cn/shop/checkout?_s=Review"])
        self.assertIn("secure7.www.apple.com.cn", got[0])

    def test_remembered_host_beats_the_guesses(self):
        from hunter.checkout import checkout_candidates
        got = checkout_candidates("cn", remembered="secure3.www.apple.com.cn")
        self.assertIn("secure3.www.apple.com.cn", got[0])

    def test_prefixless_url_comes_before_the_guesses(self):
        from hunter.checkout import checkout_candidates, SECURE_FALLBACKS
        got = checkout_candidates("cn")
        plain = next(i for i, u in enumerate(got) if "//www.apple.com.cn" in u)
        first_guess = next(i for i, u in enumerate(got) if SECURE_FALLBACKS[0] in u)
        self.assertLess(plain, first_guess, "先让 Apple 自己路由，再猜主机")

    def test_no_duplicates_and_region_respected(self):
        from hunter.checkout import checkout_candidates
        got = checkout_candidates("hk", seen_urls=["https://secure8.www.apple.com/hk/shop/bag"])
        self.assertEqual(len(got), len(set(got)))
        self.assertTrue(all("apple.com" in u for u in got))


class BillingRetryTests(unittest.TestCase):
    """「我以为点了、系统认为没点」——靠页面提示判断并重试。"""

    def test_known_complaint_phrases_are_recognised(self):
        from hunter.checkout import BILLING_COMPLAINTS
        real = "请从以下支付方式中选择一种进行付款。"
        self.assertTrue(any(p in real for p in BILLING_COMPLAINTS),
                        "这句是实测日志里页面给出的原文，必须能认出来")

    def test_unrelated_text_is_not_a_complaint(self):
        from hunter.checkout import BILLING_COMPLAINTS
        for benign in ("分期付款方案：", "24 期, 每期金额约为 RMB 284",
                       "通过支付宝选择更多银行"):
            self.assertFalse(any(p in benign for p in BILLING_COMPLAINTS), benign)


class StaysCheckedTests(unittest.TestCase):
    """点完要复核：选银行那下的重渲染会把刚点上的期数冲掉。"""

    class FakePage:
        def __init__(self, values):
            self.values = list(values)
            self.waits = 0

        def wait_for_timeout(self, _ms):
            self.waits += 1

        def evaluate(self, _js, _arg=None):
            return self.values.pop(0) if self.values else True

    def placer(self):
        from hunter.checkout import OrderPlacer
        return OrderPlacer(log=lambda *_: None)

    def test_reports_false_when_reset(self):
        page = self.FakePage([True, False, True])
        self.assertFalse(self.placer()._stays_checked(page, "_r_26_", ms=800))

    def test_reports_false_when_element_replaced(self):
        page = self.FakePage([None])
        self.assertFalse(self.placer()._stays_checked(page, "_r_26_", ms=400))

    def test_reports_true_when_it_holds(self):
        page = self.FakePage([True] * 20)
        self.assertTrue(self.placer()._stays_checked(page, "_r_26_", ms=400))

    def test_empty_id_is_treated_as_ok(self):
        self.assertTrue(self.placer()._stays_checked(self.FakePage([]), ""))


class SettleThenClickTests(unittest.TestCase):
    """先等页面稳，再用 Playwright 真点。"""

    def test_pick_js_no_longer_clicks(self):
        # 回归：匹配和点击分开——JS 只负责找元素，点击交给 Playwright，
        # 因为只有它会等元素可见、可点、不再移动。
        from hunter.checkout import JS_PICK_PAYMENT, JS_PICK_TERM
        for js in (JS_PICK_PAYMENT, JS_PICK_TERM):
            self.assertNotIn(".click()", js)

    def test_settle_watches_dom_not_a_fixed_sleep(self):
        from hunter.checkout import JS_SETTLE_WATCH, JS_SETTLE_CHECK
        self.assertIn("MutationObserver", JS_SETTLE_WATCH)
        self.assertIn("aria-busy", JS_SETTLE_CHECK)
        self.assertIn("readyState", JS_SETTLE_CHECK)

    def test_label_selector_survives_dotted_ids(self):
        # 付款选项的 id 带点：checkout.billing.billingoptions.installments0001321713
        # 属性选择器里加引号就不用转义，别改成 #id 形式
        from hunter.checkout import click_label

        class Page:
            def __init__(self): self.sel = None
            def wait_for_timeout(self, _ms): pass
            def locator(self, sel):
                self.sel = sel
                page = self
                class L:
                    def count(self): return 1
                    @property
                    def first(self): return self
                    def inner_text(self, **_kw): return "标签"
                    def click(self, **_kw): page.clicked = True
                return L()

        pg = Page()
        self.assertTrue(click_label(pg, "checkout.billing.billingoptions.credit"))
        self.assertEqual('label[for="checkout.billing.billingoptions.credit"]', pg.sel)


class SignInShortCircuitTests(unittest.TestCase):
    """撞上登录页时别再换主机——换主机解决不了没登录。"""

    class Page:
        def __init__(self, url):
            self.url = url
            self.polls = 0
        def wait_for_timeout(self, _ms):
            self.polls += 1
        def evaluate(self, *_a, **_k):
            return None

    def test_on_checkout_bails_immediately_on_signin(self):
        import time
        from hunter.checkout import on_checkout
        page = self.Page("https://idmsa.apple.com/appleauth/auth/signin")
        t = time.monotonic()
        self.assertFalse(on_checkout(page, timeout_ms=3000))
        self.assertLess(time.monotonic() - t, 0.5, "登录页应当立刻返回，不该轮询到超时")
        self.assertEqual(0, page.polls)

    def test_on_checkout_still_polls_for_a_real_checkout_page(self):
        from hunter.checkout import on_checkout
        page = self.Page("https://secure7.www.apple.com.cn/shop/checkout?_s=Fulfillment-init")
        self.assertFalse(on_checkout(page, timeout_ms=600))
        self.assertGreater(page.polls, 0, "非登录页要给它时间渲染")

    def test_enter_stops_at_the_first_signin_redirect(self):
        from hunter.checkout import OrderPlacer

        class Ctx:
            pages = []

        placer = OrderPlacer(log=lambda *_: None)
        landed = self.Page("https://idmsa.apple.com/appleauth/auth/signin")
        tried = []

        def goto(_ctx, _page, url):
            tried.append(url)
            return landed

        placer._goto_checkout = goto
        got = placer.enter(Ctx(), self.Page("https://www.apple.com.cn/shop/bag"))
        self.assertIs(landed, got)
        self.assertEqual(1, len(tried), f"只该试第一个候选，实际试了 {tried}")


class BillingSubmitLoopTests(unittest.TestCase):
    """「DOM 已选中、页面仍说没选」的处理。"""

    class Page:
        def wait_for_timeout(self, _ms): pass

    def placer(self, **kw):
        from hunter.checkout import OrderPlacer
        return OrderPlacer(log=kw.pop("log", lambda *_: None), **kw)

    def test_submits_before_reading_the_complaint(self):
        """回归：原来在循环里只重选不提交，读到的永远是上一次留下的旧提示。"""
        import hunter.checkout as C
        order = []
        p = self.placer(log=order.append)
        p._fill_billing = lambda page, force_change=False: order.append(
            f"fill(force={force_change})") or True
        p._billing_error = lambda page: ""
        with mock.patch.object(C, "continue_when_ready",
                               lambda *a, **k: order.append("submit") or "检查订单"), \
             mock.patch.object(C, "card_would_charge", lambda *a: False), \
             mock.patch.object(C, "snapshot", lambda p: {}):
            p._step_billing(self.Page(), {})
        self.assertLess(order.index("fill(force=False)"), order.index("submit"))
        self.assertIn("submit", order)

    def test_retry_forces_a_real_change_event(self):
        """第二轮起必须弹开再选回来——已 checked 的 radio 再点不派发 change。"""
        import hunter.checkout as C
        forces = []
        p = self.placer()
        p._fill_billing = lambda page, force_change=False: forces.append(force_change) or True
        p._billing_error = lambda page: "请从以下支付方式中选择一种进行付款。"
        with mock.patch.object(C, "continue_when_ready", lambda *a, **k: "检查订单"), \
             mock.patch.object(C, "card_would_charge", lambda *a: False), \
             mock.patch.object(C, "snapshot", lambda p: {}):
            why = p._step_billing(self.Page(), {})
        self.assertEqual([False, True, True], forces)
        self.assertIn("没能提交", why)

    def test_stops_as_soon_as_the_page_moves_on(self):
        import hunter.checkout as C
        p = self.placer()
        p._fill_billing = lambda page, force_change=False: True
        p._billing_error = lambda page: "不该被读到"
        with mock.patch.object(C, "continue_when_ready", lambda *a, **k: C.MOVED_ON), \
             mock.patch.object(C, "card_would_charge", lambda *a: False), \
             mock.patch.object(C, "snapshot", lambda p: {}):
            self.assertIn("已进入下一步", p._step_billing(self.Page(), {}))

    def test_sibling_lookup_skips_credit_card(self):
        from hunter.checkout import JS_SIBLING_OPTION
        # 弹开时绝不能弹到信用卡上——那条是即时扣款
        self.assertIn('endsWith(".credit")', JS_SIBLING_OPTION)
