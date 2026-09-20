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
from urllib.parse import urlparse

#: 六步用到的字段前缀。写全是为了在出问题时能一眼对上 HAR。
_FUL = "checkout.fulfillment"
_LOC = f"{_FUL}.pickupTab.pickup.storeLocator"
_ADDR = f"{_LOC}.address.stateCitySelectorForCheckout"
#: 取货时段。2026-09-17 的 HAR 里，continueFromFulfillmentToPickupContact 多了
#: 这一组字段（09-14 那份 HAR 完全没有）——Apple 给自提加了「选具体时间」。
_SLOT = f"{_FUL}.pickupTab.pickup.timeSlot.dateTimeSlots"
_CONTACT = "checkout.pickupContact"
_SELF = f"{_CONTACT}.selfPickupContact"
_BILL = "checkout.billing.billingOptions"
_INSTALL = f"{_BILL}.selectedBillingOptions.installments.installmentOptions"

#: 被这些状态码拦住就立刻停。跟 checkout.py 里的判断保持一致。
BLOCK_CODES = (403, 541, 503, 429)

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
#: checkStatus 属于 status 页，不是结账页。见 JS_POST 上面那段注释。
STATUS_MODEL_PAGE = "checkoutStatusPage"

#: Review 那一步的页面地址。六步全是 XHR，服务端状态早就到 Review 了，
#: 但**标签页还停在原来那一步**，URL 里挂着的还是 `?_s=Fulfillment-init`
#: 之类的锚点——人手动按 F5 等于带着那个锚点重开，页面就回到那一步，
#: 看着就像「又重新走了一遍流程」。所以别让人刷，直接导航过去。
REVIEW_PATH = "/shop/checkout?_s=Review"

#: 「操作超时」页。结账会话 5 分钟没交互就作废，然后整页被扔到这儿。
#: 参数写在结账页模型的 `checkout.session` 里：interactionMs=300000、
#: alertMs=60000、ttl≈20 分钟，还带着一个 `extendSession` 的续期接口。
EXPIRED_URL = "/shop/sorry/session_expired"

#: 在页面里发一个同源 POST。走页面的 fetch 而不是 Python 的 requests，
#: 是因为这样复用的是浏览器自己的 cookie、TLS 指纹和 UA——换成外部客户端，
#: 指纹对不上反而更容易被风控盯上，而我们打的正是最敏感的那族端点。
#:
#: `x-aos-model-page` **不是常量**，这一点栽过一次（2026-09-18 07:15）：
#: 前六步和提交在 `checkoutPage` 上，而提交之后的 `checkStatus` 属于
#: **`checkoutStatusPage`**（真实浏览器的请求头就是这么写的）。拿 checkoutPage
#: 去问 checkStatus，服务端一律回「还在处理」——9 轮全废，而订单其实早就建好了。
JS_POST = r"""
async ([path, query, body, stk, callId, modelPage]) => {
    const url = location.origin + path + "?" + query;
    const t0 = performance.now();
    try {
        const res = await fetch(url, {
            method: "POST",
            credentials: "include",
            signal: AbortSignal.timeout(30000),
            headers: {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "*/*",
                "X-Requested-With": "Fetch",
                "syntax": "graviton",
                "modelVersion": "v2",
                "x-aos-model-page": modelPage || "checkoutPage",
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
                retry_after: res.headers.get("Retry-After"),
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
#: 不成就停止本次尝试，不猜测其它结账入口。
JS_BAG_TO_CHECKOUT = r"""
async ([path, query, stk]) => {
    const out = {stk: !!stk};
    try {
        const res = await fetch(location.origin + path + "?" + query, {
            method: "POST",
            credentials: "include",
            signal: AbortSignal.timeout(30000),
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
        out.retry_after = res.headers.get("Retry-After");
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
        const res = await fetch("/shop/bag", {credentials: "include", signal: AbortSignal.timeout(30000)});
        if (!res.ok) return {status: res.status, cart: false, origin: location.origin,
                             retry_after: res.headers.get("Retry-After")};
        return {...parse(await res.text()), status: res.status, origin: location.origin};
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
            signal: AbortSignal.timeout(30000),
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
        return {status: res.status, left: left, retry_after: res.headers.get("Retry-After")};
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


#: 加购其实是一个 **GET**，不是 POST（2026-09-19 从 HAR 里挖出来的）：
#:
#:   GET /shop/buy-iphone/iphone-18-pro/mjy94ch/a
#:         ?product=MJY94CH/A&purchaseOption=fullPrice&step=select
#:         &acpart=none&atbtoken=<40位十六进制>&igt=true&add-to-cart=add-to-cart
#:   → 303 → …?product=mjy94ch/a&step=attach
#:
#: 折抵和 AppleCare 就编码在 purchaseOption / acpart 里——所以这条路连「等页面
#: 水合、点两个单选框」都不需要。实测空袋加一台 **388ms**，而走产品页是 11~28s。
ATB_COOKIE = "as_atb"


def atb_token(ctx) -> str:
    """从浏览器的 cookie jar 里读 atbtoken。零请求。

    它不是「页面 JS 在点击时现算」的（README 以前是这么写的，错了），而是
    cookie `as_atb` 用 `|` 分割后的最后一段。页面 JS 自己就是这么取的：

        const t = Is.get("as_atb"), a = (t ? t.split("|") : []).pop();

    形如 `1.0|<base64 时间戳>|<40 位十六进制>`。读自己会话的 cookie 跟伪造令牌
    是两回事，性质和 x-aos-stk 完全相同。

    **一次性**：用过的 token 再发一次，服务端返回 200、页面照常、袋子纹丝不动
    （实测）。所以每次都要现读，而且加完必须复核。
    """
    if ctx is None:
        return ""
    try:
        jar = ctx.cookies()
        if not isinstance(jar, (list, tuple)):
            return ""
        for c in jar:
            if isinstance(c, dict) and str(c.get("name") or "") == ATB_COOKIE:
                seg = str(c.get("value") or "").split("|")
                return seg[-1].strip() if seg else ""
    except Exception:
        return ""
    return ""


def atb_add_url(buy_url: str, part: str, token: str) -> str:
    """拼出「加购」那个 GET 的地址。

    路径用小写（HAR 里就是 `/…/mjy94ch/a`），而 `product` 参数用大写原样。
    """
    from urllib.parse import quote, urlsplit, urlunsplit
    u = urlsplit(buy_url)
    q = ("product=" + quote(part.upper(), safe="")
         + "&purchaseOption=fullPrice&step=select&acpart=none"
         + "&atbtoken=" + quote(token, safe="")
         + "&igt=true&add-to-cart=add-to-cart")
    return urlunsplit((u.scheme, u.netloc, u.path.lower(), q, ""))


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
    raise_if_blocked(st, "读取购物袋")
    if not _origin_ok(st, want_origin, log):
        return {"ok": False, "reason": "当前页不在主站上，读到的购物袋状态不可信"}
    stk = str(st.get("stk") or "")
    items = [str(x) for x in (st.get("items") or []) if x]
    skus = [str(x).upper() for x in (st.get("skus") or []) if x]
    qty = [int(n) for n in (st.get("qty") or []) if isinstance(n, int)]
    want = (want_part or "").upper().strip()

    if not st.get("cart") or st.get("count") is None:
        return {"ok": False, "reason": "未读到有效购物袋模型，不能当成空袋"}
    if not items and st.get("count") != 0:
        return {"ok": False, "reason": "购物袋非空但条目解析失败"}
    if not items:
        log("[快车道] 购物袋本来就是空的，直接加购")
        return {"ok": True, "kept": False, "removed": 0}
    # 「已经正好是目标」必须三个条件都满足：只有一条、型号对、**数量是 1**。
    # 少了数量那条就会放过「同型号两台」——集合比对看不出来，实测中招过。
    if want and len(items) == 1 and skus == [want] and qty[:1] == [1]:
        log(f"[快车道] 购物袋里已经正好是 {want} × 1，跳过清空和加购")
        # 把刚读到的原始状态带出去：紧接着 bag_to_checkout 要的是同一份东西，
        # 递过去就少 fetch 一次。放货那一刻一次袋状态读要 2.5 秒。
        return {"ok": True, "kept": True, "removed": 0, "state": st}
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
        raise_if_blocked(r, "删除购物袋条目")
        if r.get("status") != 200:
            return {"ok": False, "removed": removed,
                    "reason": f"删 {key[:16]}… 返回 {r.get('status')}"}
        removed += 1
    log(f"[快车道] 已清空购物袋（接口删了 {removed} 件，"
        f"{(time.monotonic() - t0) * 1000:.0f}ms，没加载购物袋页）")
    return {"ok": True, "kept": False, "removed": removed}


def wait_for_bag_count(page, minimum: int = 1, cap_ms: int = 2000,
                       step_ms: int = 100) -> float:
    """轮询购物袋接口，等袋里至少有 minimum 件。返回实际等了多少毫秒。

    替掉加购后那个固定的 600ms：快的时候（通常 100~200ms 就进袋了）立刻往下走，
    慢的时候多给到 2s，而不是雷打不动睡 600ms——固定睡法快慢两头都不讨好。

    等不到也不报错：调用方紧接着就是 bag_to_checkout，它对空袋本来就有明确处理。
    """
    t0 = time.monotonic()
    deadline = t0 + cap_ms / 1000
    while True:
        try:
            st = page.evaluate(JS_CART_STATE)
        except Exception:
            st = {}
        if not isinstance(st, dict):
            # 读不到可用的购物袋模型——再轮询也是同样的结果，交给 bag_to_checkout
            # 去下结论，别在这儿空转。
            break
        count = st.get("count")
        if isinstance(count, int) and count >= minimum:
            break
        if time.monotonic() >= deadline:
            break
        try:
            page.wait_for_timeout(step_ms)
        except Exception:
            break
    return (time.monotonic() - t0) * 1000


def bag_to_checkout(page, want_part: str = "", want_qty: int = 1,
                    want_origin: str = "", log=print, state: dict | None = None) -> str:
    """从购物袋接口直接拿到结账地址。拿不到返回空串，调用方停止本次尝试。

    want_part 给了就**强制复核**袋里的型号，对不上抛 CartMismatch——
    宁可这一单不下，也不能买错机器。

    `state` 是**刚刚**读到的购物袋状态（毫秒级之前）。给了就不再自己 fetch 一次：
    放货那一刻主站很慢，2026-09-20 09:50 实测一次袋状态读就要 2.5 秒，而加购后
    的复核和这里读的是同一份东西。省一次就是省 2.5 秒，那是整趟 21 秒里的一大块。
    """
    t0 = time.monotonic()
    if isinstance(state, dict) and state:
        st = state
    else:
        try:
            st = page.evaluate(JS_CART_STATE) or {}
        except Exception as e:
            log(f"[快车道] 读购物袋状态失败：{type(e).__name__}")
            return ""
    raise_if_blocked(st, "读取购物袋")
    stk, count = str(st.get("stk") or ""), st.get("count")
    # 空袋子不能往下走：加购很可能压根没成，这时候进结账只会得到一个空订单
    # 或者莫名其妙的跳转，而且掩盖了「加购失败」这个真正的问题。
    if count == 0:
        log("[快车道] ⚠️ 购物袋是空的——加购没成？不进结账，停止本次尝试")
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
        log(f"[快车道] ⚠️ 购物袋 HTML 里读不到 sku，没法复核型号，停止本次尝试")
        return ""
    if not stk:
        if want and count == 1 and skus == [want] and qty == [want_qty]:
            raise EntryUnavailable("已核对购物袋，接口入口缺少令牌，需要页面建立结账会话")
        return ""
    if want and (not qty or any(n <= 0 for n in qty) or len(qty) != len(skus)):
        raise CartMismatch("购物袋数量字段不完整，不能确认实际购买数量")
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
    raise_if_blocked(r, "购物袋结账")
    from .checkout import is_sign_in
    if r.get("status") == 200 and ("/shop/checkout" in url or is_sign_in(url)):
        log(f"[快车道] 直接从购物袋接口进结账（{dt:.0f}ms，没加载购物袋页）")
        return url
    if EXPIRED_URL in url or "/shop/sorry/" in url:
        # 报成「没给出结账地址」会让人去查购物袋、查令牌，全是白查。
        raise SessionExpired(f"购物袋接口把我们指向「操作超时」页（{url[:60]}）")
    if (r.get('status') == 200 and want and count == 1 and skus == [want]
            and qty == [want_qty] and (not url or urlparse(url).path.rstrip('/') == '/shop/bag')):
        raise EntryUnavailable('购物袋已核对，接口没有建立结账会话，使用页面入口')
    log(f"[快车道] 购物袋接口没给出结账地址（{dt:.0f}ms，"
        f"status={r.get('status')} head={r.get('head')} url={url[:60] or '无'}），停止本次尝试")
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
    """被边缘节点拦了。携带服务端要求的冷却时间。"""

    def __init__(self, message: str, retry_after=0):
        from .apple import parse_retry_after
        super().__init__(message)
        self.retry_after = parse_retry_after(retry_after)


#: 响应里可能藏着拒绝原因的键。Apple 的结账模型没有统一的错误位置，
#: 所以按键名捞——捞到什么都比「返回 200 但没有 review 这一节」强。
_ERR_KEYS = ("error", "errormessage", "errors", "message", "messages",
             "validation", "warning", "alert", "reason")


def stall_hints(data, limit: int = 4) -> str:
    """从没推进的响应里把像「拒绝原因」的文字捞出来。

    2026-09-20 08:57 走到了第 6 步（前五步全过、取货时段都拿到了），
    continueFromBillingToReview 返回 200 却停在 billing。请求体跟 HAR 里
    手动走通的那次**逐字段一致**，所以不是我们写错了——是服务端拒绝了，
    而我们当时完全看不到原因。这个函数就是为了下次能看到。

    只捞短文本，不落盘整个响应体：那里面有姓名、手机、地址。
    """
    out, seen, walked = [], set(), set()
    stack = [data]
    while stack and len(out) < limit:
        cur = stack.pop()
        # 结账模型里有自引用（companionBar 之间互指），不记访问过的会原地打转
        if id(cur) in walked:
            continue
        walked.add(id(cur))
        if isinstance(cur, dict):
            for k, v in cur.items():
                kl = str(k).lower()
                if isinstance(v, str) and v.strip() and any(e in kl for e in _ERR_KEYS):
                    txt = " ".join(v.split())[:120]
                    if txt not in seen:
                        seen.add(txt)
                        out.append(f"{k}={txt}")
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(x for x in cur if isinstance(x, (dict, list)))
    return "；".join(out[:limit])


def raise_if_blocked(response: dict, action: str) -> None:
    if response.get('status') in BLOCK_CODES:
        raise Blocked(f"{action}被拦（{response['status']}）", response.get('retry_after'))



class EntryUnavailable(Exception):
    """购物袋已核实，但接口入口未就绪；允许通过购物袋页面建立会话。"""


class CartMismatch(Exception):
    """购物袋里装的不是这次要买的东西。

    **这比慢几秒严重得多**：清袋静默失败、或者加购加错型号时，件数一样是 1，
    只数件数根本发现不了，然后就一路下单买错机器。原来的流程靠「先清袋再加购」
    来保证型号正确，但**加购之后没有任何地方复核过**——这个异常就是那道复核。
    """


class SessionExpired(Exception):
    """结账会话作废了（被指向 `/shop/sorry/session_expired`）。

    **跟 Stalled 必须分开。** Stalled 是「这一步没生效，重新加载页面接着走」；
    会话过期是「整条链路作废」，在上面重试、重新加载都没用——只能重新登录、
    从购物袋重新开始。

    来路写在结账页的模型里（2026-09-17 的 HAR）：`checkout.session` 那一节的
    `interactionMs: 300000` —— **5 分钟没有交互就作废**，`expiredUrl` 就是那一页。
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


#: 时段字段的对照表：左边是请求体里的表单名，右边是响应里那个时段对象的键名。
#: 两边名字对不上是 Apple 自己的事，对照关系抄自 checkout.js 里那段 setValue
#: （`b("timeSlotId",t.SlotId||"")`…），不是猜的。
SLOT_FIELDS = (
    ("startTime", "checkInStart"),
    ("endTime", "checkInEnd"),
    ("displayStartTime", "displayStart"),
    ("displayEndTime", "displayEnd"),
    ("timeSlotType", "timeSlotType"),
    ("timeSlotId", "SlotId"),
    ("signKey", "signKey"),
    ("timeZone", "timeZone"),
    ("timeSlotValue", "timeSlotValue"),
    ("isRestricted", "isRestricted"),
)

#: "12:30 PM" / "9:05 AM" / "12:30"。取货时段的开始时刻长这几种样子。
_CLOCK = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*([AaPp])?[Mm]?\.?\s*$")


def clock_minutes(text: str) -> int:
    """把时刻折算成当天的分钟数，看不懂返回 -1。

    **12 小时制必须认**：响应里的 `checkInStart` 是「12:30 PM」这种写法，
    按 24 小时制硬读会把下午一点读成凌晨一点，于是配了 "13:00" 反而选到最早那档。
    """
    m = _CLOCK.match(str(text or ""))
    if not m:
        return -1
    hour, minute, half = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if minute > 59 or hour > (12 if half else 23):
        return -1
    if half == "A" and hour == 12:
        hour = 0
    elif half == "P" and hour != 12:
        hour += 12
    return hour * 60 + minute


def slot_minutes(slot: dict) -> int:
    """一档时段几点开始。先认 `checkInStart`，它不在就从 timeSlotValue 拆。

    timeSlotValue 形如 `18-12:30-12:45`（日-开始-结束），是兜底而不是首选：
    它没有 AM/PM，只在 checkInStart 缺席时才比它强。
    """
    got = clock_minutes(slot.get("checkInStart"))
    if got >= 0:
        return got
    parts = str(slot.get("timeSlotValue") or "").split("-")
    return clock_minutes(parts[1]) if len(parts) >= 2 else -1


def slot_label(chosen: dict) -> str:
    """给日志用的人话：`2026-09-18 12:30 – 12:45`。"""
    slot = chosen.get("slot") or {}
    when = str(slot.get("Label") or slot.get("timeSlotValue") or "").strip()
    return f"{chosen.get('date') or '?'} {when}".strip()


def _dedupe(items) -> list[str]:
    """保序去重，顺手 strip。门店编号列表两头都要用。"""
    out: list[str] = []
    for x in items or []:
        v = str(x or "").strip()
        if v and v not in out:
            out.append(v)
    return out


class FastCheckout:
    """六步走完结账向导，停在 Review。

    store / city / state / district 要跟你真实选的那家门店对得上：`search` 那步
    是靠它们把门店定位出来的。默认值来自 2026-09-14 的实测（五角场 R581）。
    """

    def __init__(self, *, store: str, id_last4: str, last_name: str, first_name: str,
                 email: str = "", phone: str = "",
                 city: str = "上海", state: str = "上海", district: str = "杨浦区",
                 payment_label: str = "招商银行", installment_months: int = 24,
                 fapiao: str = "e_personal_fdf", place_order: bool = False,
                 pickup_time: str = "", stk_timeout_ms: int = 15000, log=print,
                 submit_guard=None, cancelled=None, stores=None,
                 require_slot: bool = True, stages=None):
        self.submit_guard = submit_guard
        self.cancelled = cancelled or (lambda: False)
        self.store = (store or "").strip()
        #: 备选门店，第 2 步排不上取货时**就地**换下一家，不必把整条链路重来。
        #: 抢手时这一步最要命：一趟重来是十几秒 + 一次重新加购，而换店只是
        #: 一次 fulfillment 请求。第一家永远是 self.store。
        self.stores = [x for x in _dedupe([self.store] + list(stores or [])) if x]
        #: 实际下单用的那家。轮换之后跟 self.store 可能不同，日志和结果都看它。
        self.store_used = self.store
        #: 拿不到取货时段就换店、全都拿不到就停。置 False 可退回老流程（无时段
        #: 也继续），仅在 Apple 回退到 2026-09-17 之前的流程时才需要。
        self.require_slot = bool(require_slot)
        self.id_last4 = (id_last4 or "").strip()
        self.last_name = (last_name or "").strip()
        self.first_name = (first_name or "").strip()
        #: 联系方式的兜底值。多数账号 Apple 会预填，只有没预填的账号才用得上，
        #: 见 contact_fields。
        self.email = (email or "").strip()
        self.phone = (phone or "").strip()
        self.city, self.state, self.district = city, state, district
        self.payment_label = (payment_label or "招商银行").strip()
        self.installment_months = int(installment_months or 0)
        self.fapiao = fapiao
        #: 走到 Review 之后要不要真的提交。提交只创建待付款订单，不扣款——
        #: 但仅限扫码通道，信用卡路径在 _may_place 里硬拒。
        self.place_order = bool(place_order)
        #: 想要哪一档取货时段：earliest（默认，最早可选）/ latest / "HH:MM"。
        #: 具体含义见 choose_slot。
        self.pickup_time = (pickup_time or "").strip()
        #: 第 2 步挑好的那一档，第 3 步要连同门店一起回传。空 dict = 这家店
        #: 这一版流程不需要选时段（2026-09-17 之前就是这样）。
        self.slot: dict = {}
        #: 「立即下单」那一发**已经送出去了**。一旦为 True，订单就可能已经在
        #: Apple 那边建好了——哪怕后面轮询没能拿到结论。调用方必须靠它决定
        #: 「还能不能再点一次下单」，见 2026-09-18 07:15 那单的教训。
        self.submitted = False
        #: 等 x-aos-stk 出现的上限。页面越慢这条路越值钱，所以别急着放弃。
        self.stk_timeout_ms = int(stk_timeout_ms)
        self.log = log
        self.order_url = ""
        self.failure_kind = ""
        self.retry_after = 0.0
        self.stk = ""
        self.billing_option = ""
        self.timings: list[tuple[str, float]] = []

    # ---------- 底层 ----------

    def _post(self, page, path: str, action: str, module: str,
              fields: list[tuple[str, str]], model_page: str = "") -> dict:
        t0 = time.monotonic()
        query = encode([("_a", action), ("_m", module)])
        body = encode(fields)
        r = page.evaluate(JS_POST, [path, query, body, self.stk, new_call_id(),
                                    model_page or "checkoutPage"]) or {}
        dt = time.monotonic() - t0
        self.timings.append((action, dt))
        status = r.get("status")
        if status in BLOCK_CODES:
            from .apple import parse_retry_after
            self.retry_after = parse_retry_after(r.get("retry_after"))
            raise Blocked(f"{action} 被拦（{status}）")
        if status != 200:
            raise RuntimeError(f"{action} 返回 {status}"
                               + (f"：{r['error']}" if r.get("error") else ""))
        data = r.get("json") or {}
        if not isinstance(data, dict):
            raise Stalled(f"{action} 没有返回结账对象")
        business_status = (data.get("head") or {}).get("status")
        if business_status is not None and str(business_status).isdigit() and int(business_status) >= 400:
            raise Stalled(f"{action} HTTP 200，但业务状态为 {business_status}")
        # 会话过期时服务端照样回 200，跳转写在响应体里（见 follow 的注释）。
        # 这一步必须排在 EXPECT 之前：不然会被报成「响应里没有 xxx 这一节」，
        # 看着像 Apple 改了结构，实际是会话早就没了。
        dest = str(((data.get("head") or {}).get("data") or {}).get("url") or "")
        if EXPIRED_URL in dest or "/shop/sorry/" in dest:
            raise SessionExpired(
                f"{action} 被指向「操作超时」页（{dest}）——结账会话作废了")
        want = EXPECT.get(action)
        if want:
            got = ((data.get("body") or {}).get("checkout") or {})
            if want not in got:
                hints = stall_hints(data)
                raise Stalled(
                    f"{action} 返回 200，但响应里没有 `{want}` 这一节"
                    f"（实际有：{', '.join(list(got)[:8]) or '空'}）——这一步没生效"
                    + (f"。响应里捞到的说法：{hints}" if hints
                       else "。响应里没有任何错误文字，服务端没说为什么"))
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

    def step2_store(self, page) -> dict:
        """选门店。**返回值别丢**：取货时段就挂在这一步的响应里。"""
        return self._post(page, "/shop/checkoutx/fulfillment", "search", _LOC,
                          self._store_fields())

    def step3_to_contact(self, page) -> dict:
        return self._post(page, "/shop/checkoutx/fulfillment",
                   "continueFromFulfillmentToPickupContact", _FUL,
                   [(f"{_FUL}.fulfillmentOptions.selectFulfillmentLocation", "RETAIL")]
                   + self._store_fields() + self.slot_fields(self.slot))

    def step4_to_billing(self, page, contact_model: dict) -> dict:
        fields = self.contact_fields(contact_model)
        got = {k.rsplit(".", 1)[-1]: v for k, v in fields}
        self.log("[快车道] 取货人信息："
                 + "、".join(f"{k}({len(v)}字)" for k, v in got.items() if v))
        missing = [k for k in ("lastName", "firstName", "nationalIdSelf") if not got.get(k)]
        # 邮箱/手机只有「这一步问了、而且两边都空」时才算缺：没进 fields 的那些
        # 是 Apple 已经预填好的，不该报缺。
        missing += [k for k, _key, _cfg in self.CONTACT_FALLBACK
                    if k in got and not got[k]]
        if missing:
            raise Stalled(
                f"取货人信息缺 {', '.join(missing)}——Apple 没预填、config 里也没有。"
                f"在 autobuy 里补：姓名 pickup_last_name / pickup_first_name，"
                f"身份证后四位 id_last4，邮箱/手机 pickup_email / pickup_phone。")
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
        fields = [(f'{_BILL}.selectBillingOption', self.billing_option),
                  (f'{_BILL}.bankLookUp.selectBank', '')]
        if months:
            fields.append((f'{_INSTALL}.selectInstallmentOption', str(months)))
        return self._post(page, '/shop/checkoutx/billing', 'continueFromBillingToReview',
                          'checkout.billing', fields)

    # ---------- 取货时段 ----------
    #
    # 2026-09-17 的 HAR 里，自提多了一步「选具体时间」：第 3 步的请求体里
    # 多出 13 个 `...timeSlot.dateTimeSlots.*` 字段，其中 timeSlotId 和 signKey
    # 是**服务端签发**的，编不出来——只能从第 2 步的响应里原样取出来回传。
    # 老流程（09-14 的 HAR）完全没有这一组，所以这里一律「有就带、没有就算」，
    # 免得在还没上这套流程的门店/地区上平白多发一堆字段。

    @classmethod
    def store_availability(cls, data: dict) -> list[tuple[str, bool, str]]:
        """从第 2 步的响应里读出**结账自己**看到的每店库存。

        返回 [(门店编号, 现在能不能取, 文案)]，顺序是 Apple 给的（按距离）。

        这是 2026-09-19 手录 HAR 挖出来的：一次 `search` 的响应里带着附近 12 家
        店的 `availability.availableNowForAllLines` 和「目前不可取货」这类文案，
        按 `storeId`（就是 R359 这种编号）索引。

        意义在于**换店不用再盲试**：原来一家不行就重发一次 search 试下一家，
        每次 10 秒；现在第一次 search 回来就知道哪几家真有货，直接挑对的那家。
        而且这是结账侧的口径——监控用的 pickup-message 接口跟它可能不一致
        （2026-09-18 07:08 和 07:15 同店同型号一败一成，多半就是这个差异）。
        """
        out: list[tuple[str, bool, str]] = []
        seen = set()
        for node in cls._walk(data, "retailStores"):
            for store in node.get("retailStores") or []:
                if not isinstance(store, dict):
                    continue
                sid = str(store.get("storeId") or "").strip().upper()
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                av = store.get("availability") or {}
                out.append((sid, bool(av.get("availableNowForAllLines")),
                            str(av.get("storeAvailability") or "").strip()))
        return out

    @classmethod
    def slot_candidates(cls, data: dict) -> list[dict]:
        """把响应里的取货时段摊平成候选列表，保持 Apple 给的先后顺序。

        模型是两个平行的列表：`pickUpDates` 是可选的日子，`timeSlotWindows`
        是每天的时段，按 `dayOfMonth` 索引（见 checkout.js 里的 `p[a][t.dayOfMonth]`）。
        `enabled: false` 的档位是页面上灰掉的那些，选了也结不了账，直接扔掉。
        """
        out: list[dict] = []
        for node in cls._walk(data, "timeSlotWindows"):
            dates = node.get("pickUpDates")
            windows = node.get("timeSlotWindows")
            if not isinstance(dates, list) or not isinstance(windows, list):
                continue
            for i, day in enumerate(dates):
                if not isinstance(day, dict):
                    continue
                dom = str(day.get("dayOfMonth") or "")
                bucket = windows[i] if i < len(windows) else None
                if not (isinstance(bucket, dict) and dom in bucket):
                    # 下标对不上就按 dayOfMonth 找。顺序是 Apple 的实现细节，
                    # 别让它一变就整条路走不通。
                    bucket = next((b for b in windows
                                   if isinstance(b, dict) and dom in b), None)
                for slot in ((bucket or {}).get(dom) or []):
                    if not isinstance(slot, dict) or slot.get("enabled") is False:
                        continue
                    if not slot.get("timeSlotValue"):
                        continue
                    out.append({"slot": slot, "dayOfMonth": dom,
                                "date": str(day.get("date") or "")})
            if out:
                break
        return out

    def choose_slot(self, data: dict) -> dict:
        """按配置挑一档。挑不出来返回空 dict。

        * 空 / earliest：最早那一档（默认——抢购场景就是越早拿到越好）
        * latest：**最早那一天**里最晚的一档，不是最后一天
        * "HH:MM"：那天里第一档不早于它的；当天没有就退到当天最后一档

        日子一律只看最早有档期的那天：自提的意义就在当天/次日拿货，
        为了一个时间点把取货日推后几天不是这个工具该替人做的决定。
        """
        cands = self.slot_candidates(data)
        if not cands:
            return {}
        want = self.pickup_time.lower()
        if want in ("", "earliest", "最早"):
            return cands[0]

        first_day = cands[0]["dayOfMonth"]
        same_day = [c for c in cands if c["dayOfMonth"] == first_day]
        if want in ("latest", "最晚"):
            return same_day[-1]

        mins = clock_minutes(want)
        if mins < 0:
            self.log(f"[快车道] ⚠️ 看不懂取货时段「{self.pickup_time}」"
                     f"（要 earliest / latest / HH:MM），按最早的那档选")
            return cands[0]
        later = [c for c in same_day if slot_minutes(c["slot"]) >= mins]
        if later:
            return later[0]
        self.log(f"[快车道] ⚠️ {same_day[0].get('date') or '当天'} {self.pickup_time} "
                 f"之后没有档期了，改用当天最后一档")
        return same_day[-1]

    @staticmethod
    def slot_fields(chosen: dict) -> list[tuple[str, str]]:
        """拼第 3 步要多带的那组字段。没挑到时返回空表——一个字段都别发。"""
        if not chosen:
            return []
        slot = chosen.get("slot") or {}

        def val(v) -> str:
            if v is None or v is False:
                return ""          # 实测 isRestricted / timeSlotType 就是发空串
            return "true" if v is True else str(v)

        out = [(f"{_SLOT}.{name}", val(slot.get(src))) for name, src in SLOT_FIELDS]
        out.append((f"{_SLOT}.date", str(chosen.get("date") or "")))
        out.append((f"{_SLOT}.dayRadio", str(chosen.get("dayOfMonth") or "")))
        out.append((f"{_SLOT}.isRecommended",
                    "true" if slot.get("recommendationLabel") else "false"))
        return out

    def select_store(self, page) -> dict:
        """选门店并挑时段。第一家不行就按**结账侧的库存**精准换，不盲试。

        2026-09-18 07:06 那一单就是盲试丢的：冰川蓝色在南京东路、浦东、静安、
        环球港四家同时放货，我们只试了南京东路（恰好是先卖完的那家），另外三家
        一家都没试就整条链路重来了。

        现在第一次 search 回来就能看到 12 家的库存，换店只在**结账说有货**的
        那几家里换；一家都没有就当场停住，把结账自己的说法原样报出来，而不是
        带着空时段撞进一个必死的第 3 步。
        """
        first = self.store
        data = self.step2_store(page)
        self.store_used = self.store
        try:
            self.take_slot(data)
            return data
        except Stalled as e:
            # except 的变量在块结束时会被 Python 删掉，得换个名字留住它
            first_miss, first_data = e, data
        avail = self.store_availability(data)

        ready = [sid for sid, ok, _ in avail if ok]
        if avail:
            shown = "、".join(f"{sid}{'✓' if ok else f'({q or "不可取"})'}"
                              for sid, ok, q in avail[:8])
            self.log(f"[快车道] 结账侧门店库存：{shown}")

        # 按我们的偏好顺序取「结账说有货」的那几家，首选那家已经试过了
        nxt = [x for x in self.stores if x in ready and x != self.store]
        nxt += [x for x in ready if x not in self.stores and x not in nxt]
        if avail and not ready:
            raise Stalled(
                f"结账侧 {len(avail)} 家门店全是「目前不可取货」——这一单已经没货了"
                f"（监控看到的是 {self.store}，结账不认）")
        if not nxt:
            return self._no_store_left(first, first_data, first_miss)

        for i, store in enumerate(nxt):
            self.store = store
            data = self.step2_store(page)
            try:
                self.take_slot(data)
            except Stalled as e:
                self.log(f"[快车道] {store} 结账说有货但排不上时段（{e}）"
                         + ("，换下一家" if i + 1 < len(nxt) else "，没有下一家了"))
                if i + 1 < len(nxt):
                    continue
                return self._no_store_left(first, first_data, first_miss)
            self.store_used = store
            self.log(f"[快车道] 实际下单门店 {store}（首选 {self.stores[0]} 没排上，"
                     f"按结账侧库存改选）")
            return data
        return self._no_store_left(first, first_data, first_miss)

    def _no_store_left(self, store: str, data: dict, why: Stalled) -> dict:
        """一家都没排上时的收场。

        默认抛出——没有任何「无时段却下单成功」的样本，继续走第 3 步只是白烧
        一个 10 秒的请求还把服务端状态改脏。require_slot=False 时退回老流程
        （2026-09-14 那版根本没有时段字段），这个开关只为 Apple 回退时留的。
        """
        if self.require_slot:
            raise why
        self.store = self.store_used = store
        self.slot = {}
        self.log(f"[快车道] ⚠️ 没有门店给出取货时段，按 require_pickup_slot=false "
                 f"仍用 {store} 继续：{why}")
        return data

    def take_slot(self, data: dict) -> dict:
        """第 2 步之后挑时段。挑不出来一律抛 Stalled，交给 select_store 换店。

        **拿不到时段就是拿不到这家店的货。** 2026-09-16 起 11 次记录里，有时段的
        2 次 step3 全成功，没时段的 9 次 step3 全部「返回 200 但没有 pickupContact」
        ——一次例外都没有。

        原来这里分两种情况：有 `timeSlotWindows` 却没有可选档位才抛，响应里压根
        没有这个模块就静默放行。而实际发生的恰恰是后者——于是带着空时段撞进一个
        必死的第 3 步，白烧一个 10 秒的请求，还把服务端状态改脏了。
        """
        self.slot = self.choose_slot(data)
        if self.slot:
            self.log(f"[快车道] 取货时段 {slot_label(self.slot)}"
                     f"（{self.pickup_time or 'earliest'}）")
            return self.slot
        if self._walk(data, "timeSlotWindows"):
            raise Stalled(
                f"门店 {self.store} 要选取货时段，但响应里一个可选的档位都没有"
                f"——这家店当下排不上取货")
        raise Stalled(
            f"门店 {self.store} 的第 2 步响应里没有取货时段模块——这家店当下"
            f"拿不到货（继续走第 3 步必定空转，已实测 9/9）")

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

    #: 联系方式：这两个字段**只在 Apple 没预填时才回传**。
    #: 预填过的账号，模型里给的是打码值（test@gmail.com、••••••••••09），
    #: 原样发回去等于把打码字符当成真值提交，校验不过；页面上的规则也一样
    #: （b.<字段>.submit = 「值跟 d.was 不同才提交」），所以照抄它：
    #: 服务端有值就一个字都不发，服务端为空才拿 config 里的填。
    CONTACT_FALLBACK = (
        ("emailAddress", f"{_SELF}.selfContact.address.emailAddress", "pickup_email"),
        ("fullDaytimePhone", f"{_SELF}.selfContact.address.fullDaytimePhone",
         "pickup_phone"),
    )

    @staticmethod
    def _find(node, field: str) -> str | None:
        """从 pickupContact 模型里取某个字段的**当前值**；模型里没有就返回 None。

        模型把当前值放在 `d` 下、上一次的值放在 `was` 下，所以必须只认 `d`，
        否则会把旧值发回去。「没有这个字段」和「有但是空」得分开：前者是这一版
        流程根本不问（不该发），后者才是要拿 config 兜底的那种。
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
        return None

    @classmethod
    def _harvest(cls, node, field: str) -> str:
        return cls._find(node, field) or ""

    def contact_fields(self, contact_model: dict) -> list[tuple[str, str]]:
        """拼第 4 步的请求体。

        姓名：**账号里的名字优先**，config 只在 Apple 没预填时兜底——取货要跟
        证件对得上，账号带出来的那份才是 Apple 认的，不该被配置里的旧值顶掉。
        两边都空时**不要发空姓名**，服务端校验不过、返回 200 却停在原地
        （这正是「200 不等于生效」那个坑的现场），交给 step4 停住报缺。
        身份证后四位账号永远不会预填，必须由 config 提供。

        邮箱/手机是另一种情况：预填过的账号，模型里给的是打码值，只能不发。
        所以模型里有这个字段、而且当前是空的时候，才补上 config 里的
        pickup_email / pickup_phone——见 CONTACT_FALLBACK。
        """
        #: 只能由我们给：身份证后四位账号永远不预填，发票类型是我们选的。
        forced = {
            "nationalIdSelf": self.id_last4,
            "selectFapiao": self.fapiao,
        }
        #: config 里的兜底值，只有账号没预填时才轮得到它。
        spare = {
            "lastName": self.last_name,
            "firstName": self.first_name,
        }
        out = []
        for field, key in self.CONTACT_FIELDS:
            val = (forced.get(field)
                   or self._harvest(contact_model, field)
                   or spare.get(field, ""))
            out.append((key, val))
        fallback = {"emailAddress": self.email, "fullDaytimePhone": self.phone}
        for field, key, _cfg in self.CONTACT_FALLBACK:
            cur = self._find(contact_model, field)
            if cur is None or cur:        # 这一步不问 / Apple 已经预填（可能是打码值）
                continue
            out.append((key, fallback[field]))
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
        # 发出去之前就置位：请求一旦离开这台机器，订单就可能已经建好了，
        # 而响应有没有回来、回来的是什么，都不改变这件事。
        if self.cancelled():
            raise Stalled("购买流程已停止，未提交订单")
        if self.submit_guard is not None:
            self.submit_guard.submitted(store=self.store)
        self.submitted = True
        data = self._post(page, PLACE_PATH, PLACE_ACTION, PLACE_MODULE, [])
        return str(((data.get("head") or {}).get("data") or {}).get("url") or "")

    def follow(self, page, url: str) -> bool:
        """跟着服务端给的「假跳转」真的导航过去。成了返回 True。

        Apple 这套框架的跳转**不是 HTTP 301/302**：HTTP 层永远是 200，
        跳转写在响应体里（`{"head":{"status":302,"data":{"url":"…"}}}`），
        由前端读出来做 `window.location.href = …`。HAR 里那两跳
        （`/shop/checkout/status`、`/shop/checkout/thankyou`）的请求头是
        `Sec-Fetch-Mode: navigate`，是**整页导航**，不是 XHR。

        发包这条路原来把这两跳全省了——省错了：省掉之后每一步都还挂在结账页上，
        请求头里的 `x-aos-model-page` 和 referer 都跟真实浏览器不一样。
        """
        if not url:
            return False
        full = url if url.startswith("http") else self.origin(page) + url
        try:
            page.goto(full, timeout=20000, wait_until="domcontentloaded")
            self.log(f"[快车道] 跟着跳到 {full.split('//')[-1][:60]}")
            return True
        except Exception as e:
            self.log(f"[快车道] 跳转 {full[:50]} 失败（{type(e).__name__}），"
                     f"留在当前页继续问")
            return False

    def show_review(self, page) -> bool:
        """把标签页真的带到 Review 上，别让人自己按 F5（见 REVIEW_PATH）。"""
        return self.follow(page, REVIEW_PATH)

    @classmethod
    def review_url(cls, page) -> str:
        """Review 页的完整地址。会话分在哪台 secureN 上，就得用哪台的。"""
        return cls.origin(page) + REVIEW_PATH

    @staticmethod
    def origin(page) -> str:
        here = str(getattr(page, "url", "") or "")
        m = re.match(r"(https?://[^/]+)", here)
        return m.group(1) if m else ""

    def step8_check_status(self, page, tries: int = 12, delay: float = 1.5,
                           status_url: str = "") -> str:
        """轮询下单结果，返回最终去处的 URL；拿不到结论就返回空串。

        **成功与否看这个 URL，不要看页面文案。** status 页在处理中显示「正在处理」，
        失败时也一样——只有最终跳去哪里能分辨。

        2026-09-18 07:15 那一单（9 轮全「处理中」，而订单确认邮件已经到了）
        教了三件事：

        1. **得先真的跳到 status 页再问。** `checkStatus` 属于 `checkoutStatusPage`，
           在结账页上问它，服务端就一直回「处理中」。浏览器那次是先整页导航到
           `/shop/checkout/status`，第一轮就拿到了 thankyou。
        2. **每一轮拿到什么必须打出来。** 那次只记了耗时，事后没法判断是服务端慢
           还是我们问错了地方。
        3. **服务端不给跳转时，页面自己可能已经到了。** 所以每轮顺手看一眼 page.url。
        """
        self.follow(page, status_url)
        last = ""
        for i in range(max(1, tries)):
            data = self._post(page, STATUS_PATH, STATUS_ACTION, STATUS_MODULE, [],
                              model_page=STATUS_MODEL_PAGE)
            url = str(((data.get("head") or {}).get("data") or {}).get("url") or "")
            self.log(f"[快车道] checkStatus 第 {i + 1} 轮 → {url or '（没给跳转）'}")
            last = url or last
            if url and "/shop/checkout/status" not in url:
                return url
            here = str(getattr(page, "url", "") or "")
            if self.ORDER_OK.search(here.split("?")[0]):
                self.log(f"[快车道] 服务端没给跳转，但页面已经在 {here[:60]}")
                return here
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

    @classmethod
    def order_unknown(cls, url: str) -> bool:
        """这次下单的结果**根本没拿到结论**吗。

        跟「被驳回」必须分开，因为两者该做的事正好相反：被驳回意味着订单没建
        起来、可以重试；结果不明意味着**订单可能已经建好了**，这时候重试就是
        再下一单。

        2026-09-18 07:15：六步 + 提交全部 200，checkStatus 连着 9 轮都是
        `/shop/checkout/status`（处理中），而 Apple 的订单确认邮件**已经到了**。
        按老写法这会被判成「被驳回」，然后退回点页面再下一单。
        """
        if not cls.order_rejected(url):
            return False
        # 只有明确返回结账向导才视为驳回；登录、错误页等都不能证明订单没建。
        return urlparse(url or "").path.rstrip("/") != "/shop/checkout"

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
        options = [(str(obj.get('labelImageAlt') or '').strip(), str(obj.get('value') or ''))
                   for obj in self._walk(data, 'labelImageAlt')]
        exact = {val for alt, val in options if alt == self.payment_label and val}
        if len(exact) == 1:
            return exact.pop()
        partial = {val for alt, val in options if alt and val and
                   (self.payment_label in alt or alt in self.payment_label)}
        return partial.pop() if len(partial) == 1 else ''

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
        return 0

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
        try:
            return self._run(page)
        finally:
            if self.submitted and self.submit_guard is not None:
                # 跳回 checkout 也不能作为无订单的证明；都保留提交记录待核对。
                status = 'confirmed' if not self.order_rejected(self.order_url) else 'unknown'
                try:
                    self.submit_guard.finish(status, self.order_url)
                except OSError as e:
                    self.log(f'[订单记录] 更新失败，保留提交前记录：{e}')

    def _run(self, page) -> tuple[bool, str, str]:
        """执行同源结账；按 place_order 决定是否创建待付款订单。"""
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
                "本次尝试已停止。")
        self.log(f"[快车道] 令牌就位（{len(self.stk)} 字符，等了 "
                 f"{(time.monotonic() - t0) * 1000:.0f}ms），开始六步")

        try:
            self.step1_pickup(page)
            self.select_store(page)
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
            months = 0
            if self.installment_months > 0:
                months = self.find_installment(opts, self.installment_months)
                if not months:
                    return False, '⚠️ 没有所配置的分期期数', (
                        f'服务端没有提供 {self.installment_months} 期，未擅自更改付款条件。')
            elif (self.billing_option.startswith('installments')
                  or self._walk(opts, 'selectInstallmentOption')):
                return False, '⚠️ 付款配置不一致', '所选方式需要分期，请明确配置期数。'
            self.step6_to_review(page, months)

            if self.place_order:
                why = self._may_place()
                if why:
                    self.log(f"[快车道] 不提交订单：{why}")
                else:
                    dest = self.step7_place_order(page)
                    self.log(f"[快车道] 已提交，处理中（{dest or '?'}）")
                    final = self.step8_check_status(page, status_url=dest)
                    self.order_url = final
                    # 成功了就把页面也带过去。发包这条路不跳页面，人打开浏览器
                    # 只会看到还停在结账页的那个标签——而二维码在 thankyou 上。
                    if final and not self.order_rejected(final):
                        self.follow(page, final)
                    if self.order_unknown(final):
                        # **不能说成「被驳回」，也绝对不能让上层重试。**
                        # 提交已经送出去了，订单很可能已经建好（邮件都到了），
                        # 只是这个会话的 status 一直没翻过来。
                        return False, "⚠️ 下单结果不明", (
                            f"六步和提交都是 200，但轮询 {final or '一直停在处理中'}，"
                            f"没拿到最终去处。**订单很可能已经创建**——"
                            f"去邮箱或 https://www.apple.com.cn/shop/order/list 确认，"
                            f"别再让它下一单。要付款的话在那儿扫码，本工具不代付款。")
                    if self.order_rejected(final):
                        return False, "⚠️ 下单被驳回", (
                            f"六步都走通了，提交后被打回结账页（{final}）。"
                            f"最常见的原因是所选取货门店在提交那一刻已不能履约——"
                            f"Apple 的原话是「你所选择的『送货与取货』选项已不再为本订单提供」。"
                            f"请去浏览器里看结账页上的提示。")
                    return True, "✅ 待付款订单已创建", (
                        f"跳转到 {final}。"
                        f"请尽快按订单页面显示的期限扫码支付——**本工具不代付款**。")
        except KeyboardInterrupt:
            # KeyboardInterrupt 是 BaseException，下面的 except Exception 接不住它——
            # 而最该喊一嗓子的恰恰是这一刻。submitted 在发包**之前**就置位，为的
            # 就是这种「请求已经离开本机、进程马上要死」的时候还能说清楚。
            #
            # 2026-09-18 23:50 实测：六步走完 4 秒后按了 Ctrl+C，那台机器每发要
            # 8～10 秒，中断正落在「立即下单」那一发的途中。请求照样到了 Apple，
            # 订单真的建好了，而日志里连「已提交」都没有——人以为没走到那步。
            if self.submitted:
                self.log("[快车道] ⚠️ 中断时「立即下单」那一发**已经送出去了**，"
                         "订单可能已经创建：去 apple.com.cn/shop/order/list 或邮箱"
                         "确认，别急着再下一单。")
            raise
        except SessionExpired as e:
            self.failure_kind = "session_expired"
            return False, "⚠️ 结账会话已过期", (
                f"{e}。**在这条链路上重试没有意义**——要重新登录、"
                f"从购物袋重新走一遍。（Apple 的结账会话 5 分钟没交互就作废。）")
        except Stalled as e:
            self.failure_kind = "stalled"
            return False, "⚠️ 步骤没生效", (
                f"{e}。已经改动过的服务端状态和页面可能不一致，"
                f"本次尝试已停止，不再自动操作页面。")
        except Blocked as e:
            self.failure_kind = "blocked"
            return False, "⚠️ 结账被限流", (
                f"{e}。这是 Akamai 的拦截，不是页面问题——**别重试**，"
                f"越撞退避越深。见 README 坑 9。")
        except Exception as e:
            self.failure_kind = "error"
            return False, f"⚠️ {type(e).__name__}", f"{e}。本次尝试已停止。"

        total = time.monotonic() - t0
        detail = " / ".join(f"{a} {d * 1000:.0f}ms" for a, d in self.timings)
        when = f"取货时段 {slot_label(self.slot)}。" if self.slot else ""
        return True, "已到 Review（未下单）", (
            f"六步走完共 {total:.1f}s（{detail}）。"
            f"{when}付款方式 {self.payment_label} {months} 期。"
            f"**没有提交订单**，下单那一下留给你。")
