"""会话探针：把「登录态到底能挂多久」从猜测变成一张表。

为什么要有这个东西
------------------
捡漏场景要把监控挂上几天，而放货那一刻才发现会话掉了就全完了。但「会话
多久掉、靠什么能续」此前全是推测。2026-09-14 用 CDP 实测了一轮，结论是
**不同 cookie 的续期条件完全不一样**：

  as_dc      路由（分到哪台 secureN），2h TTL —— 任何一个请求都能续，XHR 就行
  as_sfa     店面会话标识，180d       —— 只有**真实导航 + 跑 JS** 才续；
                                         把 259KB 的 HTML 整个 fetch 下来都不算
  shld_bt_*  反爬 shield，20~35min    —— 上面两种都续不动，靠什么续**未知**
  DES…       idmsa 认证，15d          —— 真正的「登录态」，比想象中耐命

最后那个 shld_bt_* 就是这个探针要量的东西：它到期之后是自动重签，还是
就此失效、把你卡在结账门口。

设计上的一条线
--------------
**采样本身零请求**：cookie 是从 CDP 的 cookie jar 直接读的，不碰 Apple。
所以「自然衰减」能干干净净地测出来——只有 --act 指定了动作才会发请求。
把观测和干预分开，测出来的才是 Apple 的行为，不是探针自己的行为。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import PY_CMD
from .autobuy import cdp_candidates, probe_cdp
from .checkout import login_state

#: 要盯的 cookie 和它们各自的角色。key 是 cookie 名，value 是人话解释。
#: 不在这张表里的 cookie 也会记个数，但不逐个展开——jsonl 会胖得没法看。
ROLES = {
    "as_dc":      "路由(secureN)",
    "as_sfa":     "店面会话",
    "as_atb":     "加购令牌相关",
    "shld_bt_m":  "反爬shield",
    "shld_bt_ck": "反爬shield",
    "as_pcts":    "店面会话(session)",
    "aasp":       "idmsa会话",
    "acn01":      "账号标识",
}

#: idmsa 的认证 cookie 名字是带哈希后缀的（DES5059e1bc…），只能按前缀认。
_AUTH_PREFIX = re.compile(r"^DES[0-9a-f]{8,}$", re.I)

_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*$", re.I)


def parse_duration(text: str, default: float = 300.0) -> float:
    """把 "90s" / "5m" / "2h" 解析成秒。裸数字按分钟算（探针的自然单位）。"""
    if text is None or text == "":
        return default
    m = _DUR.match(str(text))
    if not m:
        raise ValueError(f"时长格式不对：{text!r}，应该像 30s / 5m / 2h")
    n, unit = float(m.group(1)), (m.group(2) or "m").lower()
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


@dataclass
class Sample:
    """一次采样。cookies 里只放到期时间和值长度，**绝不放值本身**——
    那些等同于凭证，落盘就是把账号写进日志里。"""

    ts: float
    elapsed_s: float
    act: str = "none"
    cookies: dict = field(default_factory=dict)
    auth_cookie: dict | None = None
    other_count: int = 0
    signed_in: bool | None = None
    evidence: str = ""
    secure_host: str = ""
    act_result: dict = field(default_factory=dict)
    gone: list = field(default_factory=list)
    reissued: list = field(default_factory=list)

    def as_json(self) -> dict:
        d = {
            "ts": datetime.fromtimestamp(self.ts).isoformat(timespec="seconds"),
            "elapsed_min": round(self.elapsed_s / 60, 1),
            "act": self.act,
            "cookies": self.cookies,
            "others": self.other_count,
        }
        if self.auth_cookie:
            d["auth"] = self.auth_cookie
        if self.signed_in is not None:
            d["signed_in"] = self.signed_in
        if self.secure_host:
            d["secure_host"] = self.secure_host
        if self.evidence:
            d["evidence"] = self.evidence
        if self.act_result:
            d["act_result"] = self.act_result
        if self.gone:
            d["gone"] = self.gone
        if self.reissued:
            d["reissued"] = self.reissued
        return d


def read_jar(ctx, now: float | None = None) -> tuple[dict, dict | None, int]:
    """读 cookie jar。零请求。

    返回 (关注的cookie, 认证cookie, 其它apple cookie数量)。
    注意 Playwright 不会返回已过期的 cookie——所以某个名字**从表里消失**
    本身就是信号：它到期了而且没人给它续。
    """
    now = now or time.time()
    watched: dict = {}
    auth = None
    others = 0
    for c in ctx.cookies():
        dom = c.get("domain", "")
        if "apple" not in dom:
            continue
        name = c["name"]
        exp = c.get("expires", -1) or -1
        rec = {
            "exp": round(exp, 0) if exp > 0 else None,
            "left_s": round(exp - now) if exp > 0 else None,
            "vlen": len(c.get("value", "")),
            "domain": dom,
        }
        if name in ROLES:
            rec["role"] = ROLES[name]
            watched[name] = rec
        elif _AUTH_PREFIX.match(name):
            rec["role"] = "idmsa认证"
            auth = {"name": name[:6] + "…", **rec}
        else:
            others += 1
    return watched, auth, others


#: 登录探测的代价是一次真实导航，它**必然会顺带续上 as_dc 和 as_sfa**。
#: 这一点不藏着——每次探测都在样本里记成一次干预（act 带 +login），否则读数据
#: 的人会把探针自己造成的续期当成 Apple 的自然行为。判据本身在 checkout.py，
#: 跟自动下单共用一套，免得两边各写一份、改了一边忘了另一边。


#: 保活动作。项目本来就在打 availability-message，加这一个请求不改变流量特征。
JS_XHR = r"""
async () => {
    const t = Date.now();
    const res = await fetch('/shop/sba/availability-message?parts.0=MJY64CH/A',
                            {credentials: 'include'});
    const txt = await res.text();
    return {kind: 'xhr', status: res.status, bytes: txt.length, ms: Date.now() - t};
}
"""


class SessionProbe:
    """挂到已登录的 Chrome 上，按固定节奏记录会话状态。

    act 决定每次采样**之后**做什么动作（先观测再干预，顺序不能反，
    否则记下来的就是动作后的状态、看不到自然衰减）：
        none  什么都不做——纯衰减曲线，零请求
        xhr   发一个 API 请求——测「轻动作」够不够
        nav   真实导航并跑 JS——测「重动作」能续到什么程度
    """

    KEEPALIVE_URL = "https://www.apple.com.cn/shop/bag"

    def __init__(self, cdp_url: str = "", cdp_port: int = 9222, *,
                 every: float = 300.0, act: str = "none",
                 login_every: float = 0.0, hours: float = 0.0,
                 notifier=None, sink=None, log=print):
        self.cdp_url = cdp_url
        self.cdp_port = cdp_port
        self.every = max(30.0, every)
        self.act = act if act in ("none", "xhr", "nav") else "none"
        self.login_every = max(0.0, login_every)
        self.hours = max(0.0, hours)
        self.notifier = notifier
        self.sink = sink          # 可调用对象，收 dict，负责落 jsonl
        self.log = log
        self.t0 = 0.0
        self._prev: dict = {}
        self._last_login = 0.0
        self._warned_signout = False

    # ---------- 连接 ----------

    def _attach(self, pw):
        for url in cdp_candidates(self.cdp_url, self.cdp_port):
            if probe_cdp(url):
                b = pw.chromium.connect_over_cdp(url)
                ctx = b.contexts[0] if b.contexts else b.new_context()
                self.log(f"[探针] 已挂到 {url}")
                return ctx
        raise SystemExit(
            f"没找到开着调试端口的 Chrome（试过 "
            f"{'、'.join(cdp_candidates(self.cdp_url, self.cdp_port))}）。\n"
            f"    先跑：{PY_CMD} -m hunter connect --launch")

    def _page(self, ctx):
        """找一个 apple.com.cn 上的标签页来发同源请求。没有就开一个。"""
        for p in ctx.pages:
            try:
                if "apple.com" in (p.url or ""):
                    return p
            except Exception:
                continue
        p = ctx.new_page()
        p.goto(self.KEEPALIVE_URL, wait_until="domcontentloaded", timeout=30000)
        return p

    # ---------- 一次采样 ----------

    def sample(self, ctx, page) -> Sample:
        now = time.time()
        watched, auth, others = read_jar(ctx, now)
        s = Sample(ts=now, elapsed_s=now - self.t0, act=self.act,
                   cookies=watched, auth_cookie=auth, other_count=others)

        # 跟上一次比：谁没了（到期且无人续），谁被重签了（值变了）
        for name, prev in self._prev.items():
            cur = watched.get(name)
            if cur is None:
                s.gone.append(name)
            elif cur["vlen"] != prev["vlen"] or (
                    cur.get("exp") and prev.get("exp") and cur["exp"] > prev["exp"] + 2):
                s.reissued.append({
                    "name": name,
                    "exp_delta_s": round((cur.get("exp") or 0) - (prev.get("exp") or 0)),
                    "vlen": [prev["vlen"], cur["vlen"]],
                })
        self._prev = watched

        # 登录态要真实导航才测得准，按自己的节奏来，别每次采样都打
        if self.login_every and (now - self._last_login >= self.login_every
                                 or self._last_login == 0.0):
            self._last_login = now
            s.act = f"{self.act}+login"      # 如实标注：这一轮有额外干预
            try:
                page.goto(self.KEEPALIVE_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3500)
                r = login_state(page)
                s.signed_in = r["signed_in"]
                if r["secure_host"]:
                    s.secure_host = r["secure_host"]
                s.evidence = r["evidence"]
            except Exception as e:
                s.evidence = f"登录探测失败：{type(e).__name__}: {str(e)[:60]}"
        return s

    def act_now(self, page) -> dict:
        """采样之后做保活动作。失败不致命，如实记下来。"""
        if self.act == "none":
            return {}
        try:
            if self.act == "xhr":
                return page.evaluate(JS_XHR)
            t = time.time()
            page.goto(self.KEEPALIVE_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3500)     # 等页面 JS 跑起来，shield 才有机会重签
            return {"kind": "nav", "url": page.url[:80],
                    "ms": round((time.time() - t) * 1000)}
        except Exception as e:
            return {"kind": self.act, "error": f"{type(e).__name__}: {str(e)[:60]}"}

    # ---------- 输出 ----------

    def describe(self, s: Sample) -> str:
        bits = []
        for name in ("as_dc", "as_sfa", "shld_bt_m", "shld_bt_ck"):
            c = s.cookies.get(name)
            if c is None:
                bits.append(f"{name}=已消失")
            elif c.get("left_s") is None:
                bits.append(f"{name}=session")
            else:
                bits.append(f"{name}={c['left_s'] / 60:.0f}min")
        line = f"T+{s.elapsed_s / 60:>5.0f}min  " + "  ".join(bits)
        if s.signed_in is not None:
            line += f"  登录={'是' if s.signed_in else '否'}"
        if s.secure_host:
            line += f"({s.secure_host})"
        if s.gone:
            line += f"  ⚠️ 消失: {','.join(s.gone)}"
        if s.reissued:
            line += "  ♻️ 重签: " + ",".join(
                f"{r['name']}({r['exp_delta_s']:+}s)" for r in s.reissued)
        return line

    def _alert(self, s: Sample) -> None:
        """只在**状态翻转**时叫一次，不是每轮都叫——否则就成了噪音，真掉线时反而被忽略。"""
        if not self.notifier:
            return
        if s.signed_in is False and not self._warned_signout:
            self._warned_signout = True
            self.notifier.send(
                "⚠️ Apple 登录态掉了",
                f"挂了 {s.elapsed_s / 3600:.1f} 小时后检测到登出。"
                f"现在去 Chrome 里重新登录一次——趁还没放货，有时间慢慢处理双重认证。",
                critical=True)
        elif s.signed_in:
            self._warned_signout = False

    # ---------- 主循环 ----------

    def run(self) -> int:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SystemExit(f"没装 playwright。用虚拟环境跑：{PY_CMD} -m hunter ...")

        self.t0 = time.time()
        deadline = self.t0 + self.hours * 3600 if self.hours else 0.0
        self.log(f"[探针] 采样间隔 {self.every / 60:.1f}min，动作={self.act}，"
                 f"登录探测={'关' if not self.login_every else f'每 {self.login_every / 60:.0f}min'}，"
                 f"时长={'不限' if not deadline else f'{self.hours}h'}")
        self.log("[探针] 采样本身零请求；只有 act/登录探测会碰 Apple。Ctrl+C 结束。\n")

        n = 0
        with sync_playwright() as pw:
            ctx = self._attach(pw)
            page = self._page(ctx)
            while True:
                n += 1
                s = self.sample(ctx, page)
                s.act_result = self.act_now(page)
                self.log(self.describe(s))
                if self.sink:
                    self.sink(s.as_json())
                self._alert(s)
                if deadline and time.time() >= deadline:
                    self.log(f"\n[探针] 到时收工，共 {n} 次采样。")
                    return 0
                time.sleep(self.every)
