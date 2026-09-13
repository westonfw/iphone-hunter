"""挂到已登录的 Chrome，记录你在结账页的点击 / 输入框 id / 步骤 URL。

不记录身份证、密码、卡号的明文，只记控件 id 和值的长度。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from .autobuy import AutoBuyUnavailable, cdp_candidates, probe_cdp


def _p(*a) -> None:
    print(*a, flush=True)

TRACE = "checkout-trace.jsonl"

RECORDER_JS = r"""
() => {
    if (window.__hunterRec) return "already";
    window.__hunterRec = true;
    const send = (ev) => {
        ev.ts = Date.now();
        ev.url = location.href;
        try { ev.step = new URLSearchParams(location.search).get("_s"); } catch (e) { ev.step = null; }
        try { if (typeof hunterLog === "function") hunterLog(ev); } catch (e) {}
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
    const origFetch = window.fetch;
    window.fetch = function(input, init) {
        try {
            const url = String(typeof input === "string" ? input : (input && input.url) || "");
            if (/checkout|bagx|shop\//i.test(url)) {
                send({type: "fetch", method: (init && init.method) || "GET", req: url.slice(0, 220)});
            }
        } catch (err) {}
        return origFetch.apply(this, arguments);
    };
    const xo = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__hunterUrl = url;
        this.__hunterMethod = method;
        return xo.apply(this, arguments);
    };
    const xs = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function() {
        try {
            const url = String(this.__hunterUrl || "");
            if (/checkout|bagx|shop\//i.test(url)) {
                send({type: "xhr", method: this.__hunterMethod, req: url.slice(0, 220)});
            }
        } catch (err) {}
        return xs.apply(this, arguments);
    };
    return "injected";
}
"""


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

    with out.open("w", encoding="utf-8") as fp, sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(found)

        def on_log(source, ev):
            if isinstance(ev, dict):
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
                        _write(fp, json.loads(t[11:]))
                    except Exception:
                        pass

            def on_nav(frame):
                if frame != page.main_frame:
                    return
                _write(fp, {"type": "nav", "url": page.url})
                try:
                    page.evaluate(RECORDER_JS)
                except Exception:
                    pass

            page.on("console", on_console)
            page.on("framenavigated", on_nav)
            try:
                page.evaluate(RECORDER_JS)
            except Exception:
                pass
            _write(fp, {"type": "hook", "url": page.url})

        def hook_ctx(ctx):
            cid = id(ctx)
            if cid not in bound_ctx:
                bound_ctx.add(cid)
                try:
                    ctx.expose_binding("hunterLog", on_log)
                except Exception:
                    pass
                try:
                    ctx.add_init_script(f"({RECORDER_JS})()")
                except Exception:
                    pass
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
            start.goto(buy_url, wait_until="commit", timeout=45000)
            log(f"已打开购买页：{buy_url}")
        _write(fp, {"type": "session_start", "cdp": found, "start_url": start.url})
        n = len(hooked_pages)
        log(f"已挂上 {n} 个标签。可以开始操作了。")

        try:
            while True:
                sweep()
                time.sleep(0.4)
        except KeyboardInterrupt:
            _write(fp, {"type": "session_end"})
            log(f"\n已停止。轨迹在 {out}")
    return 0
