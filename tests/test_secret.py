import os
import unittest
from unittest import mock

from hunter.autobuy import PWD_ENV, _apple_password
from hunter.checkout import fill_field
from test_fill import FakePage


class PasswordSourceTests(unittest.TestCase):
    def test_reads_from_environment(self):
        with mock.patch.dict(os.environ, {PWD_ENV: "s3cret"}, clear=False):
            self.assertEqual("s3cret", _apple_password({}, log=lambda *_: None))

    def test_empty_when_env_missing(self):
        env = {k: v for k, v in os.environ.items() if k != PWD_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual("", _apple_password({}, log=lambda *_: None))

    def test_config_password_is_ignored_and_warned_about(self):
        msgs = []
        env = {k: v for k, v in os.environ.items() if k != PWD_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            got = _apple_password({"pwd": "from-config"}, log=msgs.append)
        self.assertEqual("", got, "config.json 里的密码必须被忽略")
        self.assertTrue(any(PWD_ENV in m for m in msgs))

    def test_env_wins_and_config_still_warns(self):
        msgs = []
        with mock.patch.dict(os.environ, {PWD_ENV: "from-env"}, clear=False):
            got = _apple_password({"pwd": "from-config"}, log=msgs.append)
        self.assertEqual("from-env", got)
        self.assertTrue(any("请删掉" in m for m in msgs))


class SecretLoggingTests(unittest.TestCase):
    """密码绝不能进日志。"""

    ID = "password_text_field"

    def failing_page(self):
        # 填进去了但读回来对不上——就是这条分支以前把值打了出来
        page = FakePage({self.ID: ""})
        page.fill_silently_noop = True
        original = page.evaluate

        def evaluate(js, arg=None):
            from hunter.checkout import JS_FILL
            if js is JS_FILL:
                return {"ok": False, "got": "hunter2"}
            return original(js, arg)

        page.evaluate = evaluate
        return page

    def test_secret_never_logs_the_value(self):
        msgs = []
        self.assertFalse(fill_field(self.failing_page(), self.ID, "hunter2",
                                    timeout_ms=300, log=msgs.append, secret=True))
        blob = " ".join(msgs)
        self.assertNotIn("hunter2", blob)
        self.assertIn("值对不上", blob)

    def test_non_secret_still_shows_the_value_for_debugging(self):
        # 同一个字段、同一条失败分支，只是没开 secret：值应当照常打出来帮排查
        msgs = []
        fill_field(self.failing_page(), self.ID, "1234",
                   timeout_ms=300, log=msgs.append)
        self.assertIn("hunter2", " ".join(msgs))


if __name__ == "__main__":
    unittest.main()
