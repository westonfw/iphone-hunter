import unittest

from hunter.checkout import (JS_CONTINUE, click_button, click_id, click_label,
                             css_button, css_id, find_continue)


class FakeLocator:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.sel in self.page.texts or self.page.match_any else 0

    def inner_text(self, timeout=None):
        return self.page.texts.get(self.sel, "")

    def click(self, timeout=None, force=False):
        self.page.attempts.append((self.sel, force))
        if force and self.page.needs_force:
            self.page.clicked = self.sel
            return
        if self.page.click_fails or (self.page.needs_force and not force):
            raise RuntimeError("intercepts pointer events")
        self.page.clicked = self.sel


class FakePage:
    def __init__(self, texts=None, click_fails=False, needs_force=False,
                 js_click_ok=False, match_any=False):
        self.texts = texts or {}
        self.match_any = match_any
        self.click_fails = click_fails
        self.needs_force = needs_force
        self.js_click_ok = js_click_ok
        self.attempts = []
        self.clicked = None
        self.js_calls = 0

    def locator(self, sel):
        return FakeLocator(self, sel)

    def evaluate(self, js, arg=None):
        self.js_calls += 1
        if js is JS_CONTINUE:
            return self.texts.get("__continue__", {"text": "", "id": ""})
        if self.js_click_ok:
            self.clicked = arg
            return True
        return False

    def wait_for_timeout(self, _ms):
        pass


class SelectorBuilderTests(unittest.TestCase):
    def test_id_selector_avoids_hash(self):
        # Apple 的 id 带点，#id 会被 CSS 当成类选择器
        self.assertEqual('[id="a.b.c"]', css_id("a.b.c"))

    def test_button_selector_pins_text_and_id(self):
        got = css_button("立即下单", "rs-checkout-continue-button-bottom")
        self.assertIn('[id="rs-checkout-continue-button-bottom"]', got)
        self.assertIn('has-text("立即下单")', got)
        self.assertIn(":visible", got)

    def test_button_selector_escapes_quotes(self):
        self.assertIn(r'has-text("说\"好\"")', css_button('说"好"'))


class ClickButtonTests(unittest.TestCase):
    SEL = 'button:visible:has-text("立即下单")'

    def test_returns_label_and_clicks(self):
        page = FakePage({self.SEL: "立即下单"})
        self.assertEqual("立即下单", click_button(page, self.SEL))
        self.assertEqual(self.SEL, page.clicked)

    def test_retries_with_force_when_intercepted(self):
        page = FakePage({self.SEL: "立即下单"}, needs_force=True)
        self.assertEqual("立即下单", click_button(page, self.SEL))
        self.assertEqual([(self.SEL, False), (self.SEL, True)], page.attempts)

    def test_falls_back_to_js_only_after_both_clicks_fail(self):
        page = FakePage({self.SEL: "立即下单"}, click_fails=True, js_click_ok=True)
        msgs = []
        self.assertEqual("立即下单", click_button(page, self.SEL, log=msgs.append))
        self.assertEqual(2, len(page.attempts))
        self.assertTrue(any("JS 兜底" in m for m in msgs))

    def test_returns_empty_when_nothing_works(self):
        page = FakePage({self.SEL: "立即下单"}, click_fails=True)
        self.assertEqual("", click_button(page, self.SEL))

    def test_click_label_delegates(self):
        page = FakePage(match_any=True)
        self.assertTrue(click_label(page, "_r_26_"))
        self.assertEqual('label[for="_r_26_"]', page.clicked)


class FindContinueTests(unittest.TestCase):
    def test_js_no_longer_clicks(self):
        # 回归：JS 只找不点，点击交给 Playwright
        self.assertNotIn(".click()", JS_CONTINUE)

    def test_returns_text_id_and_frame(self):
        page = FakePage({"__continue__": {"text": "检查订单", "id": "rs-x"}})
        got = find_continue(page)
        self.assertEqual("检查订单", got["text"])
        self.assertEqual("rs-x", got["id"])
        self.assertIs(page, got["frame"])       # 主文档命中

    def test_survives_evaluate_failure(self):
        class Boom:
            def evaluate(self, *_a, **_k): raise RuntimeError("gone")
        boom = Boom()
        got = find_continue(boom)
        self.assertEqual("", got["text"])
        self.assertIs(boom, got["frame"])       # 兜底成 page 自己，调用方不用判空

    def test_finds_continue_inside_iframe(self):
        inner = FakePage({"__continue__": {"text": "立即下单", "id": "rs-y"}})
        page = FakePage({"__continue__": None})   # 主文档没有
        page.frames = [page, inner]
        got = find_continue(page)
        self.assertEqual("立即下单", got["text"])
        self.assertIs(inner, got["frame"])


if __name__ == "__main__":
    unittest.main()


class ClickLoggingTests(unittest.TestCase):
    """点击要能说话——静默失败是最难查的那种。"""

    def test_click_id_forwards_log_and_accepts_click_button_args(self):
        # 回归：click_id 原来只收 2 个参数，按 click_button 的写法调用会 TypeError
        page = FakePage()          # 什么都匹配不到
        msgs = []
        self.assertEqual("", click_id(page, "sign-in", 800, False, msgs.append))
        self.assertTrue(any("sign-in" in m for m in msgs), msgs)

    def test_missing_element_is_reported(self):
        msgs = []
        click_button(FakePage(), '[id="ghost"]', timeout_ms=500, log=msgs.append)
        self.assertTrue(any("找不到" in m for m in msgs))

    def test_unclickable_element_is_reported(self):
        page = FakePage({'[id="x"]': "按钮"}, click_fails=True)
        msgs = []
        self.assertEqual("", click_button(page, '[id="x"]', timeout_ms=500,
                                          log=msgs.append))
        self.assertTrue(any("点不动" in m for m in msgs))

    def test_js_fallback_is_flagged_as_unreliable(self):
        page = FakePage({'[id="x"]': "按钮"}, click_fails=True, js_click_ok=True)
        msgs = []
        click_button(page, '[id="x"]', timeout_ms=500, log=msgs.append)
        self.assertTrue(any("JS 兜底" in m for m in msgs))


class OverlayTests(unittest.TestCase):
    """遮罩：点不到时要说清楚是谁挡的，强制点击要标出来。"""

    REAL_ERR = ('Locator.click: Timeout 8000ms exceeded.\n'
                'Call log:\n'
                '  - attempting click action\n'
                '  - <div class="globalnav-flyout rf-bag-flyout">…</div>'
                ' intercepts pointer events\n')

    def test_extracts_the_blocking_element(self):
        from hunter.checkout import _interceptor
        self.assertEqual("<div.globalnav-flyout>", _interceptor(self.REAL_ERR))

    def test_no_interceptor_when_not_blocked(self):
        from hunter.checkout import _interceptor
        self.assertEqual("", _interceptor("Timeout 8000ms exceeded."))

    def test_force_click_through_overlay_is_flagged(self):
        page = FakePage({'[id="x"]': "结账"}, needs_force=True)
        real_err = self.REAL_ERR

        class Loc(FakeLocator):
            def click(self, timeout=None, force=False):
                page.attempts.append((self.sel, force))
                if not force:
                    raise RuntimeError(real_err)
                page.clicked = self.sel

        page.locator = lambda sel: Loc(page, sel)
        msgs = []
        self.assertEqual("结账", click_button(page, '[id="x"]', 500, log=msgs.append))
        blob = " ".join(msgs)
        self.assertIn("globalnav-flyout", blob, "要说出是谁挡的")
        self.assertIn("未必生效", blob, "强制点击不能装作成功")
