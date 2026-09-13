import unittest
from pathlib import Path
from unittest import mock

from hunter.autobuy import SIGNIN_WAIT, AutoBuy, PWD_ENV

FAST = {k: 150 for k in SIGNIN_WAIT}   # 测试里别真等几秒


def make(**cfg):
    return AutoBuy(dict({"enabled": True}, **cfg), Path("/tmp"), log=lambda *_: None)


class FakePage:
    """按脚本推进的假登录页。"""

    def __init__(self, urls, ids=(), blocked="", values=None):
        self._urls = list(urls)
        self.ids = set(ids)
        self.blocked = blocked
        self.values = dict(values or {})
        self.clicked = []

    @property
    def url(self):
        return self._urls[0] if len(self._urls) == 1 else self._urls.pop(0)

    def wait_for_timeout(self, _ms):
        pass

    def locator(self, sel):
        page = self
        class L:
            def count(self): return 1
            @property
            def first(self): return self
            def inner_text(self, **_kw): return ""
            def click(self, **_kw): page.clicked.append(sel)
        return L()

    def evaluate(self, js, arg=None):
        if "MutationObserver" in js:       # wait_settled 装观察者
            return True
        if "__hunterSettle" in js:         # wait_settled 查/收观察者
            return ({"quiet": 9999, "busy": False, "ready": "complete"}
                    if "aria-busy" in js else True)
        if "one-time-code" in js:          # _sign_in_blocked 的探测
            return self.blocked
        if "getElementById" in js and "value" not in js:
            return arg in self.ids         # frame_for_id 的探测
        if "getElementById" in js:         # read_field
            return self.values.get(arg)
        return None


class SignInGuardTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(SIGNIN_WAIT, FAST)
        patch.start()
        self.addCleanup(patch.stop)

    def test_no_password_says_how_to_fix_it(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            ok, why = make()._sign_in(FakePage(["https://idmsa.apple.com/x"]))
        self.assertFalse(ok)
        self.assertIn(PWD_ENV, why)
        self.assertIn("手动登录", why)

    def test_remembered_account_needs_no_config(self):
        # 信任设备上账号是记住的：不该因为 config 里没填 apple_id 就失败，
        # 更不该拿 config 里的值去覆盖页面上已有的账号
        page = FakePage(["https://idmsa.apple.com/x"],
                        ids={"account_name_text_field"},
                        values={"account_name_text_field": "someone@example.com"})
        msgs = []
        with mock.patch.dict("os.environ", {PWD_ENV: "x"}, clear=False):
            ab = make()            # 密码在构造时读，patch 必须包住 make()
            ab.log = msgs.append
            ab._sign_in(page)
        self.assertTrue(any("页面记着账号" in m for m in msgs), msgs)
        self.assertTrue(any("s***@example.com" in m for m in msgs), "日志里账号要遮罩")
        self.assertFalse(any("someone@example.com" in m for m in msgs),
                         "完整账号不该进日志")
        self.assertIn('[id="sign-in"]', page.clicked)

    def test_missing_apple_id_at_account_step(self):
        page = FakePage(["https://idmsa.apple.com/x"], ids={"account_name_text_field"})
        with mock.patch.dict("os.environ", {PWD_ENV: "x"}, clear=False):
            ok, why = make()._sign_in(page)
        self.assertFalse(ok)
        self.assertIn("apple_id", why)
        self.assertIn("没记住", why)

    def test_unknown_form_is_reported(self):
        page = FakePage(["https://idmsa.apple.com/x"])      # 两个框都没有
        with mock.patch.dict("os.environ", {PWD_ENV: "x"}, clear=False):
            ok, why = make()._sign_in(page)
        self.assertFalse(ok)
        self.assertIn("页面结构可能变了", why)


class AwaitSignInTests(unittest.TestCase):
    def test_leaving_signin_page_counts_as_success(self):
        page = FakePage(["https://www.apple.com.cn/shop/checkout?_s=Fulfillment-init"])
        self.assertEqual((True, ""), make()._await_sign_in(page, max_s=1))

    def test_two_factor_is_handed_back_to_the_human(self):
        page = FakePage(["https://idmsa.apple.com/x"], blocked="需要双重认证验证码")
        ok, why = make()._await_sign_in(page, max_s=1)
        self.assertFalse(ok)
        self.assertIn("验证码在你手机上", why)
        self.assertIn("不代劳", why)

    def test_wrong_password_is_reported_not_retried(self):
        page = FakePage(["https://idmsa.apple.com/x"], blocked="密码不正确")
        ok, why = make()._await_sign_in(page, max_s=1)
        self.assertFalse(ok)
        self.assertIn("登录被拒", why)

    def test_times_out_without_hanging_forever(self):
        page = FakePage(["https://idmsa.apple.com/x"])
        ok, why = make()._await_sign_in(page, max_s=0.5)
        self.assertFalse(ok)
        self.assertIn("没离开登录页", why)


if __name__ == "__main__":
    unittest.main()
