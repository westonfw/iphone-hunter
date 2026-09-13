"""在已登录会话里创建待付款订单。

中国大陆到店取货结账向导（以 2026-08 实测为准）：

  Fulfillment-init   选自提 + 门店
  PickupContact-init 填身份证后四位（姓名电话通常已从 Apple ID 带出）
  Billing            选支付宝/微信
  检查订单           进入 Review
  Review             确认下单 → 待付款/扫码

加购必须在预热页点（atbtoken）。结账主机 secureN 是探出来的，不写死。
绝不代付：不填支付密码、不确认 Apple Pay；信用卡路径上不点确认下单。
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .apple import REGIONS

# secureN 这个前缀**不能写死**：具体分到 secure7 还是 secure8 是 Apple 在
# 建会话时决定的，登录一次换一个，不同账号也不一样。
#
# 之前写死 secure8 能跑通，纯粹是因为 Apple 目前会帮你 302 到会话所在的那台——
# 那是运气，不是契约。所以改成按优先级探：会话里已经见过的主机 > 记住的 >
# 不带前缀让 Apple 自己路由 > 最后才是历史上见过的那几台。
CHECKOUT_PATH = "/shop/checkout?_s=Fulfillment-init"

#: 实在探不到时按顺序试的历史主机，只作兜底。
SECURE_FALLBACKS = ("secure8", "secure7", "secure6", "secure9", "secure5")

_SECURE_HOST = re.compile(r"^secure\d+\.www\.apple\.com", re.I)


def secure_host_of(url: str) -> str:
    """从 URL 里取出 secureN 主机名；不是 secureN 就返回空串。"""
    host = urlparse(url or "").netloc
    return host if _SECURE_HOST.match(host) else ""


def checkout_candidates(region: str = "cn", remembered: str = "",
                        seen_urls: list[str] | None = None) -> list[str]:
    """按优先级列出可以尝试的结账地址。"""
    base = REGIONS.get(region) or REGIONS["cn"]
    host = urlparse(base).netloc                     # www.apple.com.cn
    out: list[str] = []

    def add(u: str) -> None:
        if u and u not in out:
            out.append(u)

    # 1) 这个浏览器里已经开着的页面用的是哪台，就用哪台——最准
    for u in (seen_urls or []):
        h = secure_host_of(u)
        if h:
            add(f"https://{h}{CHECKOUT_PATH}")
    # 2) 本进程之前成功过的那台
    if remembered:
        add(f"https://{remembered}{CHECKOUT_PATH}")
    # 3) 不带 secureN 前缀，让 Apple 自己路由到会话所在的那台
    add(f"{base}{CHECKOUT_PATH}")
    # 4) 兜底：历史上见过的几台
    for pre in SECURE_FALLBACKS:
        add(f"https://{pre}.{host}{CHECKOUT_PATH}")
    return out

# 切换到取货的按钮文案。2026-09 实测大陆结账页是「我要取货」/「为我送货」，
# 原来那组「到店取货/门店取货」一个都匹配不上，会静默走成送货——所以把实测
# 文案放在第一位，其余留作改版兜底。
#
# 注意不能用裸「取货」：页面底部有「送货与取货常见问题解答」这个链接，
# 匹配是子串包含，裸词会点到 FAQ 上去。
PICKUP_SWITCH = ("我要取货", "到店取货", "零售店取货", "门店取货", "自提")

# 切到取货之后，门店列表要 ~1.5s 才异步渲染出来（2026-09-13 实测）。
# 这段时间里页面上的「继续」按钮还是送货那栏的「继续填写送货地址」，
# 谁先点它谁就把订单推成了送货——所以必须等确认切过去了再往下走。
PICKUP_READY_MS = 20000

# 每一步等控件就绪的上限。翻页时按钮会先消失再重建，转圈久一点就要十几秒，
# 原来按 timeout_ms//4 算出来只有 7.5s，经常刚好差一点。
STEP_WAIT_MS = 15000

# Apple 的结账页是手风琴式的：走到下一步之后，前面步骤的 DOM **不会被移除**，
# 只是折叠起来。所以一切判断都必须看「可见」，不能看「存在」——否则翻页之后
# 还会以为自己停在配送步骤，跑去点下一步的按钮。
# 只认分段控件本身，绝不满页找「我要取货」：进到下一步之后，摘要区里也有
# 这四个字，用通用文本匹配会点中那儿的返回链接，把流程一路弹回 Fulfillment。
JS_PICKUP_STATE = r"""
() => {
    const vis = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && el.offsetParent !== null;
    };
    const seg = [...document.querySelectorAll(".rc-segmented-control-button")].filter(vis);
    const btn = seg.find(b => (b.textContent || "").includes("我要取货"));
    // 门店列表渲染出来的标志：出现了带门店的、可见的取货单选项
    const listed = [...document.querySelectorAll("input[type=radio]")].some(i => {
        const lab = i.id ? document.querySelector(`label[for="${CSS.escape(i.id)}"]`) : null;
        return lab && vis(lab) && /店内取货|可取货/.test(lab.textContent || "");
    });
    return {hasSeg: seg.length > 0, on: !!btn && btn.className.includes("selected"), listed};
}
"""

JS_SEG_CLICK = r"""
(want) => {
    const vis = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && el.offsetParent !== null;
    };
    for (const b of document.querySelectorAll(".rc-segmented-control-button")) {
        if (vis(b) && (b.textContent || "").includes(want)) { b.click(); return true; }
    }
    return false;
}
"""

# 门店条目是 <label for="_r_p_">，而且它的 innerText 是空的、只有 textContent
# 有内容——所以通用的 click_text（读 innerText）一个都匹配不上。
JS_PICK_STORE = r"""
(wants) => {
    const rows = [];
    const vis = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && el.offsetParent !== null;
    };
    for (const inp of document.querySelectorAll("input[type=radio]")) {
        if (!inp.id) continue;
        const lab = document.querySelector(`label[for="${CSS.escape(inp.id)}"]`);
        if (!lab || !vis(lab)) continue;
        const t = (lab.textContent || "").replace(/\s+/g, " ").trim();
        if (!/店内取货|可取货/.test(t)) continue;
        rows.push({inp, lab, t, ok: !/目前不可取货|暂不可取货/.test(t)});
    }
    if (!rows.length) return {clicked: "", seen: []};
    for (const want of wants) {
        for (const r of rows) {
            if (!r.t.includes(want)) continue;
            if (!r.ok) return {clicked: "", seen: rows.map(x => x.t.slice(0, 40)),
                               skipped: want};
            // 已经选中就别再点：重复点会白白触发一次重渲染，把「继续」按钮
            // 从 DOM 里换掉，接下来那一下就点了个空。
            if (r.inp.checked) return {clicked: want, already: true};
            r.lab.click();
            return {clicked: want, already: false};
        }
    }
    return {clicked: "", seen: rows.map(x => x.t.slice(0, 40))};
}
"""

# 「分期付款方案」那一组：花呗分期 / 微信分付 / 各家银行分期。
# 页面上这组底下写着「通过支付宝选择更多银行」——它们都走支付宝通道，
# 点「现在下单」只是创建待付款订单，二维码留给用户扫，不会即时扣款。
#
# 注意它跟「其他支付方式 → 信用卡 (Visa, Mastercard)」是**两回事**：
# 后者才是即时扣款，仍然留在 CARD_PAY 里拦着。
# 名字取自页面上 <img alt>。注意工行的 alt 是「工商银行」不是「中国工商银行」，
# 所以匹配是双向包含，两种写法都认。
BANK_INSTALLMENT = ("花呗分期", "微信分付", "招商银行", "中国建设银行",
                    "工商银行", "中国工商银行", "掌上生活")

SCAN_PAY = ("支付宝", "微信", "花呗", "微信支付") + BANK_INSTALLMENT
CARD_PAY = ("信用卡", "借记卡", "Visa", "Mastercard", "American Express", "银联卡")

BAG_CHECKOUT_TRIES = (
    "/shop/bagx?_a=checkout&_m=shoppingCart.actions",
    "/shop/bagx?_a=checkout_now&_m=shoppingCart.actions",
    "/shop/bagx/checkout_now",
    "/shop/checkout/start",
)

CONTINUE_BTN = "rs-checkout-continue-button-bottom"

ID_NATIONAL = "checkout.pickupContact.selfPickupContact.nationalIdSelf.nationalIdSelf"
ID_LAST_NAME = "checkout.pickupContact.selfPickupContact.selfContact.address.lastName"
ID_FIRST_NAME = "checkout.pickupContact.selfPickupContact.selfContact.address.firstName"
ID_EMAIL = "checkout.pickupContact.selfPickupContact.selfContact.address.emailAddress"
ID_PHONE = "checkout.pickupContact.selfPickupContact.selfContact.address.fullDaytimePhone"
# Apple 登录页（idmsa.apple.com 的 iframe）上的控件。分两步：
# 先填账号点继续，密码框才出现。
ID_ACCOUNT = "account_name_text_field"
ID_PWD = "password_text_field"
BTN_SIGN_IN = "sign-in"

PAY_IDS = {
    "支付宝": (
        "checkout.billing.billingoptions.alipay_label",
        "checkout.billing.billingoptions.alipay",
    ),
    "微信": (
        "checkout.billing.billingoptions.wechat_label",
        "checkout.billing.billingoptions.wechat",
    ),
    "微信支付": (
        "checkout.billing.billingoptions.wechat_label",
        "checkout.billing.billingoptions.wechat",
    ),
    "花呗": (
        "checkout.billing.billingoptions.huabei_label",
        "checkout.billing.billingoptions.huabei",
    ),
}

JS_AOS_POST = r"""
async ([paths]) => {
    const findStk = () => {
        const meta = document.querySelector('meta[name="x-aos-stk"]');
        if (meta && meta.content) return meta.content;
        const inp = document.querySelector('input[name="x-aos-stk"]');
        if (inp && inp.value) return inp.value;
        const html = document.documentElement.innerHTML;
        const m = html.match(/"xAosStk"\s*:\s*"([^"]+)"/)
               || html.match(/x-aos-stk["']?\s*[:=]\s*["']([^"']+)/i);
        return m ? m[1] : "";
    };
    const stk = findStk();
    const origin = location.origin;
    const tried = [];
    for (const path of paths) {
        const url = path.startsWith("http") ? path : origin + path;
        try {
            const res = await fetch(url, {
                method: "POST",
                credentials: "include",
                headers: {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Requested-With": "Fetch",
                    "syntax": "graviton",
                    "modelVersion": "v2",
                    ...(stk ? {"x-aos-stk": stk} : {}),
                },
                body: "",
            });
            const text = await res.text();
            let json = null;
            try { json = JSON.parse(text); } catch (e) { json = null; }
            const loc = (res.headers && res.headers.get && res.headers.get("location")) || "";
            const next = (json && json.head && json.head.data && (json.head.data.url || json.head.data.location))
                      || (json && json.body && json.body.url)
                      || loc;
            tried.push({path, status: res.status, next: next || ""});
            if (next && /checkout|thank|order/i.test(String(next))) {
                const abs = String(next).startsWith("http") ? String(next)
                          : new URL(String(next), origin).href;
                return {ok: true, goto: abs, stk: !!stk, tried};
            }
            if (res.ok && json) {
                return {ok: true, goto: "", stk: !!stk, tried, jsonHint: Object.keys(json).slice(0, 6)};
            }
        } catch (e) {
            tried.push({path, error: String(e).slice(0, 80)});
        }
    }
    return {ok: false, goto: "", stk: !!stk, tried};
}
"""

JS_SNAPSHOT = r"""
() => {
    const body = document.body ? (document.body.innerText || "") : "";
    const url = location.href;
    const compact = body.replace(/\s+/g, " ").slice(0, 500);
    const qs = new URLSearchParams(location.search);
    const step = qs.get("_s") || "";
    const order = (body.match(/订单\s*#?\s*(W\d{8,})/) || [])[1]
               || (body.match(/订单(?:编号|号)\s*[:：#]?\s*([A-Z0-9]{8,})/) || [])[1]
               || "";
    // 这里只放**下单之后才可能出现**的字样，宁可漏报也不能误报。踩过两次坑：
    //   「掌上生活」——招商银行 App 的名字，选付款方式时就印在 Billing 页上；
    //   裸「待付款」——Review 页那句「取货日期待付款完成后确定」里就有这三个字。
    const unpaid = /等待付款|请在.{0,20}内完成付款|扫描二维码支付|订单已提交|感谢你的订购/
        .test(body);
    const thank = /thankyou|thank-you|\/shop\/checkout\/thankyou|\/shop\/order\//i.test(url);
    const signIn = /\/signin|idmsa\.apple\.com/i.test(url);
    const idEl = document.getElementById("checkout.pickupContact.selfPickupContact.nationalIdSelf.nationalIdSelf");
    const cont = document.getElementById("rs-checkout-continue-button-bottom");
    const contText = cont ? (cont.innerText || cont.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim() : "";
    return {
        url, step, order, unpaid, thank, signIn, compact,
        hasIdField: !!(idEl),
        idFilled: !!(idEl && String(idEl.value || "").trim()),
        hasPickup: /到店取货|零售店取货|门店取货|自提/.test(body),
        scanPay: /支付宝|微信支付|花呗/.test(body),
        cardPay: /信用卡|借记卡/.test(body),
        hasCheckOrder: /检查订单/.test(body) || /检查订单/.test(contText),
        hasPlace: /现在下单|确认下单/.test(body) || /现在下单|确认下单/.test(contText),
        continueEnabled: cont ? !cont.disabled : null,
        continueText: contText.slice(0, 24),
    };
}
"""

#: 文案匹配到的元素先打上这个属性，再用 Playwright 按属性选择器真点。
#: 这样文案匹配也能拿到「等元素稳定 + 完整指针事件」，而且天然支持 iframe。
HIT_ATTR = "data-hunter-hit"

JS_MARK_TEXT = r"""
([needles, attr]) => {
    const blob = (el) => ((el.innerText || "") + " " +
        (el.getAttribute("aria-label") || "")).replace(/\s+/g, " ").trim();
    for (const old of document.querySelectorAll("[" + attr + "]")) {
        old.removeAttribute(attr);
    }
    const nodes = document.querySelectorAll(
        "button, a, label, input, [role=button], [role=radio], [role=tab], [data-autom]");
    for (const el of nodes) {
        const t = blob(el);
        if (!t) continue;
        if (needles.some((n) => t.includes(n))) {
            el.setAttribute(attr, "1");
            return t.slice(0, 48);
        }
    }
    return "";
}
"""

JS_UNMARK = """
(attr) => {
    for (const el of document.querySelectorAll("[" + attr + "]")) el.removeAttribute(attr);
    return true;
}
"""


#: 等不到「完全安静」时，过了这个比例的预算就退而求其次。
SETTLE_RELAX_AT = 0.5
#: 退而求其次的安静门槛（毫秒）。
SETTLE_RELAXED_QUIET = 250


def wait_settled(page, quiet_ms: int = 600, max_ms: int = 5000, log=None) -> bool:
    """等页面安静下来：DOM 不再变动、没有可见的转圈、readyState 完成。

    以前是 wait_for_timeout(600) 一把梭。重渲染要多久取决于网速和 Apple 的
    接口延迟，写死常数必然有时候不够——页面还在重建时点下去，DOM 上 radio
    会变成 checked，应用层却完全没收到，提交时按没选处理。

    但也不能死等：Apple 的页面有后台埋点、倒计时之类的持续小改动，DOM 永远
    不会连续安静 600ms。所以过了一半预算就放宽标准——只要不转圈、readyState
    完成、DOM 有 250ms 没大动，就认为可以动手了。真等满预算才是最坏情况。
    """
    import time as _t
    try:
        page.evaluate(JS_SETTLE_WATCH, quiet_ms)
    except Exception:
        page.wait_for_timeout(quiet_ms)
        return False

    started = _t.monotonic()
    deadline = started + max_ms / 1000
    relax_at = started + max_ms / 1000 * SETTLE_RELAX_AT
    ok, st = False, {}
    while _t.monotonic() < deadline:
        page.wait_for_timeout(150)
        try:
            st = page.evaluate(JS_SETTLE_CHECK)
        except Exception:
            break
        if st.get("ready") != "complete" or st.get("busy"):
            continue
        need = (quiet_ms if _t.monotonic() < relax_at else SETTLE_RELAXED_QUIET)
        if st.get("quiet", 0) >= need:
            ok = True
            break
    try:
        page.evaluate(JS_SETTLE_STOP)
    except Exception:
        pass
    if not ok and log:
        # 说清楚卡在哪个条件上，否则这条日志等于没说
        why = (f"还在转圈（{st['busy']}）" if st.get("busy")
               else f"readyState={st.get('ready')}" if st.get("ready") != "complete"
               else f"DOM 一直在变（最长静默 {st.get('quiet', 0)}ms）")
        log(f"[下单] 页面 {max_ms / 1000:.0f}s 内没安静下来：{why}，继续往下走")
    return ok


# React 受控输入框：直接赋 .value 不会触发 onChange，得用原型上的 setter
# 再手动派发 input/change 事件。这是 fill() 走不通时的退路。
JS_FILL = r"""
([id, value]) => {
    const el = document.getElementById(id);
    if (!el) return {ok: false, why: "没有这个 id"};
    if (el.disabled || el.readOnly) return {ok: false, why: "只读或已禁用"};
    const proto = Object.getOwnPropertyDescriptor(
        el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
                                          : HTMLInputElement.prototype, "value");
    if (proto && proto.set) proto.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
    return {ok: el.value === value, got: el.value || ""};
}
"""


# ---------- iframe ----------
#
# Playwright 的 locator 不穿透 iframe，evaluate 也只在自己那个文档里跑。
# Apple 的支付/验证环节有些控件在 iframe 里，所以定位之前得先找对 frame。
# 主文档命中就直接用（绝大多数情况），只有找不到才去逐个 iframe 里找。

JS_HAS_ID = "(id) => !!document.getElementById(id)"


def all_frames(page) -> list:
    """这个页面里的所有 frame（含主文档）。传进来的若不是 Page 就原样返回。"""
    frames = getattr(page, "frames", None)
    if frames:
        return list(frames)
    owner = getattr(page, "page", None)          # Frame.page → Page
    if owner is not None:
        frames = getattr(owner, "frames", None)
        if frames:
            return list(frames)
    return [page]


def eval_in_frames(page, js, arg=None):
    """在主文档和各 iframe 里依次执行 js，返回第一个「有结果」的 (frame, result)。

    主文档优先。跨域或已销毁的 frame 抛异常就跳过。都没结果时返回 (None, None)。
    """
    for fr in [page] + [f for f in all_frames(page) if f is not page]:
        try:
            r = fr.evaluate(js, arg) if arg is not None else fr.evaluate(js)
        except Exception:
            continue
        if r:
            return fr, r
    return None, None


def frame_for_id(page, elem_id: str, timeout_ms: int = 4000, log=None):
    """找到真正含这个 id 的 frame。找不到返回 None。

    先探主文档——常见情况一次调用就命中，不为了 iframe 支持给主流程加开销。
    """
    import time as _t
    deadline = _t.monotonic() + timeout_ms / 1000
    while True:
        try:
            if page.evaluate(JS_HAS_ID, elem_id):
                return page
        except Exception:
            pass
        for fr in all_frames(page):
            if fr is page:
                continue
            try:
                if fr.evaluate(JS_HAS_ID, elem_id):
                    if log:
                        log(f"[定位] {elem_id} 在 iframe 里：{getattr(fr, 'url', '')[:60]}")
                    return fr
            except Exception:
                continue        # 跨域或已销毁的 frame，跳过
        if _t.monotonic() >= deadline:
            return None
        page.wait_for_timeout(200)


def frame_for_selector(page, selector: str, timeout_ms: int = 4000, log=None):
    """找到能匹配这个选择器的 frame。找不到返回 None。"""
    import time as _t
    deadline = _t.monotonic() + timeout_ms / 1000
    while True:
        for fr in [page] + [f for f in all_frames(page) if f is not page]:
            try:
                if fr.locator(selector).count():
                    if fr is not page and log:
                        log(f"[定位] {selector[:40]} 在 iframe 里")
                    return fr
            except Exception:
                continue
        if _t.monotonic() >= deadline:
            return None
        page.wait_for_timeout(200)


def read_field(page, elem_id: str, frame=None) -> str | None:
    """读某个输入框当前的值。None = 页面上没这个 id。

    frame 给了就在那个 frame 里读，省掉重新解析（fill_field 会这么用）。
    """
    for target in ([frame] if frame is not None else [page]):
        try:
            return target.evaluate(
                """(id) => { const el = document.getElementById(id);
                             return el ? String(el.value == null ? "" : el.value) : null; }""",
                elem_id)
        except Exception:
            return None
    return None


def fill_field(page, elem_id: str, value: str, timeout_ms: int = 8000, log=None,
               secret: bool = False) -> bool:
    """按 id 找到输入框，把 value 填进去，并复核确实填上了。

    先用 Playwright 的 fill()：它会等元素可见、可编辑，聚焦、清空，再派发
    完整的输入事件。**不要图省事直接在 JS 里赋 .value**——React 受控组件
    不认这种赋值，DOM 上看着有值、应用层完全没收到，提交时按空处理。
    （跟选付款方式那边「DOM 是 checked、页面却说没选」是同一类坑。）

    fill() 不行时退回 JS 原型 setter + 手动派发事件，最后再复核一次。

    secret=True 时日志里绝不出现值本身（密码框必须开）。默认关着的话，
    「填了但读回来不一样」这条日志会把字段当前内容打出来——对密码框就是
    把密码写进日志和终端记录里。

    注意选择器用 [id="..."] 而不是 #id：Apple 的 id 带点
    （checkout.pickupContact.selfPickupContact...），写成 #id 会被 CSS
    当成类选择器解析，永远匹配不到。
    """
    if value is None:
        return False
    value = str(value)
    sel = css_id(elem_id)

    # 元素可能在 iframe 里：locator 和 evaluate 都不穿透，得先找对 frame
    target = frame_for_id(page, elem_id, timeout_ms=min(timeout_ms, 4000), log=log)
    if target is None:
        if log:
            log(f"[填表] 页面上找不到 {elem_id}（含 iframe 都找过了）")
        return False

    try:
        target.locator(sel).first.fill(value, timeout=timeout_ms)
        if read_field(target, elem_id, frame=target) == value:
            return True
        if log:
            log(f"[填表] {elem_id} fill() 之后值不对，改用事件注入")
    except Exception as e:
        if log:
            log(f"[填表] {elem_id} fill() 失败（{type(e).__name__}），改用事件注入")

    try:
        r = target.evaluate(JS_FILL, [elem_id, value]) or {}
    except Exception as e:
        if log:
            log(f"[填表] {elem_id} 注入也失败：{str(e)[:60]}")
        return False

    if not r.get("ok"):
        if log:
            why = r.get("why")
            if not why:
                why = ("值对不上" if secret
                       else f"读回来是 {r.get('got')!r}")
            log(f"[填表] {elem_id} 没填进去：{why}")
        return False
    return read_field(target, elem_id, frame=target) == value


def css_id(elem_id: str) -> str:
    """按 id 选元素的选择器。

    用 [id="..."] 而不是 #id：Apple 的 id 带点
    （checkout.billing.billingoptions.credit），#id 会被 CSS 当成类选择器，
    永远匹配不到。
    """
    return f'[id="{elem_id}"]'


def css_button(text: str, elem_id: str = "") -> str:
    """按文案（可选再加 id）选一个可见按钮。

    带上文案的好处是**定位在点击那一刻才解析**：如果页面已经翻页、按钮
    换成了下一步的，这个选择器就匹配不到，click 直接失败而不是误点。
    Apple 各步骤的继续按钮共用同一个 id，只按 id 点挡不住这种误点。
    """
    safe = text.replace('"', '\\"')
    head = f'button{css_id(elem_id)}' if elem_id else "button"
    return f'{head}:visible:has-text("{safe}")'


#: Playwright 报「谁挡住了」时长这样：
#:   <div class="rf-flyout">…</div> intercepts pointer events
#: 注意 intercepts 前面通常是**闭合**标签，所以要往回找最后一个开标签。
_OPEN_TAG = re.compile(r"<([a-zA-Z][\w-]*)((?:\s+[^<>]*)?)>")


def _interceptor(msg: str) -> str:
    """从 Playwright 的报错里抠出「是谁挡住了」，用于日志。"""
    head, sep, _ = (msg or "").partition("intercepts pointer events")
    if not sep:
        return ""
    tags = _OPEN_TAG.findall(head)
    if not tags:
        return ""
    name, attrs = tags[-1]
    cls = re.search(r'class="([^"]*)"', attrs or "")
    first = (cls.group(1).split() or [""])[0] if cls else ""
    return f"<{name}.{first}>" if first else f"<{name}>"


def click_button(page, selector: str, timeout_ms: int = 8000,
                 settle: bool = False, log=None) -> str:
    """点一个按钮/可点元素。返回点中的文案，点不到返回空串。

    跟 fill_field 一套思路，而且是同一个教训换来的：

      1. 需要的话先等页面安静（settle=True）——重渲染途中点下去，DOM 变了
         应用层没变，表现就是「我以为点了、系统认为没点」。
      2. 用 Playwright 的 click：它会等元素可见、可点、位置不再移动，再派发
         完整的指针事件序列。JS 的 el.click() 这些一样都没有。
      3. 被挡住时退回 force=True，最后才是 JS 硬点兜底。

    选择器交给调用方，配 css_id() / css_button() 用。
    """
    if settle:
        wait_settled(page, log=log)
    # 按钮也可能在 iframe 里
    target = frame_for_selector(page, selector, timeout_ms=min(timeout_ms, 4000), log=log)
    if target is None:
        if log:
            log(f"[点击] 页面上找不到 {selector[:50]}（含 iframe 都找过了）")
        return ""
    try:
        loc = target.locator(selector).first
    except Exception:
        return ""

    label = ""
    try:
        label = (loc.inner_text(timeout=1500) or "").replace("\n", " ").strip()[:24]
    except Exception:
        pass

    blocked_by = ""
    for force in (False, True):
        try:
            loc.click(timeout=timeout_ms, force=force)
            if force and log:
                # force 会绕开可交互性检查，把事件直接派给被遮住的元素。
                # 有时确实能用，但遮罩还在时行为不可预期——所以要说出来。
                log(f"[点击] {selector[:40]} 被{blocked_by or '某个元素'}挡着，"
                    "用了强制点击，未必生效")
            return label or selector[:24]
        except Exception as e:
            msg = str(e)
            if "intercepts pointer events" in msg:
                blocked_by = _interceptor(msg)
            continue

    # 最后退路：JS 硬点。不可靠（就是它坑了我们好几轮），但比什么都不做强。
    # 注意 :visible / :has-text 是 Playwright 的扩展语法，querySelector 不认，
    # 这种选择器走到这里必然失败——那正好，说明该让上层重试。
    try:
        ok = bool(target.evaluate(
            """(sel) => { const el = document.querySelector(sel);
                          if (!el) return false; el.click(); return true; }""",
            selector))
    except Exception:
        ok = False
    if log:
        log(f"[点击] {selector[:40]} " +
            ("走了 JS 兜底，可能不生效" if ok else "点不动（可见性/遮挡/禁用都试过了）"))
    return (label or selector[:24]) if ok else ""


def click_label(page, elem_id: str, timeout_ms: int = 8000) -> bool:
    """点某个 radio 的 label。"""
    return bool(click_button(page, f'label[for="{elem_id}"]', timeout_ms))


def is_sign_in(url: str) -> bool:
    """Apple 未登录时结账会被重定向到 signIn 页 / idmsa。"""
    u = (url or "").lower()
    return "/signin" in u or "idmsa.apple.com" in u


def on_checkout(page, timeout_ms: int = 9000) -> bool:
    """这个页面是不是真的进到结账向导了。**要等它渲染完再判。**

    向导是 React 渲染的，domcontentloaded 那一刻按钮还不存在。不等就判，
    每个候选地址都会被误判成失败，然后一个接一个跳过去——真正对的那台
    也被跳掉了。

    但被重定向到登录页时要**立刻**返回：那不是「这台主机不对」，而是
    「还没登录」，再等下去、再换下一台都没有意义。
    """
    import time as _t
    deadline = _t.monotonic() + timeout_ms / 1000
    while _t.monotonic() < deadline:
        try:
            url = page.url or ""
            if is_sign_in(url):
                return False
            if "/shop/checkout" in url and \
                    step_from_button(find_continue(page).get("text", "")):
                return True
        except Exception:
            return False        # 页面/浏览器没了，别再等
        page.wait_for_timeout(300)
    return False


def step_key(url: str) -> str:
    raw = (parse_qs(urlparse(url or "").query).get("_s") or [""])[0]
    raw = unquote(raw)
    return raw.split("-")[0].lower() if raw else ""


# 「当前在哪一步」以可见的那个继续按钮的文案为准，URL 里的 _s= 只当兜底。
#
# 两个原因：URL 更新滞后于 DOM；而且结账页是手风琴式的，已完成的步骤仍然
# 留在屏幕上——分段控件和下一步的继续按钮会同时可见，光看「元素在不在、
# 可不可见」分不出当前是哪一步，只有那个唯一的继续按钮能分。
STEP_BY_BUTTON = (
    ("继续填写取货详情", "fulfillment"),
    ("继续填写送货地址", "fulfillment"),
    ("继续选择付款方式", "pickupcontact"),
    ("检查订单", "billing"),
    ("立即下单", "review"),   # 2026-09 实测：Review 页就是这四个字
    ("现在下单", "review"),
    ("确认下单", "review"),
)


def step_from_button(text: str) -> str:
    for needle, key in STEP_BY_BUTTON:
        if needle in (text or ""):
            return key
    return ""


def snapshot(page) -> dict:
    try:
        s = page.evaluate(JS_SNAPSHOT) or {}
    except Exception as e:
        s = {"error": str(e)[:80], "url": getattr(page, "url", "")}
    from_url = step_key(s.get("url") or getattr(page, "url", ""))
    btn = find_continue(page).get("text", "")
    s["continueVisible"] = btn
    s["keyFromUrl"] = from_url
    s["key"] = step_from_button(btn) or from_url
    return s


def click_id(page, elem_id: str, timeout_ms: int = 8000,
             settle: bool = False, log=None) -> str:
    """按 id 点。参数跟 click_button 一一对应，只是省掉自己拼选择器。"""
    return click_button(page, css_id(elem_id), timeout_ms, settle, log)


def click_text(page, *needles: str, timeout_ms: int = 8000, log=None) -> str:
    """按文案点一个元素（跨 iframe）。返回点中的文案，没点到返回空串。

    先用 JS 在各 frame 里找到目标并打上 data-hunter-hit 属性，再用 Playwright
    按这个属性真点。绕这一圈是为了两件事：JS 的 el.click() 不等元素稳定、
    React 那边经常接不住；而且 evaluate 不穿透 iframe。打标记之后两个问题
    一起解决——点完就把属性清掉。
    """
    frame, text = eval_in_frames(page, JS_MARK_TEXT, [list(needles), HIT_ATTR])
    if not text:
        return ""
    try:
        hit = click_button(frame, f"[{HIT_ATTR}]", timeout_ms=timeout_ms, log=log)
    finally:
        try:
            frame.evaluate(JS_UNMARK, HIT_ATTR)
        except Exception:
            pass
    return text if hit else ""


# 只认渲染出来的那个按钮：重渲染期间 innerText 为空、尺寸为 0，
# 这时候按 id 点会点在一个正在被替换的节点上，等于没点。
JS_CONTINUE = r"""
() => {
    for (const b of document.querySelectorAll("button")) {
        const t = (b.innerText || "").replace(/\s+/g, " ").trim();
        if (!/继续|检查订单|立即下单|现在下单|确认下单/.test(t)) continue;
        const r = b.getBoundingClientRect();
        if (!r.width || !r.height || b.offsetParent === null || b.disabled) continue;
        return {text: t.slice(0, 24), id: b.id || ""};
    }
    return null;     // 返回 null 而不是空对象：eval_in_frames 靠真假值判断有没有命中
}
"""


def find_continue(page) -> dict:
    """找当前这一步那个可见的「继续」按钮，返回 {text, id, frame}。只找不点。

    跨 iframe 找：Apple 有些环节的按钮在内嵌 frame 里，只看主文档会漏。
    """
    frame, r = eval_in_frames(page, JS_CONTINUE)
    if not r or not r.get("text"):
        return {"text": "", "id": "", "frame": page}
    return {"text": r.get("text", ""), "id": r.get("id", ""), "frame": frame}


# 付款选项的 label 里没有文字——银行名是**图片**，只能靠 <img alt> 认。
# 而 radio 的 id 是 installments0001321713 这种随机数字，不能写死。
JS_PICK_PAYMENT = r"""
(wants) => {
    const opts = [];
    const vis = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && el.offsetParent !== null;
    };
    for (const inp of document.querySelectorAll("input[type=radio]")) {
        if (!inp.id || !inp.id.includes("billingoptions")) continue;
        const lab = document.querySelector(`label[for="${CSS.escape(inp.id)}"]`);
        if (!lab || !vis(lab)) continue;
        const names = [(lab.textContent || "").replace(/\s+/g, " ").trim()];
        for (const img of lab.querySelectorAll("img")) {
            if (img.alt) names.push(img.alt.trim());
        }
        opts.push({inp, lab, names: names.filter(Boolean),
                   isCard: inp.id.endsWith(".credit")});
    }
    const seen = opts.map(o => o.names.join("/") || "(无名)");
    for (const want of wants) {
        for (const o of opts) {
            // 双向包含：配「中国工商银行」也要能匹配 alt 里的「工商银行」
            const hit = o.names.some(n =>
                n.length >= 2 && (n.includes(want) || want.includes(n)));
            if (!hit) continue;
            if (o.isCard) return {clicked: "", seen, refusedCard: want};
            // 只找不点：真正的点击交给 Playwright，它会等元素稳定
            return {clicked: want, id: o.inp.id, already: !!o.inp.checked, seen};
        }
    }
    return {clicked: "", seen};
}
"""


# 期数是每家银行各一组 radio，只有选中那家的那组尺寸非 0，其余都折叠成 0x0。
# 所以「属于展开的那家银行」用 input 自身有没有尺寸来判断（label 是 0x0，别用它）。
# 文案形如「24 期0% 年化利率RMB 284/月总计 RMB 6,799」。
JS_PICK_TERM = r"""
(months) => {
    const rows = [];
    for (const inp of document.querySelectorAll("input[type=radio]")) {
        if (!inp.id || inp.id.includes("billingoptions")) continue;
        const r = inp.getBoundingClientRect();
        if (!r.width || !r.height) continue;
        const lab = document.querySelector(`label[for="${CSS.escape(inp.id)}"]`);
        if (!lab) continue;
        const t = (lab.textContent || "").replace(/\s+/g, " ").trim();
        if (!/^\d+\s*期/.test(t)) continue;   // 门店那些 label 不是这个形状
        rows.push({inp, lab, t});
    }
    const seen = rows.map(r => r.t.slice(0, 14));
    if (!rows.length) return {clicked: "", seen, none: true};
    const want = new RegExp("^" + months + "\\s*期");
    for (const r of rows) {
        if (!want.test(r.t)) continue;
        return {clicked: r.t.slice(0, 24), id: r.inp.id,
                already: !!r.inp.checked, seen};
    }
    return {clicked: "", seen};
}
"""


# 页面「稳没稳」：DOM 不再变动、没有转圈、readyState 完成。
# 这比 wait_for_timeout(600) 靠谱——重渲染要多久是网速决定的，不是常数。
JS_SETTLE_WATCH = """
(quietMs) => {
    if (window.__hunterSettle) window.__hunterSettle.stop();
    const s = {last: Date.now(), stopped: false};
    const ob = new MutationObserver(() => { s.last = Date.now(); });
    ob.observe(document.body, {childList: true, subtree: true,
                               attributes: true, characterData: true});
    s.stop = () => { ob.disconnect(); s.stopped = true; };
    window.__hunterSettle = s;
    return true;
}
"""

JS_SETTLE_CHECK = """
() => {
    const s = window.__hunterSettle;
    // 只算**看得见**的转圈。Apple 页面里常驻着隐藏的 spinner/loading 节点，
    // 不判可见性的话 busy 恒为真，这个函数就永远等到超时为止。
    let busy = "";
    for (const el of document.querySelectorAll(
            '[aria-busy="true"], [class*="spinner" i], [class*="loading" i]')) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && el.offsetParent !== null) {
            busy = String(el.className || el.tagName || "?").slice(0, 40);
            break;
        }
    }
    return {quiet: s ? Date.now() - s.last : 0, busy,
            ready: document.readyState};
}
"""

JS_SETTLE_STOP = """
() => { if (window.__hunterSettle) window.__hunterSettle.stop(); return true; }
"""

JS_IS_CHECKED = """
(id) => {
    const el = document.getElementById(id);
    return el ? !!el.checked : null;   // null = 元素被重渲染换掉了
}
"""

# Apple 的校验提示：付款方式/期数没真正提交上去时，点「检查订单」页面不动，
# 只在上方冒一条提示。它是判断「我以为点了、系统认为没点」的最直接依据。
BILLING_COMPLAINTS = (
    "请从以下支付方式中选择",
    "请选择一种",
    "请选择付款方式",
    "请选择分期",
)

JS_FORM_ERROR = r"""
(pats) => {
    const seen = [];
    for (const el of document.querySelectorAll(
            "[role=alert], [aria-live=assertive], [aria-live=polite]")) {
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height || el.offsetParent === null) continue;
        const t = (el.innerText || "").replace(/\s+/g, " ").trim();
        if (t && t.length < 200) seen.push(t);
    }
    for (const t of seen) {
        if (pats.some(p => t.includes(p))) return t;
    }
    // 有些提示不带 role=alert，退回全文里找已知句式
    const body = document.body.innerText || "";
    for (const p of pats) {
        const i = body.indexOf(p);
        if (i >= 0) return body.slice(i, i + 60).split("\n")[0].trim();
    }
    return "";
}
"""


# 同组里随便找另一个选项，用来「先选别的、再选回来」。
# radio 已经是 checked 时再点它，浏览器不会派发 change 事件——应用层收不到
# 任何信号，于是 DOM 显示已选、提交时却按没选处理。弹开一次再选回来才有
# 真正的状态变化。
JS_SIBLING_OPTION = r"""
([wantId, kind]) => {
    const vis = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    for (const inp of document.querySelectorAll("input[type=radio]")) {
        if (!inp.id || inp.id === wantId) continue;
        const isPay = inp.id.includes("billingoptions");
        if (kind === "pay" && (!isPay || inp.id.endsWith(".credit"))) continue;
        if (kind === "term" && isPay) continue;
        const lab = document.querySelector(`label[for="${CSS.escape(inp.id)}"]`);
        if (!lab || !vis(inp)) continue;
        if (kind === "term" && !/^\d+\s*期/.test(
                (lab.textContent || "").replace(/\s+/g, " ").trim())) continue;
        return inp.id;
    }
    return "";
}
"""


JS_FIELD_READY = r"""
(elemId) => {
    const el = document.getElementById(elemId);
    if (!el) return "missing";
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || el.offsetParent === null) return "hidden";
    return el.value ? "filled" : "empty";
}
"""


def wait_for_field(page, elem_id: str, timeout_ms: int = 6000) -> str:
    """等某个输入框渲染出来。返回 missing / hidden / empty / filled。

    这一步的字段是异步渲染的：翻页之后立刻读，会读到「没有这个字段」，
    于是该填的没填、直接点了继续，校验不过又不报错，循环就卡死在这一步。
    """
    import time as _t
    deadline = _t.monotonic() + timeout_ms / 1000
    state = "missing"
    while _t.monotonic() < deadline:
        try:
            state = page.evaluate(JS_FIELD_READY, elem_id)
        except Exception:
            state = "missing"
        if state in ("empty", "filled"):
            return state
        page.wait_for_timeout(200)
    return state


#: continue_when_ready 的返回值：等按钮的这几秒里页面自己翻页了。
#: 不是错误——只是这一步已经不归当前处理函数管了，交回外层重新判断。
MOVED_ON = "__moved_on__"


def continue_when_ready(page, timeout_ms: int = 6000, log=None, expect: str = "") -> str:
    """等「继续」按钮稳定下来再点，返回点中的文案。

    两件事都得防：

    1. 点完门店那一下页面会重渲染，按钮被换掉。原来点完只等 400ms 就去点，
       抓不到元素、静默返回空字符串，循环里却以为点过了。所以要求连续两次
       探到同一个按钮才认为渲染稳了。
    2. 等的这几秒里页面可能已经自己翻到下一步。这时那个按钮属于**下一步**，
       点下去就是替下一步做决定（比如取货人信息还没填就被推去付款页）。
       所以 expect 给了就在点之前再校验一次。
    """
    import time as _t
    deadline = _t.monotonic() + timeout_ms / 1000
    stable = 0
    seen = ""
    while _t.monotonic() < deadline:
        found = find_continue(page)
        now = found.get("text", "")
        if now and now == seen:
            stable += 1
            if stable >= 2:
                if expect and step_from_button(now) not in ("", expect):
                    if log:
                        log(f"[下单] 页面已翻到「{now}」这一步，不点了，重新判断")
                    return MOVED_ON
                # 选择器同时带上文案：定位在点击那一刻才解析，页面若已翻页
                # 就匹配不到、点击失败，而不是误点下一步的按钮（各步共用同一个 id）。
                return click_button(found.get("frame") or page,
                                    css_button(now, found.get("id", "")),
                                    timeout_ms=4000, log=log)
        else:
            stable = 0
        seen = now
        page.wait_for_timeout(200)
    if log:
        log(f"[下单] ⚠️ {timeout_ms / 1000:.0f}s 内没等到可点的「继续」按钮")
    return ""


def click_continue(page, log=None) -> str:
    hit = click_id(page, CONTINUE_BTN, log=log)
    if hit:
        return hit
    return click_text(page, "检查订单", "立即下单", "现在下单", "确认下单", "继续",
                      log=log)


def try_bag_checkout_xhr(page) -> dict:
    try:
        return page.evaluate(JS_AOS_POST, list(BAG_CHECKOUT_TRIES)) or {}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}


def looks_unpaid(snap: dict) -> bool:
    """判断「待付款订单已经创建出来了」。只认硬证据。

    **只看文案会误报，这一点栽了三次**：
      「掌上生活」  —— 招商银行 App 的名字，选付款方式时就印在 Billing 页；
      裸「待付款」  —— Review 页那句「取货日期待付款完成后确定」里就有；
      翻页中间态   —— 按钮短暂消失时，「不在向导里」的判断也会成立。

    误报的代价比漏报大得多：报了成功就不再重试，人也不会去补救，
    结果是你以为下单了、其实购物袋还在那儿。所以只认两样：
    URL 跳到 thankyou/order，或者页面上真的出现了订单号。
    """
    return bool(snap.get("thank") or snap.get("order"))


def card_would_charge(snap: dict, want: str) -> bool:
    if want in SCAN_PAY:
        return False
    text = snap.get("compact") or ""
    if any(p in text for p in SCAN_PAY):
        return False
    return any(p in text for p in CARD_PAY)


class OrderPlacer:
    def __init__(self, *, region: str = "cn", pickup_stores: list[str] | None = None,
                 payment: str = "支付宝", delivery: str = "pickup",
                 id_last4: str = "", last_name: str = "", first_name: str = "",
                 email: str = "", phone: str = "", installment_months: int = 0,
                 secure_host: str = "", stop_at_review: bool = False,
                 timeout_ms: int = 30000, log=print):
        self.region = region
        # 可接受的取货门店，按优先级排。监控命中时会把「真有货的那几家」放在前面。
        self.pickup_stores = [x.strip() for x in (pickup_stores or []) if x and x.strip()]
        self.payment = (payment or "支付宝").strip() or "支付宝"
        self.delivery = delivery if delivery in ("pickup", "shipping") else "pickup"
        self.id_last4 = re.sub(r"\s+", "", id_last4 or "").upper()
        self.last_name = (last_name or "").strip()
        self.first_name = (first_name or "").strip()
        self.email = (email or "").strip()
        self.phone = (phone or "").strip()
        self.installment_months = int(installment_months or 0)
        # 会话所在的 secureN 主机。探到之后本进程复用，省掉挨个试的开销。
        self.secure_host = (secure_host or "").strip()
        #: 走到 Review 页就停，不点「立即下单」。测试整条链路时用。
        self.stop_at_review = bool(stop_at_review)
        self.timeout_ms = timeout_ms
        self.log = log

    def enter(self, ctx, page) -> Any:
        xhr = try_bag_checkout_xhr(page)
        if xhr.get("stk"):
            self.log("[下单] 页面里读到了 x-aos-stk，已用同源请求试结账")
        else:
            self.log("[下单] 页面里没有 x-aos-stk，跳过伪造，改为直跳结账页")
        if xhr.get("goto"):
            self.log("[下单] 接口给出结账地址，直跳")
            page.goto(xhr["goto"], timeout=self.timeout_ms, wait_until="domcontentloaded")
            return page

        seen = []
        try:
            seen = [p.url for p in ctx.pages]
        except Exception:
            pass
        cands = checkout_candidates(self.region, self.secure_host, seen)

        last = page
        for i, url in enumerate(cands, 1):
            self.log(f"[下单] 直跳结账（{i}/{len(cands)}，不加载购物袋页）：{url}")
            landed = self._goto_checkout(ctx, page, url)
            if is_sign_in(getattr(landed, "url", "")):
                # 换主机解决不了没登录。立刻交回上层去走登录流程，
                # 否则剩下几个候选每个都要空等 on_checkout 超时。
                self.log("[下单] 被重定向到登录页——不是主机的问题，先去登录")
                return landed
            if on_checkout(landed):
                host = secure_host_of(landed.url)
                if host and host != self.secure_host:
                    self.secure_host = host
                    self.log(f"[下单] 结账主机记为 {host}")
                return landed
            last = landed
        self.log("[下单] 几个结账地址都没进到向导，退回购物袋点结账")
        return last

    def _goto_checkout(self, ctx, page, url: str):
        """跳到某个结账地址。Apple 有时会在新标签页打开，两种都接住。"""
        try:
            with ctx.expect_page(timeout=4000) as info:
                page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
            nxt = info.value
            try:
                nxt.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            return nxt
        except Exception:
            try:
                page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
            except Exception as e:
                self.log(f"[下单] 跳转失败：{str(e)[:70]}")
            return page

    def place(self, page, t0: float) -> tuple[bool, str, str, str]:
        deadline = time.monotonic() + max(180.0, self.timeout_ms / 1000 * 4)
        last = "还在结账向导里"
        last_key = None
        acted_at = 0.0

        while time.monotonic() < deadline:
            try:
                page.wait_for_timeout(400)
            except Exception:
                pass
            snap = snapshot(page)
            key = snap.get("key") or ""

            if snap.get("signIn"):
                return False, "⚠️ 卡在登录页", "结账被登录墙拦住，先在这个 Chrome 里登录 Apple ID。", ""
            if looks_unpaid(snap):
                return self._ok(t0, snap.get("order") or "")

            # 刚点过这一步，等 URL 切到下一步，避免连点
            if key == last_key and acted_at and time.monotonic() - acted_at < 2.8:
                continue

            if key in ("fulfillment", ""):
                last = self._step_fulfillment(page, snap)
                if last.startswith("⚠️"):
                    return False, last, (
                        "没能切到到店取货，已停住——再往下点会下成送货订单。"
                        "请在页面上手动选「我要取货 → 门店」。"), ""
            elif key == "pickupcontact":
                ok, last = self._step_contact(page, snap)
                if not ok:
                    return False, "⚠️ 卡在取货人信息", last, ""
            elif key in ("billing", "invoice"):
                last = self._step_billing(page, snap)
                if last.startswith("⚠️"):
                    return False, last, "请改选支付宝或微信后再点检查订单。", ""
            elif key == "review" and self.stop_at_review:
                return True, "已到 Review 页（按配置停住，未下单）", (
                    f"stop_at_review=true，没有点「立即下单」，购物袋里的货还在。"
                    f"要真下单把 config.json 的 autobuy.stop_at_review 改回 false。"
                    f"总耗时 {time.monotonic() - t0:.1f}s"), ""
            elif key == "review":
                last = self._step_review(page, snap)
                if last.startswith("⚠️"):
                    return False, last, "当前不像扫码支付，确认下单会走卡授权，已停住。", ""
            else:
                last = self._step_unknown(page, snap, key)

            last_key = key
            acted_at = time.monotonic()
            self.log(f"[下单] {last}  （_s={snap.get('step') or key or '?'}）")

        snap = snapshot(page)
        if looks_unpaid(snap):
            return self._ok(t0, snap.get("order") or "")
        return False, "⚠️ 没能创建订单", (
            f"{last}。请在打开的结账页手动走完："
            f"自提 → 身份证后四位 → 付款方式 → 检查订单 → 确认下单。"
            f"当前 {snap.get('url', '')[:90]}"
        ), ""

    def _ok(self, t0: float, order_id: str) -> tuple[bool, str, str, str]:
        return True, "已创建待付款订单", (
            f"订单号 {order_id or '（页面未解析到，请看标签页）'}。"
            f"请在付款窗口内自己扫码支付（约 30 分钟，以页面倒计时为准）。"
            f"总耗时 {time.monotonic() - t0:.1f}s"
        ), order_id

    def _step_fulfillment(self, page, snap: dict) -> str:
        if self.delivery != "pickup":
            hit = click_continue(page)
            return f"送货后点了「{hit or snap.get('continueText') or '继续'}」"

        state = self._switch_to_pickup(page)
        if state == "gone":
            # 分段控件不在了 = 已经不在这一步。什么都别点，让外层循环重新判步骤；
            # 在下一步的页面上乱点会把流程弹回来。
            return "已离开配送步骤，等下一轮重新判断"
        if state != "ok":
            # 绝不能往下点「继续」：那个按钮还是送货那栏的，
            # 一点就把订单推成了 3-5 个工作日送货，而且不会报错。
            return "⚠️ 没能切到「我要取货」"

        picked = self._click_store(page)
        hit = continue_when_ready(page, STEP_WAIT_MS, self.log, "fulfillment")
        where = f"门店「{picked}」" if picked else "门店（未自动选中）"
        if hit == MOVED_ON:
            return f"已选好{where}，页面已进入下一步"
        if not hit:
            return f"⚠️ 选好{where}了，但没等到可点的「继续」按钮"
        return f"自提 + {where} 后点了「{hit or snap.get('continueText') or '继续'}」"

    def _switch_to_pickup(self, page) -> str:
        """切到「我要取货」并等门店列表渲染出来。返回 ok / gone / fail。

        点击本身是生效的（实测 el.click() 有用），慢的是它后面那个异步请求：
        门店列表要 ~1.5s 才出来。原来点完不等就往下走，「继续」抓到的还是
        送货那栏的按钮，一点就把订单推成送货，二十轮全卡在这个竞态上。

        gone = 分段控件不在了，说明已经翻页到下一步，调用方什么都别点。
        """
        deadline = time.monotonic() + PICKUP_READY_MS / 1000
        st: dict = {}
        acted_at = 0.0
        toggles = 0
        while time.monotonic() < deadline:
            try:
                st = page.evaluate(JS_PICKUP_STATE)
            except Exception:
                page.wait_for_timeout(250)
                continue
            if not st.get("hasSeg"):
                return "gone"
            if st.get("listed"):
                self.log("[下单] 已切到「我要取货」，门店列表已就绪")
                return "ok"

            idle = time.monotonic() - acted_at
            if not st.get("on") and idle > 2.5:
                # 还没切过去，点一下。点了就生效，别连点——会打断它的异步加载。
                page.evaluate(JS_SEG_CLICK, "我要取货")
                acted_at = time.monotonic()
            elif st.get("on") and idle > 3.5 and toggles < 2:
                # 样式显示已选中、门店列表却一直不来：页面刚加载完就被点过，
                # 视觉状态变了但那次点击没触发取门店的请求。来回切一次逼它重发。
                self.log("[下单] 取货已选中但门店列表没来，切回送货再切回来试一次")
                page.evaluate(JS_SEG_CLICK, "为我送货")
                page.wait_for_timeout(900)
                page.evaluate(JS_SEG_CLICK, "我要取货")
                acted_at = time.monotonic()
                toggles += 1
            page.wait_for_timeout(250)

        self.log(f"[下单] ⚠️ {PICKUP_READY_MS / 1000:.0f}s 内没能切到取货态"
                 f"（选中={st.get('on')} 列表={st.get('listed')}），"
                 "已停住不往下点——继续点会变成送货订单")
        return "fail"

    def _click_store(self, page) -> str:
        """按优先级挨个试，点中第一家就停。绝不「兜底选第一家」——选错店比没选更糟。

        门店条目是 <label for=radio>，而且 innerText 为空、只有 textContent 有内容，
        所以不能用通用的 click_text，得单独按 textContent 匹配。
        标着「目前不可取货」的门店会跳过，选中了也结不了账。
        """
        if not self.pickup_stores:
            return ""
        try:
            r = page.evaluate(JS_PICK_STORE, list(self.pickup_stores)) or {}
        except Exception as e:
            self.log(f"[下单] 选门店出错：{str(e)[:80]}")
            return ""

        if r.get("clicked"):
            how = "本来就选中的" if r.get("already") else "已点选"
            self.log(f"[下单] 取货门店「{r['clicked']}」（{how}）")
            return r["clicked"]
        if r.get("skipped"):
            self.log(f"[下单] ⚠️「{r['skipped']}」当前标着不可取货，没有选它")
        seen = r.get("seen") or []
        self.log(f"[下单] 没找到这几家里的任何一家：{'、'.join(self.pickup_stores)}"
                 + (f"（页面上有 {len(seen)} 家：{seen[:3]}）" if seen else "（页面上没有门店列表）"))
        return ""

    def _step_contact(self, page, snap: dict) -> tuple[bool, str]:
        # 别信刚翻页时那一眼的 snapshot：字段是异步渲染的，早读一步就是「没有」，
        # 于是身份证没填就点了继续，校验默默不过，外层只好一轮轮重试。
        state = wait_for_field(page, ID_NATIONAL, STEP_WAIT_MS)
        if state == "empty":
            if not re.fullmatch(r"[0-9]{3}[0-9X]", self.id_last4):
                return False, (
                    "取货需要身份证后四位。在 config.json 的 autobuy.id_last4 填 4 位"
                    "（最后一位可以是 X），再跑一次。"
                )
            if not fill_field(page, ID_NATIONAL, self.id_last4, log=self.log):
                return False, "找到了身份证后四位输入框，但没能填进去，请在页面上手动填。"
            self.log("[下单] 已填身份证后四位")
        elif state == "missing":
            self.log("[下单] 这一步没有身份证后四位字段，跳过")

        if self.last_name:
            fill_field(page, ID_LAST_NAME, self.last_name, log=self.log)
        if self.first_name:
            fill_field(page, ID_FIRST_NAME, self.first_name, log=self.log)
        if self.email:
            fill_field(page, ID_EMAIL, self.email, log=self.log)
        if self.phone:
            fill_field(page, ID_PHONE, self.phone, log=self.log)

        hit = continue_when_ready(page, STEP_WAIT_MS, self.log, "pickupcontact")
        if hit == MOVED_ON:
            return True, "取货人信息已填好，页面已进入下一步"
        if not hit:
            snap2 = snapshot(page)
            if snap2.get("continueEnabled") is False:
                return False, (
                    "「继续」是灰的：姓名/电话/身份证后四位可能没填全。"
                    "可在 autobuy 里补 pickup_last_name / pickup_first_name / pickup_phone。"
                )
        return True, f"取货人信息后点了「{hit or '继续'}」"

    def _step_billing(self, page, snap: dict) -> str:
        """选付款方式 + 期数，然后点「检查订单」。

        这一步最容易出现「我以为点了、系统认为没点」：选中银行会触发一次
        重渲染，把刚点上的期数冲掉；DOM 里 radio 是 checked 的，提交时却按
        没选处理，页面纹丝不动只在上方冒一条「请从以下支付方式中选择一种
        进行付款。」。所以每次点完都复核，并且以页面的提示为准重试。
        """
        why = ""
        for attempt in range(3):
            # 第二轮起强制「弹开再选回来」：DOM 已经是选中的，再点一次不会
            # 派发 change，应用层收不到任何信号，光重选是没用的。
            if not self._fill_billing(page, force_change=attempt > 0):
                return "⚠️ 没能选中付款方式"

            if card_would_charge(snapshot(page), self.payment):
                return "⚠️ 停在付款页（信用卡会立刻扣款）"

            # 必须**先提交再读提示**。页面上那条「请选择付款方式」是上一次
            # 提交失败留下的，不重新提交它就一直在——只读不交会一直读到旧消息，
            # 三轮全在原地打转。
            hit = continue_when_ready(page, STEP_WAIT_MS, self.log, "billing")
            if hit == MOVED_ON:
                return "付款方式已选好，页面已进入下一步"
            why = self._billing_error(page)
            if not why:
                return f"付款方式后点了「{hit or '检查订单'}」"
            self.log(f"[下单] 提交后页面提示「{why}」"
                     f"（第 {attempt + 1}/3 次），重新选一遍再交")
            page.wait_for_timeout(600)
        return f"⚠️ 付款方式没能提交：{why}"

    def _fill_billing(self, page, force_change: bool = False) -> bool:
        """选银行 + 选期数，两个都复核到真的选中为止。"""
        if not self._click_payment(page, force_change=force_change):
            return False
        # 选中银行会展开它那一组期数并重渲染；不等稳就点期数，会被冲掉
        wait_settled(page, log=self.log)
        if self.installment_months:
            ok, why = self._choose_installment(page, force_change=force_change)
            if not ok:
                self.log(f"[下单] ⚠️ {why}")
                return False
        return True

    def _billing_error(self, page) -> str:
        try:
            return page.evaluate(JS_FORM_ERROR, list(BILLING_COMPLAINTS)) or ""
        except Exception:
            return ""

    def _select(self, page, elem_id: str, label: str, already: bool,
                kind: str = "", force_change: bool = False) -> bool:
        """选中一个 radio 并确认它真的留住了。最多试 3 次。

        每次点之前都先等页面安静。重渲染途中点下去，DOM 上会变成 checked、
        应用层却完全没收到——表现就是「两个都显示已选中，页面还说没选」。

        force_change=True 时，即使 DOM 上已经是选中的也要**弹开再选回来**：
        对一个已经 checked 的 radio 再点一次不会派发 change 事件，应用层
        收不到任何信号。这是上一条症状唯一的解法。
        """
        for attempt in range(3):
            if already and attempt == 0 and not force_change \
                    and self._stays_checked(page, elem_id):
                self.log(f"[下单] 「{label}」本来就选中的")
                return True
            wait_settled(page, log=self.log)
            if force_change or already:
                self._bounce(page, elem_id, kind, label)
            if not click_label(page, elem_id, self.timeout_ms // 3 or 8000):
                self.log(f"[下单] 点不到「{label}」的选项（第 {attempt + 1} 次）")
                continue
            if self._stays_checked(page, elem_id):
                return True
            self.log(f"[下单] 「{label}」点上又被冲掉了，等稳再点（第 {attempt + 1} 次）")
            already = False
        return False

    def _bounce(self, page, elem_id: str, kind: str, label: str) -> None:
        """先选同组里的另一个选项，制造一次真实的 change。"""
        if not kind:
            return
        try:
            other = page.evaluate(JS_SIBLING_OPTION, [elem_id, kind])
        except Exception:
            other = ""
        if not other:
            return
        if click_label(page, other, 4000):
            self.log(f"[下单] 「{label}」已选中但没提交上去，先弹到别的选项再选回来")
            page.wait_for_timeout(500)

    def _stays_checked(self, page, elem_id: str, ms: int = 1500) -> bool:
        """点完之后盯一会儿，确认这个 radio 没被重渲染冲掉。"""
        if not elem_id:
            return True
        deadline = time.monotonic() + ms / 1000
        while time.monotonic() < deadline:
            page.wait_for_timeout(250)
            try:
                st = page.evaluate(JS_IS_CHECKED, elem_id)
            except Exception:
                return False
            if st is not True:
                return False
        return True

    def _click_payment(self, page, force_change: bool = False) -> str:
        """选付款方式。先试配置指定的那个，再回落到扫码付名单。

        不能把两者并成一次匹配：匹配是按 DOM 顺序取第一个命中的，
        支付宝排在最前面，一起传就等于永远选支付宝。
        """
        wait_for_field(page, "checkout.billing.billingoptions.alipay",
                       STEP_WAIT_MS)
        for wants in ([self.payment], [w for w in SCAN_PAY if w != self.payment]):
            try:
                r = page.evaluate(JS_PICK_PAYMENT, list(wants)) or {}
            except Exception as e:
                self.log(f"[下单] 选付款方式出错：{str(e)[:80]}")
                return ""
            if r.get("refusedCard"):
                self.log(f"[下单] ⚠️「{r['refusedCard']}」匹配到的是信用卡选项，"
                         "没有点它——那条是即时扣款")
                return ""
            if r.get("clicked"):
                if not self._select(page, r["id"], r["clicked"], r.get("already"),
                                    kind="pay", force_change=force_change):
                    return ""
                self.log(f"[下单] 支付方式：{r['clicked']}")
                return r["clicked"]
            self.log(f"[下单] 没匹配到「{'、'.join(wants)}」，"
                     f"页面上有：{r.get('seen')}")
        return ""

    def _choose_installment(self, page, force_change: bool = False) -> tuple[bool, str]:
        """选分期期数。返回 (能不能往下走, 说明)。

        银行分期**必须**选期数：默认一个都不选，不选就点「检查订单」页面
        纹丝不动，而且不报错。所以这里选不中要挡住，不能放过去。
        """
        want = self.installment_months
        try:
            r = page.evaluate(JS_PICK_TERM, str(want)) or {}
        except Exception as e:
            return False, f"选分期期数出错：{str(e)[:70]}"

        if r.get("none"):
            return True, ""   # 这个付款方式没有分期期数（比如支付宝），正常
        if r.get("clicked"):
            if not self._select(page, r["id"], r["clicked"][:10], r.get("already"),
                                kind="term", force_change=force_change):
                return False, f"「{want} 期」反复被重置，页面可能还在加载"
            self.log(f"[下单] 分期期数：{r['clicked']}")
            return True, ""
        return False, (f"没找到「{want} 期」，这家银行只有：{r.get('seen')}。"
                       "改 config.json 的 autobuy.installment_months")

    def _step_review(self, page, snap: dict) -> str:
        if card_would_charge(snap, self.payment):
            return "⚠️ 停在下单页（信用卡会立刻扣款）"
        hit = continue_when_ready(page, STEP_WAIT_MS, self.log, "review")
        if hit == MOVED_ON:
            return "Review 页已翻过去"
        return f"Review 确认下单：{hit or '没等到可点的下单按钮'}"

    def _step_unknown(self, page, snap: dict, key: str) -> str:
        if snap.get("hasCheckOrder"):
            hit = click_text(page, "检查订单") or click_continue(page)
            return f"未知步骤 {key or '?'}，点了检查订单：{hit}"
        if snap.get("hasPlace"):
            hit = click_text(page, "立即下单", "现在下单", "确认下单") or click_continue(page)
            return f"未知步骤 {key or '?'}，点了确认下单：{hit}"
        hit = click_continue(page)
        return f"未知步骤 {key or '?'}，点了继续：{hit or '无按钮'}"
