import time
import unittest

from hunter.session import ROLES, Sample, SessionProbe, parse_duration, read_jar


class FakeCtx:
    """只实现 cookies()，足够喂给 read_jar。"""

    def __init__(self, cookies):
        self._c = cookies

    def cookies(self):
        return self._c


def ck(name, domain=".apple.com.cn", expires=-1, value="x"):
    return {"name": name, "domain": domain, "expires": expires, "value": value}


class DurationTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(30.0, parse_duration("30s"))
        self.assertEqual(300.0, parse_duration("5m"))
        self.assertEqual(7200.0, parse_duration("2h"))

    def test_bare_number_is_minutes(self):
        # 探针的自然单位是分钟，裸数字按分钟解释才不会让人写出 5 秒一采样
        self.assertEqual(300.0, parse_duration("5"))

    def test_empty_falls_back_to_default(self):
        self.assertEqual(99.0, parse_duration("", 99.0))

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_duration("每五分钟")


class ReadJarTests(unittest.TestCase):
    def test_ignores_non_apple_cookies(self):
        ctx = FakeCtx([ck("as_dc", domain=".google.com"), ck("as_dc")])
        watched, _, _ = read_jar(ctx, now=1000)
        self.assertEqual(1, len(watched))

    def test_computes_remaining_seconds(self):
        ctx = FakeCtx([ck("as_dc", expires=1600)])
        watched, _, _ = read_jar(ctx, now=1000)
        self.assertEqual(600, watched["as_dc"]["left_s"])
        self.assertEqual("路由(secureN)", watched["as_dc"]["role"])

    def test_session_cookie_has_no_expiry(self):
        ctx = FakeCtx([ck("as_atb", expires=-1)])
        watched, _, _ = read_jar(ctx, now=1000)
        self.assertIsNone(watched["as_atb"]["exp"])
        self.assertIsNone(watched["as_atb"]["left_s"])

    def test_never_records_cookie_values(self):
        """cookie 值等同于凭证，落盘就是把账号写进日志。"""
        ctx = FakeCtx([ck("as_dc", expires=1600, value="SECRET-VALUE")])
        watched, _, _ = read_jar(ctx, now=1000)
        blob = repr(watched)
        self.assertNotIn("SECRET-VALUE", blob)
        self.assertEqual(12, watched["as_dc"]["vlen"])   # 只留长度

    def test_picks_out_idmsa_auth_cookie_by_prefix(self):
        ctx = FakeCtx([ck("DES5059e1bcf187f254071a0af3",
                          domain=".idmsa.apple.com.cn", expires=2000)])
        _, auth, _ = read_jar(ctx, now=1000)
        self.assertIsNotNone(auth)
        self.assertEqual("idmsa认证", auth["role"])
        self.assertTrue(auth["name"].endswith("…"))      # 名字也截断

    def test_counts_unknown_apple_cookies_without_expanding(self):
        ctx = FakeCtx([ck("whatever"), ck("another"), ck("as_dc")])
        watched, _, others = read_jar(ctx, now=1000)
        self.assertEqual(1, len(watched))
        self.assertEqual(2, others)


class DiffTests(unittest.TestCase):
    """探针的价值全在「跟上次比」：谁没了、谁被重签了。"""

    def _probe(self):
        p = SessionProbe()
        p.t0 = time.time()
        return p

    def test_disappeared_cookie_is_reported(self):
        p = self._probe()
        p._prev = {"shld_bt_m": {"exp": 1600, "left_s": 600, "vlen": 84}}
        s = p.sample(FakeCtx([ck("as_dc", expires=1600)]), page=None)
        self.assertEqual(["shld_bt_m"], s.gone)

    def test_extended_expiry_counts_as_reissue(self):
        p = self._probe()
        now = time.time()
        p._prev = {"as_dc": {"exp": now + 100, "left_s": 100, "vlen": 4}}
        s = p.sample(FakeCtx([ck("as_dc", expires=now + 7300)]), page=None)
        self.assertEqual(1, len(s.reissued))
        self.assertEqual("as_dc", s.reissued[0]["name"])
        self.assertGreater(s.reissued[0]["exp_delta_s"], 7000)

    def test_changed_value_counts_as_reissue(self):
        """到期时间没动但值变了，也是重签——shield 就可能是这种。"""
        p = self._probe()
        now = time.time()
        p._prev = {"shld_bt_m": {"exp": now + 600, "left_s": 600, "vlen": 84}}
        s = p.sample(FakeCtx([ck("shld_bt_m", expires=now + 600, value="y" * 96)]),
                     page=None)
        self.assertEqual(["shld_bt_m"], [r["name"] for r in s.reissued])

    def test_steady_state_reports_nothing(self):
        p = self._probe()
        now = time.time()
        p._prev = {"as_dc": {"exp": now + 600, "left_s": 600, "vlen": 1}}
        s = p.sample(FakeCtx([ck("as_dc", expires=now + 600)]), page=None)
        self.assertEqual([], s.gone)
        self.assertEqual([], s.reissued)


class AlertTests(unittest.TestCase):
    class Spy:
        def __init__(self):
            self.sent = []

        def send(self, title, body, url="", critical=False):
            self.sent.append(title)

    def test_alerts_once_per_flip_not_every_round(self):
        """每轮都叫就成了噪音，真掉线那次反而会被忽略。"""
        spy = self.Spy()
        p = SessionProbe(notifier=spy)
        out = Sample(ts=0, elapsed_s=3600, signed_in=False)
        p._alert(out)
        p._alert(out)
        self.assertEqual(1, len(spy.sent))

        p._alert(Sample(ts=0, elapsed_s=3600, signed_in=True))   # 恢复
        p._alert(out)                                            # 再掉才再叫
        self.assertEqual(2, len(spy.sent))

    def test_silent_when_login_not_checked(self):
        spy = self.Spy()
        SessionProbe(notifier=spy)._alert(Sample(ts=0, elapsed_s=1, signed_in=None))
        self.assertEqual([], spy.sent)


class SampleTests(unittest.TestCase):
    def test_json_omits_empty_fields(self):
        d = Sample(ts=time.time(), elapsed_s=60).as_json()
        for absent in ("signed_in", "evidence", "gone", "reissued", "secure_host"):
            self.assertNotIn(absent, d)

    def test_roles_cover_the_cookies_we_reason_about(self):
        for name in ("as_dc", "as_sfa", "shld_bt_m", "shld_bt_ck"):
            self.assertIn(name, ROLES)


if __name__ == "__main__":
    unittest.main()


class RecorderTests(unittest.TestCase):
    """录制器的脱敏是硬要求：结账表单里有身份证和手机号，
    记了值就是把明文写进仓库里的文件。"""

    def test_safe_keys_are_injected_into_js(self):
        from hunter.record import RECORDER_JS, SAFE_KEYS
        self.assertNotIn("__SAFE_KEYS__", RECORDER_JS)   # 占位符必须被替换掉
        for k in SAFE_KEYS:
            self.assertIn(f'"{k}"', RECORDER_JS)

    def test_safe_keys_carry_no_personal_fields(self):
        """白名单只该放决定「打到哪一步」的结构性参数。"""
        from hunter.record import SAFE_KEYS
        for k in SAFE_KEYS:
            for bad in ("name", "phone", "email", "national", "id_", "card", "address"):
                self.assertNotIn(bad, k.lower())

    def test_recorder_records_lengths_not_values(self):
        from hunter.record import RECORDER_JS
        # 非白名单字段走的是 {k, len} 分支，不该有把原值塞进去的写法
        self.assertIn("len: val.length", RECORDER_JS)
        self.assertIn("fromBody", RECORDER_JS)


class CheckoutBlockTests(unittest.TestCase):
    """541 被当成「页面不存在」这件事真发生过，而且第一反应全查错了方向：
    去查 slug、查 part、查登录态，实际上结账页开得好好的，是它的 XHR 被拦了。"""

    class FakeResp:
        def __init__(self, url, status, headers=None):
            self.url, self.status = url, status
            self.headers = headers or {}

    class FakePage:
        def __init__(self):
            self._h = []

        def on(self, _ev, fn):
            self._h.append(fn)

        def fire(self, resp):
            for f in self._h:
                f(resp)

    def test_flags_blocked_checkout_xhr(self):
        from hunter.checkout import watch_checkout_block
        p = self.FakePage()
        st = watch_checkout_block(p)
        p.fire(self.FakeResp(
            "https://secure7.www.apple.com.cn/shop/checkoutx/fulfillment?_a=x", 541))
        self.assertEqual(1, len(st["hits"]))
        self.assertEqual(541, st["hits"][0]["status"])

    def test_reads_retry_after(self):
        from hunter.checkout import watch_checkout_block
        p = self.FakePage()
        st = watch_checkout_block(p)
        p.fire(self.FakeResp("https://secure7.www.apple.com.cn/shop/checkoutx/f", 503,
                             {"retry-after": "30"}))
        self.assertEqual(30.0, st["retry_after"])

    def test_ignores_unrelated_and_successful_responses(self):
        from hunter.checkout import watch_checkout_block
        p = self.FakePage()
        st = watch_checkout_block(p)
        p.fire(self.FakeResp("https://www.apple.com.cn/shop/bag", 541))        # 不是 checkoutx
        p.fire(self.FakeResp("https://secure7.www.apple.com.cn/shop/checkoutx/f", 200))
        self.assertEqual([], st["hits"])


class NotFoundGuardTests(unittest.TestCase):
    class FakePage:
        """/shop/404 返回的是 200，所以只能看落地 URL——这正是容易漏的地方。"""

        def __init__(self, urls):
            self._urls, self.url, self.gotos = list(urls), "", 0

        def goto(self, url, **kw):
            self.gotos += 1
            self.url = self._urls.pop(0) if self._urls else url

        def wait_for_timeout(self, _ms):
            pass

    def test_retries_past_a_transient_404(self):
        from hunter.checkout import goto_buy_page
        p = self.FakePage(["https://www.apple.com.cn/shop/404",
                           "https://www.apple.com.cn/shop/buy-iphone/iphone-17/mg704ch/a"])
        self.assertTrue(goto_buy_page(p, "u", tries=3))
        self.assertEqual(2, p.gotos)

    def test_gives_up_when_404_is_persistent(self):
        from hunter.checkout import goto_buy_page
        p = self.FakePage(["https://www.apple.com.cn/shop/404"] * 3)
        self.assertFalse(goto_buy_page(p, "u", tries=3))
        self.assertEqual(3, p.gotos)

    def test_succeeds_first_try_without_extra_navigations(self):
        from hunter.checkout import goto_buy_page
        p = self.FakePage(["https://www.apple.com.cn/shop/buy-iphone/iphone-17/mg704ch/a"])
        self.assertTrue(goto_buy_page(p, "u", tries=3))
        self.assertEqual(1, p.gotos)




class BlockWatchCoversNewTabsTests(unittest.TestCase):
    """点「结账」会开新标签（坑 7），被拦的 fulfillment 请求就发生在那个新标签里。
    只挂当前页等于什么都看不到——而看不到就会把限流误报成「页面不存在」。"""

    class FakePage:
        def __init__(self):
            self.handlers = []

        def on(self, _ev, fn):
            self.handlers.append(fn)

        def fire(self, resp):
            for f in self.handlers:
                f(resp)

    class FakeCtx:
        def __init__(self, pages):
            self.pages = list(pages)
            self._on_page = None

        def on(self, _ev, fn):
            self._on_page = fn

        def open_new(self, page):
            self.pages.append(page)
            if self._on_page:
                self._on_page(page)

    class Resp:
        def __init__(self, url, status):
            self.url, self.status, self.headers = url, status, {}

    URL = "https://secure7.www.apple.com.cn/shop/checkoutx/fulfillment?_a=x"

    def test_catches_block_in_a_tab_opened_later(self):
        from hunter.checkout import watch_checkout_block
        first = self.FakePage()
        ctx = self.FakeCtx([first])
        st = watch_checkout_block(first, ctx=ctx)

        later = self.FakePage()
        ctx.open_new(later)              # 点结账开出来的那个标签
        later.fire(self.Resp(self.URL, 541))
        self.assertEqual(1, len(st["hits"]))

    def test_does_not_double_register_the_same_page(self):
        """页面同时出现在 ctx.pages 和入参里，不该把同一个响应记两遍。"""
        from hunter.checkout import watch_checkout_block
        page = self.FakePage()
        st = watch_checkout_block(page, ctx=self.FakeCtx([page]))
        page.fire(self.Resp(self.URL, 541))
        self.assertEqual(1, len(st["hits"]))


class RecorderCaptureTests(unittest.TestCase):
    def test_watch_paths_match_real_checkout_urls(self):
        """真实地址是 /shop/checkout?_s=Review —— 带问号不带斜杠。
        写成 "/shop/checkout/" 一条都匹配不上，而且不会报错，只是静默录不到。"""
        from hunter.record import WATCH_PATHS
        for u in (
            "https://secure6.www.apple.com.cn/shop/checkout?_s=Review",
            "https://secure6.www.apple.com.cn/shop/checkoutx/fulfillment?_a=x",
            "https://www.apple.com.cn/shop/bagx/checkout_now?_a=checkout",
            "https://www.apple.com.cn/shop/bag",
        ):
            self.assertTrue(any(k in u for k in WATCH_PATHS), u)

    def test_body_inventory_keeps_structure_hides_personal_data(self):
        from hunter.record import body_inventory
        got = body_inventory("_a=selectFulfillmentLocationAction&_m=checkout.fulfillment"
                             "&nationalId=440301199001011234&phone=13800138000")
        by = {d["k"]: d for d in got}
        self.assertEqual("selectFulfillmentLocationAction", by["_a"]["v"])   # 结构性，留值
        self.assertNotIn("v", by["nationalId"])                              # 个人信息，只留长度
        self.assertEqual(18, by["nationalId"]["len"])
        self.assertEqual(11, by["phone"]["len"])

    def test_body_inventory_handles_json_and_empty(self):
        from hunter.record import body_inventory
        self.assertEqual([], body_inventory(None))
        self.assertEqual([], body_inventory(""))
        got = body_inventory('{"_a":"x","nationalId":"440301199001011234"}')
        by = {d["k"]: d for d in got}
        self.assertEqual("x", by["_a"]["v"])
        self.assertEqual(18, by["nationalId"]["len"])

    def test_recorder_token_defeats_a_stale_injection(self):
        """上一轮录制的 init script 留在浏览器里，会把新录制器挡在门外。
        令牌不同就必须重新装监听——布尔标记做不到这件事。"""
        from hunter.record import new_token, recorder_js
        a, b = new_token(), new_token()
        self.assertNotEqual(a, b)
        self.assertIn(a, recorder_js(a))
        self.assertNotIn("__TOKEN__", recorder_js(a))
        self.assertNotIn(a, recorder_js(b))


class HarScanTests(unittest.TestCase):
    """HAR 里有 cookie、身份证、手机号的明文——解析器绝不能把值打出来。"""

    HAR = {"log": {"entries": [
        {"request": {
            "url": "https://secure6.www.apple.com.cn/shop/checkoutx/fulfillment"
                   "?_a=selectFulfillmentLocationAction&_m=checkout.fulfillment",
            "method": "POST",
            "headers": [{"name": "x-aos-stk", "value": "SECRETTOKEN"},
                        {"name": "Cookie", "value": "as_dc=SECRETCOOKIE"}],
            "postData": {"text": "_a=doIt&nationalId=440301199001011234"}},
         "response": {"status": 200}},
        {"request": {"url": "https://www.apple.com.cn/static/app.js", "method": "GET",
                     "headers": []},
         "response": {"status": 200}},
        {"request": {"url": "https://example.com/unrelated", "method": "GET",
                     "headers": []},
         "response": {"status": 200}},
    ]}}

    def _scan(self, har=None):
        import json as _j
        import tempfile
        from pathlib import Path as _P

        from hunter.harscan import scan
        f = _P(tempfile.mkdtemp()) / "t.har"
        f.write_text(_j.dumps(har if har is not None else self.HAR), encoding="utf-8")
        out = []
        scan(f, log=lambda *a: out.append(" ".join(str(x) for x in a)))
        return "\n".join(out)

    def test_never_prints_secrets(self):
        blob = self._scan()
        self.assertNotIn("SECRETTOKEN", blob)
        self.assertNotIn("SECRETCOOKIE", blob)
        self.assertNotIn("440301199001011234", blob)

    def test_reports_token_header_presence_and_field_lengths(self):
        blob = self._scan()
        self.assertIn("x-aos-stk", blob)          # 报「有没有」
        self.assertIn("nationalId(len 18)", blob)  # 只报长度

    def test_skips_static_assets_and_unrelated_hosts(self):
        blob = self._scan()
        self.assertNotIn("app.js", blob)
        self.assertNotIn("unrelated", blob)
        self.assertIn("命中 1 条", blob)

    def test_warns_when_nothing_matched(self):
        blob = self._scan({"log": {"entries": []}})
        self.assertIn("一条都没命中", blob)

    def test_rejects_non_har_file(self):
        import tempfile
        from pathlib import Path as _P

        from hunter.harscan import scan
        f = _P(tempfile.mkdtemp()) / "bad.har"
        f.write_text("not json", encoding="utf-8")
        with self.assertRaises(SystemExit):
            scan(f, log=lambda *a: None)
