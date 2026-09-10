"""监控主循环。

两种模式：
  上架监控 (LaunchWatcher) —— 新机型购买页还没上线时用，页面一出现配置就通知。
  库存监控 (StockWatcher)  —— 已知 part number 后用，状态一变就通知。

两者共用一套"状态指纹 + 变化才通知"的逻辑，避免每轮都刷屏。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from .apple import AppleClient, Availability, Blocked, NotLive, Stock, StorePickup, sleep_with_jitter
from .autobuy import AutoBuy, AutoBuyUnavailable
from .notify import Broadcaster, open_in_browser


def _p(*a) -> None:
    """行缓冲的 print，保证 tee / 重定向时也能实时看到进度。"""
    print(*a, flush=True)


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


class State:
    """把上一轮的状态落盘，重启后不会把旧状态当成新变化重发一遍。"""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, key: str):
        return self.data.get(key)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        try:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass


class BaseWatcher:
    def __init__(self, cfg: dict, root: Path, sprint: bool = False, log=_p):
        self.cfg = cfg
        self.log = log
        self.sprint = sprint
        self.client = AppleClient(
            region=cfg.get("region", "cn"),
            timeout=int(cfg.get("timeout", 15)),
            proxy=cfg.get("proxy") or None,
        )
        self.bc = Broadcaster(cfg.get("notifiers"), log=log)
        self.state = State(root / "state.json")
        self.open_browser = bool(cfg.get("open_browser_on_hit", True))
        self.jitter = float(cfg.get("jitter", 0.3))
        self.fail_streak = 0

        ab = dict(cfg.get("autobuy") or {})
        ab.setdefault("region", cfg.get("region", "cn"))
        self.autobuy = AutoBuy(ab, root, log=log) if ab.get("enabled") else None
        self.autobuy_done = False
        # 预热：开卖前把产品页加载好、选项选好，放货时省掉整个页面加载
        self.warm_enabled = bool(ab.get("warm", True))
        self.warm_url = ""
        self.warm_failed_at = 0.0

    @property
    def interval(self) -> float:
        key = "sprint_interval" if self.sprint else "poll_interval"
        return float(self.cfg.get(key, 5 if self.sprint else 20))

    def wait(self) -> None:
        # 被拦截后指数退避，最多退到 5 分钟，避免把自己彻底打进黑名单
        if self.fail_streak:
            backoff = min(300, self.interval * (2 ** min(self.fail_streak, 5)))
            self.log(f"[{now()}] 连续 {self.fail_streak} 次失败，退避 {backoff:.0f}s")
            sleep_with_jitter(backoff, self.jitter)
        else:
            sleep_with_jitter(self.interval, self.jitter)

    def hit(self, title: str, body: str, url: str) -> None:
        # 先推送——自动下单要花几十秒，不能让通知等它
        self.bc.send(title, body, url, critical=True)

        if self.autobuy and not self.autobuy_done:
            # 只跑一次：袋子里已经有货了，重复跑只会添乱
            self.autobuy_done = True
            try:
                # 页面预热过就直接开火，省掉加载产品页那 700KB
                r = self.autobuy.fire() if self.autobuy.warmed else self.autobuy.buy(url)
                extra = f"\n订单号 {r.order_id}" if r.order_id else ""
                self.bc.send(
                    f"{'✅' if r.ok else '⚠️'} 自动下单：{r.stage}",
                    (r.detail or "详见终端") + extra, r.url or url, critical=True,
                )
                if not r.ok and not self.autobuy.order_placed:
                    self.autobuy_done = False
            except AutoBuyUnavailable as e:
                self.autobuy_done = False  # 环境问题，下次还能再试
                self.log(f"[自动下单] 不可用：{e}")
                self.bc.send("⚠️ 自动下单没能启动", str(e), url, critical=True)
            except Exception as e:
                # 还没创建订单就可以再试：没加购则整段重来，已加购则下次只走结账。
                if not self.autobuy.order_placed:
                    self.autobuy_done = False
                    retry = "（订单未创建，下一轮会重试）"
                else:
                    retry = "（订单已创建，不再自动重试）"
                self.log(f"[自动下单] 出错：{type(e).__name__}: {e} {retry}")
                self.bc.send("⚠️ 自动下单失败，请手动下单",
                             f"{type(e).__name__}: {e}\n{retry}", url, critical=True)
            return

        if self.open_browser:
            open_in_browser(url, log=self.log)

    def run(self) -> None:
        raise NotImplementedError

    def loop(self) -> None:
        mode = "冲刺" if self.sprint else "常规"
        self.log(f"[{now()}] 启动（{mode}模式，间隔约 {self.interval:.0f}s，Ctrl+C 停止）")
        try:
            while True:
                try:
                    self.run()
                    self.fail_streak = 0
                except Blocked as e:
                    self.fail_streak += 1
                    self.log(f"[{now()}] 请求被拦截：{e}")
                except Exception as e:
                    self.fail_streak += 1
                    self.log(f"[{now()}] 出错：{type(e).__name__}: {e}")
                self.wait()
        except KeyboardInterrupt:
            self.log(f"\n[{now()}] 已停止")


class LaunchWatcher(BaseWatcher):
    """盯新机型购买页什么时候真正可以下单。

    发布会当晚 Apple 会把 /shop/buy-iphone/<slug> 从跳转落地页切成真正的购买页，
    这个切换的瞬间就是"第一时间"。
    """

    def __init__(self, cfg, root, slugs: list[str], sprint=False, log=_p):
        super().__init__(cfg, root, sprint, log)
        self.slugs = slugs

    def run(self) -> None:
        for slug in list(self.slugs):
            try:
                skus = self.client.catalog(slug)
            except NotLive as e:
                self.log(f"[{now()}] {slug}: 未上线（{e}）")
                continue

            if self.state.get(f"launched:{slug}"):
                # 已经通知过了，转为常规日志，避免重复轰炸
                self.log(f"[{now()}] {slug}: 已上线，{len(skus)} 个配置")
                continue

            cheapest = skus[0]
            url = cheapest.buy_url(self.client.base)
            lines = [f"{s.name or s.part} {('¥%.0f' % s.price) if s.price else ''}".strip()
                     for s in skus[:8]]
            self.hit(
                title=f"🚨 {slug} 开卖了！",
                body=f"{len(skus)} 个配置已上线\n" + "\n".join(lines),
                url=url,
            )
            self.state.set(f"launched:{slug}", {"at": datetime.now().isoformat(),
                                                "parts": [s.part for s in skus]})
            self.log(f"[{now()}] 已把 {slug} 的 {len(skus)} 个 part 写进 state.json，"
                     f"可以用 `parts {slug} --save` 加进监控列表")


class StockWatcher(BaseWatcher):
    """盯已知 part number 的两件事：

      1. 能不能下单 / 多久发货  —— sba/availability-message
      2. 哪家直营店今天能取货    —— retail/pickup-message

    第 2 项才是开售后捡漏的关键，而且必须用 pickup-message：sba 那个接口
    在不带门店上下文时对有货机型也报 0 家，照它做判断会永远不叫你。
    """

    def __init__(self, cfg, root, sprint=False, log=_p):
        super().__init__(cfg, root, sprint, log)
        self.items = cfg.get("watch") or []
        if not self.items:
            raise SystemExit("config.json 里 watch 是空的，先跑 `parts <机型> --save` 填进去")
        self.parts = [i["part"] for i in self.items]
        self.part_groups: dict[str, list[str]] = {}
        for item in self.items:
            group = item.get("request_group") or "default"
            self.part_groups.setdefault(group, []).append(item["part"])
        self.slug_of = {i["part"]: i.get("model_slug", "") for i in self.items}
        self.note_of = {i["part"]: i.get("note", "") for i in self.items}

        pk = cfg.get("pickup") or {}
        self.pickup_on = bool(pk.get("enabled", True))
        self.location = str(pk.get("location", "")).strip()
        self.only_stores = [s.upper() for s in (pk.get("stores") or [])]
        if self.pickup_on and not self.location:
            self.log("[提示] config.pickup.location 没填邮编，本次跳过门店取货监控")
            self.pickup_on = False

    def _ensure_warm(self) -> None:
        """保证预热标签页活着。

        开卖前就把产品页开好、必选项选好，放货那一刻只剩「点加购 + 跳结账」。
        这是慢网下最有效的一招：页面早就在本地了，服务器再卡也不用重新加载它。
        预热失败不影响监控，只是回退到临场加载。
        """
        if not (self.autobuy and self.warm_enabled) or self.autobuy_done:
            return
        if self.autobuy.warmed:
            return
        if time.monotonic() - self.warm_failed_at < 120:
            return  # 刚失败过，别每轮都重试
        try:
            self.autobuy.warm(self.warm_url or self._buy_url(self.parts[0]))
        except Exception as e:
            self.warm_failed_at = time.monotonic()
            self.log(f"[预热] 失败（不影响监控）：{type(e).__name__}: {e}")

    def run(self) -> None:
        self._ensure_warm()
        avail: dict[str, Availability] = {}
        pickup: dict[str, list[StorePickup]] = {}
        for parts in self.part_groups.values():
            avail.update(self.client.availability(parts))
            if self.pickup_on:
                pickup.update(self.client.pickup(parts, location=self.location))

        for part in self.parts:
            self._check_buyable(part, avail.get(part))
            if self.pickup_on:
                self._check_pickup(part, pickup.get(part) or [])

    # ---------- 能不能下单 ----------

    def _check_buyable(self, part: str, av: Availability | None) -> None:
        label = self.note_of.get(part) or part
        if av is None:
            # 接口没返回 = 未知，不写状态，下一轮重来
            self.log(f"[{now()}] {label}: 下单状态未知（接口没返回该型号）")
            return

        prev = self.state.get(f"stock:{part}")
        cur = list(av.key)
        self.log(f"[{now()}] {label}: {av.describe()}")
        if prev == cur:
            return

        url = self._buy_url(part)
        if av.buyable and prev is not None and prev[0] is False:
            self.hit(f"🚨 {label} 可以下单了", av.describe(), url)
        elif prev is not None:
            self.bc.send(f"{label} 状态变化", av.describe(), url, critical=False)
        self.state.set(f"stock:{part}", cur)

    # ---------- 门店能不能取货 ----------

    def _check_pickup(self, part: str, stores: list[StorePickup]) -> None:
        label = self.note_of.get(part) or part

        if self.only_stores:
            stores = [s for s in stores if s.store_number.upper() in self.only_stores]
            if not stores:
                self.log(f"[{now()}] {label}: 指定的门店编号没出现在结果里，检查 pickup.stores")
                return

        unknown = [s for s in stores if s.state is Stock.UNKNOWN]
        if unknown and len(unknown) == len(stores):
            # 全未知：明确报出来，绝不当成无货，也绝不写进状态
            self.log(f"[{now()}] {label}: 门店库存未知 — {unknown[0].reason}")
            return

        ready = [s for s in stores if s.state is Stock.AVAILABLE]
        prev = self.state.get(f"pickup:{part}")           # None = 这一轮是第一次看到
        first_run = prev is None
        prev = prev or []
        cur = sorted(s.store_number for s in ready)

        if ready:
            names = "、".join(f"{s.store_name}" for s in ready[:6])
            more = f" 等 {len(ready)} 家" if len(ready) > 6 else ""
            self.log(f"[{now()}] {label}: 🏬 {names}{more} 可取货")
        else:
            self.log(f"[{now()}] {label}: {len(stores)} 家门店均无货"
                     + (f"（另有 {len(unknown)} 家状态未知）" if unknown else ""))

        if cur == prev:
            return

        fresh = [s for s in ready if s.store_number not in prev]
        if fresh:
            lines = [f"{s.store_name}（{s.city}）{s.quote}" for s in fresh[:8]]
            # 首轮就有货也要说，但标注清楚是「启动时就有」而不是「刚刚放货」
            prefix = "🏬 启动时已有货" if first_run else "🚨 刚放货"
            self.hit(f"{prefix}：{label}", "\n".join(lines), self._buy_url(part))
        self.state.set(f"pickup:{part}", cur)

    def _buy_url(self, part: str) -> str:
        slug = self.slug_of.get(part, "")
        return self.client.buy_url(slug, part) if slug else self.client.base + "/shop/buy-iphone"
