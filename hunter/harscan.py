"""解析 Chrome 导出的 HAR，输出结账链路的请求清单。

为什么不用自己写的录制器
------------------------
`hunter record` 靠 Playwright 的 connect_over_cdp 挂到你自己的 Chrome 上。实测
（2026-09-14）这条路在真实使用中会**静默卡死**：进程活着、日志正常、CPU 时间
却是 0，因为它阻塞在一个等不到响应的 CDP 调用上，一条事件都录不到。而且只要
有第二个 Playwright 客户端连同一个端点，两边的连接会互相搅乱。

Chrome 自己的 Network 面板没有这些问题——它就是浏览器的一部分，不跟任何人抢
CDP。导出 HAR 再离线解析，比在活会话里跟 CDP 搏斗可靠得多。

隐私
----
**HAR 里有 cookie、身份证、手机号的明文。** 这个模块只输出字段名和值的长度，
绝不把值打出来或落盘（SAFE_KEYS 那几个结构性参数除外，它们不含个人信息）。
看完请自己删掉 HAR 文件。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .record import SAFE_KEYS, WATCH_PATHS, body_inventory

#: 这些请求头决定「能不能重放」，所以要看它在不在——但**不打印值**。
TOKEN_HEADERS = ("x-aos-stk", "x-aos-model-page", "x-requested-with",
                 "syntax", "modelversion")


def _fmt_fields(fields: list[dict]) -> str:
    out = []
    for f in fields:
        out.append(f"{f['k']}={f['v']}" if "v" in f else f"{f['k']}(len {f['len']})")
    return ", ".join(out) if out else "（无请求体）"


def scan(path: Path, log=print) -> int:
    try:
        har = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"找不到文件：{path}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"不是合法的 HAR（JSON 解析失败）：{e}")

    entries = (har.get("log") or {}).get("entries") or []
    log(f"HAR 共 {len(entries)} 条请求，筛出结账链路的：\n")

    hits, kinds = 0, Counter()
    for e in entries:
        req = e.get("request") or {}
        url = req.get("url") or ""
        if not any(k in url for k in WATCH_PATHS):
            continue
        # 静态资源不看
        if any(url.split("?")[0].endswith(x) for x in
               (".js", ".css", ".png", ".jpg", ".svg", ".woff", ".woff2", ".ico")):
            continue
        hits += 1
        status = (e.get("response") or {}).get("status")
        method = req.get("method", "?")
        kinds[f"{method} {status}"] += 1

        heads = {h.get("name", "").lower(): h.get("value", "")
                 for h in (req.get("headers") or [])}
        present = [h for h in TOKEN_HEADERS if heads.get(h)]

        post = req.get("postData") or {}
        raw = post.get("text") or ""
        fields = body_inventory(raw)
        if not fields and post.get("params"):
            fields = [{"k": p.get("name", "?"), "v": str(p.get("value", ""))[:40]}
                      if p.get("name") in SAFE_KEYS
                      else {"k": p.get("name", "?"), "len": len(str(p.get("value", "")))}
                      for p in post["params"]]

        log(f"[{hits}] {method} {status}  {url[:110]}")
        if present:
            log(f"     令牌头: {', '.join(present)}")     # 只报有没有，不打印值
        log(f"     请求体: {_fmt_fields(fields)}")
        log("")

    log(f"命中 {hits} 条。分布：{dict(kinds)}")
    if not hits:
        log("⚠️ 一条都没命中。确认导出时勾了 Preserve log，而且走的是结账流程。")
    else:
        log("\n判断依据：请求体里除了 _a/_m 这类结构性参数和 x-aos-stk，")
        log("如果没有别的来路不明的长令牌，结账就能改成发包。")
    log("\n⚠️ HAR 里有 cookie 和身份证明文，看完请删掉它。")
    return 0
