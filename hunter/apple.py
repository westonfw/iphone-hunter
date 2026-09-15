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
from .pacing import Breaker

# 一套「身份」= TLS 握手 + HTTP/2 设置 + 请求头，三者必须同源。
# curl_cffi 的 impersonate 会把这三样一起换掉；只改 UA 而 JA3 还是 python-requests
# 的那串，在 Akamai 眼里等于举着牌子写「我是脚本」。装不上就退回 requests——
# 功能不受影响，只是指纹是裸的。
try:
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests.exceptions import RequestException as CurlError
except ImportError:                                    # pragma: no cover
    curl_requests = None
    CurlError = None

#: 会被 Akamai 按端点单独限速的两个接口。熔断是按 path 分家的，所以要有名字。
AVAIL_PATH = "/shop/sba/availability-message"
PICKUP_PATH = "/shop/retail/pickup-message"

#: 身份池。ua 只用来打日志——真正发出去的头由 curl_cffi 按 impersonate 生成，
#: 它保证跟 TLS 指纹自洽（我们自己拼的 sec-ch-ua 做不到这一点）。
#: 退回 requests 时才用 ch_ua / platform 手工拼。
BROWSERS = [
    {
        "impersonate": "chrome150",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "ch_ua": '"Chromium";v="150", "Google Chrome";v="150", "Not?A_Brand";v="24"',
        "platform": '"macOS"',
    },
    {
        "impersonate": "safari184",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/18.4 Safari/605.1.15",
        "ch_ua": "",   # Safari 不发 client hints，发了才是破绽
        "platform": "",
    },
    {
        "impersonate": "chrome146",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "ch_ua": '"Chromium";v="146", "Google Chrome";v="146", "Not?A_Brand";v="24"',
        "platform": '"macOS"',
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
        self.cooldown = 0.0   # 熔断器决定这个端点要静默多久，由 _get 填上


class CoolingDown(Exception):
    """这个端点正在熔断静默期里，这一次**一个包都不发**。

    跟 Blocked 分开是有意的：Blocked 是「刚刚挨了一下」，要记账、要退避；
    CoolingDown 是「我们自己决定先别碰」，调用方应当安静跳过这一项——
    既不能当成故障去重试（重试就是给封禁续期），也**绝不能当成「查到了、没货」**。
    """

    def __init__(self, path: str, left: float):
        super().__init__(f"{path} 熔断中，还要静默 {left:.0f}s")
        self.path = path
        self.left = left


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


#: 两套底层客户端的网络异常基类不一样，catch 的时候得都算上。
_NET_ERRORS = tuple(e for e in (requests.RequestException, CurlError) if e is not None)


def _retry_after(resp) -> float:
    """把 Retry-After 头解析成秒。对方明说了要等多久，就别自己猜。"""
    raw = (resp.headers.get("Retry-After") or "").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0  # HTTP-date 形式的很少见，忽略即可


class AppleClient:
    def __init__(self, region: str = "cn", timeout: int = 15, proxy: str | None = None,
                 breaker: dict | None = None):
        if region not in REGIONS:
            raise ValueError(f"不支持的 region: {region}，可选 {'/'.join(REGIONS)}")
        self.region = region
        self.base = REGIONS[region]
        self.timeout = timeout
        self.proxy = proxy
        self.requests_made = 0        # 供 Pacer 记账用
        self.browser = random.choice(BROWSERS)
        # 熔断按 path 分家：541 是端点级的（实测 pickup 被拦时 availability 照样
        # 通），一个端点出事没有理由把另一个也停掉。
        self._breaker_kw = dict(breaker or {})
        self.breakers: dict[str, Breaker] = {}
        self.s = None  # type: ignore[assignment]
        self._open_session()

    # ---------- 熔断 ----------

    def breaker(self, path: str) -> Breaker:
        """取某个端点的熔断器（按 path 缓存，第一次用时才建）。"""
        key = path.split("?", 1)[0]
        if key.startswith("http"):
            key = "/" + key.split("/", 3)[-1] if key.count("/") > 2 else key
        br = self.breakers.get(key)
        if br is None:
            br = self.breakers[key] = Breaker(**self._breaker_kw)
        return br

    # ---------- 会话 ----------

    #: 这个请求是「页面里的 XHR」才该带的头。UA / sec-ch-ua / Accept-Encoding
    #: 交给 curl_cffi 按 impersonate 生成——那几个必须跟 TLS 指纹同源，手拼必错。
    XHR_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest",
    }

    def _open_session(self) -> None:
        b = self.browser
        headers = dict(self.XHR_HEADERS, Referer=f"{self.base}/shop/buy-iphone")
        if curl_requests is not None:
            # impersonate 一次把 TLS 握手（JA3）、HTTP/2 SETTINGS、头顺序和 UA
            # 全换成真浏览器的。这才是 Akamai 真正在看的那几维。
            s = curl_requests.Session(impersonate=b["impersonate"])
        else:
            s = requests.Session()
            # 退路：指纹是 python-requests 的，只能把头尽量补齐
            headers.update({
                "User-Agent": b["ua"],
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            })
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

    def renew_session(self, new_identity: bool = False) -> str:
        """丢掉当前会话重开一个。

        **默认不换身份，被拦时也不换。** 2026-09-15 的日志把这件事证死了：
        26 次 541 在 4 套 UA 上均匀分布（8/7/6/5 次），而 07:50 被拦的那套
        （Version/18.6）07:58 原样再发就是 200——同 IP、同参数、同 UA。
        判定的键是「出口 IP + 端点」，身份这一维根本不参与。

        同一个 IP 上反复换指纹不但没用，本身还是个 bot 信号。换身份只有在
        **同时换了出口 IP** 时才是净收益，所以要换得由调用方显式说。
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
        br = self.breaker(path)
        # 熔断优先于一切：静默期里连包都不发。探测本身就是在给封禁续期——
        # 那天退到 900s 还爬不出来，就是被自己的 6 次探测续起来的。
        if not br.ready():
            raise CoolingDown(path.split("?", 1)[0], br.left())

        self.requests_made += 1
        rec: dict = {"n": self.requests_made, "url": url}
        if params:
            rec["params"] = {k: str(v) for k, v in params.items()}
        t0 = time.monotonic()
        try:
            r = self.s.get(url, params=params, timeout=self.timeout, allow_redirects=True,
                           headers=headers)
        except _NET_ERRORS as e:
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
            out = self._decode(r, url, want_json, missing_means_not_live)
        except Blocked as e:
            e.cooldown = br.trip()
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
            rec["cooldown"] = round(e.cooldown)
            raise
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
            raise
        else:
            br.ok()   # 这个端点通了就立刻满速，封禁一过就没有理由再慢
            return out
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
            data = self._get(AVAIL_PATH, params)
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

        body = (self._get(PICKUP_PATH, params).get("body") or {})

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

