import unittest

from hunter.checkout import OrderPlacer
from hunter.fastpath import BLOCK_CODES, Blocked, FastCheckout, encode, new_call_id


class FakePage:
    """按顺序吐出预设响应，并记下每次请求的参数。"""

    #: 假页面也要有 url：提交之后 step8 会跟着服务端给的跳转真的导航过去，
    #: 而 follow() 要靠它拼出 origin。
    url = "https://secure6.www.apple.com.cn/shop/checkout?_s=Review"

    def __init__(self, responses, html='"x-aos-stk":"TOKEN1234567890123456789"'):
        self._r = list(responses)
        self._html = html
        self.calls = []
        self.gotos = []

    def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = url

    def evaluate(self, js, arg=None):
        if arg is None:                       # JS_READ_STK
            import re
            m = re.search(r'["\']x-aos-stk["\']\s*:\s*["\']([^"\']+)', self._html)
            return m.group(1) if m else ""
        path, query, body, stk, call_id, model_page = arg
        self.calls.append({"path": path, "query": query, "body": body,
                           "stk": stk, "call_id": call_id, "model_page": model_page})
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


#: 2026-09-17 起自提必须选具体时段：第 2 步的响应里带着 timeSlot 模块，拿不到
#: 就是拿不到这家店的货（实测 9/9 会死在第 3 步）。固件按现在的真实形状来，
#: 老流程（没有这一组）另有 FUL_NO_SLOT 专门覆盖。
_SLOT = {"checkInStart": "2026-09-18T10:00", "checkInEnd": "2026-09-18T10:15",
         "displayStart": "10:00 AM", "displayEnd": "10:15 AM",
         "timeSlotType": "regular", "SlotId": "SLOT-1", "signKey": "SIGN-1",
         "timeZone": "Asia/Shanghai", "timeSlotValue": "18-10:00-10:15",
         "isRestricted": False}
_SLOT_MODEL = {"d": {"dayRadio": "18",
                     "pickUpDates": [{"dayOfMonth": "18", "date": "2026-09-18"}],
                     "timeSlotWindows": [{"18": [_SLOT]}]}}
FUL = resp("fulfillment", {"pickupTab": {"pickup": {
    "timeSlot": {"dateTimeSlots": _SLOT_MODEL}}}})   # 第 1、2 步
FUL_NO_SLOT = resp("fulfillment")               # 老流程：整个时段模块都没有
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

    def test_refuses_to_change_months_when_requested_absent(self):
        self.assertEqual(0, placer().find_installment(MONTHS["json"], 36))

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

    def test_detail_reports_stop_without_page_fallback(self):
        """状态不一致就停止，不再通过刷新和点击尝试挽救。"""
        page = FakePage([FUL, FUL, FUL, BANKS, MONTHS, REVIEW])
        _, _, detail = placer().run(page)
        self.assertIn("本次尝试已停止", detail)

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
    """取货人信息优先用 Apple 预填的那份，config 里的只在账号没带出来时兜底。
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

    def test_prefilled_name_wins_over_config(self):
        """账号带出来的名字才是 Apple 认的（取货要跟证件对得上），别拿配置顶掉。"""
        f = placer(last_name="李", first_name="四")
        got = {k.rsplit(".", 1)[-1]: v for k, v in f.contact_fields(self.MODEL)}
        self.assertEqual("张", got["lastName"])
        self.assertEqual("三", got["firstName"])

    def test_config_name_fills_in_when_account_has_none(self):
        blank = {"x": {"d": {"lastName": "", "firstName": ""}},
                 "y": {"d": {"nationalIdSelf": ""}}}
        f = placer(last_name="李", first_name="四")
        got = {k.rsplit(".", 1)[-1]: v for k, v in f.contact_fields(blank)}
        self.assertEqual("李", got["lastName"])
        self.assertEqual("四", got["firstName"])

    def test_stalls_with_clear_reason_when_name_unavailable(self):
        """两边都没有姓名时，与其发一个注定失败的请求，不如直接说清楚。"""
        bare = resp("pickupContact")          # 什么都没预填
        page = FakePage([FUL, FUL, bare, BANKS, MONTHS, REVIEW])
        ok, stage, detail = placer(last_name="", first_name="").run(page)
        self.assertFalse(ok)
        self.assertIn("lastName", detail)
        self.assertEqual(3, len(page.calls))   # 第 4 步根本没发出去

    #: 有的账号 Apple 不预填邮箱/手机：字段在，值是空的
    BLANK_CONTACT = {"x": {"d": {"lastName": "张", "firstName": "三",
                                 "emailAddress": "", "fullDaytimePhone": ""}},
                     "y": {"d": {"nationalIdSelf": ""}}}
    #: 预填过的账号：模型里给的是打码值，原样发回去校验不过
    MASKED_CONTACT = {"x": {"d": {"lastName": "张", "firstName": "三",
                                  "emailAddress": "test@gmail.com",
                                  "fullDaytimePhone": "••••••••••09"}},
                      "y": {"d": {"nationalIdSelf": ""}}}

    def test_fills_contact_from_config_when_server_blank(self):
        f = placer(email="a@b.c", phone="13800000000")
        got = {k.rsplit(".", 1)[-1]: v for k, v in f.contact_fields(self.BLANK_CONTACT)}
        self.assertEqual("a@b.c", got["emailAddress"])
        self.assertEqual("13800000000", got["fullDaytimePhone"])

    def test_never_resends_masked_contact(self):
        """预填过就一个字都不发——打码值发回去等于提交一串圆点。"""
        f = placer(email="a@b.c", phone="13800000000")
        got = {k.rsplit(".", 1)[-1]: v for k, v in f.contact_fields(self.MASKED_CONTACT)}
        self.assertNotIn("emailAddress", got)
        self.assertNotIn("fullDaytimePhone", got)

    def test_skips_contact_when_step_does_not_ask(self):
        """模型里根本没这个字段的流程，别平白多发两个字段。"""
        f = placer(email="a@b.c", phone="13800000000")
        got = {k.rsplit(".", 1)[-1]: v for k, v in f.contact_fields(self.MODEL)}
        self.assertNotIn("emailAddress", got)
        self.assertNotIn("fullDaytimePhone", got)

    def test_stalls_when_contact_blank_on_both_sides(self):
        blank = resp("pickupContact", {"selfPickupContact": {
            "selfContact": {"address": {"d": {
                "lastName": "张", "firstName": "三",
                "emailAddress": "", "fullDaytimePhone": ""}}},
            "nationalIdSelf": {"d": {"nationalIdSelf": ""}}}})
        page = FakePage([FUL, FUL, blank, BANKS, MONTHS, REVIEW])
        ok, _, detail = placer(email="", phone="").run(page)
        self.assertFalse(ok)
        self.assertIn("emailAddress", detail)
        self.assertIn("pickup_email", detail)
        self.assertEqual(3, len(page.calls))   # 第 4 步根本没发出去

    def test_stalls_when_id_last4_missing(self):
        page = FakePage([FUL, FUL, CONTACT, BANKS, MONTHS, REVIEW])
        f = FastCheckout(store="R581", id_last4="", last_name="", first_name="",
                         stk_timeout_ms=50, log=lambda *a: None)
        ok, _, detail = f.run(page)
        self.assertFalse(ok)
        self.assertIn("id_last4", detail)


class InterruptedSubmitTests(unittest.TestCase):
    """Ctrl+C 正落在「立即下单」那一发上：请求已经出门，订单可能已经建好。

    2026-09-18 23:50 就是这么丢的——六步走完 4 秒后按了 Ctrl+C，而那台机器每发
    要 8～10 秒，中断落在提交途中。订单真建了，日志里却连「已提交」都没有。
    KeyboardInterrupt 是 BaseException，`except Exception` 接不住，于是专门为
    这一刻置的 submitted 标志没有任何人去读。"""

    class Interrupting(FakePage):
        """跑到第 stop_at 个 POST 时按下 Ctrl+C。"""

        def __init__(self, responses, stop_at):
            super().__init__(responses)
            self.stop_at = stop_at

        def evaluate(self, js, arg=None):
            if arg is not None and len(self.calls) == self.stop_at:
                raise KeyboardInterrupt
            return super().evaluate(js, arg)

    def test_shouts_before_dying(self):
        logs = []
        page = self.Interrupting(list(HAPPY), stop_at=6)   # 六步走完，第 7 发被打断
        fc = placer(place_order=True, log=logs.append)
        with self.assertRaises(KeyboardInterrupt):
            fc.run(page)
        self.assertTrue(fc.submitted)                      # 请求已经出门
        self.assertTrue(any("订单可能已经创建" in m for m in logs), logs)

    def test_stays_quiet_when_nothing_went_out(self):
        """还没提交就被打断，就别吓唬人。"""
        logs = []
        page = self.Interrupting(list(HAPPY), stop_at=0)   # 第 1 步就被打断
        with self.assertRaises(KeyboardInterrupt):
            placer(place_order=True, log=logs.append).run(page)
        self.assertFalse(any("订单可能已经创建" in m for m in logs), logs)


class ShowReviewTests(unittest.TestCase):
    """六步只改服务端状态，标签页还停在原来那一步——得把它带过去。

    让人自己按 F5 是不行的：URL 里还挂着上一步的 _s= 锚点，刷新等于带着那个
    锚点重开，页面回到那一步，看着就像整个流程又走了一遍。"""

    def test_navigates_to_review_on_the_same_host(self):
        page = FakePage([])
        page.url = "https://secure6.www.apple.com.cn/shop/checkout?_s=Fulfillment-init"
        self.assertTrue(placer().show_review(page))
        self.assertEqual(
            ["https://secure6.www.apple.com.cn/shop/checkout?_s=Review"], page.gotos)

    def test_review_url_follows_the_session_host(self):
        """会话分在哪台 secureN 上就得用哪台，拼死地址会跳错主机。"""
        page = FakePage([])
        page.url = "https://secure8.www.apple.com.cn/shop/checkout?_s=Billing"
        self.assertEqual("https://secure8.www.apple.com.cn/shop/checkout?_s=Review",
                         FastCheckout.review_url(page))


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
        """轮询耗尽时最后拿到的还是 status 页——绝不能算成功。

        但**也不能算「被驳回」**：2026-09-18 07:15 那一单就是这个形状，
        9 轮全是「处理中」而 Apple 的订单确认邮件已经到了。被驳回可以重试，
        结果不明只能停手，两者混在一起就会再下一单。详见 order_unknown。
        """
        page = FakePage(HAPPY + [self.PLACED]
                        + [self._status("/shop/checkout/status")] * 12)
        fc = placer(place_order=True)
        ok, stage, detail = fc.run(page)
        self.assertFalse(ok)
        self.assertIn("结果不明", stage)
        self.assertNotIn("驳回", stage)
        self.assertTrue(fc.submitted, "提交发出去了，这个标记必须立着")
        self.assertIn("订单很可能已经创建", detail)

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
    响应会明确说成没成，不成就停止，不用猜。"""

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
        """别把随便一个跳转当成功——停止本次尝试是安全的，跳错地方不是。"""
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
        """读不出型号就别赌——停止本次尝试是安全的。"""
        url, p = self._run(self.state(1, []), "MG6W4CH/A")
        self.assertEqual("", url)
        self.assertFalse(p.posted)

    def test_no_want_part_skips_model_check(self):
        url, _ = self._run(self.state(1, ["ANYTHING/A"]), "")
        self.assertIn("/shop/checkout", url)


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

    def test_reports_failure_so_caller_stops(self):
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
