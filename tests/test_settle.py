import time
import unittest

from hunter.checkout import (JS_SETTLE_CHECK, SETTLE_RELAXED_QUIET, wait_settled)


class FakePage:
    """按脚本回放 settle 状态的假页面。"""

    def __init__(self, states):
        self.states = list(states)
        self.waited = 0

    def wait_for_timeout(self, ms):
        # 必须真的睡：wait_settled 用真实时间算「过了多少预算」，
        # 假页面不睡的话放宽那一档永远触发不了
        self.waited += ms
        time.sleep(ms / 1000)

    def evaluate(self, js, arg=None):
        if "MutationObserver" in js:
            return True
        if js is JS_SETTLE_CHECK:
            # 用完就一直重复最后一个状态，别悄悄变回默认值
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return True


def quiet(ms, busy="", ready="complete"):
    return {"quiet": ms, "busy": busy, "ready": ready}


class SettleTests(unittest.TestCase):
    def test_settles_as_soon_as_dom_is_quiet(self):
        page = FakePage([quiet(100), quiet(400), quiet(700)])
        self.assertTrue(wait_settled(page, quiet_ms=600, max_ms=5000))

    def test_hidden_spinner_must_not_block(self):
        # 回归：原来 busy 用 querySelector 不判可见性，Apple 页面里常驻
        # 隐藏的 spinner，导致每次都白等满预算
        self.assertNotIn("querySelector(\n", JS_SETTLE_CHECK)
        self.assertIn("offsetParent", JS_SETTLE_CHECK)
        self.assertIn("getBoundingClientRect", JS_SETTLE_CHECK)

    def test_visible_spinner_does_block(self):
        page = FakePage([quiet(900, busy="rs-spinner")] * 40)
        self.assertFalse(wait_settled(page, quiet_ms=600, max_ms=400))

    def test_relaxes_after_half_the_budget(self):
        # DOM 一直有小改动，永远到不了 600ms 静默；过了一半预算应当接受 250ms
        page = FakePage([quiet(SETTLE_RELAXED_QUIET + 10)] * 60)
        t = time.monotonic()
        self.assertTrue(wait_settled(page, quiet_ms=600, max_ms=1000))
        self.assertLess(time.monotonic() - t, 1.0, "不该等满预算")

    def test_says_why_it_gave_up(self):
        msgs = []
        wait_settled(FakePage([quiet(900, busy="rf-loading")] * 40),
                     quiet_ms=600, max_ms=300, log=msgs.append)
        self.assertIn("还在转圈", " ".join(msgs))
        self.assertIn("rf-loading", " ".join(msgs))

    def test_says_why_when_dom_keeps_changing(self):
        msgs = []
        wait_settled(FakePage([quiet(10)] * 40), quiet_ms=600, max_ms=300,
                     log=msgs.append)
        self.assertIn("DOM 一直在变", " ".join(msgs))


if __name__ == "__main__":
    unittest.main()
