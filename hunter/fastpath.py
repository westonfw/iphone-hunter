"""结账快车道：用 6 个同源 POST 走完结账向导，而不是点页面。

为什么这条路成立（2026-09-14 从真实 HAR 确认）
----------------------------------------------
加购那一步**不能**这么干：`atbtoken` 由页面 JS 在点击瞬间现算，伪造它就是绕
防自动化机制，本项目不做。但结账向导是另一回事：

  * 整个向导只有 6 个 `application/x-www-form-urlencoded` POST
  * 请求体里全是自解释的模型路径，**没有任何来路不明的令牌**
    （`verificationToken` 实测就是空值）
  * 唯一的令牌 `x-aos-stk` 恒定不变，而且**明文写在结账页 HTML 的内联 JSON 里**：
        ..."x-aos-model-page":"checkoutPage","modelVersion":"v2","x-aos-stk":"<值>"

读出页面发给自己的令牌再回传，跟伪造 `atbtoken` 是两码事。而结账页本来就必须
加载（要建会话），所以这一步零额外成本。

边界
----
**提交订单可以，付款绝不代劳。** 招商银行分期 / 支付宝 / 微信这类扫码通道，
点下单只是创建**待付款订单**，二维码留给人扫。信用卡路径会即时扣款，所以
`place_order` 对非扫码付款方式**硬拒**，不给配置绕过。

风险提示
--------
`checkoutx` 正是会返 541 的那族端点（见 README 坑 9）。发包比点页面更快更密，
风控风险**更高**，所以每一步都检查拦截码，一旦被拦立刻停——不重试、不换路径。
"""

from __future__ import annotations

import json
import random
import re
import string
import time

#: 六步用到的字段前缀。写全是为了在出问题时能一眼对上 HAR。
_FUL = "checkout.fulfillment"
_LOC = f"{_FUL}.pickupTab.pickup.storeLocator"
_ADDR = f"{_LOC}.address.stateCitySelectorForCheckout"
_CONTACT = "checkout.pickupContact"
_SELF = f"{_CONTACT}.selfPickupContact"
_BILL = "checkout.billing.billingOptions"
_INSTALL = f"{_BILL}.selectedBillingOptions.installments.installmentOptions"

#: 被这些状态码拦住就立刻停。跟 checkout.py 里的判断保持一致。
BLOCK_CODES = (541, 503, 429)

#: 下单那两步（2026-09-14 从真实 HAR 抓到，两次尝试形状一致）：
#:   POST /shop/checkoutx/review?_a=continueFromReviewToProcess&_m=checkout.review.placeOrder
#:        → {"head":{"status":302,"data":{"url":"/shop/checkout/status"}}}
#:   POST /shop/checkoutx/statusX?_a=checkStatus&_m=spinner
#:        → {"head":{"status":302,"data":{"url":<最终去处>}}}
#: 两个都是**空请求体**。
PLACE_PATH, PLACE_ACTION, PLACE_MODULE = (
    "/shop/checkoutx/review", "continueFromReviewToProcess", "checkout.review.placeOrder")
STATUS_PATH, STATUS_ACTION, STATUS_MODULE = (
    "/shop/checkoutx/statusX", "checkStatus", "spinner")

#: 在页面里发一个同源 POST。走页面的 fetch 而不是 Python 的 requests，
#: 是因为这样复用的是浏览器自己的 cookie、TLS 指纹和 UA——换成外部客户端，
#: 指纹对不上反而更容易被风控盯上，而我们打的正是最敏感的那族端点。
JS_POST = r"""
async ([path, query, body, stk, callId]) => {
    const url = location.origin + path + "?" + query;
    const t0 = performance.now();
    try {
        const res = await fetch(url, {
            method: "POST",
            credentials: "include",
            headers: {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "*/*",
                "X-Requested-With": "Fetch",
                "syntax": "graviton",
                "modelVersion": "v2",
                "x-aos-model-page": "checkoutPage",
                "x-aos-stk": stk,
                "x-aos-ui-fetch-call-1": callId,
            },
            body: body,
        });
        // 分段计时：headers 是「等服务端」，body 是「收数据」。
        // 两者分开才知道慢在哪——总耗时看起来一样，成因可能完全不同。
        const tHead = performance.now();
        const text = await res.text();
        const tBody = performance.now();
        let data = null;
        try { data = JSON.parse(text); } catch (e) { data = null; }
        let net = null;
        try {
            const es = performance.getEntriesByName(url);
            const e = es && es.length ? es[es.length - 1] : null;
            if (e) net = {ttfb: Math.round(e.responseStart - e.requestStart),
                          total: Math.round(e.duration),
                          stalled: Math.round(e.requestStart - e.startTime)};
        } catch (e) {}
        return {status: res.status, json: data, len: text.length,
                ms: {head: Math.round(tHead - t0), body: Math.round(tBody - tHead)},
                net: net};
    } catch (e) {
        return {status: 0, json: null, len: 0, error: String(e).slice(0, 120)};
    }
}
"""

#: 直接从购物袋接口进结账，跳过加载 259KB 的购物袋页。
#:
#: 为什么值得：2026-09-14 实测，服务端侧这一段只要 ~2.2s
#:   POST bagx/checkout_now  430ms → GET checkout/start 818ms → GET /shop/checkout 951ms
#: 而走「加载购物袋页 → 等它安静 → 找按钮 → 点 → 等新标签」要 ~8s。
#: 多出来的 ~6s 全是客户端渲染开销。
#:
#: 用的令牌是**购物袋作用域**的那个（43 字符，x-aos-model-page: cart），
#: 跟结账页那个 27 字符的不是一回事，别混用。
#:
#: 真实请求体带着购物车条目 id（item-26bb3a43-…），每单都不同、没法写死，
#: 所以这里发空体试——响应的 head.status/url 会明确告诉我们成没成，
#: 不成就退回点页面，不需要猜。
JS_BAG_TO_CHECKOUT = r"""
async ([path, query, stk]) => {
    const out = {stk: !!stk};
    try {
        const res = await fetch(location.origin + path + "?" + query, {
            method: "POST",
            credentials: "include",
            headers: {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "*/*",
                "X-Requested-With": "Fetch",
                "syntax": "graviton",
                "modelVersion": "v2",
                "x-aos-model-page": "cart",
                "x-aos-stk": stk,
            },
            body: "",
        });
        out.status = res.status;
        const text = await res.text();
        try {
            const j = JSON.parse(text);
            out.head = (j.head && j.head.status) || null;
            out.url = (j.head && j.head.data && j.head.data.url) || "";
        } catch (e) { out.parse = text.slice(0, 80); }
    } catch (e) { out.error = String(e).slice(0, 100); }
    return out;
}
"""

#: 购物袋作用域的令牌 + 件数。**两样都从同一份 HTML 里取**——反正为了令牌
#: 已经把购物袋 HTML 拉下来了，件数是顺带的，不额外花一个请求。
#:
#: 为什么必须看件数：跳过购物袋页的同时也跳过了原来顺带做的体检。空袋子去调
#: checkout_now 会怎样没人验证过，而 Apple 对 iPhone 限购 2 台、超限时点结账
#: 是**静默卡死**（坑 6）。与其去试出来，不如先看一眼——反正不要钱。
JS_CART_STATE = r"""
async () => {
    const parse = (html) => {
        const t = html.match(/["']x-aos-stk["']\s*:\s*["']([^"']+)["']/i);
        const c = html.match(/["']bagCount["']\s*:\s*(\d+)/i);
        const cart = /["']x-aos-model-page["']\s*:\s*["']cart["']/i.test(html);
        // 袋里到底装了什么：sku 就是 part number，用它复核型号
        const skus = [...new Set((html.match(/["']sku["']\s*:\s*["']([^"']+)["']/gi) || [])
            .map(m => (m.match(/["']sku["']\s*:\s*["']([^"']+)["']/i) || [])[1])
            .filter(Boolean))];
        // 只认 shoppingCart.items.* 下的条目：「稍后购买」是 bagSavedListItems.*，
        // 那是用户自己存的东西，误删了不好交代。
        const items = [...new Set((html.match(/shoppingCart\.items\.(item-[0-9a-f-]{8,})/g) || [])
            .map(m => m.replace("shoppingCart.items.", "")))];
        // 每条目的数量。只看 sku 集合会漏掉「两台同型号」——集合仍然只有一个元素。
        const qty = (html.match(/["']quantity["']\s*:\s*"?(\d+)"?/gi) || [])
            .map(m => parseInt((m.match(/(\d+)/) || [])[1], 10))
            .filter(n => !isNaN(n));
        return {stk: t ? t[1] : "", count: c ? parseInt(c[1], 10) : null,
                cart, skus, items, qty, origin: location.origin};
    };
    const here = parse(document.documentElement.innerHTML);
    if (here.stk && here.cart) return here;          // 已经在购物袋页上
    try {
        // 相对地址会跟着当前页的 origin 走。页面停在 secureN 上时，
        // fetch("/shop/bag") 打的是 secureN，读回来是空的——调用方必须校验 origin。
        const res = await fetch("/shop/bag", {credentials: "include"});
        return {...parse(await res.text()), origin: location.origin};
    } catch (e) {
        return {stk: "", count: null, cart: false, origin: location.origin,
                error: String(e).slice(0, 80)};
    }
}
"""

#: Apple 对 iPhone 限购 2 台。超了点结账不报错、只是静默不动（坑 6）。
BAG_LIMIT = 2


#: 删一条购物袋条目。空请求体，带购物袋作用域的令牌。
JS_BAG_DELETE = r"""
async ([itemKey, stk]) => {
    try {
        const res = await fetch(location.origin + "/shop/bagx?_a=delete&_m=" +
                                encodeURIComponent("shoppingCart.items." + itemKey), {
            method: "POST",
            credentials: "include",
            headers: {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "*/*",
                "X-Requested-With": "Fetch",
                "syntax": "graviton",
                "modelVersion": "v2",
                "x-aos-model-page": "cart",
                "x-aos-stk": stk,
            },
            body: "",
        });
        const text = await res.text();
        let left = null;
        const m = text.match(/["']bagCount["']\s*:\s*(\d+)/i);
        if (m) left = parseInt(m[1], 10);
        return {status: res.status, left: left};
    } catch (e) { return {status: 0, error: String(e).slice(0, 90)}; }
}
"""


def _origin_ok(st: dict, want_origin: str, log) -> bool:
    """校验读到的状态确实来自主站。

    2026-09-14 实测踩过：上一轮跑完页面停在 secure8 的结账页，这时
    fetch("/shop/bag") 的相对地址跟着 location.origin 走、打到了 secure8，
    读回来「购物袋是空的」——于是跳过清空、又加了一台，最后袋里两台。
    """
    got = str(st.get("origin") or "")
    if not want_origin or not got or got.rstrip("/") == want_origin.rstrip("/"):
        return True
    log(f"[快车道] 当前页在 {got} 上，读到的购物袋不是主站的，不能当真")
    return False


def prepare_bag(page, want_part: str = "", want_origin: str = "", log=print) -> dict:
    """确保购物袋里**只有**这次要买的那台。返回 {ok, kept, removed, reason}。

    比「打开购物袋页 → 找移除按钮 → 逐个点」快得多：实测点页面那条路要 ~6s
    （大头是加载 259KB 的页面 + 渲染），这里一次 fetch 拿状态、每条一个 POST。

    `kept=True` 表示袋里已经正好是目标型号、什么都没动——**这才是「重复购买
    同一配置可以跳过加购」该有的依据**：读服务端的真实状态，而不是记一个
    进程内的布尔量（那个会因为进程重启、或者上一轮加的是别的颜色而失真）。
    """
    t0 = time.monotonic()
    try:
        st = page.evaluate(JS_CART_STATE) or {}
    except Exception as e:
        return {"ok": False, "reason": f"读购物袋状态失败：{type(e).__name__}"}
    if not _origin_ok(st, want_origin, log):
        return {"ok": False, "reason": "当前页不在主站上，读到的购物袋状态不可信"}
    stk = str(st.get("stk") or "")
    items = [str(x) for x in (st.get("items") or []) if x]
    skus = [str(x).upper() for x in (st.get("skus") or []) if x]
    qty = [int(n) for n in (st.get("qty") or []) if isinstance(n, int)]
    want = (want_part or "").upper().strip()

    if not items:
        log("[快车道] 购物袋本来就是空的，直接加购")
        return {"ok": True, "kept": False, "removed": 0}
    # 「已经正好是目标」必须三个条件都满足：只有一条、型号对、**数量是 1**。
    # 少了数量那条就会放过「同型号两台」——集合比对看不出来，实测中招过。
    if want and len(items) == 1 and skus == [want] and qty[:1] == [1]:
        log(f"[快车道] 购物袋里已经正好是 {want} × 1，跳过清空和加购")
        return {"ok": True, "kept": True, "removed": 0}
    if want and skus == [want] and (len(items) > 1 or qty[:1] != [1]):
        log(f"[快车道] 购物袋里是 {want} 但有 {len(items)} 条 / 数量 {qty[:3]}"
            f"——不是要的那一台，照样清空重加")
    if not stk:
        return {"ok": False, "reason": "读不到购物袋令牌，没法用接口清空"}

    removed = 0
    for key in items:
        try:
            r = page.evaluate(JS_BAG_DELETE, [key, stk]) or {}
        except Exception as e:
            return {"ok": False, "removed": removed,
                    "reason": f"删 {key[:16]}… 失败：{type(e).__name__}"}
        if r.get("status") != 200:
            return {"ok": False, "removed": removed,
                    "reason": f"删 {key[:16]}… 返回 {r.get('status')}"}
        removed += 1
    log(f"[快车道] 已清空购物袋（接口删了 {removed} 件，"
        f"{(time.monotonic() - t0) * 1000:.0f}ms，没加载购物袋页）")
    return {"ok": True, "kept": False, "removed": removed}


def bag_to_checkout(page, want_part: str = "", want_qty: int = 1,
                    want_origin: str = "", log=print) -> str:
    """从购物袋接口直接拿到结账地址。拿不到返回空串，调用方退回点页面。

    want_part 给了就**强制复核**袋里的型号，对不上抛 CartMismatch——
    宁可这一单不下，也不能买错机器。
    """
    t0 = time.monotonic()
    try:
        st = page.evaluate(JS_CART_STATE) or {}
    except Exception as e:
        log(f"[快车道] 读购物袋状态失败：{type(e).__name__}")
        return ""
    stk, count = str(st.get("stk") or ""), st.get("count")
    if not stk:
        log("[快车道] 购物袋页里没读到令牌，退回点页面")
        return ""
    # 空袋子不能往下走：加购很可能压根没成，这时候进结账只会得到一个空订单
    # 或者莫名其妙的跳转，而且掩盖了「加购失败」这个真正的问题。
    if count == 0:
        log("[快车道] ⚠️ 购物袋是空的——加购没成？不进结账，退回点页面去查")
        return ""
    if count is not None and count > BAG_LIMIT:
        log(f"[快车道] ⚠️ 购物袋里有 {count} 件，超过限购 {BAG_LIMIT} 台"
            f"——超限时点结账是静默卡死，先清空再抢")
        return ""
    if not _origin_ok(st, want_origin, log):
        return ""
    skus = [str(x).upper() for x in (st.get("skus") or []) if x]
    qty = [int(n) for n in (st.get("qty") or []) if isinstance(n, int)]
    want = (want_part or "").upper().strip()
    total = sum(qty) if qty else (count or 0)
    if want and want_qty and total and total != want_qty:
        raise CartMismatch(
            f"购物袋里共 {total} 台（条目 {count}，数量 {qty[:3]}），这次只要 {want_qty} 台。"
            f"多半是清空没成、又加了一台。")
    if want and skus and set(skus) != {want}:
        raise CartMismatch(
            f"购物袋里是 {'、'.join(skus)}，这次要买的是 {want}。"
            f"清空购物袋那一步可能没成，或者加购加错了型号。")
    if want and not skus:
        log(f"[快车道] ⚠️ 购物袋 HTML 里读不到 sku，没法复核型号，退回点页面")
        return ""
    if count:
        log(f"[快车道] 购物袋 {count} 件 · {'、'.join(skus) or '?'}，型号已复核")
    try:
        r = page.evaluate(JS_BAG_TO_CHECKOUT,
                          ["/shop/bagx/checkout_now",
                           encode([("_a", "checkout"), ("_m", "shoppingCart.actions")]),
                           stk]) or {}
    except Exception as e:
        log(f"[快车道] 购物袋接口调用失败：{type(e).__name__}")
        return ""
    url = str(r.get("url") or "")
    dt = (time.monotonic() - t0) * 1000
    if r.get("status") == 200 and "/shop/checkout" in url:
        log(f"[快车道] 直接从购物袋接口进结账（{dt:.0f}ms，没加载购物袋页）")
        return url
    log(f"[快车道] 购物袋接口没给出结账地址（{dt:.0f}ms，"
        f"status={r.get('status')} head={r.get('head')} url={url[:60] or '无'}），退回点页面")
    return ""


#: 从结账页 HTML 里读 x-aos-stk。
#:
#: 别照 <meta> / <input> 去找——那两处**都没有**（实测零命中）。键名也是
#: `x-aos-stk` 而不是 `xAosStk`，写错了就静默取不到。
JS_READ_STK = r"""
() => {
    const html = document.documentElement.innerHTML;
    const m = html.match(/["']x-aos-stk["']\s*:\s*["']([^"']+)["']/i);
    return m ? m[1] : "";
}
"""


def new_call_id() -> str:
    """仿 `m072unh8yy-mu0u0tf3` 这个形状。

    实测它是客户端自己生成的调用关联 ID（每个请求一个新值），不是安全令牌——
    服务端不会校验它的内容，但缺了它请求形状就跟真实浏览器对不上。
    """
    a = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    b = format(int(time.time() * 1000), "x")[-8:]
    return f"{a}-{b}"


def encode(pairs: list[tuple[str, str]]) -> str:
    from urllib.parse import urlencode
    return urlencode(pairs)


class Blocked(Exception):
    """被边缘节点拦了。不重试、不换路径——见模块开头的风险提示。"""


class CartMismatch(Exception):
    """购物袋里装的不是这次要买的东西。

    **这比慢几秒严重得多**：清袋静默失败、或者加购加错型号时，件数一样是 1，
    只数件数根本发现不了，然后就一路下单买错机器。原来的流程靠「先清袋再加购」
    来保证型号正确，但**加购之后没有任何地方复核过**——这个异常就是那道复核。
    """


class Stalled(Exception):
    """请求返回 200，但这一步**并没有推进**。

    200 不等于生效，这一点栽过一次：四步全 200，服务端却一直停在 Fulfillment，
    结果是找不到付款选项、再退回点页面时又对着一个状态错位的页面死点。
    判据见 EXPECT——每个 continue 步骤必须在响应里产出下一节。
    """


#: 每一步做完之后，body.checkout 里必须出现的那一节。
#: 名字来自真实响应（2026-09-14），是「这步真的生效了」的硬证据。
EXPECT = {
    "selectFulfillmentLocationAction": "fulfillment",
    "search": "fulfillment",
    "continueFromFulfillmentToPickupContact": "pickupContact",
    "continueFromPickupContactToBilling": "billing",
    "selectBillingOptionAction": "billing",
    "continueFromBillingToReview": "review",
}


class FastCheckout:
    """六步走完结账向导，停在 Review。

    store / city / state / district 要跟你真实选的那家门店对得上：`search` 那步
    是靠它们把门店定位出来的。默认值来自 2026-09-14 的实测（五角场 R581）。
    """

    def __init__(self, *, store: str, id_last4: str, last_name: str, first_name: str,
                 city: str = "上海", state: str = "上海", district: str = "杨浦区",
                 payment_label: str = "招商银行", installment_months: int = 24,
                 fapiao: str = "e_personal_fdf", place_order: bool = False,
                 stk_timeout_ms: int = 15000, log=print):
        self.store = (store or "").strip()
        self.id_last4 = (id_last4 or "").strip()
        self.last_name = (last_name or "").strip()
        self.first_name = (first_name or "").strip()
        self.city, self.state, self.district = city, state, district
        self.payment_label = (payment_label or "招商银行").strip()
        self.installment_months = int(installment_months or 0)
        self.fapiao = fapiao
        #: 走到 Review 之后要不要真的提交。提交只创建待付款订单，不扣款——
        #: 但仅限扫码通道，信用卡路径在 _may_place 里硬拒。
        self.place_order = bool(place_order)
        #: 等 x-aos-stk 出现的上限。页面越慢这条路越值钱，所以别急着放弃。
        self.stk_timeout_ms = int(stk_timeout_ms)
        self.log = log
        self.order_url = ""
        self.stk = ""
        self.billing_option = ""
        self.timings: list[tuple[str, float]] = []

    # ---------- 底层 ----------

    def _post(self, page, path: str, action: str, module: str,
              fields: list[tuple[str, str]]) -> dict:
        t0 = time.monotonic()
        query = encode([("_a", action), ("_m", module)])
        body = encode(fields)
        r = page.evaluate(JS_POST, [path, query, body, self.stk, new_call_id()]) or {}
        dt = time.monotonic() - t0
        self.timings.append((action, dt))
        status = r.get("status")
        if status in BLOCK_CODES:
            raise Blocked(f"{action} 被拦（{status}）")
        if status != 200:
            raise RuntimeError(f"{action} 返回 {status}"
                               + (f"：{r['error']}" if r.get("error") else ""))
        data = r.get("json") or {}
        want = EXPECT.get(action)
        if want:
            got = ((data.get("body") or {}).get("checkout") or {})
            if want not in got:
                raise Stalled(
                    f"{action} 返回 200，但响应里没有 `{want}` 这一节"
                    f"（实际有：{', '.join(list(got)[:8]) or '空'}）——这一步没生效")
        seg = r.get("ms") or {}
        net = r.get("net") or {}
        extra = f"（等服务端 {seg.get('head', '?')}ms / 收包 {seg.get('body', '?')}ms"
        if net:
            extra += f" · TTFB {net.get('ttfb')}ms / 排队 {net.get('stalled')}ms"
        self.log(f"[快车道] {action} ✓ {dt * 1000:.0f}ms {extra}）")
        return data

    # ---------- 六步 ----------

    def _search_input(self) -> str:
        return f"{self.city} {self.district}".strip()

    def _store_fields(self) -> list[tuple[str, str]]:
        return [
            (f"{_LOC}.showAllStores", "false"),
            (f"{_LOC}.selectStore", self.store),
            (f"{_LOC}.searchInput", self._search_input()),
            (f"{_ADDR}.city", self.city),
            (f"{_ADDR}.state", self.state),
            (f"{_ADDR}.provinceCityDistrict", f"{self.city} {self.district}"),
            (f"{_ADDR}.countryCode", "CN"),
            (f"{_ADDR}.district", self.district),
        ]

    def step1_pickup(self, page) -> None:
        self._post(page, "/shop/checkoutx/fulfillment",
                   "selectFulfillmentLocationAction", f"{_FUL}.fulfillmentOptions",
                   [(f"{_FUL}.fulfillmentOptions.selectFulfillmentLocation", "RETAIL")])

    def step2_store(self, page) -> None:
        self._post(page, "/shop/checkoutx/fulfillment", "search", _LOC,
                   self._store_fields())

    def step3_to_contact(self, page) -> dict:
        return self._post(page, "/shop/checkoutx/fulfillment",
                   "continueFromFulfillmentToPickupContact", _FUL,
                   [(f"{_FUL}.fulfillmentOptions.selectFulfillmentLocation", "RETAIL")]
                   + self._store_fields())

    def step4_to_billing(self, page, contact_model: dict) -> dict:
        fields = self.contact_fields(contact_model)
        got = {k.rsplit(".", 1)[-1]: v for k, v in fields}
        self.log("[快车道] 取货人信息："
                 + "、".join(f"{k}({len(v)}字)" for k, v in got.items() if v))
        missing = [k for k in ("lastName", "firstName", "nationalIdSelf") if not got.get(k)]
        if missing:
            raise Stalled(
                f"取货人信息缺 {', '.join(missing)}——Apple 没预填、config 里也没有。"
                f"姓名通常由账号带出；身份证后四位必须填 autobuy.id_last4。")
        return self._post(page, "/shop/checkoutx",
                          "continueFromPickupContactToBilling", _CONTACT, fields)

    def step5_select_bank(self, page) -> dict:
        return self._post(page, "/shop/checkoutx/billing", "selectBillingOptionAction",
                          _BILL, [
                              (f"{_BILL}.selectBillingOption", self.billing_option),
                              (f"{_BILL}.bankLookUp.selectBank", ""),
                              ("checkout.locationConsent.locationConsent", "true"),
                          ])

    def step6_to_review(self, page, months: int) -> dict:
        return self._post(page, "/shop/checkoutx/billing", "continueFromBillingToReview",
                          "checkout.billing", [
                              (f"{_BILL}.selectBillingOption", self.billing_option),
                              (f"{_BILL}.bankLookUp.selectBank", ""),
                              (f"{_INSTALL}.selectInstallmentOption", str(months)),
                          ])

    # ---------- 取货人信息 ----------

    #: 第 4 步要回传的字段 → 在 pickupContact 模型里的相对位置。
    #: 值**不要自己编**：Apple 已经按账号预填好了，第 3 步的响应里就带着。
    CONTACT_FIELDS = (
        ("lastName", f"{_SELF}.selfContact.address.lastName"),
        ("firstName", f"{_SELF}.selfContact.address.firstName"),
        ("verificationToken",
         f"{_SELF}.selfContact.address.verificationModule.verificationToken"),
        ("nationalIdSelf", f"{_SELF}.nationalIdSelf.nationalIdSelf"),
        ("selectFapiao", f"{_CONTACT}.eFapiaoSelector.selectFapiao"),
        ("invoiceHeader", f"{_CONTACT}.eFapiaoSelector.ePersonalFapiao.invoiceHeader"),
    )

    @staticmethod
    def _harvest(node, field: str) -> str:
        """从 pickupContact 模型里取某个字段的**当前值**。

        模型把当前值放在 `d` 下、上一次的值放在 `was` 下，所以必须只认 `d`，
        否则会把旧值发回去。
        """
        stack = [(node, "")]
        while stack:
            cur, path = stack.pop()
            if isinstance(cur, dict):
                for k, v in cur.items():
                    p = f"{path}.{k}" if path else k
                    if (k == field and isinstance(v, str)
                            and ".was" not in path and path.endswith("d")):
                        return v
                    stack.append((v, p))
            elif isinstance(cur, list):
                stack.extend((v, path) for v in cur)
        return ""

    def contact_fields(self, contact_model: dict) -> list[tuple[str, str]]:
        """拼第 4 步的请求体。

        **姓名/邮箱/电话一律用 Apple 预填的那份**，不要求用户在 config 里重填一遍——
        实测配置里为空时发空姓名过去，服务端校验不过、返回 200 却停在原地
        （这正是「200 不等于生效」那个坑的现场）。config 里填了才覆盖。
        身份证后四位是唯一账号不会预填的，必须由 config 提供。
        """
        override = {
            "lastName": self.last_name,
            "firstName": self.first_name,
            "nationalIdSelf": self.id_last4,
            "selectFapiao": self.fapiao,
        }
        out = []
        for field, key in self.CONTACT_FIELDS:
            val = override.get(field) or self._harvest(contact_model, field)
            out.append((key, val))
        return out

    # ---------- 下单 ----------

    def _may_place(self) -> str:
        """能不能提交。返回空串表示可以，否则是拒绝的理由。

        这一条不给配置绕过：信用卡 / 借记卡路径点下单是**即时扣款**，那等于
        代人付款，超出这个工具的边界。扫码通道只创建待付款订单，性质不同。
        """
        from .checkout import CARD_PAY, SCAN_PAY
        want = self.payment_label
        if any(p in want or want in p for p in SCAN_PAY):
            return ""
        if any(p in want or want in p for p in CARD_PAY):
            return f"「{want}」是即时扣款的卡支付，不代下单"
        return f"「{want}」不在已知的扫码付款方式里，保险起见不代下单"

    def step7_place_order(self, page) -> str:
        data = self._post(page, PLACE_PATH, PLACE_ACTION, PLACE_MODULE, [])
        return str(((data.get("head") or {}).get("data") or {}).get("url") or "")

    def step8_check_status(self, page, tries: int = 12, delay: float = 1.5) -> str:
        """轮询下单结果，返回最终去处的 URL。

        **成功与否看这个 URL，不要看页面文案。** status 页在处理中显示「正在处理」，
        失败时也一样——只有最终跳去哪里能分辨：回到 /shop/checkout 就是被驳回。
        """
        last = ""
        for i in range(max(1, tries)):
            data = self._post(page, STATUS_PATH, STATUS_ACTION, STATUS_MODULE, [])
            url = str(((data.get("head") or {}).get("data") or {}).get("url") or "")
            last = url or last
            if url and "/shop/checkout/status" not in url:
                return url
            if i + 1 < tries:
                page.wait_for_timeout(int(delay * 1000))
        return last

    #: 只有跳到这些地方才算下单成功。
    ORDER_OK = re.compile(r"(thankyou|/shop/order|orderstatus)", re.I)

    @classmethod
    def order_rejected(cls, url: str) -> bool:
        """下单是不是没成。**只有明确跳到 thankyou / 订单页才算成功，其余一律算没成。**

        反过来写（「回到结账页就算失败，其余算成功」）会制造假成功：轮询超时时
        最后拿到的还是 `/shop/checkout/status`，按那种写法会被判成下单成功，
        于是不重试、也不提醒，而实际上购物袋还在那儿。误报成功的代价比误报
        失败大得多，所以这里只认硬证据。

        实测被驳回时 checkStatus 返回 302 到 `.../shop/checkout`，页面上才出现
        「你所选择的『送货与取货』选项已不再为本订单提供」——光看文案会漏判，
        因为处理中和失败的 status 页文案都是「正在处理」。
        """
        return not cls.ORDER_OK.search((url or "").split("?")[0])

    # ---------- 从响应里挖选项 ----------

    @staticmethod
    def _walk(node, want: str):
        """在任意深度的 JSON 里找含指定键的对象。响应结构 Apple 改过几次，
        按路径硬取一改版就断，按键名找稳得多。"""
        out = []
        stack = [node]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if want in cur:
                    out.append(cur)
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
        return out

    def billing_labels(self, data: dict) -> list[str]:
        """列出响应里所有付款选项的名字。匹配失败时打出来，省得下次继续猜。"""
        out = []
        for obj in self._walk(data, "labelImageAlt"):
            alt = str(obj.get("labelImageAlt") or "").strip()
            if alt and obj.get("value") and alt not in out:
                out.append(alt)
        return out

    def find_billing_option(self, data: dict) -> str:
        """按银行名字找付款选项的 value。

        **不能写死**：实测是 `installments0001321713` 这种 ID，随会话/商品变。
        认的是 labelImageAlt（页面上那个银行 logo 的 alt 文字）。
        """
        for obj in self._walk(data, "labelImageAlt"):
            alt = str(obj.get("labelImageAlt") or "")
            val = str(obj.get("value") or "")
            if val and (self.payment_label in alt or alt in self.payment_label):
                return val
        return ""

    def find_installment(self, data: dict, months: int) -> int:
        """找分期期数。选项形如 {"value": 24, "label": "24 期"}。"""
        best = []
        for obj in self._walk(data, "selectInstallmentOption"):
            for opt in (obj.get("options") or []):
                try:
                    best.append(int(opt.get("value")))
                except (TypeError, ValueError):
                    continue
        if months in best:
            return months
        return 0 if not best else max(best)

    # ---------- 编排 ----------

    def wait_for_stk(self, page, timeout_ms: int = 0) -> str:
        """等结账页把 x-aos-stk 吐出来，拿到就返回。

        **这是快车道相对点页面的真正优势所在。** 令牌写在服务端返回的 HTML
        内联 JSON 里，`domcontentloaded` 就能读到——不需要等 React 把向导渲染
        出来。点页面那条路必须等「继续」按钮真的画出来才能操作（on_checkout
        最多等 9s），页面渲染越慢它越没辙；发包只要 HTML 到了就能干活。

        所以这里要**轮询等**，不能读一次拿不到就放弃——那等于在页面最慢、
        最需要这条路的时候主动退回到更慢的那条路上去。
        """
        deadline = time.monotonic() + (timeout_ms or self.stk_timeout_ms) / 1000
        while True:
            try:
                tok = page.evaluate(JS_READ_STK) or ""
            except Exception:
                tok = ""          # 页面正在跳转，执行上下文没了，等下一轮
            if tok:
                return tok
            if time.monotonic() >= deadline:
                return ""
            page.wait_for_timeout(200)

    def run(self, page) -> tuple[bool, str, str]:
        """跑完六步。返回 (是否到 Review, 阶段, 说明)。**不下单。**"""
        t0 = time.monotonic()
        # 后台标签页会被 Chrome 降网络优先级、还会挨 timer 节流。抢购这几十秒
        # 全花在等服务端上，没理由让浏览器自己再给它打个折。
        #
        # 注意：这不是 2026-09-15 那次「每步被垫到 10 秒」的解药——那个是出口
        # IP 的问题（见 README），切到前台一样 10 秒。留着只是因为它本来就该做。
        try:
            page.bring_to_front()
        except Exception as e:
            self.log(f"[快车道] 切前台失败（不影响后续）：{type(e).__name__}: {e}")
        self.stk = self.wait_for_stk(page)
        if not self.stk:
            return False, "⚠️ 读不到 x-aos-stk", (
                "结账页里没找到令牌——可能不在结账页上，或者 Apple 改了内联 JSON 的写法。"
                "退回点页面的老路。")
        self.log(f"[快车道] 令牌就位（{len(self.stk)} 字符，等了 "
                 f"{(time.monotonic() - t0) * 1000:.0f}ms），开始六步")

        try:
            self.step1_pickup(page)
            self.step2_store(page)
            contact = self.step3_to_contact(page)
            billing = self.step4_to_billing(page, contact)

            self.billing_option = self.find_billing_option(billing)
            if not self.billing_option:
                have = self.billing_labels(billing)
                return False, f"⚠️ 没找到付款方式「{self.payment_label}」", (
                    f"第 4 步响应里可选的付款方式是：{'、'.join(have) or '（一个都没有）'}。"
                    f"一个都没有通常说明这一步其实没走到 Billing；有但对不上就是"
                    f"名字写法不一致，按上面的名字改 payment_method。")
            self.log(f"[快车道] 付款方式 {self.payment_label} → {self.billing_option}")

            opts = self.step5_select_bank(page)
            months = self.find_installment(opts, self.installment_months)
            if not months:
                return False, "⚠️ 没找到可用的分期期数", "第 5 步的响应里没有分期选项。"
            if months != self.installment_months:
                self.log(f"[快车道] ⚠️ 没有 {self.installment_months} 期，改用 {months} 期")
            self.step6_to_review(page, months)

            if self.place_order:
                why = self._may_place()
                if why:
                    self.log(f"[快车道] 不提交订单：{why}")
                else:
                    dest = self.step7_place_order(page)
                    self.log(f"[快车道] 已提交，处理中（{dest or '?'}）")
                    final = self.step8_check_status(page)
                    self.order_url = final
                    if self.order_rejected(final):
                        return False, "⚠️ 下单被驳回", (
                            f"六步都走通了，提交后被打回结账页（{final or '无跳转'}）。"
                            f"最常见的原因是所选取货门店在提交那一刻已不能履约——"
                            f"Apple 的原话是「你所选择的『送货与取货』选项已不再为本订单提供」。"
                            f"请去浏览器里看结账页上的提示。")
                    return True, "✅ 待付款订单已创建", (
                        f"跳转到 {final}。"
                        f"请在约 30 分钟内自己扫码支付——**本工具不代付款**。")
        except Stalled as e:
            return False, "⚠️ 步骤没生效", (
                f"{e}。已经改动过的服务端状态和页面可能不一致，"
                f"调用方需要重新加载结账页再接管。")
        except Blocked as e:
            return False, "⚠️ 结账被限流", (
                f"{e}。这是 Akamai 的拦截，不是页面问题——**别重试**，"
                f"越撞退避越深。见 README 坑 9。")
        except Exception as e:
            return False, f"⚠️ {type(e).__name__}", f"{e}。退回点页面的老路。"

        total = time.monotonic() - t0
        detail = " / ".join(f"{a} {d * 1000:.0f}ms" for a, d in self.timings)
        return True, "已到 Review（未下单）", (
            f"六步走完共 {total:.1f}s（{detail}）。"
            f"付款方式 {self.payment_label} {months} 期。"
            f"**没有提交订单**，下单那一下留给你。")
