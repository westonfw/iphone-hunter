"""挂到已登录的 Chrome，记录你在结账页的点击 / 输入框 id / 步骤 URL / 表单提交。

**不记录任何字段的值**，只记字段名和值的长度。结账表单里有身份证、姓名、手机号，
记了值就是把这些明文写进仓库里的文件。唯一的例外是一小撮结构性字段（_s/_a/_m
之类的步骤标记），它们决定请求打到哪一步，而且本身不含个人信息——见 SAFE_KEYS。

为什么要记表单体
----------------
「结账向导能不能改成直接发包」这个问题，光看 URL 和 method 判断不了，得知道：
  1. 每一步 POST 的字段清单有多长、有没有藏着不可伪造的令牌
  2. 那些令牌是页面 JS 现算的（像加购的 atbtoken），还是 DOM 里就有的（像 x-aos-stk）
只有第 2 种能重放。跑一次 `hunter record` 走完人工结账，这份 trace 就能回答。
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from .autobuy import AutoBuyUnavailable, cdp_candidates, probe_cdp
from .checkout import goto_buy_page


def _p(*a) -> None:
    print(*a, flush=True)

TRACE = "checkout-trace.jsonl"

#: 允许连值一起记的字段名。只放决定「打到哪一步」的结构性参数——它们不含个人
#: 信息，而且不知道值就没法判断请求该怎么构造。其余一律只记名字和长度。
SAFE_KEYS = ("_s", "_a", "_m", "_n", "step", "syntax", "modelVersion", "purchaseOption")

RECORDER_JS = r"""
() => {
    // 用**每次运行唯一**的令牌，不是布尔量。
    // 布尔量踩过一次坑：上一轮录制的 init script 是通过 CDP 注册在浏览器端的，
    // 进程被杀了它还在。于是每次翻页旧录制器先跑、把标记置成 true 并绑到一个
    // 已经死掉的回传通道上；新录制器看到标记已存在就 return "already"，
    // 监听器根本没装。表面上钩子在、通道是 function，实际一条都录不到。
    if (window.__hunterRec === "__TOKEN__") return "already";
    window.__hunterRec = "__TOKEN__";
    const send = (ev) => {
        ev.ts = Date.now();
        ev.url = location.href;
        try { ev.step = new URLSearchParams(location.search).get("_s"); } catch (e) { ev.step = null; }
        // 通道名带令牌：同名的死绑定还在页面上时，别当成自己的
        try {
            const ch = window["__hunterLog__TOKEN__"];
            if (typeof ch === "function") { ch(ev); return; }
        } catch (e) {}
        console.log("HUNTER_REC " + JSON.stringify(ev));
    };
    const info = (el) => {
        if (!el || !el.getAttribute) return {tag: "?"};
        const text = ((el.innerText || el.getAttribute("aria-label") || "") + "")
            .replace(/\s+/g, " ").trim().slice(0, 80);
        return {
            tag: el.tagName,
            id: el.id || "",
            name: el.getAttribute("name") || "",
            autom: el.getAttribute("data-autom") || "",
            role: el.getAttribute("role") || "",
            type: el.getAttribute("type") || "",
            text,
        };
    };
    send({type: "ready", title: document.title});
    document.addEventListener("click", (e) => {
        const el = e.target.closest("button,a,label,input,select,[role=button],[role=radio],[role=tab],[data-autom]")
                || e.target;
        send({type: "click", ...info(el)});
    }, true);
    document.addEventListener("change", (e) => {
        const el = e.target;
        const v = String(el.value || "");
        send({type: "change", ...info(el), valueLen: v.length});
    }, true);
    // 把一组 key/value 压成「字段清单」：名字留着，值一律只留长度。
    // SAFE 里那几个结构性参数除外——不知道它们的值就判断不了请求打到哪一步。
    const SAFE = __SAFE_KEYS__;
    const inventory = (pairs) => pairs.map(([k, v]) => {
        const key = String(k);
        const val = String(v == null ? "" : v);
        return SAFE.includes(key)
            ? {k: key, v: val.slice(0, 40)}
            : {k: key, len: val.length};
    });
    const fromBody = (body) => {
        try {
            if (!body) return [];
            if (typeof body === "string") {
                if (body.trim().startsWith("{")) {
                    return inventory(Object.entries(JSON.parse(body)));
                }
                return inventory([...new URLSearchParams(body).entries()]);
            }
            if (typeof FormData !== "undefined" && body instanceof FormData) {
                return inventory([...body.entries()]);
            }
        } catch (e) {}
        return [{k: "<解析不了>", len: -1}];
    };

    // 表单提交：结账向导每一步大概率是整页 form POST，不是 XHR。
    // 不钩这个就只能看到一次 nav，看不到它带了什么过去。
    document.addEventListener("submit", (e) => {
        try {
            const f = e.target;
            if (!f || !f.tagName || f.tagName !== "FORM") return;
            const fd = new FormData(f);
            send({
                type: "submit",
                action: (f.getAttribute("action") || location.href).slice(0, 200),
                method: (f.getAttribute("method") || "GET").toUpperCase(),
                fields: inventory([...fd.entries()]),
            });
        } catch (err) {}
    }, true);

    const origFetch = window.fetch;
    window.fetch = function(input, init) {
        const url = String(typeof input === "string" ? input : (input && input.url) || "");
        let rec = null;
        try {
            if (/checkout|bagx|shop\//i.test(url)) {
                rec = {
                    type: "fetch",
                    method: (init && init.method) || "GET",
                    req: url.slice(0, 220),
                    body: fromBody(init && init.body),
                    // x-aos-stk 是能不能重放的关键：它在 DOM 里拿得到，
                    // 跟加购那个现算的 atbtoken 不是一回事
                    stk: !!(init && init.headers && (
                        (init.headers["x-aos-stk"]) ||
                        (typeof init.headers.get === "function" && init.headers.get("x-aos-stk")))),
                };
                send(rec);
            }
        } catch (err) {}
        const p = origFetch.apply(this, arguments);
        if (rec) {
            try {
                p.then((res) => send({type: "fetch_res", req: url.slice(0, 220),
                                      status: res.status, redirected: res.redirected,
                                      finalUrl: (res.url || "").slice(0, 200)}))
                 .catch(() => {});
            } catch (err) {}
        }
        return p;
    };
    const xo = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__hunterUrl = url;
        this.__hunterMethod = method;
        return xo.apply(this, arguments);
    };
    const xs = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function(body) {
        try {
            const url = String(this.__hunterUrl || "");
            if (/checkout|bagx|shop\//i.test(url)) {
                send({type: "xhr", method: this.__hunterMethod, req: url.slice(0, 220),
                      body: fromBody(body)});
                this.addEventListener("load", () => {
                    try { send({type: "xhr_res", req: url.slice(0, 220), status: this.status}); }
                    catch (e) {}
                });
            }
        } catch (err) {}
        return xs.apply(this, arguments);
    };
    return "injected";
}
"""


#: JS 里的白名单从 Python 这份生成，避免两处各写一份、改了一边忘了另一边。
RECORDER_JS = RECORDER_JS.replace(
    "__SAFE_KEYS__", json.dumps(list(SAFE_KEYS)))


def new_token() -> str:
    """每次录制用一个新令牌。见 RECORDER_JS 里防重入那段的说明。"""
    return uuid.uuid4().hex[:12]


def recorder_js(token: str) -> str:
    return RECORDER_JS.replace("__TOKEN__", token)


#: 只关心这些路径的请求。结账向导是 XHR 驱动的 SPA（2026-09-14 实测：Review
#: 页上唯一的 <form> 是站内搜索框），所以要抓的是 checkoutx/bagx 这一族接口。
#: 注意不能写成 "/shop/checkout/"：真实地址是 "/shop/checkout?_s=Review"，
#: 带问号不带斜杠，用斜杠版本一条都匹配不上。
WATCH_PATHS = ("/shop/checkoutx", "/shop/bagx", "/shop/checkout", "/shop/bag")


def body_inventory(raw: str | None) -> list[dict]:
    """把请求体压成字段清单：名字留着，值一律只留长度。

    SAFE_KEYS 里那几个结构性参数除外——不知道它们的值就判断不了请求打到哪一步。
    结账请求里有身份证、姓名、手机号，记了值就是把明文写进仓库里的文件。
    """
    if not raw:
        return []
    try:
        if raw.lstrip().startswith("{"):
            data = json.loads(raw)
            if isinstance(data, dict):
                return [{"k": k, "v": str(v)[:40]} if k in SAFE_KEYS
                        else {"k": k, "len": len(str(v))}
                        for k, v in data.items()]
        from urllib.parse import parse_qsl
        return [{"k": k, "v": v[:40]} if k in SAFE_KEYS else {"k": k, "len": len(v)}
                for k, v in parse_qsl(raw, keep_blank_values=True)]
    except Exception:
        return [{"k": "<解析不了>", "len": len(raw)}]


def _write(fp, obj: dict) -> None:
    obj.setdefault("wall", datetime.now().strftime("%H:%M:%S"))
    fp.write(json.dumps(obj, ensure_ascii=False) + "\n")
    fp.flush()


def run(cfg: dict, root: Path, buy_url: str = "", log=_p) -> int:
    ab = dict(cfg.get("autobuy") or {})
    out = root / TRACE
    found = None
    for url in cdp_candidates(ab.get("cdp_url", ""), int(ab.get("cdp_port", 9222))):
        info = probe_cdp(url)
        if info:
            found = url
            break
    if not found:
        raise AutoBuyUnavailable("没找到开着调试端口的 Chrome。先跑：python -m hunter connect --launch")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise AutoBuyUnavailable("没装 playwright") from e

    log(f"记录写入 {out}（本次覆盖旧文件）")
    log("请只在这个调试 Chrome 里操作。结账若弹出 secureN 新标签也会自动跟上。")
    log("走：加购 → 自提 → 身份证后四位 → 付款方式 → 检查订单 → Review。不必付款。")
    log("做完后回来说一声。身份证/密码不记明文。\n")

    hooked_pages: set[int] = set()
    bound_ctx: set[int] = set()
    token = new_token()
    js = recorder_js(token)
    binding = f"__hunterLog{token}"
    stats = {"events": 0, "binding_ok": 0, "binding_fail": 0}

    with out.open("w", encoding="utf-8") as fp, sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(found)

        def on_log(source, ev):
            if isinstance(ev, dict):
                stats["events"] += 1
                _write(fp, ev)

        def hook_page(page):
            pid = id(page)
            if pid in hooked_pages:
                return
            hooked_pages.add(pid)

            def on_console(msg):
                t = msg.text
                if t.startswith("HUNTER_REC "):
                    try:
                        stats["events"] += 1
                        _write(fp, json.loads(t[11:]))
                    except Exception:
                        pass

            def on_nav(frame):
                if frame != page.main_frame:
                    return
                _write(fp, {"type": "nav", "url": page.url})
                try:
                    page.evaluate(js)
                except Exception:
                    pass

            def on_request(req):
                try:
                    if not any(k in req.url for k in WATCH_PATHS):
                        return
                    if req.resource_type in ("image", "stylesheet", "font", "media"):
                        return
                    h = req.headers or {}
                    stats["events"] += 1
                    _write(fp, {
                        "type": "req",
                        "method": req.method,
                        "url": req.url[:220],
                        "body": body_inventory(req.post_data),
                        # x-aos-stk 是「能不能重放」的关键：DOM 里读得到的才可复用，
                        # 跟加购那个由 JS 现算的 atbtoken 不是一回事
                        "stk": bool(h.get("x-aos-stk")),
                        "xhr": req.resource_type,
                    })
                except Exception:
                    pass

            def on_response(res):
                try:
                    if not any(k in res.url for k in WATCH_PATHS):
                        return
                    if res.status == 200 and "/shop/checkoutx/" not in res.url:
                        return          # 只有结账那族才逐条记成功的，否则太吵
                    stats["events"] += 1
                    _write(fp, {"type": "res", "status": res.status,
                                "url": res.url[:220]})
                except Exception:
                    pass

            page.on("request", on_request)
            page.on("response", on_response)
            page.on("console", on_console)
            page.on("framenavigated", on_nav)
            injected = ""
            try:
                injected = page.evaluate(js)
            except Exception as e:
                injected = f"失败：{type(e).__name__}"
            _write(fp, {"type": "hook", "url": page.url, "injected": injected})

        def hook_ctx(ctx):
            cid = id(ctx)
            if cid not in bound_ctx:
                bound_ctx.add(cid)
                # 绑定失败必须说出来。上一次就是被 `except: pass` 藏住的：
                # 通道其实是上一轮留下的死绑定，表面一切正常，一条都没录到。
                try:
                    ctx.expose_binding(binding, on_log)
                    stats["binding_ok"] += 1
                except Exception as e:
                    stats["binding_fail"] += 1
                    log(f"⚠️ 回传通道注册失败（会退回 console 捕获）：{type(e).__name__}: {str(e)[:70]}")
                try:
                    ctx.add_init_script(f"({js})()")
                except Exception as e:
                    log(f"⚠️ 翻页自动注入注册失败：{type(e).__name__}: {str(e)[:70]}")
                ctx.on("page", hook_page)
            for p in list(ctx.pages):
                hook_page(p)

        def sweep():
            for ctx in list(browser.contexts):
                hook_ctx(ctx)

        sweep()
        start = None
        for ctx in browser.contexts:
            start = ctx.new_page()
            break
        if start is None:
            start = browser.new_context().new_page()
        hook_page(start)
        if buy_url:
            if goto_buy_page(start, buy_url, log=log):
                log(f"已打开购买页：{start.url}")
            else:
                log(f"⚠️ 购买页打不开（一直落到 /shop/404），请自己导航：{buy_url}")
        _write(fp, {"type": "session_start", "cdp": found, "start_url": start.url})
        n = len(hooked_pages)
        log(f"已挂上 {n} 个标签。可以开始操作了。")

        try:
            while True:
                sweep()
                time.sleep(0.4)
        except KeyboardInterrupt:
            _write(fp, {"type": "session_end", **stats})
            log(f"\n已停止。共记录 {stats['events']} 条事件，轨迹在 {out}")
            if stats["events"] == 0:
                log("⚠️ 一条事件都没录到——钩子没真正生效，别拿这份轨迹做判断。")
    return 0
