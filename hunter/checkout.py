"""在已登录会话里创建待付款订单。

中国大陆到店取货结账向导（以 2026-08 实测为准）：

  Fulfillment-init   选自提 + 门店
  PickupContact-init 填身份证后四位（姓名电话通常已从 Apple ID 带出）
  Billing            选支付宝/微信
  检查订单           进入 Review
  Review             确认下单 → 待付款/扫码

加购必须在预热页点（atbtoken）。结账优先直跳 secure8，再按 _s= 逐步走。
绝不代付：不填支付密码、不确认 Apple Pay；信用卡路径上不点确认下单。
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

SECURE_CHECKOUT = {
    "cn": "https://secure8.www.apple.com.cn/shop/checkout?_s=Fulfillment-init",
}

SCAN_PAY = ("支付宝", "微信", "花呗", "微信支付", "掌上生活")
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
    const unpaid = /等待付款|待付款|请在.{0,20}内完成付款|扫描二维码支付|掌上生活/.test(body);
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

JS_SET_VALUE = r"""
([id, value]) => {
    const el = document.getElementById(id);
    if (!el) return {ok: false, reason: "missing"};
    const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
    if (proto && proto.set) proto.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
    return {ok: true, value: String(el.value || "").length};
}
"""

JS_CLICK_ID = r"""
([id]) => {
    const el = document.getElementById(id);
    if (!el) return {ok: false, reason: "missing"};
    if (el.disabled) return {ok: false, reason: "disabled", text: (el.innerText || "").slice(0, 24)};
    el.click();
    return {ok: true, text: ((el.innerText || el.getAttribute("aria-label") || id) + "").replace(/\s+/g, " ").trim().slice(0, 40)};
}
"""

JS_CLICK_TEXT = r"""
([needles]) => {
    const blob = (el) => ((el.innerText || "") + " " + (el.getAttribute("aria-label") || "")).replace(/\s+/g, " ").trim();
    const nodes = document.querySelectorAll(
        "button, a, label, input, [role=button], [role=radio], [role=tab], [data-autom]");
    for (const el of nodes) {
        const t = blob(el);
        if (!t) continue;
        if (needles.some((n) => t.includes(n))) {
            el.click();
            return {ok: true, text: t.slice(0, 48)};
        }
    }
    return {ok: false, text: ""};
}
"""


def checkout_url(region: str = "cn") -> str:
    return SECURE_CHECKOUT.get(region, SECURE_CHECKOUT["cn"])


def step_key(url: str) -> str:
    raw = (parse_qs(urlparse(url or "").query).get("_s") or [""])[0]
    raw = unquote(raw)
    return raw.split("-")[0].lower() if raw else ""


def snapshot(page) -> dict:
    try:
        s = page.evaluate(JS_SNAPSHOT) or {}
    except Exception as e:
        s = {"error": str(e)[:80], "url": getattr(page, "url", "")}
    s["key"] = step_key(s.get("url") or getattr(page, "url", ""))
    return s


def click_id(page, elem_id: str) -> str:
    try:
        r = page.evaluate(JS_CLICK_ID, elem_id)
    except Exception:
        return ""
    return (r or {}).get("text", "") if (r or {}).get("ok") else ""


def click_text(page, *needles: str) -> str:
    try:
        r = page.evaluate(JS_CLICK_TEXT, list(needles))
    except Exception:
        return ""
    return (r or {}).get("text", "") if (r or {}).get("ok") else ""


def set_input(page, elem_id: str, value: str) -> bool:
    if not value:
        return False
    try:
        r = page.evaluate(JS_SET_VALUE, [elem_id, value])
        return bool((r or {}).get("ok"))
    except Exception:
        return False


def click_continue(page) -> str:
    hit = click_id(page, CONTINUE_BTN)
    if hit:
        return hit
    return click_text(page, "检查订单", "现在下单", "确认下单", "继续")


def try_bag_checkout_xhr(page) -> dict:
    try:
        return page.evaluate(JS_AOS_POST, list(BAG_CHECKOUT_TRIES)) or {}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}


def looks_unpaid(snap: dict) -> bool:
    if snap.get("unpaid") or snap.get("thank"):
        return True
    if snap.get("order") and snap.get("key") in ("", "thankyou"):
        return True
    return False


def card_would_charge(snap: dict, want: str) -> bool:
    if want in SCAN_PAY:
        return False
    text = snap.get("compact") or ""
    if any(p in text for p in SCAN_PAY):
        return False
    return any(p in text for p in CARD_PAY)


class OrderPlacer:
    def __init__(self, *, region: str = "cn", pickup_store: str = "",
                 payment: str = "支付宝", delivery: str = "pickup",
                 id_last4: str = "", last_name: str = "", first_name: str = "",
                 email: str = "", phone: str = "",
                 timeout_ms: int = 30000, log=print):
        self.region = region
        self.pickup_store = (pickup_store or "").strip()
        self.payment = (payment or "支付宝").strip() or "支付宝"
        self.delivery = delivery if delivery in ("pickup", "shipping") else "pickup"
        self.id_last4 = re.sub(r"\s+", "", id_last4 or "").upper()
        self.last_name = (last_name or "").strip()
        self.first_name = (first_name or "").strip()
        self.email = (email or "").strip()
        self.phone = (phone or "").strip()
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

        url = checkout_url(self.region)
        self.log(f"[下单] 直跳结账（不加载购物袋页）：{url}")
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
                self.log(f"[下单] 直跳结账失败：{e}")
            return page

    def place(self, page, t0: float) -> tuple[bool, str, str, str]:
        deadline = time.monotonic() + max(90.0, self.timeout_ms / 1000 * 2)
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
            elif key == "pickupcontact":
                ok, last = self._step_contact(page, snap)
                if not ok:
                    return False, "⚠️ 卡在取货人信息", last, ""
            elif key in ("billing", "invoice"):
                last = self._step_billing(page, snap)
                if last.startswith("⚠️"):
                    return False, last, "请改选支付宝或微信后再点检查订单。", ""
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
        if self.delivery == "pickup":
            click_text(page, "到店取货", "零售店取货", "门店取货", "自提")
            if self.pickup_store:
                store = click_text(page, self.pickup_store)
                if store:
                    page.wait_for_timeout(400)
        hit = click_continue(page)
        return f"自提/门店后点了「{hit or snap.get('continueText') or '继续'}」"

    def _step_contact(self, page, snap: dict) -> tuple[bool, str]:
        if snap.get("hasIdField") and not snap.get("idFilled"):
            if not re.fullmatch(r"[0-9]{3}[0-9X]", self.id_last4):
                return False, (
                    "取货需要身份证后四位。在 config.json 的 autobuy.id_last4 填 4 位"
                    "（最后一位可以是 X），再跑一次。"
                )
            if not set_input(page, ID_NATIONAL, self.id_last4):
                return False, "找到了身份证后四位输入框，但没能填进去，请在页面上手动填。"
            self.log("[下单] 已填身份证后四位")

        if self.last_name:
            set_input(page, ID_LAST_NAME, self.last_name)
        if self.first_name:
            set_input(page, ID_FIRST_NAME, self.first_name)
        if self.email:
            set_input(page, ID_EMAIL, self.email)
        if self.phone:
            set_input(page, ID_PHONE, self.phone)

        page.wait_for_timeout(300)
        hit = click_continue(page)
        if not hit:
            snap2 = snapshot(page)
            if snap2.get("continueEnabled") is False:
                return False, (
                    "「继续」是灰的：姓名/电话/身份证后四位可能没填全。"
                    "可在 autobuy 里补 pickup_last_name / pickup_first_name / pickup_phone。"
                )
        return True, f"取货人信息后点了「{hit or '继续'}」"

    def _step_billing(self, page, snap: dict) -> str:
        ids = PAY_IDS.get(self.payment) or PAY_IDS["支付宝"]
        paid = ""
        for i in ids:
            paid = click_id(page, i)
            if paid:
                break
        if not paid:
            paid = click_text(page, self.payment, *SCAN_PAY)
        if paid:
            self.log(f"[下单] 支付方式：{paid}")
            page.wait_for_timeout(300)
        if card_would_charge(snapshot(page), self.payment):
            return "⚠️ 停在付款页（信用卡会立刻扣款）"
        hit = click_continue(page) or click_text(page, "检查订单")
        return f"付款方式后点了「{hit or '检查订单'}」"

    def _step_review(self, page, snap: dict) -> str:
        if card_would_charge(snap, self.payment):
            return "⚠️ 停在下单页（信用卡会立刻扣款）"
        hit = click_continue(page) or click_text(page, "现在下单", "确认下单")
        return f"Review 确认下单：{hit or '已点'}"

    def _step_unknown(self, page, snap: dict, key: str) -> str:
        if snap.get("hasCheckOrder"):
            hit = click_text(page, "检查订单") or click_continue(page)
            return f"未知步骤 {key or '?'}，点了检查订单：{hit}"
        if snap.get("hasPlace"):
            hit = click_text(page, "现在下单", "确认下单") or click_continue(page)
            return f"未知步骤 {key or '?'}，点了确认下单：{hit}"
        hit = click_continue(page)
        return f"未知步骤 {key or '?'}，点了继续：{hit or '无按钮'}"
