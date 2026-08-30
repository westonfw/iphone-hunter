"""半自动下单：把结账前的操作全部代劳，只把「付款」那一下留给你。

为什么需要它
------------
从「有货」到「付款成功」的时间账：轮询延迟 2.5s + 推送送达 1-5s +
你看到手机 5-60s + 手动选配置结账 60-90s ≈ 70-150 秒。
补货窗口常常只有几十秒，所以瓶颈根本不在监控，在最后那 90 秒。

这个模块把 90 秒压到接近 0：检测到有货时，在你**已经登录好的**浏览器里
自动完成「打开机型页 → 选掉必选项 → 加入购物袋 → 进入结账」，然后停下来
响铃叫你，你只需要确认付款。

边界（硬性）
-----------
**绝不提交付款。** 流程停在结账/付款页就交还给你。这不是可配置项。
原因有二：付款必须由你本人确认；Apple 结算页有风控，脚本化提交容易触发。

必选项是怎么回事
---------------
SKU 深链只能带出颜色和容量，页面上「折抵换购」和「AppleCare+」两组单选
不选，「添加到购物袋」按钮会一直是 disabled——实测就是这样。这两组共约
6 次点击，正是抢购时最容易手忙脚乱的地方，所以交给脚本。
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CDP_PORT = 9222

# 这些 data-autom 是 Apple 自己的自动化测试钩子，比 class 名稳定得多，
# 也不受界面语言影响。
SEL_ADD_TO_CART = '[data-autom="add-to-cart"]'
SEL_SECTION = '[data-analytics-section="{}"]'

# 购物袋页进入结账的按钮，Apple 改过几次名字，按顺序试。
# Apple 对 iPhone 有限购（实测「每名顾客最多可购买 2 个 iPhone 17」）。
# 袋里超量时点结账既不跳转也不报错，只是静默不动——必须主动识别。
LIMIT_PATTERNS = ("最多可购买", "请调整订单", "超出购买上限", "限购", "购买数量")

CHECKOUT_SELECTORS = [
    '[data-autom="checkout"]',
    '[data-autom="proceed"]',
    '[data-autom="bagCheckoutButton"]',
    'button:has-text("结账")',
    'a:has-text("结账")',
]


def _is_sign_in(url: str) -> bool:
    """Apple 未登录时结账会被重定向到 signIn 页。"""
    u = (url or "").lower()
    return "/signin" in u or "idmsa.apple.com" in u


class AutoBuyUnavailable(RuntimeError):
    """没装 playwright 或浏览器起不来。"""


@dataclass
class BuyResult:
    ok: bool
    stage: str          # 走到了哪一步
    url: str = ""
    detail: str = ""


def cdp_candidates(configured: str = "", port: int = DEFAULT_CDP_PORT) -> list[str]:
    """可能的调试端口地址，按优先级排。

    WSL 里要分两种情况：Linux 侧的 Chrome 走 127.0.0.1；Windows 侧的 Chrome
    要走 WSL 看到的 Windows 主机 IP（除非开了 mirrored 网络模式，那时
    127.0.0.1 也通）。两个都试一遍，谁应答用谁。
    """
    out = []
    if configured:
        out.append(configured.rstrip("/"))
    out.append(f"http://127.0.0.1:{port}")
    try:
        # WSL 默认网关就是 Windows 主机
        r = subprocess.run(["ip", "route", "show", "default"],
                           capture_output=True, text=True, timeout=5)
        for tok in r.stdout.split():
            if tok.count(".") == 3 and tok[0].isdigit():
                out.append(f"http://{tok}:{port}")
                break
    except Exception:
        pass
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u); uniq.append(u)
    return uniq


def probe_cdp(url: str, timeout: float = 1.5) -> dict | None:
    """探一个调试端口，通了就返回浏览器信息。"""
    try:
        with urllib.request.urlopen(f"{url}/json/version", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


class AutoBuy:
    """两种连接方式：

    cdp     —— 挂到你**正在跑的** Chrome 上。你的 Apple ID 登录态、收货地址、
               支付方式原样可用，本工具不读取也不复制你的 profile 目录。
               需要 Chrome 带 --remote-debugging-port=9222 启动。
    profile —— 退而求其次：用一个独立的 profile 目录，你在里面单独登录一次。

    mode=auto（默认）先探 CDP，探不到再退回 profile。
    """

    def __init__(self, cfg: dict, root: Path, log=print):
        self.cfg = cfg or {}
        self.log = log
        self.root = root
        self.mode = self.cfg.get("mode", "auto")
        self.cdp_url = self.cfg.get("cdp_url", "")
        self.cdp_port = int(self.cfg.get("cdp_port", DEFAULT_CDP_PORT))
        self.profile = root / self.cfg.get("profile_dir", ".browser-profile")
        self.headless = bool(self.cfg.get("headless", False))
        self.trade_in_text = self.cfg.get("trade_in", "不折抵")
        self.applecare_text = self.cfg.get("applecare", "不加 AppleCare")
        self.timeout = int(self.cfg.get("timeout_ms", 30000))
        # 到店取货：只做「尽力尝试 + 大声提醒」，见 _choose_pickup 的说明
        self.pickup_store_name = (self.cfg.get("pickup_store_name") or "").strip()
        # 已经加进购物袋了就不能盲目重试，否则会重复下单
        self.added_to_bag = False
        # 预热用的常驻会话
        self._pwctx = self._pw = self._ctx = self._page = None
        self._attached = False
        self.warmed = False
        # 预热体检发现购物袋超限时置位：超限还继续加购只会让情况更糟
        self.bag_over_limit = ""
        # 加购前先清空购物袋。Apple 限购（iPhone 每人 2 台），袋里有存货
        # 会让新加的这台结不了账，而且 Apple 不弹错、只是静默卡住。
        self.clear_bag = bool(self.cfg.get("clear_bag_before_add", True))

    def find_cdp(self) -> tuple[str, dict] | None:
        for url in cdp_candidates(self.cdp_url, self.cdp_port):
            info = probe_cdp(url)
            if info:
                return url, info
        return None

    # ---------- 对外 ----------

    # ---------- 预热：开卖前就把页面备好 ----------

    def start(self) -> None:
        """建立常驻的浏览器连接，供预热使用。"""
        if self._pw is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise AutoBuyUnavailable(
                "没装 playwright。用项目自带的虚拟环境跑：.venv/bin/python -m hunter ..."
            ) from e
        self._pwctx = sync_playwright()
        self._pw = self._pwctx.__enter__()
        self._ctx, self._attached = self._launch(self._pw)

    def stop(self) -> None:
        if self._pwctx is not None:
            try:
                self._pwctx.__exit__(None, None, None)
            except Exception:
                pass
        self._pwctx = self._pw = self._ctx = self._page = None
        self.warmed = False

    def warm(self, url: str) -> str:
        """开卖前把产品页加载好、必选项选好，标签页一直留着。

        这才是慢网下真正的解法：放货那一刻不需要再加载 700KB 的产品页、
        不需要等 React 渲染、不需要点两个单选——那些全都提前做完了，
        只剩「点一下加购」+「跳结账」两个动作。

        （为什么不能直接发接口绕过页面：加购请求带一个 atbtoken 防自动化
        令牌，由页面 JS 生成。预热等于把生成令牌的成本挪到开卖之前。）
        """
        self.start()
        if self._page is None or self._page.is_closed():
            self._page = self._ctx.new_page()
        self._page.goto(url, timeout=self.timeout * 2, wait_until="domcontentloaded")
        self._page.wait_for_timeout(2500)
        self._pick(self._page, "tradein", self.trade_in_text)
        self._pick(self._page, "applecare", self.applecare_text)
        self.warmed = True
        try:
            enabled = self._page.locator(SEL_ADD_TO_CART).first.is_enabled()
        except Exception:
            enabled = False
        state = "按钮已可点（现在就有货）" if enabled else "按钮暂灰（无货，正常）"
        self.log(f"[预热] 产品页已就绪，必选项已选好——{state}")

        # 顺手体检购物袋：限购问题必须在开卖**之前**发现，
        # 到了放货那一刻才发现袋子满了就来不及了
        chk = self._ctx.new_page()
        try:
            chk.goto("https://www.apple.com.cn/shop/bag", timeout=self.timeout,
                     wait_until="domcontentloaded")
            chk.wait_for_timeout(1800)
            st = self.bag_state(chk)
            self.bag_over_limit = st.get("limitMsg") or ""
            if st.get("items") and self.clear_bag:
                n = self._empty_bag(chk)
                self.log(f"[预热] 购物袋已清空（移除 {n} 件）")
                state += f"；已清空购物袋（{n} 件）"
                self.bag_over_limit = ""
            elif self.bag_over_limit:
                self.log(f"[预热] ⚠️ 购物袋已超限购：{self.bag_over_limit}"
                         f"——现在就清空，否则开卖时结不了账")
                state += "；⚠️ 购物袋超限，先清空"
            elif st.get("items"):
                self.log(f"[预热] 提醒：购物袋里已有 {st['items']} 件商品，"
                         f"抢购前建议清空（iPhone 限购 2 台）")
                state += f"；购物袋已有 {st['items']} 件，建议清空"
        except Exception as e:
            self.log(f"[预热] 购物袋体检跳过：{type(e).__name__}")
        finally:
            try:
                chk.close()
            except Exception:
                pass
        return state

    def fire(self) -> BuyResult:
        """放货瞬间调用：直接用预热好的页面加购并进结账。"""
        if not self.warmed or self._page is None or self._page.is_closed():
            raise AutoBuyUnavailable("页面没预热好")
        return self._drive(self._ctx, self._page, None, dry_run=False)

    def rehearse(self, url: str) -> BuyResult:
        """排练：走到「加入购物袋」前一步就停，不改动购物袋。

        用来验证选择器还有效、以及把 Apple ID 登录态存进 profile。
        **发布前一定要跑一次**，别等抢购当天才发现页面改版了。
        """
        return self._run(url, dry_run=True)

    def buy(self, url: str) -> BuyResult:
        """真跑：加购 → 进结账页 → 停下叫人。不会提交付款。"""
        return self._run(url, dry_run=False)

    # ---------- 内部 ----------

    def _run(self, url: str, dry_run: bool) -> BuyResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise AutoBuyUnavailable(
                "没装 playwright。用项目自带的虚拟环境跑：.venv/bin/python -m hunter ..."
            ) from e

        what = "排练" if dry_run else "抢购"
        self.log(f"[自动下单] {what}模式启动：{url}")

        with sync_playwright() as pw:
            ctx, attached = self._launch(pw)
            # 挂到已有 Chrome 时开新标签页，不要抢占用户正在看的页面
            page = ctx.new_page() if attached else (ctx.pages[0] if ctx.pages else ctx.new_page())
            try:
                return self._drive(ctx, page, url, dry_run)
            finally:
                if attached:
                    # 别关别人的浏览器，只在排练时收掉自己开的标签页
                    if dry_run:
                        try:
                            page.close()
                        except Exception:
                            pass
                    else:
                        self.log("[自动下单] 标签页保留，等你确认付款")
                elif dry_run:
                    ctx.close()
                else:
                    self.log("[自动下单] 浏览器保持打开，等你确认付款")

    def _launch(self, pw):
        """返回 (context, attached)。attached=True 表示挂在用户自己的 Chrome 上。"""
        if self.mode in ("auto", "cdp"):
            found = self.find_cdp()
            if found:
                url, info = found
                browser = pw.chromium.connect_over_cdp(url)
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                self.log(f"[自动下单] 已挂到你的 Chrome（{url} / {info.get('Browser','?')}）"
                         f"——用你自己的登录态，不读取 profile 目录")
                return ctx, True
            if self.mode == "cdp":
                tried = "、".join(cdp_candidates(self.cdp_url, self.cdp_port))
                raise AutoBuyUnavailable(
                    f"没找到开着调试端口的 Chrome（试过 {tried}）。\n"
                    f"    跑 `hunter connect` 看怎么带 --remote-debugging-port={self.cdp_port} 启动。")
            self.log("[自动下单] 没探测到你的 Chrome，退回独立 profile 模式")

        self.profile.mkdir(parents=True, exist_ok=True)
        last = None
        # 优先用系统已装的 Chrome，省掉 playwright 自带浏览器的下载
        for kw in ({"channel": "chrome"}, {"executable_path": "/usr/bin/google-chrome"}, {}):
            try:
                ctx = pw.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile),
                    headless=self.headless,
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 900},
                    **kw,
                )
                return ctx, False
            except Exception as e:
                last = e
        raise AutoBuyUnavailable(f"浏览器启动失败：{last}")

    def _drive(self, ctx, page, url: str | None, dry_run: bool) -> BuyResult:
        t0 = time.monotonic()
        if url is not None:
            # url 为 None = 页面已预热好，直接省掉加载和选项这两步
            page.goto(url, timeout=self.timeout * 2, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            self._pick(page, "tradein", self.trade_in_text)
            self._pick(page, "applecare", self.applecare_text)

        # 已经超限就别再加了——加了也结不了账，只会让袋子更难收拾
        if self.bag_over_limit and not dry_run:
            return BuyResult(
                False, "⚠️ 购物袋已超限购，未加购", page.url,
                f"{self.bag_over_limit}\n先把购物袋清空再抢，否则加多少都结不了账。")

        ok, why = self._wait_add_button(page)
        if not ok:
            return BuyResult(False, "加购按钮不可用", page.url, why)

        if dry_run:
            el = (time.monotonic() - t0)
            self.log(f"[自动下单] 排练通过：{el:.1f}s 内走到可加购状态（未点击）")
            return BuyResult(True, "排练通过（未加购）", page.url,
                             f"耗时 {el:.1f}s，选择器有效")

        # 用 locator 而不是先前抓到的句柄——locator 每次操作都会重新定位
        page.locator(SEL_ADD_TO_CART).first.click(timeout=self.timeout)
        self.added_to_bag = True
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2000)
        self.log(f"[自动下单] 已加入购物袋（{time.monotonic() - t0:.1f}s）")

        page.goto(page.url.split("/shop/")[0] + "/shop/bag",
                  timeout=self.timeout, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        for sel in CHECKOUT_SELECTORS:
            el = page.query_selector(sel)
            if el and el.is_enabled():
                # 「结账」在新标签页里打开，点完必须切过去——否则后面全在
                # 旧的购物袋页上操作，会误判成功（踩过这个坑）
                page = self._click_checkout(ctx, page, el)
                total = time.monotonic() - t0

                if "/shop/checkout" not in page.url and not _is_sign_in(page.url):
                    # 没进结账页，先查是不是撞了限购——这是最常见的原因，
                    # 而且 Apple 不会主动弹错，只让流程静默卡住
                    st = self.bag_state(page)
                    if st.get("limitMsg"):
                        self.log(f"[自动下单] ⚠️ 撞到限购：{st['limitMsg']}")
                        return BuyResult(
                            False, "⚠️ 购物袋超出限购", page.url,
                            f"{st['limitMsg']}\n袋里有 {st.get('items')} 件，先清空再抢。")
                    return BuyResult(False, "⚠️ 没能进入结账页", page.url,
                                     f"点了结账但没跳转（{total:.1f}s），请手动接管。")

                # Apple 未登录时会把结账拦到 signIn 页。这时候报「已到结账页」
                # 是假成功——抢购当天你会以为一切就绪，实际卡在登录墙前面。
                if _is_sign_in(page.url):
                    self.log(f"[自动下单] ⚠️ 被拦在登录页（{total:.1f}s）——这个 Chrome 没登录 Apple ID")
                    return BuyResult(
                        False, "⚠️ 卡在登录页", page.url,
                        f"商品已加入购物袋，但结账被登录墙拦住（{total:.1f}s）。\n"
                        f"这个 Chrome 没登录 Apple ID——抢购当天会因此丢单，现在就去登录。")

                t_ck = time.monotonic()
                pickup_note = self._choose_pickup(page)
                self.log(f"[自动下单] 取货选择耗时 {time.monotonic() - t_ck:.1f}s")
                self.log(f"[自动下单] 已进入结账页（总耗时 "
                         f"{time.monotonic() - t0:.1f}s）——请手动完成付款")
                detail = f"总耗时 {total:.1f}s。付款请你自己点。"
                if pickup_note:
                    detail = f"{pickup_note}\n{detail}"
                return BuyResult(True, "已到结账页", page.url, detail)

        # 结账按钮没找到也不算失败：东西已经在袋里，人接手就行
        return BuyResult(True, "已加购，停在购物袋", page.url,
                         "没找到结账按钮（可能需要先登录）——商品已在袋中，请手动结账")

    def _empty_bag(self, page) -> int:
        """清空购物袋，返回移除的件数。

        Apple 限购 iPhone 每人 2 台，袋里的存货会顶掉抢购名额，而且超限时
        点结账**既不跳转也不弹错**，只是静默卡住。所以加购前先清干净，
        比事后诊断可靠得多。
        """
        removed = 0
        for _ in range(10):  # 每次移除都会重绘，逐件来
            try:
                clicked = page.evaluate(r"""() => {
                    const cands = document.querySelectorAll(
                        '[data-autom*="remove"], button, a');
                    for (const el of cands) {
                        const t = ((el.innerText || '') + ' ' +
                                   (el.getAttribute('aria-label') || ''));
                        if (/移除|删除/.test(t) && !/收藏|稍后/.test(t)) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }""")
            except Exception:
                break
            if not clicked:
                break
            removed += 1
            page.wait_for_timeout(1200)
        return removed

    def bag_state(self, page) -> dict:
        """查购物袋：几件商品、是否已触发限购。

        为什么必须查：Apple 限购（iPhone 每人最多 2 台）。袋里超量时点
        「结账」**既不跳转也不弹错**，只是原地不动——代码会一路空等到
        超时，白白烧掉几十秒还报不出原因。抢购当天这就是直接出局。
        """
        try:
            return page.evaluate(r"""(pats) => {
                const rows = document.querySelectorAll(
                    '[data-autom*="bagItem"], [id^="cart-items-item"]');
                const body = document.body.innerText || '';
                let limit = null;
                for (const pat of pats) {
                    const i = body.indexOf(pat);
                    if (i >= 0) {
                        limit = body.slice(Math.max(0, i - 40), i + 60)
                                    .replace(/\s+/g, ' ').trim();
                        break;
                    }
                }
                return {items: rows.length, limitMsg: limit};
            }""", list(LIMIT_PATTERNS))
        except Exception as e:
            return {"items": None, "limitMsg": None, "err": str(e)[:70]}

    def _click_checkout(self, ctx, page, el):
        """点「结账」并切到真正承载结账流程的页面。

        Apple 通常在**新标签页**打开 secure8.../shop/checkout。用
        expect_page 精确捕获这次新开的页面——早先按「不在旧列表里」判断，
        会被之前遗留的结账标签页干扰，白等到超时。

        没开新标签页就是本页跳转，回退到等本页 URL 变成结账页。
        """
        try:
            with ctx.expect_page(timeout=15000) as info:
                el.click()
            newpage = info.value
            try:
                newpage.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            self.log("[自动下单] 结账开在新标签页，已切过去")
            return newpage
        except Exception:
            pass  # 没有新标签页，按本页跳转处理

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                u = page.url
            except Exception:
                u = ""
            if "/shop/checkout" in u or _is_sign_in(u):
                break
            time.sleep(0.2)
        self._settle(page)
        return page

    def _settle(self, page, quiet_ms: int = 700, max_ms: int = 8000) -> None:
        """等页面导航稳定：URL 连续 quiet_ms 不变才算落定。

        Apple 的结账是一串重定向，用 wait_for_load_state 不够——它只等到
        当前这一跳，下一跳照样把执行上下文掀掉。
        """
        deadline = time.monotonic() + max_ms / 1000
        last_url, stable_since = None, time.monotonic()
        while time.monotonic() < deadline:
            try:
                u = page.url
            except Exception:
                u = None
            if u != last_url:
                last_url, stable_since = u, time.monotonic()
            elif (time.monotonic() - stable_since) * 1000 >= quiet_ms:
                break
            try:
                page.wait_for_timeout(200)
            except Exception:
                break
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

    def _wait_add_button(self, page) -> tuple[bool, str]:
        """等「添加到购物袋」变为可点。

        必须用 locator 而不是 ElementHandle：页面是 React 渲染的，选完
        tradein/applecare 会整块重绘，先前抓到的 ElementHandle 立刻 detach，
        再调 is_enabled() 就抛 "Element is not attached to the DOM"。
        locator 每次都重新定位，天然免疫这个问题。

        这个 bug 在前几次测试里没暴露，纯粹是重绘时机碰巧错开了——
        抢购当天碰上就是直接丢单，所以这里宁可多轮询也要等稳。
        """
        loc = page.locator(SEL_ADD_TO_CART)
        deadline = time.monotonic() + self.timeout / 1000
        last = "页面上没有 add-to-cart，可能改版了或这个机型不可售"
        while time.monotonic() < deadline:
            try:
                if loc.count() == 0:
                    last = "页面上没有 add-to-cart，可能改版了或这个机型不可售"
                elif loc.first.is_enabled():
                    return True, ""
                else:
                    last = "加购按钮一直是灰的——还有没选完的必选项，跑 rehearse 看看页面新增了什么"
            except Exception as e:
                # 重绘期间的瞬时错误，下一轮重新定位即可
                last = f"定位加购按钮反复失败：{str(e)[:70]}"
            page.wait_for_timeout(300)
        return False, last

    def _choose_pickup(self, page) -> str:
        """尽力在结账页选中「到店取货」和指定门店。

        **这一步没能在真实结账流程里验证过。** 门店选择只出现在登录后的
        结账流程里（secure.apple.com.cn），产品页和购物袋页都没有这个控件，
        所以我没法在不动你真实订单的前提下把选择器坐实。

        因此这里的原则是：
          - 只在文本**精确包含**你配置的门店名时才点，绝不「兜底选第一家」
            ——选错门店比没选更糟。
          - 无论成功失败都返回一句话，由调用方推送给你。失败时明确说
            「请手动选」，绝不静默假装成功。
        """
        if not self.pickup_store_name:
            return ""

        want = self.pickup_store_name
        # 守卫：不在结账页就别点。购物袋页上也有「五角场」字样，
        # 在那儿乱点会报成功但什么都没选中。
        try:
            cur = page.url
        except Exception:
            cur = ""
        if "/shop/checkout" not in cur:
            self.log(f"[自动下单] 当前不在结账页（{cur[:60]}），跳过取货选择")
            return f"⚠️ 没走到结账页，请手动选「到店取货 → {want}」"
        picked, err = None, None
        for attempt in range(3):
            try:
                picked = self._pickup_eval(page, want)
                err = None
                break
            except Exception as e:
                err = e
                # 多半是又跳了一次页面，等稳再试
                self._settle(page)
        if err is not None:
            self.log(f"[自动下单] 取货选择出错：{err}")
            return f"⚠️ 没能自动选取货门店，请手动选「到店取货 → {want}」"

        page.wait_for_timeout(1200)
        if picked and picked.get("store"):
            self.log(f"[自动下单] 已尝试选中取货门店「{want}」——**请在页面上再确认一眼**")
            return f"已尝试选「到店取货 → {want}」，付款前请确认页面上确实是这家店"
        if picked and picked.get("switched"):
            self.log(f"[自动下单] 切到了取货，但没找到「{want}」——请手动选门店")
            return f"⚠️ 已切到取货，但没找到「{want}」，请手动选门店"
        self.log(f"[自动下单] 结账页没找到取货选项——请手动选「到店取货 → {want}」")
        return f"⚠️ 没找到取货选项，请手动选「到店取货 → {want}」"

    def _pickup_eval(self, page, want: str) -> dict:
        return page.evaluate(
                """(want) => {
                    const hit = (el) => {
                        const t = (el.innerText || '') + ' ' +
                                  (el.getAttribute('aria-label') || '');
                        return t;
                    };
                    // 第一步：切到「到店取货」
                    let switched = false;
                    for (const el of document.querySelectorAll(
                            'button,[role=radio],[role=button],label,input')) {
                        const t = hit(el);
                        if (/到店取货|零售店取货|门店取货/.test(t)) {
                            el.click(); switched = true; break;
                        }
                    }
                    // 第二步：在门店列表里精确匹配门店名
                    let store = false;
                    for (const el of document.querySelectorAll(
                            'button,[role=radio],[role=button],label,input')) {
                        if (hit(el).includes(want)) { el.click(); store = true; break; }
                    }
                    return {switched, store};
                }""",
            want,
        )

    def _pick(self, page, section: str, keyword: str) -> bool:
        """在某个选项分区里选中标签含关键词的那一项。

        用 JS 点 label 而不是 Playwright 的 click()：这些分区是 React 渲染的，
        选中后整块会重绘，Playwright 拿到的元素句柄会失效，判定成「不可点击」
        然后一直重试到超时。实测 JS 直接触发才稳。
        """
        try:
            clicked = page.evaluate(
                """([section, keyword]) => {
                    const root = document.querySelector(
                        `[data-analytics-section="${section}"]`);
                    if (!root) return null;
                    for (const inp of root.querySelectorAll('input[type=radio]')) {
                        const lab = document.querySelector(`label[for="${inp.id}"]`);
                        if (!lab) continue;
                        const t = lab.innerText || '';
                        if (t.includes(keyword)) {
                            lab.click();
                            return t.trim().split('\\n')[0].slice(0, 24);
                        }
                    }
                    return null;
                }""",
                [section, keyword],
            )
        except Exception as e:
            self.log(f"[自动下单] {section} 选择出错：{e}")
            return False

        if clicked is None:
            self.log(f"[自动下单] {section}: 没找到含「{keyword}」的选项（页面可能改版了）")
            return False
        page.wait_for_timeout(900)
        self.log(f"[自动下单] {section}: 选中「{clicked}」")
        return True


# ---------- 启动一个带调试端口的 Chrome ----------

WIN_CHROME_PATHS = [
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]


def windows_chrome() -> str | None:
    for p in WIN_CHROME_PATHS:
        if Path(p).exists():
            return p
    return None


def windows_userprofile() -> str | None:
    """问 Windows 自己要用户目录，避免猜路径。"""
    try:
        r = subprocess.run(["cmd.exe", "/c", "echo %USERPROFILE%"],
                           cwd="/mnt/c", capture_output=True, text=True, timeout=10)
        v = r.stdout.strip()
        return v if v.startswith("C:") or ":\\" in v else None
    except Exception:
        return None


def launch_debug_chrome(port: int = DEFAULT_CDP_PORT, profile_name: str = ".iphone-hunter-chrome",
                        headless: bool = False, log=print) -> tuple[bool, str]:
    """起一个开着调试端口的 Chrome，返回 (成功, 说明)。

    为什么必须用独立 profile：Chrome 136 起 --remote-debugging-port 对**默认
    profile 直接失效**（防止攻击者挂上真实 profile 偷 cookie），必须配一个
    非默认的 --user-data-dir。所以你日常那个 Chrome 是挂不上去的。

    好在 Apple 的收货地址和付款方式存在你的 Apple ID 账号里、不在浏览器里，
    所以在这个独立 profile 里登录一次 Apple ID，该有的都有。登录态会一直
    留在这个 profile 里，只需要登录这一次。
    """
    exe = windows_chrome()
    if exe:
        home = windows_userprofile()
        if not home:
            return False, "问不到 Windows 用户目录（%USERPROFILE%）"
        profile = f"{home}\\{profile_name}"
        kind = "Windows Chrome"
    else:
        exe = "/usr/bin/google-chrome"
        if not Path(exe).exists():
            return False, "既没找到 Windows Chrome 也没找到 Linux Chrome"
        profile = str(Path.home() / profile_name)
        kind = "Linux Chrome"

    cmd = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
           "--no-first-run", "--no-default-browser-check"]
    if headless:
        cmd.append("--headless=new")
    cmd.append("https://www.apple.com.cn/shop/bag")

    log(f"[Chrome] 启动 {kind}，profile={profile}")
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return False, f"启动失败：{e}"

    for _ in range(40):
        info = probe_cdp(f"http://127.0.0.1:{port}", timeout=1.0)
        if info:
            return True, f"{kind} 已就绪（{info.get('Browser', '?')}），调试端口 {port}"
        time.sleep(0.5)
    return False, (f"{kind} 起来了但 {port} 端口探不到。"
                   "如果之前已经有用这个 profile 的 Chrome 在跑，先把它完全退出再试。")


# ---------- 只读：查看结账页的配送方式控件 ----------

FULFILL_KEYWORDS = ("取货", "送货", "配送", "门店", "零售店", "自提")


def inspect_checkout(cfg: dict, root: Path, log=print) -> list[dict]:
    """挂到你的 Chrome，打开结账页，只**读取**配送方式相关的控件。

    不点击、不修改、不提交。只为搞清楚「到店取货」那一步的选择器长什么样。
    输出刻意只保留含配送关键词的控件，避免把收货人/地址/电话打出来。
    """
    from playwright.sync_api import sync_playwright

    ab = AutoBuy(cfg, root, log=log)
    found: list[dict] = []
    with sync_playwright() as pw:
        ctx, attached = ab._launch(pw)
        page = ctx.new_page()
        try:
            base = "https://www.apple.com.cn"
            page.goto(f"{base}/shop/bag", timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            log(f"[检查] 当前页面：{page.url}")

            for el in page.query_selector_all(
                    "button, a, input[type=radio], [role=radio], [role=button]"):
                try:
                    txt = (el.inner_text() or "").strip().replace("\n", " ")
                except Exception:
                    txt = ""
                aria = el.get_attribute("aria-label") or ""
                blob = f"{txt} {aria}"
                if not any(k in blob for k in FULFILL_KEYWORDS):
                    continue
                found.append({
                    "text": txt[:44],
                    "aria": aria[:44],
                    "tag": el.evaluate("e=>e.tagName"),
                    "data_autom": el.get_attribute("data-autom"),
                    "id": el.get_attribute("id"),
                    "name": el.get_attribute("name"),
                })
        finally:
            try:
                page.close()
            except Exception:
                pass
    return found
