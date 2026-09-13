import unittest

# 定义在 checkout.py（调用点在那儿，且它不能反向 import autobuy），
# 但 autobuy 会再导出一次，两个路径都要能 import 到。
from hunter.autobuy import JS_FILL as JS_FILL_VIA_AUTOBUY
from hunter.checkout import JS_FILL, JS_HAS_ID, fill_field


class FakeLocator:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    @property
    def first(self):
        return self

    def fill(self, value, timeout=None):
        self.page.fill_calls.append((self.sel, value))
        if self.page.fill_raises:
            raise RuntimeError("element is not visible")
        self.page.values[self.page.id_of(self.sel)] = value


class FakePage:
    """够用的假 page：locator().fill() 和 evaluate() 都记账。"""

    def __init__(self, values=None, fill_raises=False, fill_silently_noop=False):
        self.values = dict(values or {})
        self.fill_raises = fill_raises
        self.fill_silently_noop = fill_silently_noop
        self.fill_calls = []
        self.evals = []

    @staticmethod
    def id_of(sel):
        return sel[len('[id="'):-len('"]')]

    def locator(self, sel):
        if self.fill_silently_noop:
            page = self
            class Noop:
                @property
                def first(self): return self
                def fill(self, value, timeout=None):
                    page.fill_calls.append((sel, value))
            return Noop()
        return FakeLocator(self, sel)

    def evaluate(self, js, arg=None):
        self.evals.append(js)
        if js == JS_HAS_ID:                     # frame 解析用的探测
            return arg in self.values
        if js is JS_FILL:                       # 事件注入
            elem_id, value = arg
            if elem_id not in self.values:
                return {"ok": False, "why": "没有这个 id"}
            self.values[elem_id] = value
            return {"ok": True, "got": value}
        return self.values.get(arg)             # read_field

    def wait_for_timeout(self, _ms):
        pass


class FakeFrame(FakePage):
    """假的 iframe：跟 FakePage 一样，只是多个 url，且不含 frames。"""

    def __init__(self, url, **kw):
        super().__init__(**kw)
        self.url = url


class FramedPage(FakePage):
    """主文档 + 若干 iframe。"""

    def __init__(self, children, **kw):
        super().__init__(**kw)
        self.children = list(children)

    @property
    def frames(self):
        return [self] + self.children


class IframeTests(unittest.TestCase):
    ID = "text_field"

    def test_finds_input_inside_iframe(self):
        inner = FakeFrame("https://pay.example/widget", values={self.ID: ""})
        page = FramedPage([inner])          # 主文档里没有这个 id
        msgs = []
        self.assertTrue(fill_field(page, self.ID, "1234", log=msgs.append))
        self.assertEqual("1234", inner.values[self.ID])
        self.assertEqual("", page.values.get(self.ID, ""))
        self.assertTrue(any("在 iframe 里" in m for m in msgs))

    def test_main_document_wins_and_iframes_are_not_touched(self):
        inner = FakeFrame("https://pay.example/widget", values={self.ID: ""})
        page = FramedPage([inner], values={self.ID: ""})
        self.assertTrue(fill_field(page, self.ID, "主文档"))
        self.assertEqual("主文档", page.values[self.ID])
        self.assertEqual("", inner.values[self.ID])   # iframe 没被动过

    def test_skips_frames_that_throw(self):
        class Dead(FakeFrame):
            def evaluate(self, *_a, **_k):
                raise RuntimeError("frame was detached")

        good = FakeFrame("https://ok.example", values={self.ID: ""})
        page = FramedPage([Dead("https://dead.example"), good])
        self.assertTrue(fill_field(page, self.ID, "x"))
        self.assertEqual("x", good.values[self.ID])

    def test_reports_failure_when_no_frame_has_it(self):
        page = FramedPage([FakeFrame("https://a.example")])
        msgs = []
        self.assertFalse(fill_field(page, "nope", "x", timeout_ms=200, log=msgs.append))
        self.assertTrue(any("含 iframe 都找过了" in m for m in msgs))


class FillFieldTests(unittest.TestCase):
    ID = "checkout.pickupContact.selfPickupContact.nationalIdSelf.nationalIdSelf"

    def test_importable_from_autobuy_too(self):
        self.assertIs(JS_FILL, JS_FILL_VIA_AUTOBUY)

    def test_fills_and_verifies(self):
        page = FakePage({self.ID: ""})
        self.assertTrue(fill_field(page, self.ID, "1234"))
        self.assertEqual("1234", page.values[self.ID])

    def test_plain_id_works_too(self):
        # 普通 id 不需要特殊处理，走的是同一条路
        page = FakePage({"text_field": ""})
        self.assertTrue(fill_field(page, "text_field", "你好"))
        self.assertEqual("你好", page.values["text_field"])
        self.assertEqual('[id="text_field"]', page.fill_calls[0][0])

    def test_uses_attribute_selector_not_hash(self):
        # id 里带点，写成 #id 会被 CSS 当类选择器，永远匹配不到
        page = FakePage({self.ID: ""})
        fill_field(page, self.ID, "1234")
        self.assertEqual(f'[id="{self.ID}"]', page.fill_calls[0][0])
        self.assertNotIn("#", page.fill_calls[0][0])

    def test_falls_back_to_event_injection_when_fill_raises(self):
        page = FakePage({self.ID: ""}, fill_raises=True)
        self.assertTrue(fill_field(page, self.ID, "1234"))
        self.assertIn(JS_FILL, page.evals)
        self.assertEqual("1234", page.values[self.ID])

    def test_falls_back_when_fill_silently_does_nothing(self):
        # React 受控组件的典型症状：调用没报错，值却没进去
        page = FakePage({self.ID: ""}, fill_silently_noop=True)
        self.assertTrue(fill_field(page, self.ID, "1234"))
        self.assertIn(JS_FILL, page.evals)

    def test_missing_element_reports_failure(self):
        page = FakePage({}, fill_raises=True)
        msgs = []
        self.assertFalse(fill_field(page, "nope", "x", timeout_ms=200, log=msgs.append))
        self.assertTrue(any("找不到" in m for m in msgs))

    def test_none_value_is_refused(self):
        self.assertFalse(fill_field(FakePage({self.ID: ""}), self.ID, None))

    def test_non_string_value_is_coerced(self):
        page = FakePage({self.ID: ""})
        self.assertTrue(fill_field(page, self.ID, 1234))
        self.assertEqual("1234", page.values[self.ID])


if __name__ == "__main__":
    unittest.main()
