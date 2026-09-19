"""「提交出去了，但没拿到结论」——这一态必须跟「被驳回」分开。

2026-09-18 07:15 的实况：六步全 200、`continueFromReviewToProcess` 200、
checkStatus 连着 9 轮都是 `/shop/checkout/status`（处理中），而 **Apple 的订单
确认邮件已经到了**。按当时的代码，这会被判成「下单被驳回」，然后：

  1. 退回点页面那条老路，对着一个可能已经成单的会话再走一遍向导、
     再点一次「立即下单」；
  2. `retriable` 默认 True，下一轮监控命中又来一遍。

两条都是「再下一单」。这一整个文件锁的就是「宁可停手」。
"""

import unittest
from unittest import mock

from hunter.checkout import OrderPlacer
from hunter.fastpath import FastCheckout


class ClassifyTests(unittest.TestCase):
    """三态判定：成功 / 明确被打回 / 结果不明。"""

    OK = ("https://secure6.www.apple.com.cn/shop/checkout/thankyou",
          "https://www.apple.com.cn/shop/order/list",
          "https://secure7.www.apple.com.cn/shop/orderstatus?id=1")
    BOUNCED = "https://secure6.www.apple.com.cn/shop/checkout"
    PROCESSING = "/shop/checkout/status"

    def test_success_is_neither_rejected_nor_unknown(self):
        for url in self.OK:
            self.assertFalse(FastCheckout.order_rejected(url), url)
            self.assertFalse(FastCheckout.order_unknown(url), url)

    def test_bounced_back_is_a_real_rejection(self):
        """跳回结账页才是真被打回——那一单没建起来，可以重试。"""
        self.assertTrue(FastCheckout.order_rejected(self.BOUNCED))
        self.assertFalse(FastCheckout.order_unknown(self.BOUNCED))

    def test_still_processing_is_unknown_not_rejection(self):
        self.assertTrue(FastCheckout.order_unknown(self.PROCESSING))
        self.assertTrue(FastCheckout.order_unknown(
            "https://secure6.www.apple.com.cn/shop/checkout/status?x=1"))

    def test_no_redirect_at_all_is_unknown(self):
        """一次跳转都没拿到。空串按「被驳回」处理会直接导致重复下单。"""
        self.assertTrue(FastCheckout.order_unknown(""))
        self.assertTrue(FastCheckout.order_unknown(None))


class SubmittedFlagTests(unittest.TestCase):
    def test_flag_goes_up_before_the_request_leaves(self):
        """请求一旦离开这台机器，订单就可能已经建好——响应回不回来都一样。
        所以这个标记必须在发之前立起来，不能等响应。"""
        fc = FastCheckout(store="R581", id_last4="0000", last_name="张",
                          first_name="三", log=lambda *a: None)

        class Boom:
            def evaluate(self, js, arg=None):
                raise RuntimeError("网络断了")

        self.assertFalse(fc.submitted)
        with self.assertRaises(RuntimeError):
            fc.step7_place_order(Boom())
        self.assertTrue(fc.submitted, "发出去之前没立标记，断网就会被当成没下单")


class FakePage:
    """够 _try_fast_path 用的最小页面；失败后不应再导航。"""

    url = "https://secure6.www.apple.com.cn/shop/checkout"

    def goto(self, *a, **kw):
        return None

    def wait_for_timeout(self, _ms):
        return None


def fake_fast_class(ok, stage, detail, order_url, submitted):
    """假的 FastCheckout 类。`_try_fast_path` 是运行时 import 的，所以能整类替换，
    这样测到的是**真实的** _try_fast_path 判断逻辑，而不是测试里复刻的一份。"""
    real = FastCheckout

    class Fake:
        order_rejected = real.order_rejected
        order_unknown = real.order_unknown

        def __init__(self, **kw):
            self.order_url = order_url
            self.submitted = submitted
            self.timings = []

        def run(self, page):
            return ok, stage, detail

    return Fake


def placer():
    return OrderPlacer(store_numbers=["R581"], fast_path=True, id_last4="0000",
                       log=lambda *a: None)


def try_fast(ok, stage, detail, order_url, submitted):
    """跑真实的 _try_fast_path，返回 (placer, 它的返回值)。"""
    p = placer()
    with mock.patch("hunter.fastpath.FastCheckout",
                    fake_fast_class(ok, stage, detail, order_url, submitted)):
        got = p._try_fast_path(FakePage())               # noqa: SLF001
    return p, got


class PlaceStopsAfterSubmitTests(unittest.TestCase):
    """提交之后结果不明 → 停手，且不允许重试。"""

    def test_unknown_result_is_recognised(self):
        p, got = try_fast(False, "⚠️ 下单结果不明", "订单很可能已经创建",
                          "/shop/checkout/status", True)
        self.assertFalse(got)
        self.assertTrue(p.fast_unknown)
        self.assertFalse(p.fast_ordered)

    def test_place_stops_without_clicking_anything(self):
        """place() 必须在这里就返回——再往下走就是对着可能已成单的会话
        重走一遍向导、再点一次「立即下单」。"""
        p = placer()
        with mock.patch("hunter.fastpath.FastCheckout",
                        fake_fast_class(False, "⚠️ 下单结果不明",
                                        "订单很可能已经创建", "/shop/checkout/status",
                                        True)):
            ok, stage, detail, order_id = p.place(FakePage(), 0.0)
        self.assertFalse(ok)
        self.assertIn("停手", stage)
        self.assertTrue(p.no_retry, "结果不明必须禁掉重试，否则下一轮再下一单")
        self.assertIn("已经创建", detail)

    def test_real_rejection_is_not_unknown(self):
        """明确被打回应与提交结果未知区分；两者都不会回退网页。"""
        p, got = try_fast(False, "⚠️ 下单被驳回", "不再为本订单提供",
                          "https://secure6.www.apple.com.cn/shop/checkout", True)
        self.assertFalse(got)
        self.assertFalse(p.fast_unknown)
        self.assertFalse(p.no_retry)

    def test_failure_before_submit_is_not_unknown(self):
        """六步中途失败仍然是未提交，保留准确状态。"""
        p, got = try_fast(False, "⚠️ 步骤没生效", "search 返回 200 但没推进",
                          "", False)
        self.assertFalse(got)
        self.assertFalse(p.fast_unknown)
        self.assertFalse(p.no_retry)

    def test_success_is_untouched(self):
        p, got = try_fast(True, "✅ 待付款订单已创建", "去扫码",
                          "https://secure6.www.apple.com.cn/shop/checkout/thankyou",
                          True)
        self.assertTrue(got)
        self.assertTrue(p.fast_ordered)
        self.assertFalse(p.fast_unknown)


class ModelPageTests(unittest.TestCase):
    """`x-aos-model-page` 不是常量——这是 07:15 那一单真正的病根。

    真实浏览器（secure7 HAR，成功那次）：
      前六步 + 提交  → `x-aos-model-page: checkoutPage`，referer 是 /shop/checkout
      checkStatus   → `x-aos-model-page: checkoutStatusPage`，referer 是 /shop/checkout/status

    我们八步全发 checkoutPage，于是 checkStatus 永远回「还在处理」。
    """

    def run_to_status(self, status_reply):
        from test_fastpath import FakePage, HAPPY, placer as fast_placer
        placed = {"status": 200, "json": {"head": {"status": 302, "data": {
            "url": "/shop/checkout/status"}}}}
        page = FakePage(HAPPY + [placed] + [status_reply] * 12)
        fc = fast_placer(place_order=True)      # 招商银行分期，属于扫码通道
        ok, stage, detail = fc.run(page)
        return page, fc, ok, stage, detail

    def test_checkstatus_is_asked_as_the_status_page(self):
        done = {"status": 200, "json": {"head": {"status": 302, "data": {
            "url": "/shop/checkout/thankyou"}}}}
        page, _, ok, stage, _ = self.run_to_status(done)
        self.assertTrue(ok, stage)
        by_action = {}
        for c in page.calls:
            action = c["query"].split("_a=")[1].split("&")[0]
            by_action.setdefault(action, c["model_page"])
        self.assertEqual("checkoutStatusPage", by_action["checkStatus"])
        # 前面那几步不能被一起改掉
        self.assertEqual("checkoutPage", by_action["continueFromReviewToProcess"])
        self.assertEqual("checkoutPage", by_action["search"])

    def test_it_navigates_to_the_status_page_first(self):
        """Apple 的跳转不是 HTTP 302，是响应体里的假跳转 + 前端整页导航。
        发包这条路原来把这两跳全省了，省掉之后 referer / model-page 都不对。"""
        done = {"status": 200, "json": {"head": {"status": 302, "data": {
            "url": "/shop/checkout/thankyou"}}}}
        page, _, ok, _, _ = self.run_to_status(done)
        self.assertTrue(ok)
        self.assertIn("https://secure6.www.apple.com.cn/shop/checkout/status",
                      page.gotos)

    def test_success_lands_the_tab_on_thankyou(self):
        """二维码在 thankyou 页上。发包不跳页面的话，人打开浏览器只看到
        还停在结账页的那个标签——这正是 2026-09-18 问的那个问题。"""
        done = {"status": 200, "json": {"head": {"status": 302, "data": {
            "url": "/shop/checkout/thankyou"}}}}
        page, _, ok, _, _ = self.run_to_status(done)
        self.assertTrue(ok)
        self.assertEqual("https://secure6.www.apple.com.cn/shop/checkout/thankyou",
                         page.url)

    def test_rejection_does_not_drag_the_tab_anywhere(self):
        """被打回时页面该留在结账页上，那儿才有 Apple 给的原因。"""
        bounced = {"status": 200, "json": {"head": {"status": 302, "data": {
            "url": "/shop/checkout"}}}}
        page, _, ok, stage, _ = self.run_to_status(bounced)
        self.assertFalse(ok)
        self.assertIn("驳回", stage)
        self.assertNotIn("/shop/checkout/thankyou", page.url)


class RetriableTests(unittest.TestCase):
    """no_retry 要真的落到 BuyResult 上，否则监控下一轮照样再下一单。"""

    def wrap(self, no_retry, ok=False):
        from hunter.autobuy import AutoBuy

        class FakePlacer:
            pass

        placer = FakePlacer()
        placer.no_retry = no_retry
        ab = AutoBuy.__new__(AutoBuy)
        ab.order_placed = False
        r = ab._wrap(placer, "https://x/checkout", ok,      # noqa: SLF001
                     "⚠️ 下单结果不明，已停手", "去邮箱确认", "")
        return ab, r

    def test_no_retry_turns_off_retriable(self):
        ab, r = self.wrap(True)
        self.assertFalse(r.retriable)
        self.assertTrue(ab.order_placed,
                        "结果不明也要当成已下单，否则监控还会再来一轮")

    def test_ordinary_failure_stays_retriable(self):
        """普通失败（比如六步没走通）照旧可以重试，别把这条路一起堵死。"""
        _, r = self.wrap(False)
        self.assertTrue(r.retriable)

    def test_monitor_would_not_retry(self):
        """监控里那句判断：`not r.ok and not order_placed and r.retriable`。"""
        ab, r = self.wrap(True)
        self.assertFalse(not r.ok and not ab.order_placed and r.retriable)


if __name__ == "__main__":
    unittest.main()
