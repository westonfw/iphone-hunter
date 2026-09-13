"""半自动下单：创建待付款订单，付款留给你。

检测到有货时，在你已经登录的浏览器里：
  预热页点加购（atbtoken 必须由页面 JS 现算）
  → 页面内同源请求 / 直跳结账（不傻等购物袋整页加载）
  → 选取货与扫码支付
  → 点「现在下单」
  → 停在待付款 / 扫码页，响铃叫你去付。

边界（硬性）
-----------
**绝不代你付款。** 不填支付密码、不确认 Apple Pay、不在信用卡路径上点下单。
支付宝 / 微信 / 花呗点「现在下单」只是创建待付款订单，窗口内由你扫码。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .apple import REGIONS
# fill_field 定义在 checkout.py：调用点在那边，而 checkout 不能反向
# import autobuy（循环导入）。这里再导出一次，保持 autobuy 也能 import。
from .checkout import (BTN_SIGN_IN, ID_ACCOUNT, ID_PWD, JS_FILL, OrderPlacer,
                       click_id, eval_in_frames, fill_field, frame_for_id,
                       is_sign_in, read_field, wait_settled)

DEFAULT_CDP_PORT = 9222

# 这些 data-autom 是 Apple 自己的自动化测试钩子，比 class 名稳定得多，
# 也不受界面语言影响。
SEL_ADD_TO_CART = '[data-autom="add-to-cart"]'

#: 页面上出现这些字样 = 这个型号现在买不到了（多半是刚被人抢走）。
#: 跟「必选项没选完」是两回事：那个重试有用，这个重试没用。
SOLD_OUT_MARKS = ("暂无供应", "已售罄", "售罄", "目前无法购买", "无法购买",
                  "缺货", "Currently unavailable", "Sold Out")

JS_SOLD_OUT = """
(marks) => {
    const body = document.body ? (document.body.innerText || "") : "";
    for (const m of marks) if (body.includes(m)) return m;
    return "";
}
"""
SEL_SECTION = '[data-analytics-section="{}"]'

# 购物袋页进入结账的按钮，Apple 改过几次名字，按顺序试。
# Apple 对 iPhone 有限购（iPhone 18 Pro 购买页注明 Pro / Pro Max 各限 2 部）。
# 袋里超量时点结账既不跳转也不报错，只是静默不动——必须主动识别。
LIMIT_PATTERNS = ("最多可购买", "请调整订单", "超出购买上限", "限购", "购买数量")

CHECKOUT_SELECTORS = [
    '[data-autom="checkout"]',
    '[data-autom="proceed"]',
    '[data-autom="bagCheckoutButton"]',
    'button:has-text("结账")',
    'a:has-text("结账")',
]


_PART_IN_URL = re.compile(r"/([A-Z0-9]{4,}[A-Z]{2}/A)(?:[/?#]|$)", re.I)


def _part_of(url: str) -> str:
    """从购买页地址里取出 part number，取不到返回空串。"""
    m = _PART_IN_URL.search(url or "")
    return m.group(1).upper() if m else ""


#: 判断逻辑在 checkout.py（那边的 enter/on_checkout 也要用，而它不能反向
#: import autobuy）。这里起个别名，保持本模块内的老叫法。
_is_sign_in = is_sign_in


class AutoBuyUnavailable(RuntimeError):
    """没装 playwright 或浏览器起不来。"""


@dataclass
class BuyResult:
    ok: bool
    stage: str          # 走到了哪一步
    url: str = ""
    detail: str = ""
    order_id: str = ""
    #: 这次失败值不值得下一轮再试。货被别人买走了就别再试了——
    #: 页面会一直是「无法购买」，重试只是每轮白烧几十秒。
    retriable: bool = True


#: Apple ID 密码从这个环境变量读，**不从 config.json 读**。
#: config.json 会被备份、同步盘、误提交带出去，而 Apple ID 泄露牵连的
#: 远不止买手机这一件事。
PWD_ENV = "HUNTER_APPLE_PWD"

def _mask(account: str) -> str:
    """打日志用的账号遮罩：a***@example.com。别把完整账号写进日志。"""
    name, _, host = account.partition("@")
    head = name[:1] if name else ""
    return f"{head}***@{host}" if host else f"{head}***"


#: 登录页上等各个控件出现的上限（毫秒）。抽成常量是为了能调、也为了测试不空转。
SIGNIN_WAIT = {"pwd": 2500, "account": 4000, "pwd_after_account": 8000}


def _apple_password(cfg: dict, log=print) -> str:
    """取 Apple ID 密码。只认环境变量。"""
    if cfg.get("pwd"):
        log(f"⚠️ config.json 里还留着 autobuy.pwd —— 已忽略，请删掉它。"
            f"密码改用环境变量 {PWD_ENV}。")
    return os.environ.get(PWD_ENV, "")


def _store_list(*sources) -> list[str]:
    """把 pickup_stores / pickup_store_name 归一成有序去重的门店名列表。

    两个键都认：新的收一串，老的收一个字符串（也允许写成逗号分隔）。
    """
    out: list[str] = []
    for src in sources:
        if not src:
            continue
        items = src if isinstance(src, (list, tuple)) else str(src).replace("，", ",").split(",")
        for x in items:
            name = str(x).strip()
            if name and name not in out:
                out.append(name)
    return out


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
        # 到店取货：只做「尽力尝试 + 大声提醒」，见 _choose_pickup 的说明。
        # 可以给一串门店——哪家真有货是放货那一刻才知道的，写死一家等于
        # 另外几家放货时白白卡在选店那一步。
        self.pickup_stores = _store_list(self.cfg.get("pickup_stores"),
                                         self.cfg.get("pickup_store_name"))
        # 已经加进购物袋了就不能盲目重试加购，否则会重复下单
        self.added_to_bag = False
        self.order_placed = False
        # 预热用的常驻会话
        self._pwctx = self._pw = self._ctx = self._page = None
        self._attached = False
        self.warmed = False
        # 预热体检发现购物袋超限时置位：超限还继续加购只会让情况更糟
        self.bag_over_limit = ""
        # 加购前先清空购物袋。Apple 限购（iPhone 每人 2 台），袋里有存货
        # 会让新加的这台结不了账，而且 Apple 不弹错、只是静默卡住。
        self.clear_bag = bool(self.cfg.get("clear_bag_before_add", True))
        self.place_order = bool(self.cfg.get("place_order", True))
        self.payment_method = (self.cfg.get("payment_method") or "支付宝").strip()
        self.installment_months = int(self.cfg.get("installment_months") or 0)
        # 结账主机（secureN）。留空 = 每次自动探；探到后本进程记住，
        # 抢购当天就不用在挨个试上浪费秒数了。
        self.secure_host = (self.cfg.get("checkout_host") or "").strip()
        # 测试用：一路走到 Review 页就停，不点「立即下单」
        self.stop_at_review = bool(self.cfg.get("stop_at_review", False))
        self.delivery = (self.cfg.get("delivery") or "pickup").strip()
        self.region = (self.cfg.get("region") or "cn").strip()
        self.id_last4 = str(self.cfg.get("id_last4") or "")
        self.apple_id = str(self.cfg.get("apple_id") or "")
        self.pwd = _apple_password(self.cfg, self.log)
        self.pickup_last_name = str(self.cfg.get("pickup_last_name") or "")
        self.pickup_first_name = str(self.cfg.get("pickup_first_name") or "")
        self.pickup_email = str(self.cfg.get("pickup_email") or "")
        self.pickup_phone = str(self.cfg.get("pickup_phone") or "")

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

    def store_candidates(self, in_stock: list[str] | None = None) -> list[str]:
        """这一单该按什么顺序试门店。

        真有货的排前面——配置里的偏好顺序只在「都有货」时才有意义，
        而放货那一刻通常只有一两家有。白名单为空 = 有货的都能下。
        """
        allow = self.pickup_stores
        hot = [s.strip() for s in (in_stock or []) if s and s.strip()]
        if not allow:
            return hot
        if not hot:
            return list(allow)
        first = [s for s in allow if any(s in h or h in s for h in hot)]
        rest = [s for s in allow if s not in first]
        return first + rest

    def fire(self, url: str = "", in_stock: list[str] | None = None) -> BuyResult:
        """放货瞬间调用：用预热好的页面加购并进结账。

        url 是**这次真正命中的那个型号**的购买页，必须核对：预热页加载的是
        监控列表里的第一个型号，而放货的可能是任何一个——不核对就会出现
        「提示银色、袋里进黑色」。对不上就老实跳转，慢几秒也比买错强。

        in_stock 是监控刚查到「有货」的门店名，用来决定去哪家取。
        """
        if not self.warmed or self._page is None or self._page.is_closed():
            raise AutoBuyUnavailable("页面没预热好")
        want, got = _part_of(url), _part_of(getattr(self._page, "url", ""))
        if want and want != got:
            self.log(f"[自动下单] 预热页是 {got or '未知'}，这次要买 {want}——"
                     "跳转到正确型号（放弃预热加速）")
            return self._drive(self._ctx, self._page, url, dry_run=False,
                               in_stock=in_stock)
        return self._drive(self._ctx, self._page, None, dry_run=False,
                           in_stock=in_stock)

    def rehearse(self, url: str) -> BuyResult:
        """排练：走到「加入购物袋」前一步就停，不改动购物袋。

        用来验证选择器还有效、以及把 Apple ID 登录态存进 profile。
        **发布前一定要跑一次**，别等抢购当天才发现页面改版了。
        """
        return self._run(url, dry_run=True)

    def buy(self, url: str, in_stock: list[str] | None = None) -> BuyResult:
        """真跑：加购 → 创建待付款订单。不会代你付款。"""
        return self._run(url, dry_run=False, in_stock=in_stock)

    # ---------- 内部 ----------

    def _run(self, url: str, dry_run: bool, in_stock: list[str] | None = None) -> BuyResult:
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
                return self._drive(ctx, page, url, dry_run, in_stock=in_stock)
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

    def _drive(self, ctx, page, url: str | None, dry_run: bool,
               in_stock: list[str] | None = None) -> BuyResult:
        t0 = time.monotonic()
        stores = self.store_candidates(in_stock)
        if stores:
            self.log(f"[自动下单] 取货门店优先级：{' > '.join(stores)}")
        if url is not None:
            # 顺序很要紧：**先清空购物袋，再打开产品页**。
            # 反过来做（开着产品页、另开标签页去清袋、再回来加购）既多一个标签页，
            # 也不是人的操作路径；而且清袋那一下会让产品页的会话状态过期。
            if not dry_run and not self.added_to_bag and self.clear_bag:
                self._clear_bag_now(page)

            page.goto(url, timeout=self.timeout * 2, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            self._pick(page, "tradein", self.trade_in_text)
            self._pick(page, "applecare", self.applecare_text)

        # 已经超限就别再加了——加了也结不了账，只会让袋子更难收拾
        if self.bag_over_limit and not dry_run and not self.added_to_bag:
            return BuyResult(
                False, "⚠️ 购物袋已超限购，未加购", page.url,
                f"{self.bag_over_limit}\n先把购物袋清空再抢，否则加多少都结不了账。")

        if not self.added_to_bag:
            ok, why = self._wait_add_button(page)
            if not ok:
                if why.startswith("SOLD_OUT:"):
                    mark = why.split(":", 1)[1]
                    return BuyResult(
                        False, "⚠️ 已经买不到了", page.url,
                        f"页面显示「{mark}」——多半是刚被人抢走。不再重试这一单。",
                        retriable=False)
                return BuyResult(False, "加购按钮不可用", page.url, why)

        if dry_run:
            el = (time.monotonic() - t0)
            self.log(f"[自动下单] 排练通过：{el:.1f}s 内走到可加购状态（未点击）")
            return BuyResult(True, "排练通过（未加购）", page.url,
                             f"耗时 {el:.1f}s，选择器有效")

        if not self.added_to_bag:
            # 用 locator 而不是先前抓到的句柄——locator 每次操作都会重新定位
            page.locator(SEL_ADD_TO_CART).first.click(timeout=self.timeout)
            self.added_to_bag = True
            # 只等加购请求出门，不加载 700KB 购物袋页（等 URL 变化最坏会烧掉数秒）
            page.wait_for_timeout(600)
            self.log(f"[自动下单] 已加入购物袋（{time.monotonic() - t0:.1f}s），不加载购物袋页")
        else:
            self.log("[自动下单] 购物袋里已有货，跳过加购，直接创建订单")

        placer = OrderPlacer(
            region=self.region,
            pickup_stores=stores,
            payment=self.payment_method,
            delivery=self.delivery,
            id_last4=self.id_last4,
            last_name=self.pickup_last_name,
            first_name=self.pickup_first_name,
            email=self.pickup_email,
            phone=self.pickup_phone,
            installment_months=self.installment_months,
            secure_host=self.secure_host,
            stop_at_review=self.stop_at_review,
            timeout_ms=self.timeout,
            log=self.log,
        )
        # 跟着页面走：加购后按页面上的「结账」按钮，让 Apple 自己把你带到
        # 它给这个会话分配的那台 secureN 上。自己拼地址跳转既容易跳错主机，
        # 也是最典型的机器行为特征。直跳只在这条路走不通时兜底。
        page = self._checkout_via_bag(ctx, page)
        if "/shop/checkout" not in (page.url or "") and not _is_sign_in(page.url):
            self.log("[自动下单] 页面上的结账按钮没走通，退回直跳结账地址")
            page = placer.enter(ctx, page)
        if self._page is not None:
            self._page = page
        # 登录页不用等它「稳定」，认出来就直接去登录，省掉那几秒
        if not _is_sign_in(page.url):
            self._settle(page)

        if _is_sign_in(page.url):
            self.log(f"[自动下单] 被拦在登录页（{time.monotonic() - t0:.1f}s），尝试自动登录")
            ok, why = self._sign_in(page)
            if not ok:
                return BuyResult(
                    False, "⚠️ 卡在登录页", page.url,
                    f"商品已加入购物袋，但结账被登录墙拦住。{why}")
            self.log(f"[自动下单] 登录完成（{time.monotonic() - t0:.1f}s）")
            self._settle(page)
            if "/shop/checkout" not in (page.url or ""):
                # 登录后 Apple 未必自动回结账，按页面路径再走一次
                page = self._checkout_via_bag(ctx, page)

        if "/shop/checkout" not in page.url:
            st = self.bag_state(page)
            if st.get("limitMsg"):
                return BuyResult(
                    False, "⚠️ 购物袋超出限购", page.url,
                    f"{st['limitMsg']}\n袋里有 {st.get('items')} 件，先清空再抢。")
            return BuyResult(False, "⚠️ 没能进入结账页", page.url,
                             f"加购后没能走到结账页（{time.monotonic() - t0:.1f}s），请手动接管。")

        if not self.place_order:
            pickup_note = self._choose_pickup(page, stores)
            return BuyResult(True, "已到结账页", page.url,
                             (pickup_note + "\n" if pickup_note else "")
                             + f"place_order=false，停在结账页。总耗时 {time.monotonic() - t0:.1f}s")

        # 把探到的结账主机记回来，同一进程里后续几轮直接命中
        if placer.secure_host:
            self.secure_host = placer.secure_host

        ok, stage, detail, order_id = placer.place(page, t0)
        if ok:
            self.order_placed = True
        return BuyResult(ok, stage, page.url, detail, order_id=order_id)

    # ---------- 登录 ----------

    def _sign_in(self, page) -> tuple[bool, str]:
        """在登录页上完成登录。返回 (是否已登录, 说明)。

        Apple 的登录框在 idmsa.apple.com 的 iframe 里，而且是**分步**的：
        先填 Apple ID 点继续，密码框才出现。所以不能一上来就填密码。

        信任设备上账号是被记住的，通常直接就是密码框（或账号框已填好），
        这条快路走一步就到。account 那一步只是给「换了 profile / 记录被清掉」
        兜底的。

        双重认证这一步不碰——验证码在你手机上，脚本拿不到也不该拿。信任
        设备一般不会问，但真问了就如实报出来，把浏览器留给你。
        """
        if not self.pwd:
            return False, (f"没配密码。设环境变量 {PWD_ENV}，或者更省事："
                           "在这个 Chrome 里手动登录一次，登录态会留在 profile 里。")

        # 第一步：账号。密码框已经在了说明这步过了——信任设备上通常如此
        if frame_for_id(page, ID_PWD, SIGNIN_WAIT["pwd"]) is None:
            frame = frame_for_id(page, ID_ACCOUNT, SIGNIN_WAIT["account"])
            if frame is None:
                return False, "登录页上既没有账号框也没有密码框，页面结构可能变了"
            remembered = read_field(frame, ID_ACCOUNT, frame=frame) or ""
            if remembered:
                # 页面记着账号，别覆盖它——直接点继续
                self.log(f"[登录] 页面记着账号（{_mask(remembered)}），直接继续")
            elif self.apple_id:
                if not fill_field(page, ID_ACCOUNT, self.apple_id, log=self.log):
                    return False, "Apple ID 没能填进去"
            else:
                return False, ("登录页要填 Apple ID，但页面没记住、config.json 里"
                               "也没有 autobuy.apple_id")
            click_id(page, BTN_SIGN_IN, settle=True, log=self.log)
            if frame_for_id(page, ID_PWD, SIGNIN_WAIT["pwd_after_account"]) is None:
                return False, (self._sign_in_blocked(page)
                               or "填完 Apple ID 之后密码框没出现")

        # 第二步：密码
        if not fill_field(page, ID_PWD, self.pwd, log=self.log, secret=True):
            return False, "密码没能填进去"
        click_id(page, BTN_SIGN_IN, settle=True, log=self.log)
        return self._await_sign_in(page)

    def _await_sign_in(self, page, max_s: float = 25.0) -> tuple[bool, str]:
        """等登录结果：离开登录页算成功；要验证码或报错就如实返回。"""
        deadline = time.monotonic() + max_s
        while time.monotonic() < deadline:
            try:
                url = page.url
            except Exception:
                return False, "登录标签页被关掉了"
            if not _is_sign_in(url):
                return True, ""
            blocked = self._sign_in_blocked(page)
            if blocked:
                return False, blocked
            page.wait_for_timeout(400)
        return False, f"{max_s:.0f}s 内没离开登录页，请手动看一眼那个标签页"

    def _sign_in_blocked(self, page) -> str:
        """登录页上有没有「需要人来处理」的东西：验证码、报错。"""
        js = """(pats) => {
            const body = document.body ? (document.body.innerText || "") : "";
            if (document.querySelector('input[id^="char"], input[autocomplete="one-time-code"]'))
                return "需要双重认证验证码";
            for (const p of pats) if (body.includes(p)) return p;
            return "";
        }"""
        pats = ["双重认证", "验证码", "Apple ID 或密码不正确", "密码不正确",
                "无法登录", "账户已被锁定", "出于安全原因"]
        _, hit = eval_in_frames(page, js, pats)
        if not hit:
            return ""
        if "验证" in hit:
            return f"{hit} —— 验证码在你手机上，请在浏览器里输入，脚本不代劳"
        return f"登录被拒：{hit}"

    def _clear_bag_now(self, page) -> None:
        """在**当前标签页**里打开购物袋并清空。失败不阻断，只是记一笔。

        用当前页而不是另开一个：多开标签页既不像人的操作，也容易让后面
        「加购 → 点结账」跟错页面。
        """
        base = REGIONS.get(self.region) or REGIONS["cn"]
        try:
            page.goto(f"{base}/shop/bag", timeout=self.timeout,
                      wait_until="domcontentloaded")
            page.wait_for_timeout(1800)
            st = self.bag_state(page)
            if st.get("items"):
                n = self._empty_bag(page)
                self.log(f"[自动下单] 加购前已清空购物袋（移除 {n} 件）")
            else:
                self.log("[自动下单] 购物袋本来就是空的")
            self.bag_over_limit = self.bag_state(page).get("limitMsg") or ""
        except Exception as e:
            self.log(f"[自动下单] 清购物袋跳过：{type(e).__name__}: {str(e)[:60]}")

    def _checkout_via_bag(self, ctx, page):
        """走到结账页：打开购物袋 → 点「结账」。

        **不点导航栏那个购物袋图标。** 它弹的是浮层，不是跳转：浮层要动画
        几秒、期间整页发卡，而且它盖在页面上——这时候去找「结账」按钮，
        很容易抓到浮层背后那个被遮住的，点下去要么没反应要么点错。

        直接打开 /shop/bag 反而更接近人的操作结果：同一个页面、同一个
        结账按钮，没有浮层这一层。这跟「别自己拼 secureN 地址」不冲突——
        购物袋页是个正常的、用户看得见的地址，结账主机仍然由 Apple 决定。
        """
        base = REGIONS.get(self.region) or REGIONS["cn"]
        try:
            page.goto(f"{base}/shop/bag", timeout=self.timeout,
                      wait_until="domcontentloaded")
        except Exception as e:
            self.log(f"[自动下单] 打开购物袋失败：{str(e)[:60]}")
            return page

        # 等页面真安静，而不是盲等 1200ms
        wait_settled(page, log=self.log)

        el = self._find_checkout_button(page)
        if el is None:
            self.log("[自动下单] 购物袋页上没找到可点的「结账」按钮")
            return page
        return self._click_checkout(ctx, page, el)

    def _find_checkout_button(self, page):
        """找购物袋页上那个**可见且可点**的结账按钮。

        原来用 query_selector 直接取第一个匹配的，它不判可见性——浮层背后
        或者折叠区域里的同名按钮照样会被取到，点了等于没点。
        """
        for sel in CHECKOUT_SELECTORS:
            try:
                loc = page.locator(sel)
                for i in range(min(loc.count(), 5)):
                    one = loc.nth(i)
                    if one.is_visible() and one.is_enabled():
                        return one.element_handle()
            except Exception:
                continue
        return None

    def _empty_bag(self, page, passes: int = 5) -> int:
        """清空购物袋，返回移除的件数。**清到真的空为止。**

        Apple 限购 iPhone 每人 2 台，袋里的存货会顶掉抢购名额，而且超限时
        点结账**既不跳转也不弹错**，只是静默卡住。所以加购前先清干净，
        比事后诊断可靠得多。

        袋里通常不止一件，而且每移除一件整块都会重绘——重绘那一瞬间抓不到
        「移除」按钮，光靠「点不动了就收工」会提前退出、留下残货。所以这里
        每轮结束都用 bag_state 复核，没清干净就重新加载购物袋再来一轮。
        """
        removed = 0
        for attempt in range(passes):
            removed += self._remove_once(page)
            st = self.bag_state(page)
            if not st.get("items"):
                return removed
            if attempt < passes - 1:
                self.log(f"[自动下单] 购物袋还剩 {st['items']} 件，重载后再清一轮")
                try:
                    page.reload(wait_until="domcontentloaded", timeout=self.timeout)
                    page.wait_for_timeout(1600)
                except Exception:
                    break
        st = self.bag_state(page)
        if st.get("items"):
            self.log(f"[自动下单] ⚠️ 清了 {passes} 轮购物袋里还有 {st['items']} 件，"
                     "请手动清空——超过限购会让结账静默卡住")
        return removed

    def _remove_once(self, page) -> int:
        """把当前这一屏能点到的都移除掉，返回件数。"""
        removed = 0
        misses = 0
        for _ in range(30):
            try:
                clicked = page.evaluate(r"""() => {
                    for (const el of document.querySelectorAll(
                            '[data-autom*="remove"], button, a')) {
                        const r = el.getBoundingClientRect();
                        if (!r.width || !r.height || el.offsetParent === null) continue;
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
                # 可能只是正在重绘，再等一拍；连续两次抓不到才算清完
                misses += 1
                if misses >= 2:
                    break
                page.wait_for_timeout(900)
                continue
            misses = 0
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

        Apple 通常在**新标签页**打开 secureN.../shop/checkout。用
        expect_page 精确捕获这次新开的页面——早先按「不在旧列表里」判断，
        会被之前遗留的结账标签页干扰，白等到超时。

        没开新标签页就是本页跳转，回退到等本页 URL 变成结账页。
        """
        try:
            # 4s 足够：真要开新标签页，点完马上就开。原来给 15s，而被重定向到
            # 登录页时是本页跳转、根本不开新标签，这 15 秒就是纯等超时。
            with ctx.expect_page(timeout=4000) as info:
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

        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                u = page.url
            except Exception:
                u = ""
            if _is_sign_in(u):
                return page          # 登录页不用再 settle，直接交给上层去登录
            if "/shop/checkout" in u:
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

    def _sold_out(self, page) -> str:
        """这个型号是不是已经买不到了。返回命中的字样或空串。"""
        try:
            return page.evaluate(JS_SOLD_OUT, list(SOLD_OUT_MARKS)) or ""
        except Exception:
            return ""

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
            gone = self._sold_out(page)
            if gone:
                # 别耗满 30 秒：页面已经明说买不到了，等下去不会变
                return False, f"SOLD_OUT:{gone}"
            page.wait_for_timeout(300)
        return False, last

    def _choose_pickup(self, page, stores: list[str] | None = None) -> str:
        """尽力在结账页选中「到店取货」和门店（按 stores 的顺序试）。

        **这一步没能在真实结账流程里验证过。** 门店选择只出现在登录后的
        结账流程里（secure.apple.com.cn），产品页和购物袋页都没有这个控件，
        所以我没法在不动你真实订单的前提下把选择器坐实。

        因此这里的原则是：
          - 只在文本**精确包含**候选门店名时才点，绝不「兜底选第一家」
            ——选错门店比没选更糟。
          - 无论成功失败都返回一句话，由调用方推送给你。失败时明确说
            「请手动选」，绝不静默假装成功。
        """
        wants = stores if stores is not None else self.pickup_stores
        wants = [w for w in wants if w]
        if not wants:
            return ""

        want = "/".join(wants)
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
                picked = self._pickup_eval(page, wants)
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
            got = picked["store"]
            self.log(f"[自动下单] 已尝试选中取货门店「{got}」——**请在页面上再确认一眼**")
            return f"已尝试选「到店取货 → {got}」，付款前请确认页面上确实是这家店"
        if picked and picked.get("switched"):
            self.log(f"[自动下单] 切到了取货，但没找到「{want}」——请手动选门店")
            return f"⚠️ 已切到取货，但没找到「{want}」，请手动选门店"
        self.log(f"[自动下单] 结账页没找到取货选项——请手动选「到店取货 → {want}」")
        return f"⚠️ 没找到取货选项，请手动选「到店取货 → {want}」"

    def _pickup_eval(self, page, wants: list[str]) -> dict:
        """返回 {switched, store}。store 是选中的门店名，没选中就是空串。"""
        return page.evaluate(
                """(wants) => {
                    const hit = (el) => (el.innerText || '') + ' ' +
                                        (el.getAttribute('aria-label') || '');
                    const SEL = 'button,[role=radio],[role=button],label,input';
                    // 第一步：切到「到店取货」
                    let switched = false;
                    for (const el of document.querySelectorAll(SEL)) {
                        if (/我要取货|到店取货|零售店取货|门店取货/.test(hit(el))) {
                            el.click(); switched = true; break;
                        }
                    }
                    // 第二步：按优先级挨个试。「Apple 五角场」这种全称优先于裸店名，
                    // 因为门店列表里带着地址，裸「浦东」会命中别家店的「浦东新区」。
                    let store = '';
                    outer:
                    for (const want of wants) {
                        for (const needle of ['Apple ' + want, want]) {
                            for (const el of document.querySelectorAll(SEL)) {
                                if (hit(el).includes(needle)) {
                                    el.click(); store = want; break outer;
                                }
                            }
                        }
                    }
                    return {switched, store};
                }""",
            wants,
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
