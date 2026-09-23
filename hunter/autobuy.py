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

from . import PY_CMD
from .apple import REGIONS
# fill_field 定义在 checkout.py：调用点在那边，而 checkout 不能反向
# import autobuy（循环导入）。这里再导出一次，保持 autobuy 也能 import。
from .checkout import (BTN_SIGN_IN, ID_ACCOUNT, ID_PWD, JS_FILL, OrderPlacer,
                       goto_buy_page, login_state, watch_checkout_block,
                       click_id, eval_in_frames, fill_field, frame_for_id,
                       is_session_expired, is_sign_in, read_field)

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

# 购物袋只读诊断使用的限购提示。
LIMIT_PATTERNS = ("最多可购买", "请调整订单", "超出购买上限", "限购", "购买数量")


_PART_IN_URL = re.compile(r"/([A-Z0-9]{4,}[A-Z]{2}/A)(?:[/?#]|$)", re.I)


def _part_of(url: str) -> str:
    """从购买页地址里取出 part number，取不到返回空串。"""
    m = _PART_IN_URL.search(url or "")
    return m.group(1).upper() if m else ""


#: 判断逻辑在 checkout.py（那边的 enter/on_checkout 也要用，而它不能反向
#: import autobuy）。这里起个别名，保持本模块内的老叫法。
_is_sign_in = is_sign_in
_is_expired = is_session_expired


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
    order_created: bool = False
    retry_after: float = 0.0
    #: 这条结果值不值得在免打扰时段把人叫醒。登录掉了就值得——凌晨掉线、
    #: 早上才发现，等于整晚的监控白挂，而重登要人过双重认证。
    wake: bool = False
    #: 已经买够 max_orders 台，收工。不是失败，也不该重试。
    quota_done: bool = False
    #: 蹲守（camp）会话到期/蹲够了，重建后接着蹲。不是失败——外层 CampWorker
    #: 看到它就重新加购+进结账，继续蹲下一段。
    rebuild: bool = False
    #: **这个型号这一轮没救了**（页面明写售罄、配置不对）。跟 retriable=False
    #: 不是一回事：被限流也是 retriable=False，但那是**全局**的，冷却过了还得
    #: 接着打这个型号——拿它判死等于一次 541 就把型号永久排除。
    fatal: bool = False


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
    """取 Apple ID 密码。只认环境变量。

    一台机器跑多个账号时，每个账号在 config 里写自己的 `pwd_env`（环境变量的
    **名字**，不是值），各读各的。不写就还是那个公共的 HUNTER_APPLE_PWD。
    """
    if cfg.get("pwd"):
        log(f"⚠️ config.json 里还留着 autobuy.pwd —— 已忽略，请删掉它。"
            f"密码改用环境变量 {PWD_ENV}。")
    return os.environ.get(str(cfg.get("pwd_env") or "").strip() or PWD_ENV, "")


def _on_main(url: str, region: str) -> bool:
    """页面是不是已经在主站上。

    购物袋接口是 `fetch("/shop/bag")`——相对地址，跟着 `location.origin` 走，
    **不读当前页面的 DOM**。所以只要 origin 对，停在主站的哪一页都能问袋子，
    根本不用先导航到 /shop/bag。

    这不是抠细节：2026-09-20 09:42 那次放货，光 `goto(/shop/bag)` 就花了
    **16 秒**（放货瞬间主站很慢），而页面本来就停在 /shop/bag 上——
    _park_after_attempt 每次失败后就把它带回那儿了。
    """
    base = REGIONS.get(region) or ""
    return bool(base) and (url or "").startswith(base + "/")


def _pages(ctx) -> set:
    """context 当前开着的标签页。拿不到就当空集——这只用来收拾自己开出来的
    多余标签，问不出来时宁可不收，也不能让预热本身炸掉。"""
    try:
        pages = ctx.pages
    except Exception:
        return set()
    return set(pages) if isinstance(pages, (list, tuple, set)) else set()


def stores_of(cfg: dict) -> list[str]:
    """这份配置的门店名单。**一份名单，一个含义：只盯这几家，也只在这几家买。**

    分成「盯的」和「买的」两份是没有意义的：放货是每家门店各自独立的，A 店有货
    完全不说明 B 店有货。所以盯一家不打算买的店，除了白烧巡检预算，还会在放货
    那一刻喊出一条没人能用的信号；反过来，能买却没盯的店放货时我们压根看不见。

    留空 = 人没有限制过门店，附近哪家有货就去哪家。

    `pickup.stores` 是正式的那个键；`autobuy.pickup_store_numbers` 是老配置里
    的写法，只在前者没写时才认。
    """
    pk = _store_list((cfg.get("pickup") or {}).get("stores"))
    if pk:
        return [x.upper() for x in pk]
    ab = cfg.get("autobuy") or {}
    return [x.upper() for x in _store_list(ab.get("pickup_store_numbers"))]


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
        self.submit_guard = None
        self.cancelled = lambda: False
        self.mode = self.cfg.get("mode", "auto")
        self.cdp_url = self.cfg.get("cdp_url", "")
        self.cdp_port = int(self.cfg.get("cdp_port", DEFAULT_CDP_PORT))
        self.profile = root / self.cfg.get("profile_dir", ".browser-profile")
        self.headless = bool(self.cfg.get("headless", False))
        self.trade_in_text = self.cfg.get("trade_in", "不折抵")
        self.applecare_text = self.cfg.get("applecare", "不加 AppleCare")
        self.timeout = int(self.cfg.get("timeout_ms", 30000))
        # 到店取货门店偏好。实际结账用门店编号。
        # 可以给一串门店——哪家真有货是放货那一刻才知道的，写死一家等于
        # 另外几家放货时白白卡在选店那一步。
        self.pickup_stores = _store_list(self.cfg.get("pickup_stores"),
                                         self.cfg.get("pickup_store_name"))
        # 已经加进购物袋了就不能盲目重试加购，否则会重复下单。
        #: 但光记「加过了」不够——必须记**加的是哪个 part**。监控盯着十几个配置，
        #: A 色放货、加购后失败重试，下一轮命中的可能是 B 色；这时如果只看
        #: 「加过了」就跳过清袋和加购，等于拿着 A 色去给 B 色结账，买错机器。
        self.bagged_part = ""
        #: 「必须停下来等人」——只有「结果不明」和「已有未了结的提交记录」会置它。
        #: **成功下单不置**：买够几台由 PurchaseGuard 的配额说了算（max_orders），
        #: 拿它当停止信号的话，一单成功就全线停摆，max_orders 大于 1 永远不生效。
        self.halt_for_human = False
        #: 这一单是不是可能产生了订单。每次 _drive 开头清零，只用来决定收尾时
        #: 要不要把标签带离结账页（那一页上有二维码和订单号）。
        self._attempt_order = False
        #: 刚读到的购物袋状态，只用来把「加购后复核」和「进结账读袋」并成一次。
        self._bag_state = None
        #: 守株待兔上下文。None = 普通冷启动下单；非 None = 进结账后不走六步，
        #: 而是停在那儿反复打 search（见 camp()）。{wake, stop, cadence, max_seconds}
        self._camp = None
        #: 上一轮打的是哪个型号、什么时候打完的。同型号重试时用来决定要不要走
        #: 「先问购物袋」的快路。用墙上时钟，因为它要跨越很长的空闲期。
        self._last_part, self._last_at = "", 0.0
        self.order_placed = False
        # 预热用的常驻会话
        self._pwctx = self._pw = self._ctx = self._page = None
        self._attached = False
        self.warmed = False
        #: 预热时验到的登录态。None = 还没验过。放货那一刻才发现没登录
        #: 就来不及了——登录要 10~25s，撞上双重认证更是直接出局。
        self.signed_in: bool | None = None
        self.login_note = ""
        #: 最后一次**权威**验到登录的墙上时钟。idmsa 的 DES 凭证只有 15 天，
        #: 到期必须人工过双重认证——脚本代不了劳，所以到期前要提前喊。
        self.login_verified_at: float | None = None
        # 预热体检发现购物袋超限时置位：超限还继续加购只会让情况更糟
        self.bag_over_limit = ""
        # 加购前先清空购物袋。Apple 限购（iPhone 每人 2 台），袋里有存货
        # 会让新加的这台结不了账，而且 Apple 不弹错、只是静默卡住。
        self.clear_bag = bool(self.cfg.get("clear_bag_before_add", True))
        #: 空闲期就把袋子清空，把清袋开销挪出抢购关键路径。见 preflight_clear_bag。
        self.preclear_bag = bool(self.cfg.get("preflight_clear_bag", True))
        #: 最多买几台。Apple 限购 2；默认 1——多买是不可逆的花钱动作，要显式写。
        self.max_orders = max(1, int(self.cfg.get("max_orders", 1) or 1))
        #: 距上次打同一型号多久之内还值得赌「袋里还在」。赌错只多花一次
        #: /shop/bag 的加载（几秒），赌对省掉一次产品页（十几到二十几秒）。
        self.bag_trust_seconds = float(self.cfg.get("bag_trust_seconds", 600))
        #: 用 atbtoken 一个 GET 直接加购，跳过整个产品页。实测 388ms vs 11~28s。
        self.fast_add = bool(self.cfg.get("fast_add_to_cart", True))
        #: 空闲期把结账那道登录墙提前撞掉。默认关——它会往购物袋里加一台
        #: （随后清掉），这个副作用得由使用者明确同意。
        self.warm_checkout = bool(self.cfg.get("preflight_warm_checkout", False))
        #: 发结账请求之前再问一次监控「货还在吗」，已经没了就别发。
        self.abort_when_gone = bool(self.cfg.get("abort_when_gone", True))
        #: 由 PurchaseWorker 注入：callable(part) -> True/False/None。
        #: None = 问不出来，那就照常往下走，宁可白跑也不错过。
        self.stock_live = None
        self.place_order = bool(self.cfg.get("place_order", True))
        self.payment_method = (self.cfg.get("payment_method") or "支付宝").strip()
        self.installment_months = int(self.cfg.get("installment_months") or 0)
        # 结账主机（secureN）。留空 = 每次自动探；探到后本进程记住，
        # 抢购当天就不用在挨个试上浪费秒数了。
        self.secure_host = (self.cfg.get("checkout_host") or "").strip()
        # 测试用：一路走到 Review 页就停，不点「立即下单」
        self.stop_at_review = bool(self.cfg.get("stop_at_review", False))
        # 旧配置里的 fast_path 不再控制路径；快车道是唯一结账实现。
        if self.cfg.get("fast_path") is False:
            self.log("[自动下单] fast_path=false 已废弃，结账仅使用快车道")
        #: 快车道要的是门店**编号**（R581），而 pickup_stores 存的是名字（五角场）。
        #: 这两者不能混：selectStore=五角场 服务端不认，而且不会报错、只是选不中。
        #: 放货那一刻优先用监控报上来的「真有货的那几家」，它们本来就是编号。
        #: 配置里的门店名单，**同时是偏好顺序和硬边界**——就这一份。
        #: 放货那一刻监控报上来的「真有货的那几家」会排到它前面，但不会超出它：
        #: 结账侧换店的候选来自 Apple 返回的附近十几家店，不设边界就会安静地
        #: 换到一家没配过的店把单下掉，等发现时人已经要跑去另一个区取机器了。
        self.pickup_store_numbers = [
            x.upper() for x in _store_list(self.cfg.get("pickup_store_numbers"))]
        self.pickup_city = str(self.cfg.get("pickup_city") or "上海")
        self.pickup_state = str(self.cfg.get("pickup_state") or "上海")
        self.pickup_district = str(self.cfg.get("pickup_district") or "杨浦区")
        #: 取货时段：earliest（默认，最早可选）/ latest / "HH:MM"（当天不早于它的第一档）。
        self.pickup_time = str(self.cfg.get("pickup_time") or "")
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
                f"没装 playwright。用项目自带的虚拟环境跑：{PY_CMD} -m hunter ..."
            ) from e
        self._pwctx = sync_playwright()
        self._pw = self._pwctx.__enter__()
        try:
            self._ctx, self._attached = self._launch(self._pw)
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if self._pwctx is not None:
            try:
                self._pwctx.__exit__(None, None, None)
            except Exception:
                pass
        self._pwctx = self._pw = self._ctx = self._page = None
        self._login_page = None
        self.warmed = False

    @property
    def warm_alive(self) -> bool:
        """预热是否**真的**还能用。

        光看 self.warmed 不够：它只在 stop() 里被清掉，所以你手滑关掉那个标签页
        之后它仍是 True。捡漏要挂好几天，这事几乎必然发生——而代价是 _ensure_warm
        直接返回、永远不再预热，等到真放货才在 fire() 里抛「页面没预热好」。
        监控日志一路正常，下单静默哑火，最坏的一种失败。
        """
        if (not self.warmed or self._page is None
                or time.monotonic() - getattr(self, "warmed_at", time.monotonic())
                > float(self.cfg.get("warm_max_age", 180))):
            return False
        try:
            if self._page.is_closed():
                return False
            # 预热页挂着的这几小时里会话可能早没了。带着一个死会话去 fire()，
            # 六步会一步步撞墙，而日志看着像「代码坏了」。
            if _is_expired(self._page.url):
                self.log("[预热] ⚠️ 预热页被踢到「操作超时」页，预热作废")
                self.warmed = False
                return False
            return True
        except Exception:
            return False

    def _remember_host(self, st: dict, where: str) -> None:
        """白捡的：账号入口的绝对地址带着本会话分到的那台 secureN，
        省掉抢购当天 checkout_candidates 挨个试的开销。"""
        if st.get("secure_host") and not self.secure_host:
            self.secure_host = st["secure_host"]
            self.log(f"[预热] 结账主机记为 {st['secure_host']}（从{where}读到）")

    def _mark_signed_in(self, st: dict, where: str) -> None:
        self.signed_in = True
        self.login_note = ""
        self.login_verified_at = time.time()
        self._remember_host(st, where)

    def _preflight_login(self, page) -> str:
        """结账预热跑不起来时的兜底：**直接去账号页**，没登录就当场登掉。

        原来是「先翻订单页验一次 → 判定没登录 → 再跳账号页 → 登完再翻一次订单页」，
        三次导航，其中两次订单页是多余的：**账号页没登录就会跳登录页，这件事本身
        就是判据**，而且它跟结账要的是同一级鉴权。多翻的那两趟不只是慢——每一次
        导航都是一次真实请求，在一个每 5 分钟跑一轮的保活里白白翻一倍。

        这条路是兜底，不是主力。正常情况下结账预热（warm_checkout_session）就把
        登录态给出来了，那才是最权威的判据——它撞的正是下单要的那道墙。
        """
        try:
            page.goto(f"{REGIONS[self.region]}/shop/account/home",
                      timeout=self.timeout, wait_until="domcontentloaded")
            self._settle(page)
        except Exception as e:
            self.signed_in = None
            self.login_note = f"跳账号页失败：{type(e).__name__}"
            return "登录态没验出来"

        if not _is_sign_in(page.url):
            # 账号页没把我们踢去登录 = 它认我们。这是服务端判的，不是页面上
            # 渲染了个账号入口——后者不算数（实测过报「已登录」、点订单却要登录）。
            self._mark_signed_in({"signed_in": True, "evidence": "账号页"}, "账号页")
            return "已登录"

        self.signed_in = False
        self.log("[预热] ⚠️ 没登录——现在就登，别等到放货那一刻")
        ok, why = self._sign_in(page)
        if not ok:
            self.login_note = why
            self.log(f"[预热] ⚠️ 自动登录没成功：{why}")
            return f"⚠️ 未登录：{why}"
        self.log("[预热] 登录完成——这 10~25s 花在开卖前，不占抢购时间")

        # 「离开了登录页」不等于「账号页认你」，再确认一次落点。
        if _is_sign_in(page.url):
            self.signed_in = None
            self.login_note = "登完还停在登录页"
            return "⚠️ 登录态不明"
        self._mark_signed_in({"signed_in": True, "evidence": "账号页"}, "账号页")
        return "已登录（预热时补登）"

    #: 登录凭证所在的域。大陆站不是 `.apple.com` 的 myacinfo，而是
    #: `.idmsa.apple.com.cn` 下的 DES<hash>，15 天到期（2026-09-14 实测）。
    LOGIN_COOKIE_DOMAIN = "idmsa.apple"
    LOGIN_COOKIE_PREFIX = "DES"

    def login_days_left(self) -> float | None:
        """登录凭证还有几天到期。None = 读不到（没连上浏览器 / 还没登录）。

        读的是 cookie 自己的到期时间戳，不是「我上次检查过了多久」——后者在进程
        重启后归零，等于永远不会预警，而凭证该到期还是到期。这一读零请求，直接
        问浏览器的 cookie jar。
        """
        ctx = self._ctx
        if ctx is None:
            return None
        try:
            jar = ctx.cookies()
        except Exception:
            return None
        now, best = time.time(), None
        for c in jar or []:
            name = str(c.get("name") or "")
            domain = str(c.get("domain") or "")
            if self.LOGIN_COOKIE_DOMAIN not in domain or not name.startswith(self.LOGIN_COOKIE_PREFIX):
                continue
            try:
                exp = float(c.get("expires") or -1)
            except (TypeError, ValueError):
                continue
            if exp <= 0:      # 会话 cookie：浏览器一关就没，给不出剩余天数
                continue
            left = (exp - now) / 86400.0
            best = left if best is None else min(best, left)
        return best

    def preflight_clear_bag(self, page) -> str:
        """空闲期把购物袋清空，让放货那一刻的 prepare_bag 直接走「本来就是空的」。

        这是抢购关键路径上最大的一块固定开销：清袋要一次状态读 + 每件一个 POST，
        清完还得重开产品页（2.5s）并重选必选项。挪到这里，放货时全省掉。
        """
        from .fastpath import prepare_bag
        r = prepare_bag(page, want_part="", want_origin=REGIONS[self.region], log=self.log)
        if not r.get("ok"):
            return f"购物袋没清成：{r.get('reason') or '原因不明'}"
        self.bag_over_limit = ""
        self.bagged_part = ""
        return f"购物袋已清空（{r.get('removed', 0)} 件）" if r.get("removed") else "购物袋本来就是空的"

    def _warm_add_via_page(self, page, buy_url: str, part: str) -> str:
        """接口加购没进袋时的兜底：走产品页真点一次加购。返回空串=进袋了。

        为什么值得这一趟 700KB：预热的全部意义是**提前把结账墙撞掉**，而进结账
        必须袋里有货。接口加购靠的 as_atb 是一次性的，清袋后 cookie 里那个用过的
        token 会让下一轮的 GET 静默失败——2026-09-20 机器 B 每轮预热都卡在这里，
        于是每次真放货都得当场登录。产品页这条路必到袋（不折抵/不 AppleCare
        选好再点加购），顺带让页面重新种一个新鲜的 as_atb，接口加购下轮又能用。
        """
        from .fastpath import prepare_bag, wait_for_bag_count
        try:
            page.goto(buy_url, timeout=self.timeout * 2, wait_until='domcontentloaded')
        except Exception as e:
            return f'探路加购没进袋，退产品页也打不开（{type(e).__name__}）'
        self._await_options(page)
        self._pick(page, 'tradein', self.trade_in_text)
        self._pick(page, 'applecare', self.applecare_text)
        ok, why = self._wait_add_button(page)
        if not ok:
            return f'探路加购没进袋，产品页也点不了加购（{why}）'
        try:
            page.locator(SEL_ADD_TO_CART).first.click(timeout=self.timeout)
        except Exception as e:
            return f'探路加购没进袋，产品页点加购失败（{type(e).__name__}）'
        wait_for_bag_count(page, cap_ms=2000)
        st = prepare_bag(page, want_part=part, want_origin=REGIONS[self.region],
                         log=self.log)
        if st.get('kept'):
            self.log('[预热] 接口加购没进袋，已退产品页点加购补进（顺带种回 as_atb）')
            return ''
        # 产品页都没能进袋：多半是掉登录，把结论留给随后的登录检查。
        return f"探路加购没进袋（{st.get('reason') or '产品页加购后袋里仍没有它'}）"

    def warm_checkout_session(self, ctx, page, buy_url: str) -> str:
        """空闲期把结账那道登录墙撞掉，别让它出现在放货的关键路径上。

        实测（2026-09-18 / 09-19 的日志，11 次进结账）：**墙一次付清，后面全免**
        ——中间清过袋、换过型号、换过门店，结账会话照样认：

            07:03:37 被拦在登录页 → 07:03:41 登录完成
            07:06:15 直接进结账，无墙（换了型号）
            07:07:53 直接进结账，无墙（清袋重加过）
            07:15:49 直接进结账，无墙（12 分钟后）

        有效期在 12~45 分钟之间（09-19 22:43 付过，23:31 又撞上了），所以由每
        10 分钟一次的保活顺手维持。代价是空闲期 2~8 秒，换掉放货时的 5~20 秒。

        加进去的那台随后由 preflight_clear_bag 清掉；真撞上「正好在这几秒放货」，
        PurchaseGuard 保证两者不会同时跑，purchase 侧的 prepare_bag 会发现型号
        不对、清掉重加，多花 ~700ms，不会买错。
        """
        from .fastpath import atb_add_url, atb_token, prepare_bag, Blocked
        part = _part_of(buy_url)
        if not part:
            return '没有可用的探路型号'
        token = atb_token(ctx)
        if not token:
            # 全新的 profile 没逛过产品页，as_atb 还没种上——2026-09-20 机器 B
            # 就是这样，每轮保活都在这儿放弃，等到放货时加购要走 11~28 秒的产品页
            # 而不是 388ms 的接口。空闲期不差这一趟：加载一次产品页把它种上。
            try:
                page.goto(buy_url, timeout=self.timeout, wait_until='domcontentloaded')
                self._settle(page)
            except Exception as e:
                return f'读不到 atbtoken，加载产品页种 cookie 也失败：{type(e).__name__}'
            token = atb_token(ctx)
            if not token:
                return '读不到 atbtoken（加载过产品页仍没有），跳过结账预热'
            self.log('[预热] atbtoken 原来没有，已加载一次产品页把它种上')
        # **先看袋里有什么，再决定加不加。** 上一单失败会把目标型号留在袋里
        # （同型号重试的快路要靠它），这时直接再加一台探路型号就成了两条——
        # 接着 prepare_bag 把两条一起删掉、报「探路加购没进袋」，外层据此
        # 误判登录态、白付一次 10~25s 的登录。2026-09-20 08:57、09:43、09:51
        # 三行「删了 2 件」全是这个形状：不是重复加购，是遗留 + 探路。
        # 袋里正好就是探路型号时连加都不用加，省一个 token；不是就先清掉。
        #
        # **只在页面已经在主站上时才先读袋。** 每轮预热的登录检查页是新开的，
        # 初始停在 about:blank（origin=null），在那儿读袋必然被主站校验判「不能
        # 当真」——白读一次、还刷一行吓人的日志。不在主站就直接走下面的加购，
        # 那一步本来就会导航到产品页、之后再读就正常了。
        here = ""
        try:
            here = page.url or ""
        except Exception:
            here = ""
        st = (prepare_bag(page, want_part=part, want_origin=REGIONS[self.region],
                          log=self.log)
              if _on_main(here, self.region) else {})
        if not (st.get('ok') and st.get('kept')):
            try:
                page.goto(atb_add_url(buy_url, part, token), timeout=self.timeout,
                          wait_until='domcontentloaded')
            except Exception as e:
                return f'探路加购失败：{type(e).__name__}'
            st = prepare_bag(page, want_part=part, want_origin=REGIONS[self.region],
                             log=self.log)
        if not st.get('kept'):
            # **接口加购没进袋不能就此放弃——那正是每次结账都要登录的病根。**
            # as_atb 是一次性的：上一轮预热用过、清了袋，cookie 里却还留着那个
            # 用过的 token，这一轮拿它再发 GET 服务端 200 但袋子不动（日志里的
            # 「token 多半已用过」）。于是预热没进结账、没撞墙，放货那一刻的
            # fire 只能当场登录 8~31s。退回产品页老老实实点一次加购：既把探路
            # 商品实打实放进袋，又让页面 JS 重新种一个新鲜的 as_atb——下一轮的
            # 接口加购也跟着能用了。这一趟 700KB 花在空闲期，不占抢购时间。
            note = self._warm_add_via_page(page, buy_url, part)
            if note:
                return note
        before = _pages(ctx)
        try:
            landed = self._enter_checkout(ctx, page, part)
            self._settle(landed)
            if _is_sign_in(landed.url):
                ok, why = self._sign_in(landed)
                self.signed_in = True if ok else False
                note = '结账登录墙已撞掉' if ok else f'⚠️ 结账登录墙没撞掉：{why}'
            else:
                # 结账页认了我们——这是比订单页更硬的登录证据，它就是下单要的那一级
                self.signed_in = True
                self.login_note = ""
                self.login_verified_at = time.time()
                note = '结账会话已就绪（本来就没墙）'
            m = re.search(r'https://(secure\d+)\.', landed.url or '')
            if m and not self.secure_host:
                self.secure_host = m.group(1)
                self.log(f'[预热] 结账主机记为 {m.group(1)}（从结账页读到）')
            return note
        except Blocked:
            raise
        except Exception as e:
            return f'进结账失败：{type(e).__name__}: {e}'
        finally:
            # 结账页有个「5 分钟不操作就超时」的计时器（模型里的 interactionMs），
            # 预热完必须收拾干净：
            #   1. _checkout_via_bag 可能**另开一个标签**并返回它，那个标签不收掉
            #      就会一直停在结账页上滴答，过会儿弹超时——用户会看见。
            #   2. 自己这一页也要带离结账页。顺带落在 /shop/bag 上，正好是后面
            #      清袋要的那个 origin。
            for extra in _pages(ctx) - before:
                try:
                    extra.close()
                except Exception:
                    pass
            try:
                page.goto(f'{REGIONS[self.region]}/shop/bag', timeout=self.timeout,
                          wait_until='domcontentloaded')
            except Exception:
                pass

    def prepare(self, probe_url: str = ""):
        """空闲期的保活 + 体检：验登录（权威）、顺手清袋，不加购。

        这一趟的每一次**真实导航**都同时续了 `as_dc`(2h) 和 `as_sfa`(180d)——
        两者都只认「导航 + 跑 JS」，fetch 整个 HTML 都不算。所以保活和验证是
        同一个动作，不额外花请求。
        """
        from .purchase_guard import PurchaseGuard
        # check_orders=False：保活只要那把串行锁，不该因为「买够了」而报错
        with PurchaseGuard(self.root, check_orders=False):
            self.start()
            check = getattr(self, '_login_page', None)
            if check is None or check.is_closed():
                check = self._ctx.new_page()
                self._login_page = check
            if _is_sign_in(check.url) and self._sign_in_blocked(check):
                return self._sign_in_blocked(check)
            # 结账预热本身就是最权威的登录验证——它撞的正是下单要的那一级鉴权。
            # 开着它的时候不用再去翻订单页，那一趟纯属多余的导航。
            def warm():
                if not (self.warm_checkout and probe_url):
                    return ''
                try:
                    return self.warm_checkout_session(self._ctx, check, probe_url)
                except Exception as e:
                    return f'结账预热出错：{type(e).__name__}'

            def add(note, more):
                return f'{note}；{more}' if note and more else (note or more)

            note, self.signed_in = '', None
            note = warm()
            if self.signed_in is None:
                # 预热关着、或者它没能给出结论（读不到 token、探路加购没进袋）：
                # 退回订单页探针，那是不下单也能问出登录态的唯一办法。
                note = add(note, self._preflight_login(check))
                if self.signed_in is True:
                    # 刚补登成功 = 这一轮的结账墙还立着。立刻补一次，别等下一轮。
                    # 2026-09-20 07:20 掉线、07:27 放货，正好撞上，白付 21 秒。
                    note = add(note, warm())
            # **不挂在 signed_in 上。** 登录判不准正是袋子最可能脏的时候（预热
            # 跑不起来多半就是因为加购没进袋），那时候更该清一遍。清一个空袋子
            # 不花什么，留一条脏条目却会让下一单整单死在「所有门店不可取」上。
            if self.clear_bag and self.preclear_bag:
                try:
                    check.goto(f'{REGIONS[self.region]}/shop/bag', timeout=self.timeout,
                               wait_until='domcontentloaded')
                    self._settle(check)
                    note = f'{note}；{self.preflight_clear_bag(check)}'
                except Exception as e:
                    # 清袋失败不该影响登录结论——放货时 prepare_bag 还会再清一次。
                    note = f'{note}；购物袋没清成：{type(e).__name__}'
            self.log(f'[购买就绪检查] {note}')
            if self.signed_in is True:
                check.close()
                self._login_page = None
            # 验证码/登录失败时保留页面，让用户完成验证。
            return note

    def warm(self, url: str):
        from .purchase_guard import PurchaseGuard
        with PurchaseGuard(self.root, check_orders=False):
            result = self._warm_inner(url)
            self.warmed_at = time.monotonic()
            return result

    def _warm_inner(self, url: str) -> str:
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
        if not goto_buy_page(self._page, url, log=self.log, timeout_ms=self.timeout * 2):
            # 预热到一个 404 页面比不预热更糟：fire() 会以为一切就绪
            self.warmed = False
            raise AutoBuyUnavailable(f"购买页打不开（连续落到 /shop/404）：{url}")
        self._await_options(self._page)
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
            # 登录预检要在体检之前：没登录的话购物袋读出来也不是你的
            state += "；" + self._preflight_login(chk)
            st = self.bag_state(chk)
            self.bag_over_limit = st.get("limitMsg") or ""
            if st.get("items") and self.clear_bag:
                from .fastpath import prepare_bag
                prepared = prepare_bag(chk, want_origin=REGIONS[self.region], log=self.log)
                if not prepared.get("ok"):
                    raise AutoBuyUnavailable(prepared.get("reason") or "购物袋准备失败")
                n = prepared.get("removed", 0)
                self.log(f"[预热] 购物袋已清空（移除 {n} 件）")
                state += f"；已清空购物袋（{n} 件）"
                self.bag_over_limit = ""
                if n:
                    self._page.goto(url, timeout=self.timeout * 2, wait_until='domcontentloaded')
                    self._await_options(self._page)
                    self._pick(self._page, 'tradein', self.trade_in_text)
                    self._pick(self._page, 'applecare', self.applecare_text)
            elif self.bag_over_limit:
                self.log(f"[预热] ⚠️ 购物袋已超限购：{self.bag_over_limit}"
                         f"——现在就清空，否则开卖时结不了账")
                state += "；⚠️ 购物袋超限，先清空"
            elif st.get("items"):
                self.log(f"[预热] 提醒：购物袋里已有 {st['items']} 件商品，"
                         f"抢购前建议清空（iPhone 限购 2 台）")
                state += f"；购物袋已有 {st['items']} 件，建议清空"
        except Exception as e:
            from .fastpath import Blocked
            if isinstance(e, Blocked):
                self.warmed = False
                raise
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
        名称只影响展示，实际候选限制使用门店编号。
        """
        allow = self.pickup_stores
        hot = [s.strip() for s in (in_stock or []) if s and s.strip()]
        if not allow:
            return hot
        if not hot:
            return list(allow)
        first = [s for s in allow if any(s in h or h in s for h in hot)]
        rest = [s for s in allow if s not in first]
        # 名称仅保留显示偏好；不能否决库存接口给出的门店编号。
        unmatched = [h for h in hot if not any(a in h or h in a for a in allow)]
        return first + unmatched + rest

    def fire(self, url: str = "", in_stock: list[str] | None = None,
             in_stock_numbers: list[str] | None = None) -> BuyResult:
        """放货瞬间调用：用预热好的页面加购并进结账。

        url 是**这次真正命中的那个型号**的购买页，必须核对：预热页加载的是
        监控列表里的第一个型号，而放货的可能是任何一个——不核对就会出现
        「提示银色、袋里进黑色」。对不上就老实跳转，慢几秒也比买错强。

        in_stock 是监控刚查到「有货」的门店**名**（用来排页面上的点击顺序），
        in_stock_numbers 是同一批店的**编号**（快车道发包只认编号）。两个都要传：
        少传编号的话，快车道会退回配置里的第一家，哪怕那家根本没货。
        """
        if not self.warmed or self._page is None or self._page.is_closed():
            raise AutoBuyUnavailable("页面没预热好")
        want, got = _part_of(url), _part_of(getattr(self._page, "url", ""))
        if want and want != got:
            self.log(f"[自动下单] 预热页是 {got or '未知'}，这次要买 {want}——"
                     "跳转到正确型号（放弃预热加速）")
            return self._drive(self._ctx, self._page, url, dry_run=False,
                               in_stock=in_stock, in_stock_numbers=in_stock_numbers)
        return self._drive(self._ctx, self._page, None, dry_run=False,
                           in_stock=in_stock, in_stock_numbers=in_stock_numbers)

    def camp(self, url: str, in_stock_numbers: list[str] | None = None,
             *, wake=None, stop=None, cadence: float = 8.0,
             idle_cadence: float = 90.0, hot_seconds: float = 25.0,
             session_seconds: float = 1080.0) -> BuyResult:
        """守株待兔一段：加购目标型号 → 进结账 → 停在 step1 反复打 search。

        跟 fire/buy 的区别只在最后一步——进结账后不走六步，而是蹲着等货
        （见 fastpath.camp）。加购、进结账、登录那套全复用冷启动的逻辑。

        返回的 BuyResult：`rebuild=True` 表示这段会话到期/蹲够了，外层 CampWorker
        重新调一次 camp() 接着蹲；成单/结果不明/被拦等跟 buy 一样处理。

        wake 是放货信号的 Event（收到该型号的 sighting 就 set），stop 是收工判据。
        cadence 是空闲时两发 search 的最小间隔，session_seconds 是多久重建（< TTL）。
        """
        self._camp = {"wake": wake, "stop": stop, "cadence": cadence,
                      "idle_cadence": idle_cadence, "hot_seconds": hot_seconds,
                      "max_seconds": session_seconds}
        try:
            return self._run(url, dry_run=False,
                             in_stock_numbers=in_stock_numbers)
        finally:
            self._camp = None

    def rehearse(self, url: str) -> BuyResult:
        """排练：走到「加入购物袋」前一步就停，不改动购物袋。

        用来验证选择器还有效、以及把 Apple ID 登录态存进 profile。
        **发布前一定要跑一次**，别等抢购当天才发现页面改版了。
        """
        return self._run(url, dry_run=True)

    def buy(self, url: str, in_stock: list[str] | None = None,
            in_stock_numbers: list[str] | None = None) -> BuyResult:
        """真跑：加购 → 创建待付款订单。不会代你付款。"""
        return self._run(url, dry_run=False, in_stock=in_stock,
                         in_stock_numbers=in_stock_numbers)

    # ---------- 内部 ----------

    def _run(self, url: str, dry_run: bool, in_stock: list[str] | None = None,
             in_stock_numbers: list[str] | None = None) -> BuyResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise AutoBuyUnavailable(
                f"没装 playwright。用项目自带的虚拟环境跑：{PY_CMD} -m hunter ..."
            ) from e

        what = "排练" if dry_run else "抢购"
        self.log(f"[自动下单] {what}模式启动：{url}")

        if getattr(self, 'managed', False):
            self.start()
            if self._page is None or self._page.is_closed():
                self._page = self._ctx.new_page()
            self.warmed = False
            return self._drive(self._ctx, self._page, url, dry_run,
                               in_stock, in_stock_numbers)

        with sync_playwright() as pw:
            ctx, attached = self._launch(pw)
            # 挂到已有 Chrome 时开新标签页，不要抢占用户正在看的页面
            page = ctx.new_page() if attached else (ctx.pages[0] if ctx.pages else ctx.new_page())
            try:
                return self._drive(ctx, page, url, dry_run, in_stock=in_stock,
                                   in_stock_numbers=in_stock_numbers)
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

    def _drive(self, ctx, page, url, dry_run, in_stock=None, in_stock_numbers=None):
        from .purchase_guard import PurchaseGuard, PurchaseBusy, PendingOrder, QuotaReached
        from .fastpath import Blocked
        if dry_run:
            return self._drive_inner(ctx, page, url, True, in_stock, in_stock_numbers)
        self._attempt_order = False
        self._bag_state = None
        try:
            with PurchaseGuard(self.root, max_orders=self.max_orders) as guard:
                self.submit_guard = guard
                guard.part = _part_of(url or page.url)
                if self.cancelled():
                    return BuyResult(False, '已停止', page.url, retriable=False)
                return self._drive_inner(ctx, page, url, False, in_stock, in_stock_numbers)
        except Blocked as e:
            return BuyResult(False, "⚠️ 购买链路被限流，已停止", page.url, str(e),
                             retriable=False, retry_after=max(120, e.retry_after))
        except QuotaReached as e:
            return BuyResult(False, '已买够，停止抢购', page.url, str(e),
                             retriable=False, quota_done=True)
        except PendingOrder as e:
            self.order_placed = self.halt_for_human = True
            return BuyResult(False, '已有订单提交记录', page.url, str(e), retriable=False)
        except PurchaseBusy as e:
            return BuyResult(False, '另一购买流程正在运行', page.url, str(e))
        finally:
            cleanup = getattr(self, "_checkout_cleanup", None)
            if cleanup:
                cleanup()
            self._checkout_cleanup = None
            self.submit_guard = None
            self._park_after_attempt(page)

    def _drive_inner(self, ctx, page, url: str | None, dry_run: bool,
               in_stock: list[str] | None = None,
               in_stock_numbers: list[str] | None = None) -> BuyResult:
        t0 = time.monotonic()
        want_part = _part_of(url or page.url)
        if not dry_run and not want_part:
            return BuyResult(False, "缺少明确的目标型号", page.url,
                             "购买链接需要包含 part number；请补全 model_slug 和 part。",
                             retriable=False, fatal=True)
        # 「袋里已经是这次要买的那台」才允许跳过清袋和加购。型号不同就得重来，
        # 哪怕上一轮确实加购成功过。
        bagged_ok = False
        if self.bagged_part and want_part and self.bagged_part != want_part:
            self.log(f"[自动下单] 袋里是上一轮的 {self.bagged_part}，这次要 {want_part}"
                     f"——重新清袋加购")
        stores = self.store_candidates(in_stock)
        if stores:
            self.log(f"[自动下单] 取货门店名称（显示参考）：{' > '.join(stores)}")
        product_url = url or page.url
        # 同型号重试的快路：上一轮已经把这台加进袋了，袋里多半还在——那整个产品页
        # 加载（实测 11.5s ~ 26s，是关键路径上最大的一块）就完全不必做。
        #
        # 但购物袋接口**认 origin**：上一轮结束时页面停在 secureN 的结账页，在那儿
        # 读会得到「袋是空的」，于是又加一台、最后袋里两台（2026-09-14 中过招）。
        # 所以要先回主站——去 /shop/bag（259KB）而不是产品页（700KB）。
        retry = (want_part and want_part == self._last_part
                 and time.time() - self._last_at <= self.bag_trust_seconds)
        # token 是零请求的 cookie 读，所以**先读再决定走哪条路**：读不到就直奔
        # 产品页，不为了一个用不上的快加购白跑一趟购物袋页。
        from .fastpath import atb_token
        token = atb_token(ctx) if (not dry_run and self._fast_add_ready()) else ""
        light = url is not None and not dry_run and self.clear_bag and bool(want_part) and (
            retry or bool(token))
        if light:
            why = (f"同型号重试（距上次 {time.time() - self._last_at:.0f}s）" if retry
                   else "接口加购（不加载产品页）")
            here = ""
            try:
                here = page.url or ""
            except Exception:
                here = ""
            if _on_main(here, self.region):
                # 已经在主站上了，一趟导航都不用——袋子是 fetch 出来的，不看 DOM。
                self.log(f"[自动下单] {why}：页面已在主站，直接问购物袋")
            else:
                self.log(f"[自动下单] {why}：先回主站，产品页能不碰就不碰")
                try:
                    page.goto(f"{REGIONS[self.region]}/shop/bag", timeout=self.timeout,
                              wait_until="domcontentloaded")
                except Exception as e:
                    self.log(f"[自动下单] 购物袋页打不开（{type(e).__name__}），走完整流程")
                    light = False
        fast = light
        self._last_part, self._last_at = want_part, time.time()

        # on_product：页面此刻是不是停在产品页上（能点加购）
        # need_pick ：必选项要不要重选。两者不是一回事——预热页本来就是产品页，
        #             而且折抵/AppleCare 早就选好了，重选纯属浪费。
        on_product, need_pick = url is None, False
        if url is not None and not fast:
            page.goto(product_url, timeout=self.timeout * 2, wait_until="domcontentloaded")
            on_product = need_pick = True
        api_cleared = False
        if not dry_run and self.clear_bag:
            from .fastpath import prepare_bag
            prepared = prepare_bag(page, want_part=want_part,
                                   want_origin=REGIONS[self.region], log=self.log)
            if not prepared.get("ok"):
                return BuyResult(False, "购物袋准备失败", page.url,
                                 prepared.get("reason") or "无法确认购物袋状态")
            bagged_ok = bool(prepared.get("kept"))
            self.bagged_part = want_part if bagged_ok else ""
            self.bag_over_limit = ""
            if fast and bagged_ok:
                self.log(f"[自动下单] 袋里还是 {want_part}，跳过产品页和加购"
                         f"（省下一次整页加载）")
            # 袋里没有：先试接口加购（一个 GET，实测 388ms）。不成再退产品页。
            if fast and not bagged_ok and token:
                bagged_ok, fresh, known = self._fast_add(page, product_url, want_part,
                                                         token)
                if not known:
                    # 加购已经发出去、袋子却读不到：不能再点一次加购去赌，
                    # 那正是「两台同型号」的来路。下一轮 prepare_bag 会重读真实状态。
                    return BuyResult(False, "购物袋状态读不到，没敢再加", page.url,
                                     "快加购已发出但复核不了袋子；再加一次可能变成两台。"
                                     "本次停手，下一轮按真实购物袋重来。")
                if bagged_ok:
                    self.bagged_part = want_part
                    # 复核刚读到的那份状态直接交给 bag_to_checkout，别再 fetch 一次
                    self._bag_state = fresh
            # 还得加购的话，现在才需要产品页：要么快路没成、要么刚清了袋。
            # 清过袋就必须重开——页面上的选择会随着清袋失效（api_cleared）。
            if not bagged_ok and (not on_product or prepared.get("removed")):
                page.goto(product_url, timeout=self.timeout * 2, wait_until="domcontentloaded")
                on_product = need_pick = True
                api_cleared = bool(prepared.get("removed"))
        if not bagged_ok and need_pick:
            # 唯一一处等页面渲染，而且等的是 _pick 真正需要的那两个分区：
            # 分区没渲染出来时 _pick 会静默什么都不选，然后加购按钮一直是灰的，
            # 日志上看着像「必选项没选完」——方向全错。
            self._await_options(page)
            self._pick(page, "tradein", self.trade_in_text)
            self._pick(page, "applecare", self.applecare_text)
        if api_cleared:
            self.log("[快车道] 清空后已重开产品页，必选项重新选过")

        # 已经超限就别再加了——加了也结不了账，只会让袋子更难收拾
        if self.bag_over_limit and not dry_run and not bagged_ok:
            return BuyResult(
                False, "⚠️ 购物袋已超限购，未加购", page.url,
                f"{self.bag_over_limit}\n先把购物袋清空再抢，否则加多少都结不了账。")

        if not bagged_ok:
            ok, why = self._wait_add_button(page)
            if not ok:
                if why.startswith("SOLD_OUT:"):
                    mark = why.split(":", 1)[1]
                    return BuyResult(
                        False, "⚠️ 已经买不到了", page.url,
                        f"页面显示「{mark}」——多半是刚被人抢走。不再重试这一单。",
                        retriable=False, fatal=True)
                return BuyResult(False, "加购按钮不可用", page.url, why)

        if dry_run:
            el = (time.monotonic() - t0)
            self.log(f"[自动下单] 排练通过：{el:.1f}s 内走到可加购状态（未点击）")
            return BuyResult(True, "排练通过（未加购）", page.url,
                             f"耗时 {el:.1f}s，选择器有效")

        if self.cancelled():
            return BuyResult(False, "已停止，未加购", page.url, retriable=False)

        if not bagged_ok:
            # 用 locator 而不是先前抓到的句柄——locator 每次操作都会重新定位
            page.locator(SEL_ADD_TO_CART).first.click(timeout=self.timeout)
            self.bagged_part = ""
            # 只等加购请求出门，不加载 700KB 购物袋页（等 URL 变化最坏会烧掉数秒）。
            # 固定睡 600ms 是两头不讨好：快的时候白等，慢的时候照样空袋进结账。
            # 改成轮询购物袋接口，袋里出现东西就立刻走。
            from .fastpath import wait_for_bag_count
            waited = wait_for_bag_count(page, cap_ms=2000)
            self.log(f"[自动下单] 已点击加购（{time.monotonic() - t0:.1f}s，"
                     f"袋内确认 {waited:.0f}ms），随后复核购物袋")
        else:
            self.log(f"[自动下单] 购物袋里已经是 {self.bagged_part}，跳过加购，直接结账")

        placer = OrderPlacer(
            region=self.region,
            # 真有货的那几家**编号**排最前。原来这里传的是 in_stock（门店名），
            # 而 OrderPlacer 用 R\d+ 过滤，名字会被整个丢掉——于是快车道永远
            # 拿配置里的第一家（R581 五角场），哪怕有货的是静安。
            store_numbers=[s for s in ((in_stock_numbers or [])
                                       + self.pickup_store_numbers) if s],
            allow_stores=self.pickup_store_numbers,
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
            place_order=self.place_order,
            pickup_city=self.pickup_city,
            pickup_state=self.pickup_state,
            pickup_district=self.pickup_district,
            pickup_time=self.pickup_time,
            timeout_ms=self.timeout,
            submit_guard=self.submit_guard, cancelled=self.cancelled,
            # search 头一枪空手时，靠它决定要不要再打一枪：监控明说没货才不打。
            still_live=(lambda p=want_part: self.stock_live(p)) if (
                self.abort_when_gone and callable(self.stock_live)) else None,
            log=self.log,
        )
        # **守株待兔不做「没货就别进结账」预判。** camp 的全部意义就是提前进结账、
        # 停在那儿蹲着等货——此刻监控报没货正是常态。这道 _gone 是给冷启动的
        # 一次性尝试用的（明知没货就别白烧一趟六步），跟 camp 的目标正相反。
        if self._camp is None and self._gone(want_part, "进结账"):
            return BuyResult(False, "货已经没了，没进结账", page.url,
                             "监控在这一刻已经报无货，再走结账只是白烧请求。")
        # 优先接口入口；仅在目标已核对、入口未建立时允许购物袋页面建立会话。
        from .fastpath import Blocked
        blocked = watch_checkout_block(page, log=self.log, ctx=ctx)
        self._checkout_cleanup = blocked.get("close")
        try:
            page = self._enter_checkout(ctx, page, want_part,
                                        state=self._bag_state)
        except Blocked:
            raise  # 包括页面入口抛出的限流，统一由 _drive 转换为冷却结果。
        except Exception as e:
            return BuyResult(False, "⚠️ 结账入口失败，已停止", page.url,
                             f"{type(e).__name__}: {e}")
        if self._page is not None:
            self._page = page
        # 登录页不用等它「稳定」，认出来就直接去登录，省掉那几秒
        if not _is_sign_in(page.url):
            self._settle(page)

        if _is_expired(page.url):
            return BuyResult(False, "⚠️ 结账会话已过期", page.url,
                             "请重新登录；本次快车道尝试已停止。")

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
                # 登录完成后重新建立结账会话，保持同一目标型号。
                try:
                    page.goto(f"{REGIONS[self.region]}/shop/bag", timeout=self.timeout,
                              wait_until="domcontentloaded")
                    page = self._enter_checkout(ctx, page, want_part)
                except Blocked:
                    raise
                except Exception as e:
                    return BuyResult(False, "⚠️ 登录后结账失败，已停止", page.url,
                                     f"{type(e).__name__}: {e}")

        if "/shop/checkout" not in page.url:
            st = self.bag_state(page)
            if st.get("limitMsg"):
                return BuyResult(
                    False, "⚠️ 购物袋超出限购", page.url,
                    f"{st['limitMsg']}\n袋里有 {st.get('items')} 件，先清空再抢。")
            if blocked.get("hits"):
                # 这条路径实际发生过，而且第一反应全走错了方向：结账页其实开了，
                # 是它自己的 fulfillment XHR 被 541 拦掉，前端才把你扔到 /shop/404。
                # 报「页面不存在」会让人去查 slug、查 part、查登录——全是白查。
                ra = blocked.get("retry_after") or 0
                return BuyResult(
                    False, "⚠️ 结账被限流（不是页面不存在）", page.url,
                    f"结账页加载正常，但它的 {len(blocked['hits'])} 个 XHR 被边缘节点拦了"
                    f"（{blocked['hits'][0]['status']}），前端把你重定向到了 /shop/404。\n"
                    f"这是限流不是封号，**别再重试**——Akamai 按累计速率判，"
                    f"越撞退避越深。" + (f"对方要求等 {ra:.0f}s。" if ra else "先停手等它自己解。"),
                    retriable=False, retry_after=max(120, ra))
            return BuyResult(False, "⚠️ 没能进入结账页", page.url,
                             f"加购后没能走到结账页（{time.monotonic() - t0:.1f}s），请手动接管。")

        if not self.place_order:
            return BuyResult(True, "已到结账页", page.url,
                             f"place_order=false，未选择门店或提交订单。总耗时 {time.monotonic() - t0:.1f}s")

        # 把探到的结账主机记回来，同一进程里后续几轮直接命中
        if placer.secure_host:
            self.secure_host = placer.secure_host

        if self._camp is not None:
            c = self._camp
            outcome = placer.camp(page, t0, wake=c.get("wake"), stop=c.get("stop"),
                                  cadence=c.get("cadence", 8.0),
                                  idle_cadence=c.get("idle_cadence", 90.0),
                                  hot_seconds=c.get("hot_seconds", 25.0),
                                  max_seconds=c.get("max_seconds", 1080.0))
        else:
            if self._gone(want_part, "六步"):
                return BuyResult(False, "货已经没了，没走六步", page.url,
                                 "结账会话已建好，但监控在这一刻已经报无货。")
            outcome = placer.place(page, t0)
        return self._wrap(placer, placer.result_url or page.url, *outcome)

    def _enter_checkout(self, ctx, page, want_part, state=None):
        """登录前后共用一个入口，页面入口异常与接口异常按同样方式传递。

        `state` 只在「刚刚读过袋子」时传，用完即弃——登录之后那条路必须重读，
        那时候袋子和会话都可能变了。
        """
        from .fastpath import EntryUnavailable, bag_to_checkout
        self._bag_state = None
        try:
            direct = bag_to_checkout(page, want_part=want_part, want_qty=1,
                                     want_origin=REGIONS[self.region], log=self.log,
                                     state=state)
        except EntryUnavailable:
            return self._checkout_via_bag(ctx, page, want_part)
        if not direct:
            raise AutoBuyUnavailable('购物袋接口未给出有效结账地址')
        page.goto(direct, timeout=self.timeout, wait_until='domcontentloaded')
        return page

    def _checkout_via_bag(self, ctx, page, want_part):
        """仅用于建立结账会话；进入快车道后绝不再调用。"""
        from .fastpath import JS_CART_STATE, CartMismatch, raise_if_blocked
        page.goto(f'{REGIONS[self.region]}/shop/bag', timeout=self.timeout,
                  wait_until='domcontentloaded')
        state = page.evaluate(JS_CART_STATE) or {}
        raise_if_blocked(state, "购物袋页面")
        if (state.get('count') != 1 or state.get('qty') != [1]
                or [str(p).upper() for p in state.get('skus', [])] != [want_part.upper()]):
            raise CartMismatch('购物袋页面无法确认目标型号和数量，未点击结账')
        opened = []
        ctx.on('page', opened.append)
        try:
            # 只点一次，兼容同页跳转和新标签；不等待必然发生的 popup。
            button = page.locator('[data-autom="checkout"]:visible, [data-autom="proceed"]:visible, '
                                  '[data-autom="bagCheckoutButton"]:visible, '
                                  'button:has-text("结账"):visible, a:has-text("结账"):visible')
            button.first.click(timeout=self.timeout)
            deadline = time.monotonic() + self.timeout / 1000
            while time.monotonic() < deadline:
                for candidate in [page] + opened:
                    if '/shop/checkout' in candidate.url or _is_sign_in(candidate.url):
                        candidate.wait_for_load_state('domcontentloaded', timeout=self.timeout)
                        return candidate
                page.wait_for_timeout(100)
            raise AutoBuyUnavailable('点击结账后未得到结账或登录页')
        finally:
            ctx.remove_listener('page', opened.append)

    def _gone(self, want_part: str, where: str) -> bool:
        """监控是不是已经明说这个型号没货了。

        2026-09-20 两台机器一共发了 17 次 search，其中 **10 次是在监控已经打出
        「N 家门店均无货」之后 1~7 秒才发出去的——10 次全输**。而 search 一次要
        10.8 秒、整趟要十几个 checkoutx 请求，还跟「尝试之后一分钟内掉线」强相关
        （6 次里 4 次）。明知必输还打，赔的是会话和下一次机会。

        问不出来（没注入、或者没有记录）一律放行：宁可白跑一趟，也不能因为自己
        的判断失误错过一次真放货。
        """
        if not self.abort_when_gone or not want_part:
            return False
        probe = self.stock_live
        if not callable(probe):
            return False
        try:
            live = probe(want_part)
        except Exception:
            return False
        if live is False:
            self.log(f"[自动下单] 监控已报「{want_part} 没货了」，{where}之前停手——"
                     f"不白烧十几个结账请求")
            return True
        return False

    def _park_after_attempt(self, page) -> None:
        """一单结束后别把标签留在结账页上。

        结账页的模型里写着 `interactionMs: 300000`——**五分钟不操作就自己跳到
        「操作超时」页**。留一个在那儿滴答，人打开浏览器只看到一个吓人的超时提示；
        而且 warm_alive 会因为 _is_expired 把预热判成作废。

        **这一单可能已经下单的一律不动**：`_attempt_order` 覆盖「成功」和「结果
        不明」两种（见 _wrap）。那一页上有二维码和订单号，导航走就找不回来了。
        用它而不是 order_placed——后者成单之后就一直是 True，会让之后每一次
        失败尝试都把标签留在结账页上滴答。
        """
        if self._attempt_order:
            return
        target = getattr(self, "_page", None) or page
        try:
            if target is None or target.is_closed():
                return
            url = target.url or ""
            if "/shop/checkout" not in url and not _is_expired(url):
                return
            target.goto(f"{REGIONS[self.region]}/shop/bag", timeout=self.timeout,
                        wait_until="domcontentloaded")
        except Exception:
            pass

    def _wrap(self, placer, url: str, ok: bool, stage: str, detail: str,
              order_id: str) -> BuyResult:
        """把 placer 的结论包成 BuyResult。

        **`no_retry` 必须在这里变成 `retriable=False`。** 默认的 retriable=True
        会让监控下一轮再跑一遍整条链路——而「结果不明」意味着那一单可能已经成了
        （2026-09-18 07:15 就是这样，订单确认邮件都到了）。重试就是再下一单。
        """
        from .checkout import OrderPlacer
        if not ok and stage == OrderPlacer.CAMP_REBUILD:
            # 蹲守会话到期：不是失败、不推送，让外层重建后接着蹲。
            return BuyResult(False, stage, url, detail, retriable=True, rebuild=True)
        if ok:
            # 成单了：通知和「别动这个标签」都要用它，但**不是**停止信号。
            self.order_placed = self._attempt_order = True
        if getattr(placer, "no_retry", False):
            # 结果不明：这之后谁都别再动手，等人去看邮箱/订单列表。这才是停止信号。
            self.order_placed = self._attempt_order = self.halt_for_human = True
            return BuyResult(ok, stage, url, detail, order_id=order_id,
                             retriable=False)
        retriable = getattr(placer, "retriable", True)
        return BuyResult(ok, stage, url, detail, order_id=order_id,
                         retriable=retriable,
                         # 配置类的死局（不是自提、没有门店编号）才判死这个型号；
                         # 被限流虽然也 retriable=False，但那是全局的，别判死。
                         fatal=bool(not ok and not retriable
                                    and not getattr(placer, "blocked", False)),
                         order_created=bool(getattr(placer, "fast_ordered", False)),
                         retry_after=max(120, getattr(placer, "retry_after", 0))
                         if getattr(placer, "blocked", False) is True else 0)

    def _recover_session(self, page) -> str:
        """从「操作超时」页里爬出来：重新登录，然后回到购物袋。

        返回空串 = 已经恢复，调用方可以接着往下走；返回说明 = 没救回来。

        为什么不能在原地重试：那一页是 `/shop/sorry/session_expired`，上面连登录框
        都没有，结账会话已经作废（模型里 `interactionMs: 300000`——5 分钟没交互
        就过期）。所以只能先离开它、确认登录态、再从购物袋重新走一遍。
        """
        self.log("[自动下单] ⚠️ 撞上「操作超时」页，结账会话作废了——重新登录")
        self.signed_in = False
        note = self._preflight_login(page)
        self.log(f"[自动下单] 重新登录：{note}")
        if not self.signed_in:
            return (f"结账会话过期，而且没能重新登录（{note}）。"
                    f"请在这个 Chrome 里手动登录 Apple ID，下一轮会再试。")
        try:
            page.goto(f"{REGIONS[self.region]}/shop/bag",
                      timeout=self.timeout, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
        except Exception as e:
            return f"重新登录成功，但回购物袋失败（{type(e).__name__}）。下一轮会再试。"
        self.log("[自动下单] 已重新登录并回到购物袋，继续")
        return ""

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

    #: 产品页「渲染好了」的判据：必选项分区里**已经有单选框**。
    #:
    #: 别把加购按钮算进来——它是服务端直出的，页面一到 domcontentloaded 就在，
    #: 而 tradein/applecare 的单选框要等 JS 水合。把按钮当判据等于一秒就返回，
    #: 然后 _pick 在空分区上什么都选不中、按钮一直是灰的。
    #: 2026-09-19 22:44 和 22:47 两单就是这么丢的（那天之前五天零发生）。
    SEL_READY = (f'{SEL_SECTION.format("tradein")} input[type=radio], '
                 f'{SEL_SECTION.format("applecare")} input[type=radio]')

    #: 快加购把两个必选项编码成查询参数（purchaseOption=fullPrice / acpart=none），
    #: 也就是「不折抵 + 不加 AppleCare」。配置不是这两个就不能走这条路——
    #: 走了会静默买成别的条件，那比慢十几秒严重得多。
    NO_TRADE_IN = ("不折抵", "不换购", "no trade", "none")
    NO_APPLECARE = ("不加", "不购买", "no applecare", "none")

    def _fast_add_ready(self) -> bool:
        if not self.fast_add:
            return False
        t, a = self.trade_in_text.lower(), self.applecare_text.lower()
        return (any(m in t for m in self.NO_TRADE_IN)
                and any(m in a for m in self.NO_APPLECARE))

    def _fast_add(self, page, product_url: str, want_part: str,
                  token: str) -> tuple[bool, dict | None]:
        """用 atbtoken 发一个 GET 把目标加进购物袋。成功返回 True。

        **失败是静默的**：token 用过一次就作废，再发一次照样 200、页面照常、
        袋子纹丝不动。所以返回值一律以 prepare_bag 复核的结果为准，绝不看状态码。
        失败时调用方老实退回产品页那条路。

        返回 (进袋了没有, 复核时读到的购物袋状态, 袋子读到了没有)。第三项为
        False 时前两项没有意义——调用方必须停手，不能退回产品页再加。状态是给紧接着的
        bag_to_checkout 用的——它要的是同一份东西，递过去就少 fetch 一次。
        放货那一刻一次袋状态读要 2.5 秒，那是整趟 21 秒里的一大块。
        状态可能为 None（读法变了、或者固件没带），那时候它自己重读，只是慢一点。
        """
        from .fastpath import atb_add_fetch, atb_add_url, prepare_bag
        t0 = time.monotonic()
        url = atb_add_url(product_url, want_part, token)
        how = "fetch"
        try:
            atb_add_fetch(page, url)          # 页面内发，不导航、不等产品页
        except Exception as e:
            # 连 fetch 都发不出去（页面被关、JS 出错）才退回导航那条老路。
            how = "导航"
            self.log(f"[自动下单] 快加购 fetch 没发成（{type(e).__name__}），改用导航")
            try:
                page.goto(url, timeout=self.timeout, wait_until="domcontentloaded")
            except Exception as e2:
                self.log(f"[自动下单] 快加购请求失败（{type(e2).__name__}），走产品页")
                return False, None
        # 复核：这一步不能省，加购失败没有任何显式信号
        st = prepare_bag(page, want_part=want_part,
                         want_origin=REGIONS[self.region], log=self.log)
        if not st.get("ok"):
            # **读不到袋子 ≠ 没进袋。** 加购那个 GET 多半已经生效了，这时按
            # 「没进袋」退回产品页再点一次加购，袋里就是两台同型号——进结账前
            # 的数量校验会把这一单整个否掉。先再读一次；还读不到就交给调用方
            # 停手，宁可这一轮不下，也不往袋里瞎加。
            try:
                page.wait_for_timeout(400)
            except Exception:
                pass
            st = prepare_bag(page, want_part=want_part,
                             want_origin=REGIONS[self.region], log=self.log)
        known = bool(st.get("ok"))
        ok = bool(known and st.get("kept"))
        el = (time.monotonic() - t0) * 1000
        what = ("快加购成功" if ok else
                "⚠️ 快加购后读不到购物袋，不敢再加" if not known else
                "⚠️ 快加购没进袋（原因见日志上一条）")
        self.log(f"[自动下单] {what}（{el:.0f}ms，{how}，未加载产品页）")
        return ok, (st.get("state") if ok else None), known

    def _await_options(self, page, cap_ms: int = 8000, step_ms: int = 100) -> float:
        """等产品页把必选项分区渲染出来。返回实际等了多少毫秒。

        原来是雷打不动的 `wait_for_timeout(2500)`，而且出现两次（首次打开、清袋后
        重开），光这一项就是 5s 的固定开销——页面通常几百毫秒就好了，按最坏情况
        睡纯属白烧抢购时间。

        等的是 _pick 需要的**单选框**，不是分区壳子、更不是加购按钮：按钮服务端
        直出，一到 domcontentloaded 就在，拿它当判据会一秒返回，然后 _pick 在还没
        水合的分区上什么都选不中，按钮一直是灰的——日志上看像「必选项没选完」，
        方向全错。上限放宽到 8 秒：它是「就绪就走」，等不满不花钱。

        等不到也不报错：紧接着的 _wait_add_button 会用整个 timeout 再等一次，
        并且能把「一直是灰的」「压根没这个按钮」「已经售罄」分开说清楚。
        """
        t0 = time.monotonic()
        deadline = t0 + cap_ms / 1000
        # locator 建一次就够，它每次操作都重新定位；重复 page.locator() 只是噪音。
        loc = page.locator(self.SEL_READY)
        while True:
            try:
                if int(loc.count()) > 0:
                    break
            except (TypeError, ValueError):
                # 数量压根问不出来（拿到的不是数）——再问一万次也一样，别空转。
                break
            except Exception:
                pass    # 重绘期间的瞬时错误，下一轮重新问
            if time.monotonic() >= deadline:
                break
            try:
                page.wait_for_timeout(step_ms)
            except Exception:
                break
        waited = (time.monotonic() - t0) * 1000
        if waited >= cap_ms:
            self.log(f"[自动下单] ⚠️ 等了 {waited:.0f}ms 必选项分区还没渲染出单选框"
                     f"——接下来的选择多半会落空，加购按钮会一直是灰的")
        return waited

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

#: 两种机器都要认：原生 Windows 上是 C:\，WSL 里同一个 Chrome 挂在 /mnt/c。
#: 只写 /mnt/c 那两条的话，在原生 Windows 上一条都命中不了，connect --launch
#: 就只能报「既没找到 Windows Chrome 也没找到 Linux Chrome」。
#: %LOCALAPPDATA% 那条是「只给当前用户装」的 Chrome，很常见。
WIN_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]


def windows_chrome() -> str | None:
    for p in WIN_CHROME_PATHS:
        p = os.path.expandvars(p)
        # 变量没展开（非 Windows 上没有 %LOCALAPPDATA%）就别去 stat 了
        if "%" in p:
            continue
        if Path(p).exists():
            return p
    return None


def windows_userprofile() -> str | None:
    """问 Windows 自己要用户目录，避免猜路径。"""
    if os.name == "nt":
        return os.environ.get("USERPROFILE") or None
    # WSL：只能隔着 cmd.exe 问，而且得站在 Windows 盘上执行，否则 cmd 会先
    # 抱怨 UNC 路径不支持。
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
