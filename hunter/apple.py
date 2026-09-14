"""Apple Store 在线库存查询客户端。

只用 Apple 自己前端在用的公开接口：
  /shop/sba/availability-message  —— 单个/批量 part 的可购买状态 + 发货信息
  /shop/sba/pickup-detail         —— 有货时的门店明细
  /shop/buy-iphone/<slug>         —— 机型页面，用来抓 part number 目录
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum

import requests

from .logbook import log_request

# 一次只用一套完整的浏览器指纹：UA 得跟 sec-ch-ua / platform 对得上，
# 只改 UA 而 client hints 还是旧的，反而比不发这些头更可疑。
BROWSERS = [
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "ch_ua": '"Chromium";v="152", "Google Chrome";v="152", "Not?A_Brand";v="24"',
        "platform": '"macOS"',
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "ch_ua": '"Chromium";v="151", "Google Chrome";v="151", "Not?A_Brand";v="24"',
        "platform": '"Windows"',
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/18.6 Safari/605.1.15",
        "ch_ua": "",   # Safari 不发 client hints，发了才是破绽
        "platform": "",
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0",
        "ch_ua": '"Chromium";v="150", "Microsoft Edge";v="150", "Not?A_Brand";v="24"',
        "platform": '"Windows"',
    },
]

UA = BROWSERS[0]["ua"]   # 兼容老代码里直接引用 UA 的地方

# 各区域站点根地址。key 就是 config 里的 region。
REGIONS = {
    "cn": "https://www.apple.com.cn",
    "hk": "https://www.apple.com/hk",
    "tw": "https://www.apple.com/tw",
    "us": "https://www.apple.com",
    "jp": "https://www.apple.com/jp",
    "sg": "https://www.apple.com/sg",
}


class Stock(str, Enum):
    """库存三态。

    绝不能把「查询失败」折叠成「无货」——那会让程序看起来一切正常、却永远不叫你。
    拿不准一律倒向 UNKNOWN：猜错成无货是错过机会，猜错成未知只是多看一眼。
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass
class StorePickup:
    """某个型号在某家直营店的到店取货状态。"""

    part: str
    store_number: str
    store_name: str
    city: str = ""
    address: str = ""
    state: Stock = Stock.UNKNOWN
    quote: str = ""
    reason: str = ""  # state 为 UNKNOWN 时必须说明原因

    def __post_init__(self):
        if self.state is Stock.UNKNOWN and not self.reason:
            raise ValueError("UNKNOWN 状态必须带原因，否则就退化成了「无货」")


class Blocked(Exception):
    """Apple 边缘节点把请求挡了（常见 541/503），需要退避重试。

    retry_after 是对方 Retry-After 头里给的秒数，没给就是 0。它比我们自己
    猜的退避时间权威，调用方应当优先照它睡。
    """

    def __init__(self, msg: str, retry_after: float = 0.0):
        super().__init__(msg)
        self.retry_after = retry_after


class NotLive(Exception):
    """机型页面还没上线（404 或跳回 iPhone 落地页）。"""


@dataclass
class Availability:
    """来自 sba/availability-message：能不能下单、多久发货。

    注意：这个接口也返回 availableAtAnyStore / partAvailableStoresCount，但实测
    不可靠——不带门店上下文时它对有货的机型也恒返回 0/False。门店库存一律以
    retail/pickup-message 为准，这里不做判断。
    """

    part: str
    name: str = ""
    buyable: bool = False
    reason: str = ""
    delivery: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple:
        """用于比对状态是否变化的指纹。"""
        return (self.buyable, self.reason, self.delivery)

    def describe(self) -> str:
        bits = ["可下单" if self.buyable else f"不可下单({self.reason or '未知'})"]
        if self.delivery:
            bits.append(f"发货 {self.delivery}")
        return " / ".join(bits)


@dataclass
class SkuInfo:
    part: str
    name: str
    price: float | None
    slug: str

    def buy_url(self, base: str) -> str:
        # part 形如 MJT74CH/A，直接拼在 slug 后面，Apple 会跳到已选好该配置的购买页
        return f"{base}/shop/buy-iphone/{self.slug}/{self.part}"


# 抓机型页是一次「导航」，不是 XHR；照 XHR 那组头发过去，头和请求类型对不上。
_NAV_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "X-Requested-With": None,   # requests 里给 None 表示不发这个头
}


def _ua_tag(ua: str) -> str:
    """把 UA 压成一个短标签（如 Chrome/152.0.0.0），日志里用来区分当前是哪套身份。

    存整条 UA 没意义：每行都一样长、还把 jsonl 撑胖。真正要回看的是「被拦之后
    换身份了吗、换成了哪个」。
    """
    tags = re.findall(r"(?:Edg|Chrome|Version|Firefox)/[\d.]+", ua)
    return tags[-1] if tags else "?"


def _retry_after(resp) -> float:
    """把 Retry-After 头解析成秒。对方明说了要等多久，就别自己猜。"""
    raw = (resp.headers.get("Retry-After") or "").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0  # HTTP-date 形式的很少见，忽略即可


class AppleClient:
    def __init__(self, region: str = "cn", timeout: int = 15, proxy: str | None = None):
        if region not in REGIONS:
            raise ValueError(f"不支持的 region: {region}，可选 {'/'.join(REGIONS)}")
        self.region = region
        self.base = REGIONS[region]
        self.timeout = timeout
        self.proxy = proxy
        self.requests_made = 0        # 供 Pacer 记账用
        self.browser = random.choice(BROWSERS)
        self.s: requests.Session = None  # type: ignore[assignment]
        self._open_session()

    # ---------- 会话 ----------

    def _open_session(self) -> None:
        s = requests.Session()
        b = self.browser
        # 真浏览器发的是一整组头。只带 UA 而缺 sec-fetch-* / Accept-Encoding，
        # 在 Akamai 眼里跟写着「我是脚本」差不多。
        headers = {
            "User-Agent": b["ua"],
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": f"{self.base}/shop/buy-iphone",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Requested-With": "XMLHttpRequest",
            "Connection": "keep-alive",
        }
        if b["ch_ua"]:
            headers["sec-ch-ua"] = b["ch_ua"]
            headers["sec-ch-ua-mobile"] = "?0"
            headers["sec-ch-ua-platform"] = b["platform"]
        s.headers.update(headers)
        if self.proxy:
            s.proxies.update({"http": self.proxy, "https": self.proxy})
        if self.s is not None:
            self.s.close()
        self.s = s

    def renew_session(self, new_identity: bool = True) -> str:
        """丢掉当前会话重开一个。被拦之后调用。

        平时不轮换：同一个 IP 上老换 UA、老丢 cookie，本身就是异常信号。
        只有在已经被标记之后，换一套身份才是净收益。
        """
        if new_identity:
            others = [b for b in BROWSERS if b is not self.browser]
            self.browser = random.choice(others or BROWSERS)
        self._open_session()
        return self.browser["ua"].split(") ", 1)[-1]

    # ---------- 底层 ----------

    def _get(self, path: str, params: dict | None = None, want_json: bool = True,
             missing_means_not_live: bool = False, headers: dict | None = None):
        """发一个请求并记账。每个请求都会写进 requests.jsonl（没装日志时是空操作）。

        判定逻辑在 _decode 里，这里只负责发请求、计数、落日志——包括**失败的那些**：
        被拦截的时刻和耗时正是事后校准预算时唯一有用的数据。
        """
        url = path if path.startswith("http") else f"{self.base}{path}"
        self.requests_made += 1
        rec: dict = {"n": self.requests_made, "url": url}
        if params:
            rec["params"] = {k: str(v) for k, v in params.items()}
        t0 = time.monotonic()
        try:
            r = self.s.get(url, params=params, timeout=self.timeout, allow_redirects=True,
                           headers=headers)
        except requests.RequestException as e:
            rec["ms"] = round((time.monotonic() - t0) * 1000)
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
            log_request(**rec)
            raise
        rec["ms"] = round((time.monotonic() - t0) * 1000)
        rec["status"] = r.status_code
        rec["bytes"] = len(r.content)
        rec["ua"] = _ua_tag(self.browser["ua"])
        if r.history:
            rec["final"] = r.url          # 被重定向了，落地在哪很关键（如跳回落地页）
        try:
            return self._decode(r, url, want_json, missing_means_not_live)
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
            raise
        finally:
            log_request(**rec)

    def _decode(self, r, url: str, want_json: bool, missing_means_not_live: bool):
        if r.status_code in (403, 429, 503, 541):
            raise Blocked(f"HTTP {r.status_code} @ {url}", _retry_after(r))
        if r.status_code == 404:
            # 机型页 404 = 还没上线，是预期内的；其他 404 才算错误
            if missing_means_not_live:
                raise NotLive(f"{url} 还没上线（404）")
            raise Blocked(f"HTTP 404 @ {url}")
        r.raise_for_status()
        if not want_json:
            return r
        try:
            return r.json()
        except json.JSONDecodeError as e:
            raise Blocked(f"返回不是 JSON @ {url}: {e}") from e

    # ---------- 库存 ----------

    def availability(self, parts: list[str]) -> dict[str, Availability]:
        """批量查询。Apple 单次接受多个 parts.N，这里按 20 个一批切分。"""
        out: dict[str, Availability] = {}
        for i in range(0, len(parts), 20):
            chunk = parts[i:i + 20]
            params = {f"parts.{n}": p for n, p in enumerate(chunk)}
            data = self._get("/shop/sba/availability-message", params)
            for item in (data.get("body") or {}).get("content") or []:
                dm = item.get("deliveryMessage") or {}
                buy = dm.get("buyability") or {}
                opts = dm.get("deliveryOptionMessages") or []
                part = item.get("partNumber", "")
                out[part] = Availability(
                    part=part,
                    name=dm.get("subHeader") or "",
                    buyable=bool(buy.get("isBuyable")),
                    reason=buy.get("reason") or "",
                    delivery=(opts[0].get("displayName") if opts else "") or "",
                    raw=item,
                )
            if i + 20 < len(parts):
                time.sleep(0.5)
        return out

    def pickup(self, parts: list[str], location: str = "", store: str = "") -> dict[str, list[StorePickup]]:
        """到店取货库存，按 part 分组。

        走 /shop/retail/pickup-message —— Apple 前端「查看门店取货」用的就是它。
        （旧的 /shop/fulfillment-messages 现已对所有请求恒返回 541 拦截页。）

        location 传**邮政编码**，中文城市名会被拒（errorMessage 提示要省市名或邮编）。
        store 传门店编号（如 R448），只查这一家。
        一次可以查多个 part，Apple 会对每家门店返回全部 part 的状态。
        """
        if not parts:
            return {}
        params: dict[str, str] = {"pl": "true", "mts.0": "regular"}
        params.update({f"parts.{n}": p for n, p in enumerate(parts[:20])})
        if store:
            params["store"] = store
        elif location:
            params["location"] = location
        else:
            raise ValueError("查门店取货必须给 location（邮编）或 store（门店编号）")

        body = (self._get("/shop/retail/pickup-message", params).get("body") or {})

        err = body.get("errorMessage")
        stores = body.get("stores")
        if err or not isinstance(stores, list):
            # 拿不到门店列表就是「未知」，不是「无货」
            why = err or "响应里没有 stores 字段"
            return {p: [StorePickup(part=p, store_number="", store_name="",
                                    state=Stock.UNKNOWN, reason=why)] for p in parts}

        out: dict[str, list[StorePickup]] = {p: [] for p in parts}
        for st in stores:
            addr = (st.get("address") or {})
            for part, info in (st.get("partsAvailability") or {}).items():
                display = (info or {}).get("pickupDisplay")
                if display == "available":
                    state, reason = Stock.AVAILABLE, ""
                elif display == "unavailable":
                    state, reason = Stock.UNAVAILABLE, ""
                else:
                    state, reason = Stock.UNKNOWN, f"pickupDisplay 是意料之外的值：{display!r}"
                out.setdefault(part, []).append(StorePickup(
                    part=part,
                    store_number=st.get("storeNumber", ""),
                    store_name=st.get("storeName", ""),
                    city=st.get("city", ""),
                    address=addr.get("address", ""),
                    state=state,
                    quote=(info or {}).get("pickupSearchQuote", ""),
                    reason=reason,
                ))
        # 请求成功但某个 part 完全没出现，同样归为未知
        for p in parts:
            if not out.get(p):
                out[p] = [StorePickup(part=p, store_number="", store_name="",
                                      state=Stock.UNKNOWN, reason="门店响应里没有这个型号")]
        return out

    def nearby_stores(self, location: str, sample_part: str) -> list[StorePickup]:
        """列出某邮编附近的直营店（借一个 part 把门店列表带出来）。"""
        return self.pickup([sample_part], location=location).get(sample_part, [])

    # ---------- 目录 ----------

    #: 页面里「颜色 slug → 本地化色名」的桥。
    #:
    #: 内嵌的商品清单（"name":"iPhone Duo 256GB Star White"）**永远是英文**，
    #: 中文站也一样。中文色名在配色图的 alt 里：
    #:   "imageName":"iphone-duo-finish-select-star-white-202609_AV2",
    #:   "originalImageName":"…","alt":"星光白色 iPhone Duo，呈折叠状态…"
    #: 靠 finish-select-<slug> 把两边对起来。
    _COLOR_IMG = re.compile(
        r'finish-select-([a-z0-9-]+?)-\d{6}[^"]*","originalImageName":"[^"]*",'
        r'"alt":"([^"]{1,40}?)[ \u00a0]'
    )

    @staticmethod
    def _localized_colors(html: str) -> dict:
        out: dict = {}
        for slug, alt in AppleClient._COLOR_IMG.findall(html):
            out.setdefault(slug, alt)
        return out

    @staticmethod
    def _localize(name: str, colors: dict) -> str:
        """把英文名里的颜色换成中文。对不上就原样返回——宁可英文，也别瞎猜。"""
        if not name or not colors:
            return name
        # 颜色是名字末尾那几个词。「Glacier Blue」的 slug 是 glacier，不是
        # glacier-blue，所以要从长到短试。
        words = name.split()
        for take in range(min(3, len(words)), 0, -1):
            tail = words[-take:]
            for cand in ("-".join(w.lower() for w in tail), tail[0].lower()):
                if cand in colors:
                    return " ".join(words[:-take] + [colors[cand]])
        return name

    def catalog(self, slug: str) -> list[SkuInfo]:
        """从机型购买页抓出全部配置的 part number / 名称 / 价格。

        页面还没上线时 Apple 会 301 回 iPhone 落地页，这里当作 NotLive 抛出。
        """
        r = self._get(f"/shop/buy-iphone/{slug}", want_json=False, missing_means_not_live=True,
                      headers=_NAV_HEADERS)
        html = r.text
        if f"/shop/buy-iphone/{slug}" not in r.url:
            raise NotLive(f"{slug} 还没上线（跳转到 {r.url}）")

        colors = self._localized_colors(html)
        skus: dict[str, SkuInfo] = {}
        # 页面里内嵌的商品清单：{"sku":"MJT74","partNumber":"MJT74CH/A","price":{"fullPrice":9999.00},...,"name":"iPhone 18 Pro 256GB Black"}
        pattern = re.compile(
            r'\{"sku":"[^"]+","partNumber":"(?P<part>[A-Z0-9]+[A-Z]{2}/A)"'
            r'(?:,"price":\{"fullPrice":(?P<price>[\d.]+)\})?'
            r'[^{}]*?,"name":"(?P<name>[^"]+)"'
        )
        for m in pattern.finditer(html):
            part = m.group("part")
            skus[part] = SkuInfo(
                part=part,
                name=self._localize(m.group("name"), colors),
                price=float(m.group("price")) if m.group("price") else None,
                slug=slug,
            )
        if not skus:
            # 兜底：页面结构变了也至少把 part number 捞出来
            for part in sorted(set(re.findall(r'"partNumber":"([A-Z0-9]+[A-Z]{2}/A)"', html))):
                skus[part] = SkuInfo(part=part, name="", price=None, slug=slug)
        if not skus:
            raise NotLive(f"{slug} 页面里没有可购买配置，可能只是宣传页")
        return sorted(skus.values(), key=lambda s: (s.price or 0, s.name))

    def page_is_live(self, slug: str) -> tuple[bool, str]:
        """轻量探测机型购买页是否已经可以下单。返回 (是否上线, 说明)。"""
        try:
            skus = self.catalog(slug)
        except NotLive as e:
            return False, str(e)
        except Blocked as e:
            return False, f"被拦截：{e}"
        return True, f"已上线，{len(skus)} 个配置"

    def buy_url(self, slug: str, part: str = "") -> str:
        u = f"{self.base}/shop/buy-iphone/{slug}"
        return f"{u}/{part}" if part else u

