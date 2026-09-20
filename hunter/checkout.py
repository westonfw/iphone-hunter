"""在已登录会话里创建待付款订单。

中国大陆到店取货结账向导（以 2026-08 实测为准）：

  Fulfillment-init   选自提 + 门店
  PickupContact-init 填身份证后四位（姓名电话通常已从 Apple ID 带出）
  Billing            选支付宝/微信
  检查订单           进入 Review
  Review             确认下单 → 待付款/扫码

加购通过页面点击生成 atbtoken；结账只走同源快车道，失败立即返回。
绝不代付：不填支付密码、不确认 Apple Pay；信用卡路径上不点确认下单。
"""

from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, unquote, urljoin, urlparse

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

#: 等取货时段下拉框渲染出来的上限。它是选完门店之后服务端那一趟回来才有的，
#: 所以必须等；但没有这一步的流程里它永远不来，所以等待随时可以被
#: 「页面上没有日期单选 + 继续按钮已经可点」打断（见 _pick_time_slot），
#: 这里只是兜底上限。
SLOT_WAIT_MS = 8000

# 每一步等控件就绪的上限。翻页时按钮会先消失再重建，转圈久一点就要十几秒，
# 原来按 timeout_ms//4 算出来只有 7.5s，经常刚好差一点。
STEP_WAIT_MS = 15000

#: 只是「瞄一眼这个框是空是满」的上限。这几个框跟身份证后四位是一起渲染的，
#: 到这时候早就在了；没在就说明这一版流程不问，不值得再等一个 STEP_WAIT_MS。
FIELD_PEEK_MS = 1500

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

# 取货时段的下拉框。2026-09-17 起自提多了「选具体时间」，不选就点不动「继续」。
# 页面上是「日期单选 + 时间下拉」两级：下拉里只有**当前选中那一天**的档位。
#
# 两个坑：
#   1. 这是 React 受控组件，直接改 .value 它不认——必须用原型上的原生 setter
#      写进去再派发 change，否则视觉上选中了、模型里还是空的，继续依然点不动。
#   2. 灰掉的档位（disabled）和占位那一项（value 为空）都得跳过，选了也白选。
JS_PICK_TIMESLOT = r"""
(want) => {
    const vis = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && el.offsetParent !== null;
    };
    const mins = (t) => {
        const m = /^\s*(\d{1,2}):(\d{2})\s*([AaPp])?/.exec(t || "");
        if (!m) return -1;
        let h = parseInt(m[1], 10);
        const half = (m[3] || "").toUpperCase();
        if (half === "A" && h === 12) h = 0;
        else if (half === "P" && h !== 12) h += 12;
        return h * 60 + parseInt(m[2], 10);
    };
    const days = [...document.querySelectorAll('input[type=radio]')].filter(
        i => (i.getAttribute("data-autom") || "").startsWith("day-of-week-option"));
    const sel = [...document.querySelectorAll("select")].find(
        s => vis(s) && (s.getAttribute("data-autom") || "").includes("availablewindow"));
    // hasDays 是给调用方判断的：日期单选在，说明这一版流程**有**选时段这一步，
    // 下拉只是还没渲染出来，得接着等。
    if (!sel) return {absent: true, hasDays: days.length > 0};
    if (sel.value) return {picked: (sel.options[sel.selectedIndex] || {}).text || "",
                           already: true};

    const opts = [...sel.options].filter(o => o.value && !o.disabled);
    if (!opts.length) {
        // 这一天排满了。换下一天再看——日期单选换了之后下拉会重渲染。
        const at = days.findIndex(d => d.checked);
        const next = days.slice(at + 1).find(d => !d.disabled);
        if (!next) return {none: true};
        const lab = next.id ? document.querySelector(`label[for="${CSS.escape(next.id)}"]`) : null;
        (lab || next).click();
        return {switched: true};
    }

    // 开始时刻从 value 里拆：形如 18-12:30-12:45（日-开始-结束）。
    const start = (o) => {
        const parts = (o.value || "").split("-");
        const t = parts.length >= 2 ? mins(parts[1]) : -1;
        return t >= 0 ? t : mins(o.text);
    };
    let pick = opts[0];
    const w = (want || "").toLowerCase();
    if (w === "latest" || w === "最晚") pick = opts[opts.length - 1];
    else if (w && w !== "earliest" && w !== "最早") {
        const want_m = mins(w);
        if (want_m >= 0) pick = opts.find(o => start(o) >= want_m) || opts[opts.length - 1];
    }

    const setter = Object.getOwnPropertyDescriptor(
        window.HTMLSelectElement.prototype, "value").set;
    setter.call(sel, pick.value);
    sel.dispatchEvent(new Event("change", {bubbles: true}));
    return {picked: (pick.text || pick.value).trim()};
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

#: 结账向导每轮轮询的间隔。原来是 400ms，而且**每轮开头都先睡**——包括第一轮，
#: 那时还什么都没点，纯白等。向导有 4~5 步，光这一项就是 1s 上下。
#: 压到 200ms 只是让「这一步已经翻页了」被更早发现，不改变任何点击逻辑。
POLL_MS = 200

#: 点完同一步之后最多等多久再考虑重点。这是**防重复提交**的闸，不是性能参数。
#:
#: 原来是 2.8s，凭感觉拍的。2026-09-14 实测：`checkoutx` 每步的 TTFB 是
#: **7.5~10.5 秒**（纯服务端处理，排队和收包都只有 1ms），于是页面还没翻页
#: 冷却就到期了——日志里「继续填写取货详情」连点 3 次、取货人那步连点 3 次，
#: 等于把同一个动作重复提交给服务端。
#:
#: 按实测的上限留余量定到 12s。注意它只是上限不是下限：step key 一变
#: （页面真翻页了）立刻往下走，所以正常路径不会因此变慢。
STEP_COOLDOWN_S = 12.0

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
            // 被边缘节点拦了就**立刻停**。剩下几个路径打的是同一族端点，
            // 继续试只会让退避更深——Akamai 按累计速率判，不是按单个路径判。
            if (res.status === 541 || res.status === 503 || res.status === 429) {
                tried.push({path, status: res.status, blocked: true});
                return {ok: false, goto: "", stk: !!stk, tried, blocked: res.status};
            }
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
    // 「操作超时」：会话没了，页面上再点什么都没用。必须比 signIn 更早判。
    const expired = /\/shop\/sorry\/|session_expired/i.test(url);
    const idEl = document.getElementById("checkout.pickupContact.selfPickupContact.nationalIdSelf.nationalIdSelf");
    const cont = document.getElementById("rs-checkout-continue-button-bottom");
    const contText = cont ? (cont.innerText || cont.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim() : "";
    return {
        url, step, order, unpaid, thank, signIn, expired, compact,
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


#: 「操作超时」页。结账页的模型里写明了它的来路（2026-09-17 的 HAR）：
#:
#:   "session": {"d": {"alertMs": "60000", "interactionMs": "300000",
#:                     "expiredUrl": "https://www.apple.com.cn/shop/sorry/session_expired",
#:                     "canExtend": true, "ttl": "1199729",
#:                     "extendSessionUrl": "/shop/checkoutx/session?_a=extendSessionUrl&…"}}
#:
#: 也就是：**结账会话 5 分钟（interactionMs）没有交互就作废**，然后整页被扔到
#: 这里；过期前 60 秒（alertMs）会先弹一次提醒；会话总寿命 ttl ≈ 20 分钟。
SORRY_EXPIRED = "/shop/sorry/session_expired"


def is_session_expired(url: str) -> bool:
    """这个 URL 是不是「会话已经没了」。

    `/shop/sorry/` 下面那几页一律算：它们都意味着同一件事——**当前这条结账链路
    已经作废，在上面再点、再发包都没有意义**，必须回去重新登录、重新走。

    跟 `is_sign_in` 分开是因为处置方式不同：登录页上就地登录就行；这一页上
    连登录框都没有，只能先离开它。
    """
    u = (url or "").lower()
    return "/shop/sorry/" in u or "session_expired" in u


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
            if is_sign_in(url) or is_session_expired(url):
                return False        # 登录墙 / 会话过期：再等、再换主机都没有意义
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


#: 登录态判据——**必须跑在渲染后的 DOM 上**。
#:
#: 试过而且不行的：fetch 回购物袋 HTML 再正则。购物袋是 React SPA，HTML 外壳里
#: 没有任何账号状态，data-autom 账号钩子 / isLoggedIn / signIn 链接 /「退出登录」
#: 四组正则全部零命中（2026-09-14 实测）。跟 shield cookie 那件事是同一个教训：
#: **fetch 到的 HTML ≠ 跑完 JS 的页面。**
JS_LOGIN_DOM = r"""
() => {
    const body = document.body ? (document.body.innerText || "") : "";
    const hrefs = [...document.querySelectorAll('a[href]')]
        .map(a => a.getAttribute('href') || "");
    // 账号入口**不是**登录证据：登出状态下这个链接照样渲染，点进去才跳 idmsa。
    // 留着它只为读 secureN，判定登录靠 signOut。
    const acct = hrefs.find(h => /\/shop\/account\/home/i.test(h)) || "";
    const signIn = hrefs.find(h => /signIn|idmsa\.apple/i.test(h)) || "";
    // 退出登录的入口只有真的登录了才会渲染出来——这才是正向证据。
    const signOut = hrefs.find(h => /signOut|logout|\/shop\/signout/i.test(h)) || "";
    const outAutom = document.querySelector(
        '[data-autom*="signOut" i], [data-autom*="logout" i]') ? 1 : 0;
    const outWords = (body.match(/退出登录|登出|Sign Out/g) || []).length;
    const words = (body.match(/登录|Sign In/g) || []).length;
    const m = acct.match(/^https:\/\/(secure\d+)\./i);
    return {
        acctLink: acct.slice(0, 60),
        signInLink: signIn.slice(0, 60),
        signOutLink: signOut.slice(0, 60),
        signOutAutom: outAutom,
        signOutWords: outWords,
        signInWords: words,
        secureHost: m ? m[1] : "",
        rendered: body.length,
    };
}
"""


def login_state(page) -> dict:
    """读当前**已渲染**页面的登录态。返回 {signed_in, secure_host, evidence}。

    正向证据只认「退出登录」入口（链接 / data-autom / 字样）。**账号入口不算**：
    登出状态下 `/shop/account/home` 这个链接照样渲染在导航里，点进去才会跳
    idmsa——原来拿它当判据，于是监控日志报「已登录」，人点进订单却被要求登录，
    而且放货那一刻才在结账页上撞见登录墙。

    没有正向证据时返回 None（判不准），不返回 False：购物袋页本来就不一定渲染
    退出入口，把「没看见」当成「没登录」会让保活每轮都去重登。真正的否定结论由
    `verify_signed_in` 用一次真实导航给出。
    """
    try:
        r = page.evaluate(JS_LOGIN_DOM) or {}
    except Exception as e:
        return {"signed_in": None, "secure_host": "",
                "evidence": f"探测失败：{type(e).__name__}: {str(e)[:60]}"}
    evidence = (f"acct={r.get('acctLink') or '无'} "
                f"signOut={r.get('signOutLink') or '无'}/"
                f"autom={r.get('signOutAutom')}/字样={r.get('signOutWords')} "
                f"signIn={r.get('signInLink') or '无'} "
                f"登录字样={r.get('signInWords')} "
                f"渲染={r.get('rendered')}字")
    signed_out_marks = (bool(r.get("signOutLink")) or bool(r.get("signOutAutom"))
                        or bool(r.get("signOutWords")))
    if signed_out_marks:
        state = True
    elif r.get("signInLink") or r.get("signInWords"):
        state = False
    else:
        state = None
    return {"signed_in": state, "secure_host": r.get("secureHost") or "", "evidence": evidence}


#: 订单列表。选它做登录探针不是随便挑的：它跟结账要的是**同一级**鉴权，
#: 而账号首页在「认得你但没登录」的状态下也可能渲染出来。用户实测过的
#: 正是这个落差——监控报已登录，点订单却被要求登录。
ORDER_LIST_PATH = "/shop/order/list"


def verify_signed_in(page, base: str, timeout_ms: int = 30000,
                     settle=None, log=print) -> dict:
    """真实导航到订单列表，给出**权威**的登录结论。

    返回 {signed_in: True/False/None, secure_host, evidence}。这一趟还顺带续了
    `as_dc`(2h) 和 `as_sfa`(180d)——两者都只认「真实导航 + 跑 JS」，所以保活和
    验证是同一个动作，不额外花请求。

    True  = 订单页真的打开了（有退出入口，或页面上出现订单列表的结构）
    False = 被弹到 signIn / idmsa，这是 Apple 自己给的否定，最硬的证据
    None  = 导航失败或页面既不像登录页也不像订单页，判不准，绝不报「已登录」
    """
    url = base.rstrip("/") + ORDER_LIST_PATH
    try:
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
    except Exception as e:
        return {"signed_in": None, "secure_host": "",
                "evidence": f"打不开订单页：{type(e).__name__}: {str(e)[:60]}"}
    if settle is not None:
        settle(page)
    try:
        landed = page.url or ""
    except Exception:
        landed = ""
    if is_sign_in(landed):
        return {"signed_in": False, "secure_host": "",
                "evidence": f"订单页被弹到登录：{landed[:80]}"}
    st = login_state(page)
    st["evidence"] = f"落地={landed[:60]} {st['evidence']}"
    if st["signed_in"] is None:
        log(f"[登录探针] 订单页既无退出入口也无登录入口，判不准：{st['evidence']}")
    return st


#: Apple 找不到页面时会 302 到这里，而且它返回 **200**，不是 404 状态码——
#: 光看 response.status 发现不了，必须看落地 URL。
NOT_FOUND_PATH = "/shop/404"

#: 结账向导自己发的那批 XHR。被拦时页面拿不到响应，会把你扔到 /shop/404，
#: 于是一个**限流**问题看起来像「页面不存在」，方向全错。
#: 2026-09-14 实测链路：
#:   POST /shop/bagx/checkout_now            200
#:   302  secure7/shop/checkout/start        302
#:   GET  secure7/shop/checkout              200   结账页真的开了
#:   GET  secure7/shop/checkoutx/fulfillment 541   ← 被 Akamai 拦
#:   →    /shop/404                                前端把你踢到这
CHECKOUTX_PATH = "/shop/checkoutx/"


def goto_buy_page(page, url: str, tries: int = 3, log=None, timeout_ms: int = 45000) -> bool:
    """打开购买页，落到 /shop/404 就重试。返回是否真的到了购买页。

    为什么重试而不是直接报错：实测同一深链用干净客户端连打 5 次全 200，浏览器里
    却偶发一次落到 /shop/404——边缘节点抖动，重试一次就好。但**不检查最危险**：
    预热会对着 404 页报告「产品页已就绪」，等放货那一刻才发现加购按钮根本不存在。
    跟 warm_alive 是同一类失败：日志一路正常，静默哑火。
    """
    for i in range(1, max(1, tries) + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(600)
        except Exception as e:
            if log:
                log(f"[购买页] 第 {i}/{tries} 次打开失败：{type(e).__name__}: {str(e)[:60]}")
            continue
        if NOT_FOUND_PATH not in (page.url or ""):
            if i > 1 and log:
                log(f"[购买页] 第 {i} 次重试成功")
            return True
        if log:
            log(f"[购买页] ⚠️ 落到 404（第 {i}/{tries} 次），疑似边缘抖动，重试")
        page.wait_for_timeout(900)
    if log:
        log(f"[购买页] ⚠️ 连续 {tries} 次都是 404，这次不是抖动：{url}")
    return False


class CheckoutBlocked(Exception):
    """结账链路被边缘节点拦了（541/503），不是页面不存在。

    必须跟「页面不存在」分开：前者要退避等待，后者重试或换地址。把它们混成
    「没能进入结账页」会让人往完全错误的方向查——实际发生过。
    """


def watch_checkout_block(page, log=None, ctx=None) -> dict:
    """盯结账 XHR 有没有被拦。返回一个会被就地更新的 dict。

    **必须同时挂到 context 上**：点「结账」会开新标签（坑 7），被拦的那个
    `checkoutx/fulfillment` 请求发生在新标签里。只挂当前页就什么都看不到——
    而看不到的后果是把限流误报成「页面不存在」，正是这个函数要防的事。
    """
    state: dict = {"hits": [], "retry_after": 0.0}
    seen: set = set()
    hooked = []

    def on_resp(r):
        try:
            if CHECKOUTX_PATH not in r.url:
                return
            if r.status in (403, 541, 503, 429):
                from .apple import parse_retry_after
                ra = parse_retry_after((r.headers or {}).get('retry-after'))
                state["hits"].append({"status": r.status, "url": r.url[:110],
                                      "retry_after": ra})
                state["retry_after"] = max(state["retry_after"], ra)
                if log:
                    log(f"[结账] ⚠️ 被拦 {r.status}：{r.url.split('?')[0][-60:]}"
                        + (f"（Retry-After {ra:.0f}s）" if ra else ""))
        except Exception:
            pass

    def hook(p) -> None:
        if p is None or id(p) in seen:
            return
        seen.add(id(p))
        try:
            p.on("response", on_resp)
            hooked.append(p)
        except Exception:
            pass

    hook(page)
    if ctx is not None:
        try:
            for p in list(ctx.pages):
                hook(p)
            ctx.on("page", hook)      # 结账开的新标签也要接住
        except Exception:
            pass
    def close():
        for p in hooked:
            try:
                p.remove_listener('response', on_resp)
            except Exception:
                pass
        if ctx is not None:
            try:
                ctx.remove_listener('page', hook)
            except Exception:
                pass
        hooked.clear()
    state['close'] = close
    return state


class OrderPlacer:
    #: 门店编号长这样：R581。名字（五角场）喂给 selectStore 服务端不认，
    #: 而且不报错、只是静默选不中——所以宁可不走快车道，也不拿名字去赌。
    STORE_NO = re.compile(r"^R\d{2,5}$", re.I)

    def __init__(self, *, region: str = "cn",
                 store_numbers: list[str] | None = None,
                 allow_stores: list[str] | None = None,
                 still_live=None,
                 payment: str = "支付宝", delivery: str = "pickup",
                 id_last4: str = "", last_name: str = "", first_name: str = "",
                 email: str = "", phone: str = "", installment_months: int = 0,
                 secure_host: str = "", stop_at_review: bool = False,
                 fast_path: bool = True, place_order: bool = True,
                 pickup_city: str = "上海",
                 pickup_state: str = "上海", pickup_district: str = "杨浦区",
                 pickup_time: str = "", timeout_ms: int = 30000, log=print,
                 submit_guard=None, cancelled=None):
        self.submit_guard = submit_guard
        self.cancelled = cancelled
        self.still_live = still_live
        self.region = region
        # 只留真正长得像编号的，顺序去重
        #: 硬限制的门店编号，空 = 不限。换店时不许越过它。
        self.allow_stores: list[str] = [
            x for x in (str(v or '').strip().upper() for v in (allow_stores or []))
            if x and self.STORE_NO.match(x)]
        self.store_numbers: list[str] = []
        for x in (store_numbers or []):
            x = (x or "").strip().upper()
            if self.STORE_NO.match(x) and x not in self.store_numbers:
                self.store_numbers.append(x)
        # 边界在这儿就兑现，别留到换店那一步：候选表里留着越界的门店，轮换照样
        # 会换过去（首选排不上时就是这么溜出去的）
        if self.allow_stores:
            self.store_numbers = [x for x in self.store_numbers
                                  if x in self.allow_stores]
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
        # fast_path 参数仅为旧调用兼容；结账始终使用同源请求。
        #: 快车道走到 Review 后要不要直接提交。提交只创建待付款订单，
        #: 付款始终由人完成；信用卡路径在 fastpath 里还有一道硬拒。
        self.place_order_flag = bool(place_order)
        self.pickup_city = pickup_city
        self.pickup_state = pickup_state
        self.pickup_district = pickup_district
        #: 想要哪一档取货时段：earliest（默认）/ latest / "HH:MM"。
        #: 按服务端返回的时段模型选择。
        self.pickup_time = (pickup_time or "").strip()
        #: 服务端是否已经确认创建订单；与 Review 和结果未知分别处理。
        self.fast_ordered = False
        #: 快车道把「立即下单」那一发**送出去了**，但没拿到明确结论。
        #: 这跟「下单失败」不是一回事：订单可能已经建好了，所以**什么都不能再点**。
        self.fast_unknown = False
        #: 这次失败值不值得下一轮再试。调用方（AutoBuy）读它来定 retriable。
        #: 「结果不明」时必须是 False——重试就是再下一单。
        self.no_retry = False
        self.retriable = True
        self.blocked = False
        self.retry_after = 0.0
        self.fast_stage = ""
        self.fast_detail = ""
        self.result_url = ""
        #: 撞上了「操作超时」页。跟 no_retry 正好相反：会话过期时订单**肯定没建**，
        #: 所以该重试——但得先重新登录，在死会话上重试多少次都一样。
        self.session_expired = False
        self.timeout_ms = timeout_ms
        self.log = log

    def _try_fast_path(self, page) -> bool:
        """唯一的结账实现；失败保留原始结论，不刷新、不点击页面。"""
        from .fastpath import FastCheckout
        self.fast_stage = "⚠️ 快车道未完成"
        self.fast_detail = ""
        if not self.store_numbers:
            self.fast_stage = "⚠️ 缺少取货门店编号"
            self.fast_detail = "请配置 pickup_store_numbers 或由库存监控提供门店编号。"
            self.retriable = False
            return False
        fc = FastCheckout(
            store=self.store_numbers[0], stores=self.store_numbers,
            allow=self.allow_stores,
            id_last4=self.id_last4, last_name=self.last_name,
            first_name=self.first_name, email=self.email, phone=self.phone,
            city=self.pickup_city, state=self.pickup_state,
            district=self.pickup_district, pickup_time=self.pickup_time,
            payment_label=self.payment, installment_months=self.installment_months,
            place_order=self.place_order_flag and not self.stop_at_review,
            submit_guard=self.submit_guard, cancelled=self.cancelled,
            still_live=self.still_live,
            log=self.log)
        try:
            ok, self.fast_stage, self.fast_detail = fc.run(page)
        finally:
            # 轮换过的话，实际下单的门店跟候选表头不是同一家——通知里必须写对，
            # 不然人跑错店。
            self.store_used = getattr(fc, "store_used", "") or self.store_numbers[0]
            # 即使 run 抛异常，也必须先把提交状态带回上层。
            self.fast_ordered = bool(
                fc.submitted and fc.order_url
                and not FastCheckout.order_rejected(fc.order_url))
            if self.fast_ordered:
                self.result_url = urljoin(page.url, fc.order_url)
            self.fast_unknown = bool(
                fc.submitted and not self.fast_ordered
                and FastCheckout.order_unknown(fc.order_url))
            self.no_retry = bool(self.fast_unknown or (fc.submitted and self.submit_guard is not None
                                                        and not self.fast_ordered))
            kind = getattr(fc, "failure_kind", "")
            self.blocked = kind == "blocked"
            self.retry_after = getattr(fc, "retry_after", 0.0)
            self.session_expired = kind == "session_expired" and not fc.submitted
            self.retriable = not self.no_retry and kind != "blocked"
        self.log(f"[快车道] {self.fast_stage}：{self.fast_detail}")
        if ok and not self.fast_ordered:
            # 仅把已经完成的 Review 展示给人，不自动接管页面操作。
            fc.show_review(page)
        return ok

    def place(self, page, t0: float) -> tuple[bool, str, str, str]:
        if is_session_expired(page.url):
            self.session_expired = True
            return False, "⚠️ 结账会话已过期", (
                "会话已失效（interactionMs 是页面的交互期限），请重新登录后再尝试。"), ""
        if is_sign_in(page.url):
            return False, "⚠️ 卡在登录页", "请先在浏览器里完成登录。", ""
        if self.delivery != "pickup":
            self.retriable = False
            return False, "⚠️ 快车道仅支持到店取货", "delivery 必须为 pickup。", ""
        try:
            ok = self._try_fast_path(page)
        except Exception as e:
            ok = False
            self.fast_stage = "⚠️ 快车道异常，已停止"
            self.fast_detail = f"{type(e).__name__}: {e}"
        if self.fast_unknown:
            self.no_retry = True
            return False, "⚠️ 下单结果不明，已停手", (
                self.fast_detail + " 订单可能已经创建，请查看订单列表；不会再次提交。"), ""
        if self.fast_ordered:
            # thankyou 的导航/渲染失败不能抹掉服务端已经确认的订单结果。
            try:
                order = snapshot(page).get("order") or ""
            except Exception:
                order = ""
            return self._ok(t0, order)
        return ok, self.fast_stage, self.fast_detail, ""

    def _ok(self, t0: float, order_id: str) -> tuple[bool, str, str, str]:
        store = getattr(self, "store_used", "") or ""
        return True, "已创建待付款订单", (
            f"订单号 {order_id or '（页面未解析到，请看标签页）'}。"
            + (f"取货门店 {store}。" if store else "")
            + f"请在付款窗口内自己扫码支付（约 30 分钟，以页面倒计时为准）。"
              f"总耗时 {time.monotonic() - t0:.1f}s"
        ), order_id
