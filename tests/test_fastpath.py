import unittest

from hunter.checkout import OrderPlacer
from hunter.fastpath import BLOCK_CODES, Blocked, FastCheckout, encode, new_call_id


class FakePage:
    """按顺序吐出预设响应，并记下每次请求的参数。"""

    def __init__(self, responses, html='"x-aos-stk":"TOKEN1234567890123456789"'):
        self._r = list(responses)
        self._html = html
        self.calls = []

    def evaluate(self, js, arg=None):
        if arg is None:                       # JS_READ_STK
            import re
            m = re.search(r'["\']x-aos-stk["\']\s*:\s*["\']([^"\']+)', self._html)
            return m.group(1) if m else ""
        path, query, body, stk, call_id = arg
        self.calls.append({"path": path, "query": query, "body": body,
                           "stk": stk, "call_id": call_id})
        return self._r.pop(0) if self._r else {"status": 200, "json": {}}

    def wait_for_timeout(self, _ms):
        pass


def resp(section, extra=None):
    """造一个真实形状的响应：body.checkout 里带着该步应当产出的那一节。
    少了这一节就算「没推进」——这是 200 之外的硬判据。"""
    node = {"d": {}}
    if extra:
        node.update(extra)
    return {"status": 200, "json": {"head": {"status": 200},
                                    "body": {"checkout": {section: node}}}}


FUL = resp("fulfillment")                       # 第 1、2 步
#: 第 3 步：Apple 按账号预填好姓名/邮箱/电话，身份证后四位留空由用户填
CONTACT = resp("pickupContact", {"selfPickupContact": {
    "selfContact": {"address": {
        "d": {"lastName": "张", "firstName": "三", "emailAddress": "a@b.c"},
        "was": {"lastName": "旧姓"}}},
    "nationalIdSelf": {"d": {"nationalIdSelf": ""}}}})
BANKS = resp("billing", {"options": [           # 第 4 步：带银行选项
    {"labelImageAlt": "招商银行", "value": "installments0001321713"},
    {"labelImageAlt": "中国建设银行", "value": "installments0009999999"},
]})
MONTHS = resp("billing", {"installmentOptions": {"selectInstallmentOption": 0, "options": [
    {"value": 1, "label": "1 期"}, {"value": 12, "label": "12 期"},
    {"value": 24, "label": "24 期"},
]}})                                            # 第 5 步
REVIEW = resp("review")                         # 第 6 步
#: 一条完整的顺利路径
HAPPY = [FUL, FUL, CONTACT, BANKS, MONTHS, REVIEW]
OK = FUL


def placer(**kw):
    base = {"store": "R581", "id_last4": "0000", "last_name": "张",
            "first_name": "三", "log": lambda *a: None,
            "stk_timeout_ms": 50}          # 测试里别真等 15 秒
    base.update(kw)
    return FastCheckout(**base)


class HelperTests(unittest.TestCase):
    def test_call_id_shape_matches_the_real_one(self):
        """实测形如 m072unh8yy-mu0u0tf3。服务端不校验内容，但形状要对得上。"""
        a, b = new_call_id(), new_call_id()
        self.assertNotEqual(a, b)
        head, _, tail = a.partition("-")
        self.assertEqual(10, len(head))
        self.assertTrue(tail)

    def test_encode_keeps_empty_values(self):
        """verificationToken / selectBank 实测就是空值，不能被丢掉。"""
        self.assertEqual("a=&b=1", encode([("a", ""), ("b", "1")]))


class OptionLookupTests(unittest.TestCase):
    def test_finds_bank_by_logo_alt(self):
        self.assertEqual("installments0001321713",
                         placer().find_billing_option(BANKS["json"]))

    def test_returns_empty_when_bank_absent(self):
        p = placer(payment_label="花呗")
        self.assertEqual("", p.find_billing_option(BANKS["json"]))

    def test_finds_requested_installment(self):
        self.assertEqual(24, placer().find_installment(MONTHS["json"], 24))

    def test_falls_back_to_longest_when_requested_absent(self):
        self.assertEqual(24, placer().find_installment(MONTHS["json"], 36))

    def test_zero_when_no_options(self):
        self.assertEqual(0, placer().find_installment({}, 24))


class WaitForTokenTests(unittest.TestCase):
    """令牌写在服务端返回的 HTML 里，domcontentloaded 就有，不用等 React。
    所以读不到时要**轮询等**——读一次就放弃，等于在页面最慢、最需要发包的
    时候主动退回到更慢的点页面那条路。"""

    class Slow:
        """前 n 次读不到令牌，之后才有。"""

        def __init__(self, n):
            self.n, self.reads = n, 0

        def evaluate(self, js, arg=None):
            if arg is None:
                self.reads += 1
                return "TOKEN1234567890123456789" if self.reads > self.n else ""
            return {"status": 200, "json": {}}

        def wait_for_timeout(self, _ms):
            pass

    def test_keeps_polling_until_token_shows_up(self):
        p = self.Slow(3)
        self.assertTrue(placer().wait_for_stk(p, timeout_ms=5000))
        self.assertEqual(4, p.reads)

    def test_survives_context_destroyed_while_navigating(self):
        class Boom(self.Slow):
            def evaluate(self, js, arg=None):
                self.reads += 1
                if self.reads < 3:
                    raise RuntimeError("Execution context was destroyed")
                return "TOKEN1234567890123456789"
        p = Boom(0)
        self.assertTrue(placer().wait_for_stk(p, timeout_ms=5000))

    def test_gives_up_after_timeout(self):
        p = self.Slow(10 ** 6)
        self.assertEqual("", placer().wait_for_stk(p, timeout_ms=30))


class RunTests(unittest.TestCase):
    def test_six_posts_in_order_and_stops_at_review(self):
        page = FakePage(list(HAPPY))
        ok, stage, detail = placer().run(page)
        self.assertTrue(ok, stage)
        self.assertEqual(6, len(page.calls))
        actions = [c["query"] for c in page.calls]
        self.assertIn("_a=selectFulfillmentLocationAction", actions[0])
        self.assertIn("_a=continueFromBillingToReview", actions[5])
        # 硬边界：任何一步都不能指向下单
        for c in page.calls:
            self.assertNotIn("placeOrder", c["query"] + c["body"])

    def test_sends_store_number_and_id_last4(self):
        page = FakePage(list(HAPPY))
        placer().run(page)
        self.assertIn("R581", page.calls[1]["body"])
        self.assertIn("0000", page.calls[3]["body"])

    def test_selected_bank_id_is_carried_into_last_two_steps(self):
        """这个 id 随会话变，必须从第 4 步响应里抓，不能写死。"""
        page = FakePage(list(HAPPY))
        placer().run(page)
        self.assertIn("installments0001321713", page.calls[4]["body"])
        self.assertIn("installments0001321713", page.calls[5]["body"])
        self.assertIn("selectInstallmentOption=24", page.calls[5]["body"])

    def test_stops_immediately_when_blocked(self):
        """被拦不重试、不换路径——越撞退避越深（README 坑 9）。"""
        for code in BLOCK_CODES:
            page = FakePage([FUL, {"status": code, "json": None}] + HAPPY[2:])
            ok, stage, _ = placer().run(page)
            self.assertFalse(ok)
            self.assertIn("限流", stage)
            self.assertEqual(2, len(page.calls))   # 第二步就停

    def test_bails_out_when_token_missing(self):
        page = FakePage([FUL], html="<html>没有令牌</html>")
        ok, stage, _ = placer().run(page)
        self.assertFalse(ok)
        self.assertIn("x-aos-stk", stage)
        self.assertEqual([], page.calls)           # 一个请求都没发

    def test_bails_out_when_bank_not_offered(self):
        empty = resp("billing")                    # 走到了 Billing，但没有银行选项
        page = FakePage([FUL, FUL, CONTACT, empty, MONTHS, REVIEW])
        ok, stage, detail = placer(payment_label="招商银行").run(page)
        self.assertFalse(ok)
        self.assertIn("付款方式", stage)
        self.assertIn("一个都没有", detail)         # 诊断信息要说清楚
        self.assertEqual(4, len(page.calls))       # 不往下走


class StoreNumberGuardTests(unittest.TestCase):
    """selectStore 只认编号。喂名字不会报错，只是静默选不中——
    所以拿不到编号时宁可不走快车道。"""

    def test_keeps_only_store_numbers(self):
        o = OrderPlacer(store_numbers=["五角场", "R581", "r359", "R581", ""])
        self.assertEqual(["R581", "R359"], o.store_numbers)

    def test_names_alone_yield_nothing(self):
        o = OrderPlacer(store_numbers=["五角场", "南京东路"])
        self.assertEqual([], o.store_numbers)


class StalledStepTests(unittest.TestCase):
    """200 不等于这一步生效了。实测栽过：四步全 200，服务端却一直停在
    Fulfillment，于是找不到付款选项，退回点页面又对着错位的 DOM 死点。"""

    def test_detects_a_step_that_did_not_advance(self):
        # 第 3 步本该产出 pickupContact，却还是 fulfillment
        page = FakePage([FUL, FUL, FUL, BANKS, MONTHS, REVIEW])
        ok, stage, detail = placer().run(page)
        self.assertFalse(ok)
        self.assertIn("没生效", stage)
        self.assertIn("pickupContact", detail)
        self.assertEqual(3, len(page.calls))        # 立刻停，不往下走

    def test_detects_stall_at_billing(self):
        page = FakePage([FUL, FUL, CONTACT, CONTACT, MONTHS, REVIEW])
        ok, stage, detail = placer().run(page)
        self.assertFalse(ok)
        self.assertIn("billing", detail)
        self.assertEqual(4, len(page.calls))

    def test_detail_tells_caller_to_reload(self):
        """状态可能已经被改过，调用方必须重新加载页面再接管。"""
        page = FakePage([FUL, FUL, FUL, BANKS, MONTHS, REVIEW])
        _, _, detail = placer().run(page)
        self.assertIn("重新加载", detail)

    def test_happy_path_passes_the_same_check(self):
        page = FakePage(list(HAPPY))
        ok, stage, _ = placer().run(page)
        self.assertTrue(ok, stage)

    def test_lists_available_payment_labels_on_mismatch(self):
        """对不上时要把可选项打出来，否则下次还是只能猜。"""
        page = FakePage([FUL, FUL, CONTACT, BANKS, MONTHS, REVIEW])
        _, _, detail = placer(payment_label="花呗").run(page)
        self.assertIn("招商银行", detail)
        self.assertIn("中国建设银行", detail)


class ContactHarvestTests(unittest.TestCase):
    """取货人信息用 Apple 预填的那份，别让用户在 config 里重填一遍。
    实测发空姓名过去，服务端返回 200 却停在原地——就是「200 不等于生效」的现场。"""

    MODEL = {"x": {"d": {"lastName": "张", "firstName": "三"},
                   "was": {"lastName": "旧姓", "firstName": "旧名"}},
             "y": {"d": {"nationalIdSelf": ""}}}

    def test_takes_current_value_not_previous(self):
        self.assertEqual("张", FastCheckout._harvest(self.MODEL, "lastName"))
        self.assertEqual("三", FastCheckout._harvest(self.MODEL, "firstName"))

    def test_uses_prefilled_name_when_config_empty(self):
        f = placer(last_name="", first_name="")
        got = {k.rsplit(".", 1)[-1]: v for k, v in f.contact_fields(self.MODEL)}
        self.assertEqual("张", got["lastName"])
        self.assertEqual("0000", got["nationalIdSelf"])   # 这个只能来自 config

    def test_config_overrides_prefilled(self):
        f = placer(last_name="李", first_name="四")
        got = {k.rsplit(".", 1)[-1]: v for k, v in f.contact_fields(self.MODEL)}
        self.assertEqual("李", got["lastName"])

    def test_stalls_with_clear_reason_when_name_unavailable(self):
        """两边都没有姓名时，与其发一个注定失败的请求，不如直接说清楚。"""
        bare = resp("pickupContact")          # 什么都没预填
        page = FakePage([FUL, FUL, bare, BANKS, MONTHS, REVIEW])
        ok, stage, detail = placer(last_name="", first_name="").run(page)
        self.assertFalse(ok)
        self.assertIn("lastName", detail)
        self.assertEqual(3, len(page.calls))   # 第 4 步根本没发出去

    def test_stalls_when_id_last4_missing(self):
        page = FakePage([FUL, FUL, CONTACT, BANKS, MONTHS, REVIEW])
        f = FastCheckout(store="R581", id_last4="", last_name="", first_name="",
                         stk_timeout_ms=50, log=lambda *a: None)
        ok, _, detail = f.run(page)
        self.assertFalse(ok)
        self.assertIn("id_last4", detail)


class PlaceOrderTests(unittest.TestCase):
    """提交订单可以（只创建待付款单），代人付款不行。
    而且「成不成」只认硬证据——误报成功会让人以为抢到了，实际购物袋还在。"""

    PLACED = {"status": 200, "json": {"head": {"status": 302, "data": {
        "url": "/shop/checkout/status"}}}}

    @staticmethod
    def _status(url):
        return {"status": 200, "json": {"head": {"status": 302, "data": {"url": url}}}}

    def test_places_order_and_reports_success(self):
        page = FakePage(HAPPY + [self.PLACED,
                         self._status("https://secure6.www.apple.com.cn/shop/checkout/thankyou")])
        ok, stage, detail = placer(place_order=True).run(page)
        self.assertTrue(ok, stage)
        self.assertIn("待付款", stage)
        self.assertEqual(8, len(page.calls))
        self.assertIn("_a=continueFromReviewToProcess", page.calls[6]["query"])
        self.assertIn("_a=checkStatus", page.calls[7]["query"])

    def test_reports_rejection_when_bounced_back_to_checkout(self):
        """实测场景：六步全通，提交后被打回结账页。绝不能报成功。"""
        page = FakePage(HAPPY + [self.PLACED,
                         self._status("https://secure6.www.apple.com.cn/shop/checkout")])
        ok, stage, detail = placer(place_order=True).run(page)
        self.assertFalse(ok)
        self.assertIn("驳回", stage)
        self.assertIn("不再为本订单提供", detail)

    def test_still_processing_is_not_success(self):
        """轮询耗尽时最后拿到的还是 status 页——这必须算失败，不能算成功。"""
        page = FakePage(HAPPY + [self.PLACED]
                        + [self._status("/shop/checkout/status")] * 12)
        ok, stage, _ = placer(place_order=True).run(page)
        self.assertFalse(ok)
        self.assertIn("驳回", stage)

    def test_never_places_on_card_payment(self):
        """信用卡点下单是即时扣款，等于代人付款——硬拒，不给配置绕过。"""
        cards = resp("billing", {"options": [
            {"labelImageAlt": "信用卡", "value": "card001"}]})
        page = FakePage([FUL, FUL, CONTACT, cards, MONTHS, REVIEW, self.PLACED])
        ok, stage, _ = placer(place_order=True, payment_label="信用卡").run(page)
        self.assertEqual(6, len(page.calls))      # 六步走完，第七步没发
        self.assertTrue(ok)                       # 到 Review 算成功
        self.assertIn("Review", stage)

    def test_does_not_place_when_disabled(self):
        page = FakePage(list(HAPPY))
        ok, _, _ = placer(place_order=False).run(page)
        self.assertTrue(ok)
        self.assertEqual(6, len(page.calls))

    def test_order_rejected_only_trusts_hard_evidence(self):
        F = FastCheckout
        self.assertFalse(F.order_rejected(".../shop/checkout/thankyou"))
        self.assertFalse(F.order_rejected("https://www.apple.com.cn/shop/order/list"))
        for bad in ("https://secure6.www.apple.com.cn/shop/checkout",
                    "/shop/checkout/status", "", "/shop/bag"):
            self.assertTrue(F.order_rejected(bad), bad)


if __name__ == "__main__":
    unittest.main()


class BagToCheckoutTests(unittest.TestCase):
    """跳过加载购物袋页直接进结账。服务端侧这一段只要 ~2.2s，
    而「加载 259KB 购物袋页 → 等安静 → 找按钮 → 点」要 ~8s。
    请求体带着每单不同的购物车条目 id，没法写死，所以发空体试——
    响应会明确说成没成，不成就退回点页面，不用猜。"""

    class Page:
        def __init__(self, stk, result):
            self.result, self.calls = result, 0
            # 购物袋状态：令牌 + 件数 + 型号，都从同一份 HTML 里取
            self.state = ({"stk": stk, "count": 1, "cart": True, "skus": ["MG6W4CH/A"],
                           "qty": [1], "origin": "https://www.apple.com.cn"}
                          if stk else {"stk": "", "count": None, "cart": False,
                                       "skus": [], "qty": [],
                                       "origin": "https://www.apple.com.cn"})

        def evaluate(self, js, arg=None):
            self.calls += 1
            return self.result if arg is not None else self.state

    def test_returns_checkout_url_on_success(self):
        from hunter.fastpath import bag_to_checkout
        p = self.Page("CARTTOKEN", {"status": 200, "head": 302,
                                    "url": "https://secure8.www.apple.com.cn/shop/checkout/start?x=1"})
        self.assertIn("/shop/checkout", bag_to_checkout(p, log=lambda *a: None))

    def test_bails_when_cart_token_missing(self):
        from hunter.fastpath import bag_to_checkout
        p = self.Page("", None)
        self.assertEqual("", bag_to_checkout(p, log=lambda *a: None))
        self.assertEqual(1, p.calls)          # 没令牌就别发请求

    def test_bails_when_no_checkout_url_returned(self):
        from hunter.fastpath import bag_to_checkout
        p = self.Page("CARTTOKEN", {"status": 200, "head": 200, "url": ""})
        self.assertEqual("", bag_to_checkout(p, log=lambda *a: None))

    def test_bails_when_url_is_not_checkout(self):
        """别把随便一个跳转当成功——退回点页面是安全的，跳错地方不是。"""
        from hunter.fastpath import bag_to_checkout
        p = self.Page("CARTTOKEN", {"status": 200, "head": 302,
                                    "url": "https://www.apple.com.cn/shop/bag"})
        self.assertEqual("", bag_to_checkout(p, log=lambda *a: None))

    def test_survives_evaluate_errors(self):
        from hunter.fastpath import bag_to_checkout

        class Boom:
            def evaluate(self, js, arg=None):
                raise RuntimeError("context destroyed")
        self.assertEqual("", bag_to_checkout(Boom(), log=lambda *a: None))


class CartVerifyTests(unittest.TestCase):
    """只数件数保证不了型号。清袋静默失败时件数一样是 1，然后就买错机器了——
    原来的流程靠「先清袋再加购」保证型号，但加购之后从没复核过。"""

    class Page:
        def __init__(self, state, result=None):
            self.state, self.result = state, result
            self.posted = False

        def evaluate(self, js, arg=None):
            if arg is None:
                return self.state
            self.posted = True
            return self.result or {"status": 200, "head": 302,
                                   "url": "https://secure8.www.apple.com.cn/shop/checkout"}

    @staticmethod
    def state(count, skus, stk="CARTTOKEN", qty=None):
        return {"stk": stk, "count": count, "cart": True, "skus": skus,
                "qty": qty if qty is not None else [1] * max(count, 0),
                "origin": "https://www.apple.com.cn"}

    def _run(self, st, want):
        from hunter.fastpath import bag_to_checkout
        p = self.Page(st)
        return bag_to_checkout(p, want_part=want, log=lambda *a: None), p

    def test_passes_when_cart_holds_exactly_the_target(self):
        url, p = self._run(self.state(1, ["MG6W4CH/A"]), "MG6W4CH/A")
        self.assertIn("/shop/checkout", url)
        self.assertTrue(p.posted)

    def test_rejects_wrong_model_even_when_count_looks_right(self):
        from hunter.fastpath import CartMismatch
        with self.assertRaises(CartMismatch) as cm:
            self._run(self.state(1, ["MG704CH/A"]), "MG6W4CH/A")
        self.assertIn("MG704CH/A", str(cm.exception))
        self.assertIn("MG6W4CH/A", str(cm.exception))

    def test_rejects_extra_item_alongside_target(self):
        """袋里多一台别的，件数 2 仍在限购内，但订单就不是你要的了。"""
        from hunter.fastpath import CartMismatch
        with self.assertRaises(CartMismatch):
            self._run(self.state(2, ["MG6W4CH/A", "MJY64CH/A"]), "MG6W4CH/A")

    def test_case_insensitive_match(self):
        url, _ = self._run(self.state(1, ["mg6w4ch/a"]), "MG6W4CH/A")
        self.assertIn("/shop/checkout", url)

    def test_refuses_when_empty(self):
        url, p = self._run(self.state(0, []), "MG6W4CH/A")
        self.assertEqual("", url)
        self.assertFalse(p.posted)          # 空袋子不发请求

    def test_refuses_when_over_purchase_limit(self):
        url, p = self._run(self.state(3, ["MG6W4CH/A"]), "")
        self.assertEqual("", url)
        self.assertFalse(p.posted)

    def test_refuses_when_sku_unreadable(self):
        """读不出型号就别赌——退回点页面是安全的。"""
        url, p = self._run(self.state(1, []), "MG6W4CH/A")
        self.assertEqual("", url)
        self.assertFalse(p.posted)

    def test_no_want_part_skips_model_check(self):
        url, _ = self._run(self.state(1, ["ANYTHING/A"]), "")
        self.assertIn("/shop/checkout", url)


class BaggedPartTests(unittest.TestCase):
    """只记「加过了」不够，必须记**加的是哪个 part**。

    监控盯着十几个配置：A 色放货、加购后失败重试，下一轮命中的可能是 B 色。
    这时如果只看「加过了」就跳过清袋和加购，等于拿 A 色去给 B 色结账。
    """

    @staticmethod
    def _ab(bagged):
        from hunter.autobuy import AutoBuy
        ab = AutoBuy.__new__(AutoBuy)
        ab.bagged_part = bagged
        return ab

    @staticmethod
    def _bagged_ok(ab, want):
        # 跟 _drive 里那个判断保持一致
        return bool(ab.bagged_part) and (not want or ab.bagged_part == want)

    def test_same_part_skips_readd(self):
        self.assertTrue(self._bagged_ok(self._ab("MG6W4CH/A"), "MG6W4CH/A"))

    def test_different_part_forces_clear_and_readd(self):
        """这就是会买错颜色的那条路。"""
        self.assertFalse(self._bagged_ok(self._ab("MG704CH/A"), "MG6W4CH/A"))

    def test_empty_bag_forces_add(self):
        self.assertFalse(self._bagged_ok(self._ab(""), "MG6W4CH/A"))

    def test_unknown_target_trusts_existing_bag(self):
        """拿不到目标 part（比如没传 url）时不强行重来，保持原行为。"""
        self.assertTrue(self._bagged_ok(self._ab("MG6W4CH/A"), ""))


class PrepareBagTests(unittest.TestCase):
    """用接口清购物袋，别打开购物袋页。

    「袋里已经正好是目标型号就跳过加购」这个优化**不会因此失效**——恰恰相反：
    判断依据从进程内的布尔量换成了服务端的真实状态，进程重启、上一轮加的是
    别的颜色，都骗不过它。
    """

    class Page:
        def __init__(self, state, delete=None):
            self.state = state
            self.delete = delete or {"status": 200, "left": 0}
            self.deleted = []

        def evaluate(self, js, arg=None):
            if arg is None:
                return self.state
            self.deleted.append(arg[0])
            return self.delete

    @staticmethod
    def st(items, skus, stk="CARTTOKEN", count=None, qty=None):
        return {"stk": stk, "cart": True, "items": items, "skus": skus,
                "count": count if count is not None else len(items),
                "qty": qty if qty is not None else [1] * len(items),
                "origin": "https://www.apple.com.cn"}

    def _run(self, page, want="MG6W4CH/A"):
        from hunter.fastpath import prepare_bag
        return prepare_bag(page, want_part=want, log=lambda *a: None)

    def test_two_of_the_same_model_is_not_kept(self):
        """实测中招：袋里两台同型号，sku 集合仍然只有一个元素，光比集合放行了。"""
        p = self.Page(self.st(["item-a", "item-b"], ["MG6W4CH/A"], count=2))
        r = self._run(p)
        self.assertFalse(r["kept"])
        self.assertEqual(["item-a", "item-b"], p.deleted)

    def test_single_line_with_quantity_two_is_not_kept(self):
        """一条目、数量 2 也是两台。"""
        p = self.Page(self.st(["item-a"], ["MG6W4CH/A"], count=2, qty=[2]))
        r = self._run(p)
        self.assertFalse(r["kept"])
        self.assertEqual(["item-a"], p.deleted)

    def test_refuses_when_page_is_on_another_origin(self):
        """页面停在 secureN 上时，fetch('/shop/bag') 打的是 secureN，读回来是空的——
        照这个结果跳过清空，就会又加一台变成两台。实测中招过。"""
        st = self.st(["item-a"], ["MG704CH/A"])
        st["origin"] = "https://secure8.www.apple.com.cn"
        p = self.Page(st)
        from hunter.fastpath import prepare_bag
        r = prepare_bag(p, want_part="MG6W4CH/A",
                        want_origin="https://www.apple.com.cn", log=lambda *a: None)
        self.assertFalse(r["ok"])
        self.assertEqual([], p.deleted)          # 不可信就什么都别删

    def test_keeps_bag_when_it_already_holds_exactly_the_target(self):
        p = self.Page(self.st(["item-a"], ["MG6W4CH/A"]))
        r = self._run(p)
        self.assertTrue(r["ok"])
        self.assertTrue(r["kept"])
        self.assertEqual([], p.deleted)          # 一条都没删

    def test_clears_when_bag_holds_a_different_model(self):
        p = self.Page(self.st(["item-a"], ["MG704CH/A"]))
        r = self._run(p)
        self.assertTrue(r["ok"])
        self.assertFalse(r["kept"])
        self.assertEqual(["item-a"], p.deleted)

    def test_clears_every_item_when_several_present(self):
        p = self.Page(self.st(["item-a", "item-b"], ["MG6W4CH/A", "MJY64CH/A"]))
        r = self._run(p)
        self.assertEqual(2, r["removed"])
        self.assertEqual(["item-a", "item-b"], p.deleted)

    def test_empty_bag_is_a_noop(self):
        p = self.Page(self.st([], []))
        r = self._run(p)
        self.assertTrue(r["ok"])
        self.assertFalse(r["kept"])
        self.assertEqual([], p.deleted)

    def test_reports_failure_so_caller_falls_back_to_clicking(self):
        p = self.Page(self.st(["item-a"], ["MG704CH/A"]),
                      delete={"status": 500})
        r = self._run(p)
        self.assertFalse(r["ok"])
        self.assertIn("500", r["reason"])

    def test_refuses_without_cart_token(self):
        p = self.Page(self.st(["item-a"], ["MG704CH/A"], stk=""))
        r = self._run(p)
        self.assertFalse(r["ok"])
        self.assertEqual([], p.deleted)


class CartQuantityTests(unittest.TestCase):
    """进结账前必须校验**总台数**，不能只比型号。
    实测中招：清空没成、又加了一台，袋里两台同型号，sku 集合仍然只有一个元素。"""

    class Page:
        def __init__(self, state):
            self.state, self.posted = state, False

        def evaluate(self, js, arg=None):
            if arg is None:
                return self.state
            self.posted = True
            return {"status": 200, "head": 302,
                    "url": "https://secure8.www.apple.com.cn/shop/checkout"}

    @staticmethod
    def st(count, skus, qty):
        return {"stk": "T", "cart": True, "count": count, "skus": skus, "qty": qty,
                "items": [f"item-{i}" for i in range(count)],
                "origin": "https://www.apple.com.cn"}

    def _run(self, page, qty_want=1):
        from hunter.fastpath import bag_to_checkout
        return bag_to_checkout(page, want_part="MG6W4CH/A", want_qty=qty_want,
                               want_origin="https://www.apple.com.cn",
                               log=lambda *a: None)

    def test_one_unit_passes(self):
        p = self.Page(self.st(1, ["MG6W4CH/A"], [1]))
        self.assertIn("/shop/checkout", self._run(p))

    def test_two_of_same_model_is_rejected(self):
        from hunter.fastpath import CartMismatch
        p = self.Page(self.st(2, ["MG6W4CH/A"], [1, 1]))
        with self.assertRaises(CartMismatch) as cm:
            self._run(p)
        self.assertIn("2 台", str(cm.exception))
        self.assertFalse(p.posted)          # 不带着两台去结账

    def test_single_line_quantity_two_is_rejected(self):
        from hunter.fastpath import CartMismatch
        p = self.Page(self.st(1, ["MG6W4CH/A"], [2]))
        with self.assertRaises(CartMismatch):
            self._run(p)

    def test_wrong_origin_falls_back_without_posting(self):
        st = self.st(1, ["MG6W4CH/A"], [1])
        st["origin"] = "https://secure8.www.apple.com.cn"
        p = self.Page(st)
        self.assertEqual("", self._run(p))
        self.assertFalse(p.posted)
