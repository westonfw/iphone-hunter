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

from .apple import (MAX_PARTS_PER_QUERY, PICKUP_PATH, REGIONS, AppleClient, Availability, Blocked, CoolingDown,
                    NotLive, Stock, StorePickup)
from .autobuy import AutoBuy, _store_list
from .notify import AsyncBroadcaster, Broadcaster, open_in_browser
from .purchase_guard import atomic_json
from .purchase_worker import Offer, PurchaseWorker
from .pacing import breaker_settings, build_pacer


def _p(*a) -> None:
    """行缓冲的 print，保证 tee / 重定向时也能实时看到进度。"""
    print(*a, flush=True)


def watch_items(cfg: dict, only_enabled: bool = True) -> list[dict]:
    """取监控列表。

    `enabled: false` 的条目会被跳过——盯着一堆用不上的型号，既白烧请求预算，
    又让日志刷满噪音，真正在等的那个反而看不见。

    **没有 enabled 字段视为启用**：老配置不写这个字段，不能因为加了开关就静默
    停掉别人的监控。
    """
    out = []
    for it in (cfg.get("watch") or []):
        if not isinstance(it, dict) or not it.get("part"):
            continue
        if only_enabled and it.get("enabled", True) is False:
            continue
        out.append(it)
    return out


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
            atomic_json(self.path, self.data)
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
            breaker=breaker_settings(cfg),
        )
        self.bc = AsyncBroadcaster(Broadcaster.from_config(cfg, log=log))
        self.state = State(root / "state.json")
        self.open_browser = bool(cfg.get("open_browser_on_hit", True))
        self.pacer = build_pacer(cfg, sprint=sprint, log=log)
        self.client.before_request = self.pacer.acquire
        self.round_cost = 1.0   # 只预估下一次请求，实际发出前逐个准入

        ab = dict(cfg.get("autobuy") or {})
        ab.setdefault("region", cfg.get("region", "cn"))
        self.autobuy = AutoBuy(ab, root, log=log) if ab.get("enabled") else None
        # 预热：开卖前把产品页加载好、选项选好，放货时省掉整个页面加载。
        #
        # **默认关。** 它只在「知道几点开卖」时成立（发布会当晚那种），提前
        # 半小时预热、半小时内开火。补货监控不知道什么时候放货，挂几小时后
        # 那个页面的会话早过期、atbtoken 也失效了，还白占一个标签页。
        # 何况预热页固定是列表里第一个型号，别的型号命中照样得跳转。
        self.warm_enabled = bool(ab.get("warm", False))
        self.purchase_worker = None

    @property
    def interval(self) -> float:
        """当前这一刻的目标平均间隔（由 Pacer 算，会随冷热时段和退避状态变）。"""
        return self.pacer.target()

    def wait(self) -> None:
        self.pacer.sleep(self.round_cost)

    def hit(self, title: str, body: str, url: str,
            in_stock: list[str] | None = None,
            in_stock_numbers: list[str] | None = None) -> None:
        """只负责通知；购买候选由每次库存观察递交，独立于状态变化。"""
        self.bc.send(title, body, url, critical=True)
        if not self.purchase_worker and self.open_browser:
            open_in_browser(url, log=self.log)

    def _report_purchase(self, result, title, url):
        if result.ok and (result.order_id or result.order_created):
            self._notify_pay(result, title)
        else:
            if self.autobuy.order_placed and not result.ok:
                url = REGIONS[self.cfg.get("region", "cn")] + "/shop/order/list"
            else:
                url = result.url or url
            self.bc.send(f"{'✅' if result.ok else '⚠️'} 自动下单：{result.stage}",
                         result.detail or '详见终端', url,
                         critical=True, wake=self.autobuy.order_placed)

    def _notify_pay(self, r, hit_title: str) -> None:
        """订单已创建、等待付款——单独发一条，别混在「下单成功」里。

        这条是整条链路上**唯一需要你动手**的提醒，所以标题直说去付款、
        把订单号和付款入口放最前面。链接指向结账页（那儿有二维码），
        不是产品页——点错了会跳去再买一台。
        """
        what = hit_title.split("：", 1)[-1] if "：" in hit_title else hit_title
        pay = (self.cfg.get("autobuy") or {}).get("payment_method") or "扫码"
        months = (self.cfg.get("autobuy") or {}).get("installment_months") or 0
        how = f"{pay} {months} 期" if months else pay
        self.bc.send(
            f"💳 去付款！{r.order_id or '待付款订单已创建'}",
            f"{what}\n{how}，请尽快扫码支付，付款期限以订单页面为准。\n"
            f"点这条打开结账页。",
            r.url, critical=True,
            # 付款通知需要及时处理，无视睡觉时段
            wake=True,
        )
        self.log(f"[{now()}] 💳 待付款订单 {r.order_id} —— 请尽快付款")

    def run(self) -> bool | None:
        raise NotImplementedError

    def loop(self) -> None:
        mode = "冲刺" if self.sprint else "常规"
        pc = self.pacer
        self.log(f"[{now()}] 启动（{mode}模式，间隔约 {pc.target():.0f}s，"
                 f"预算 {pc.budget_per_hour:.0f} 次/小时，Ctrl+C 停止）")
        if pc.hot_windows:
            self.log(f"[{now()}] 热时段 {self._windows_text()}，其余时段降速 "
                     f"{pc.cold_multiplier:g} 倍省配额")
        if self.purchase_worker:
            self.purchase_worker.start()
        try:
            while True:
                try:
                    if self.run() is not False:
                        pc.on_ok()
                except Blocked as e:
                    pc.on_blocked(getattr(e, "retry_after", 0.0))
                    # 不换会话：判定的键是「出口 IP + 端点」，同 IP 换 UA 是无效
                    # 动作，还多一个 bot 信号（详见 AppleClient.renew_session）。
                    # 真正管用的是让这个端点安静一会儿，那由熔断器负责。
                    cd = getattr(e, "cooldown", 0.0)
                    self.log(f"[{now()}] 请求被拦截：{e}（第 {pc.blocks} 次，"
                             f"该端点静默 {cd:.0f}s，巡检降到约 {pc.target():.0f}s）")
                except CoolingDown as e:
                    # 自己选择不发包，不是故障：既不记退避也不算成功，安静跳过。
                    self.log(f"[{now()}] {e}，本轮跳过")
                except Exception as e:
                    pc.on_blocked()
                    self.log(f"[{now()}] 出错：{type(e).__name__}: {e}")
                self.round_cost = 1.0  # 下一次实际请求独立准入，不预留整个批次
                self.wait()
        except KeyboardInterrupt:
            self.log(f"\n[{now()}] 已停止（共发出 {self.client.requests_made} 个请求，"
                     f"被拦 {pc.blocks} 次）")

        finally:
            if self.purchase_worker:
                self.purchase_worker.close()
            self.bc.close()

    def _windows_text(self) -> str:
        return "、".join(f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}"
                        for a, b in self.pacer.hot_windows)


class LaunchWatcher(BaseWatcher):
    """盯新机型购买页什么时候真正可以下单。

    发布会当晚 Apple 会把 /shop/buy-iphone/<slug> 从跳转落地页切成真正的购买页，
    这个切换的瞬间就是"第一时间"。
    """

    def __init__(self, cfg, root, slugs: list[str], sprint=False, log=_p):
        cfg = dict(cfg, autobuy=dict(cfg.get("autobuy") or {}, enabled=False))
        super().__init__(cfg, root, sprint, log)
        self.state = State(root / "launch-state.json")
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

      1. 记录能不能下单 / 多久发货  —— sba/availability-message（仅日志，不提醒）
      2. 哪家直营店今天能取货    —— retail/pickup-message

    第 2 项才是开售后捡漏的关键，而且必须用 pickup-message：sba 那个接口
    在不带门店上下文时对有货机型也报 0 家，照它做判断会永远不叫你。
    """

    def __init__(self, cfg, root, sprint=False, log=_p):
        super().__init__(cfg, root, sprint, log)
        self.items = watch_items(cfg)
        if not self.items:
            total = len(cfg.get("watch") or [])
            raise SystemExit(
                f"config.json 里没有启用的监控条目（共 {total} 条，全被 enabled:false 关掉了）"
                if total else
                "config.json 里 watch 是空的，先跑 `parts <机型> --save` 填进去")
        off = len(cfg.get("watch") or []) - len(self.items)
        if off:
            self.log(f"[监控] {len(self.items)} 个配置在盯，{off} 个已用 enabled:false 关掉")
        self.parts = [i["part"] for i in self.items]
        self.part_groups: dict[str, list[str]] = {}
        for item in self.items:
            group = item.get("request_group") or "default"
            self.part_groups.setdefault(group, []).append(item["part"])
        self.slug_of = {i["part"]: i.get("model_slug", "") for i in self.items}
        self.note_of = {i["part"]: i.get("note", "") for i in self.items}

        # availability-message 只用来打日志，提醒完全由 pickup-message 触发。
        # 每轮都打它等于把请求预算白花一半，所以降频轮询。
        self.avail_every = int((cfg.get("pacing") or {}).get("availability_every", 6))
        self.round_no = 0

        pk = cfg.get("pickup") or {}
        self.pickup_on = bool(pk.get("enabled", True))
        self.location = str(pk.get("location", "")).strip()
        self.only_stores = [s.upper() for s in _store_list(pk.get("stores"))]
        if self.pickup_on and not self.location:
            self.log("[提示] config.pickup.location 没填邮编，本次跳过门店取货监控")
            self.pickup_on = False

        if self.autobuy:
            ab = cfg.get('autobuy') or {}
            self.purchase_worker = PurchaseWorker(
                self.autobuy, self._report_purchase,
                max_age=ab.get('candidate_max_age', 30),
                max_attempts=ab.get('max_attempts_per_stock', 2),
                retry_delay=ab.get('retry_delay', 15),
                warm_url=self._buy_url(self.parts[0]) if self.warm_enabled else '', log=log)

    def _want_availability(self) -> bool:
        """这一轮要不要顺带查一下发货状态。"""
        if not self.pickup_on:
            return True            # 门店监控关着的话，它就是唯一的信息来源
        if self.avail_every <= 0:
            return False           # 显式关掉
        return (self.round_no - 1) % self.avail_every == 0   # 第一轮先打个底

    def run(self) -> bool:
        self.round_no += 1
        primary_ok = False
        errors = []
        # 每一批结果立即投递；后续组失败不能丢掉先前发现的货。
        if self.pickup_on:
            for parts in self.part_groups.values():
                for start in range(0, len(parts), MAX_PARTS_PER_QUERY):
                    batch = parts[start:start + MAX_PARTS_PER_QUERY]
                    try:
                        pickup = self.client.pickup(batch, location=self.location)
                        primary_ok = True
                        for part in batch:
                            self._check_pickup(part, pickup.get(part) or [])
                    except CoolingDown as e:
                        self.log(f'[{now()}] {e}，保留上次门店状态')
                    except Exception as e:
                        errors.append(e)
                        self.log(f'[{now()}] 门店查询失败：{e}')
        # pickup 开启时，AIMD 只跟随 pickup；availability 由自己的端点熔断器退避。
        # 没有实际成功的主查询就返回 False，不能因静默/辅助成功让 Pacer 加速。
        if self._want_availability():
            for parts in self.part_groups.values():
                try:
                    avail = self.client.availability(parts)
                    if not self.pickup_on:
                        primary_ok = True
                    for part in parts:
                        self._check_buyable(part, avail.get(part))
                except Exception as e:
                    self.log(f'[{now()}] 发货状态查询失败：{e}')
                    if not self.pickup_on:
                        errors.append(e)
        if errors:
            raise next((e for e in errors if isinstance(e, Blocked)), errors[0])
        return primary_ok

    # ---------- 能不能下单（仅记录，不提醒） ----------

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

        # 下单/配送状态只用于终端观察；提醒及自动下单只由直营店可提货触发。
        self.state.set(f"stock:{part}", cur)

    # ---------- 门店能不能取货 ----------

    def _check_pickup(self, part: str, stores: list[StorePickup]) -> None:
        label = self.note_of.get(part) or part

        if self.only_stores:
            stores = [s for s in stores if s.store_number.upper() in self.only_stores]
            if not stores:
                self.log(f"[{now()}] {label}: 指定的门店编号没出现在结果里，检查 pickup.stores")
                if getattr(self, "purchase_worker", None):
                    self.purchase_worker.observe(part, [])
                return

        worker = getattr(self, 'purchase_worker', None)
        if worker:
            ab = self.cfg.get('autobuy') or {}
            allowed = [s.upper() for s in _store_list(ab.get("pickup_store_numbers"))]
            accepted = [s for s in stores if
                        not allowed or s.store_number.upper() in allowed]
            observed = getattr(self.client, "observed_at", {}).get(PICKUP_PATH, time.monotonic())
            preferred = allowed or self.only_stores
            worker.observe(part, [Offer(
                part, s.store_number, s.store_name, self._buy_url(part), label, observed,
                (self.parts.index(part), preferred.index(s.store_number.upper())
                 if s.store_number.upper() in preferred else len(preferred)))
                for s in accepted if s.state is Stock.AVAILABLE],
                [s.store_number for s in accepted if s.state is Stock.UNAVAILABLE])
        if not stores:
            self.log(f'[{now()}] {label}: 门店结果为空，保留上次状态')
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
        observed_numbers = {s.store_number for s in stores if s.state is not Stock.UNKNOWN}
        cur = sorted({s.store_number for s in ready} | (set(prev) - observed_numbers))

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
            # 把「刚放货的那几家」按顺序交给自动下单——写死一家的话，
            # 另外三家放货时会卡在选店那一步。
            self.hit(f"{prefix}：{label}", "\n".join(lines), self._buy_url(part),
                     in_stock=[s.store_name for s in fresh],
                     in_stock_numbers=[s.store_number for s in fresh])
        self.state.set(f"pickup:{part}", cur)

    def _buy_url(self, part: str) -> str:
        slug = self.slug_of.get(part, "")
        return self.client.buy_url(slug, part) if slug else self.client.base + "/shop/buy-iphone"
