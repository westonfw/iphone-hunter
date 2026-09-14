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

from .apple import AppleClient, Availability, Blocked, NotLive, Stock, StorePickup
from .autobuy import AutoBuy, AutoBuyUnavailable
from .notify import Broadcaster, open_in_browser
from .pacing import build_pacer


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
        self.pacer = build_pacer(cfg, sprint=sprint, log=log)
        self.round_cost = 1.0   # 上一轮花了几个请求，用来预估下一轮

        ab = dict(cfg.get("autobuy") or {})
        ab.setdefault("region", cfg.get("region", "cn"))
        self.autobuy = AutoBuy(ab, root, log=log) if ab.get("enabled") else None
        self.autobuy_done = False
        # 预热：开卖前把产品页加载好、选项选好，放货时省掉整个页面加载。
        #
        # **默认关。** 它只在「知道几点开卖」时成立（发布会当晚那种），提前
        # 半小时预热、半小时内开火。补货监控不知道什么时候放货，挂几小时后
        # 那个页面的会话早过期、atbtoken 也失效了，还白占一个标签页。
        # 何况预热页固定是列表里第一个型号，别的型号命中照样得跳转。
        self.warm_enabled = bool(ab.get("warm", False))
        self.warm_url = ""
        self.warm_failed_at = 0.0

    @property
    def interval(self) -> float:
        """当前这一刻的目标平均间隔（由 Pacer 算，会随冷热时段和退避状态变）。"""
        return self.pacer.target()

    def wait(self) -> None:
        self.pacer.sleep(self.round_cost)

    def hit(self, title: str, body: str, url: str,
            in_stock: list[str] | None = None) -> None:
        """命中。in_stock 是这一刻真有货的门店名，决定自动下单去哪家取。"""
        # 先推送——自动下单要花几十秒，不能让通知等它
        self.bc.send(title, body, url, critical=True)

        if self.autobuy and not self.autobuy_done:
            # 只跑一次：袋子里已经有货了，重复跑只会添乱
            self.autobuy_done = True
            try:
                # 页面预热过就直接开火，省掉加载产品页那 700KB
                # url 一定要传给 fire：预热页是列表里第一个型号，
                # 放货的可能是任何一个，不核对就会买错颜色/容量
                r = (self.autobuy.fire(url, in_stock) if self.autobuy.warmed
                     else self.autobuy.buy(url, in_stock))
                if r.ok and r.order_id:
                    self._notify_pay(r, title)
                else:
                    self.bc.send(
                        f"{'✅' if r.ok else '⚠️'} 自动下单：{r.stage}",
                        r.detail or "详见终端", r.url or url, critical=True,
                    )
                # 只有「还有戏」的失败才留着下轮重试。货被抢走了就收手——
                # 再试也是每轮白烧几十秒，还把提醒刷成一片失败。
                if not r.ok and not self.autobuy.order_placed and r.retriable:
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

    def _notify_pay(self, r, hit_title: str) -> None:
        """订单已创建、等待付款——单独发一条，别混在「下单成功」里。

        这条是整条链路上**唯一需要你动手**的提醒，所以标题直说去付款、
        把倒计时和订单号放最前面。链接指向结账页（那儿有二维码），
        不是产品页——点错了会跳去再买一台。
        """
        what = hit_title.split("：", 1)[-1] if "：" in hit_title else hit_title
        pay = (self.cfg.get("autobuy") or {}).get("payment_method") or "扫码"
        months = (self.cfg.get("autobuy") or {}).get("installment_months") or 0
        how = f"{pay} {months} 期" if months else pay
        self.bc.send(
            f"💳 去付款！{r.order_id}",
            f"{what}\n{how}，请在约 30 分钟内扫码支付，超时订单会被取消。\n"
            f"点这条打开结账页。",
            r.url, critical=True,
        )
        self.log(f"[{now()}] 💳 待付款订单 {r.order_id} —— 去付款（约 30 分钟）")

    def run(self) -> None:
        raise NotImplementedError

    def loop(self) -> None:
        mode = "冲刺" if self.sprint else "常规"
        pc = self.pacer
        self.log(f"[{now()}] 启动（{mode}模式，间隔约 {pc.target():.0f}s，"
                 f"预算 {pc.budget_per_hour:.0f} 次/小时，Ctrl+C 停止）")
        if pc.hot_windows:
            self.log(f"[{now()}] 热时段 {self._windows_text()}，其余时段降速 "
                     f"{pc.cold_multiplier:g} 倍省配额")
        try:
            while True:
                before = self.client.requests_made
                try:
                    self.run()
                    pc.on_ok()
                except Blocked as e:
                    pc.on_blocked(getattr(e, "retry_after", 0.0))
                    # 被标记之后继续用同一个会话只会一路被拦，换一套身份重来
                    who = self.client.renew_session()
                    self.log(f"[{now()}] 请求被拦截：{e}（第 {pc.blocks} 次，"
                             f"降速到约 {pc.target():.0f}s，已换会话 {who}）")
                except Exception as e:
                    pc.on_blocked()
                    self.log(f"[{now()}] 出错：{type(e).__name__}: {e}")
                spent = max(1, self.client.requests_made - before)
                pc.spend(spent)
                self.round_cost = spent
                self.wait()
        except KeyboardInterrupt:
            self.log(f"\n[{now()}] 已停止（共发出 {self.client.requests_made} 个请求，"
                     f"被拦 {pc.blocks} 次）")

    def _windows_text(self) -> str:
        return "、".join(f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}"
                        for a, b in self.pacer.hot_windows)


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

      1. 记录能不能下单 / 多久发货  —— sba/availability-message（仅日志，不提醒）
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

        # availability-message 只用来打日志，提醒完全由 pickup-message 触发。
        # 每轮都打它等于把请求预算白花一半，所以降频轮询。
        self.avail_every = int((cfg.get("pacing") or {}).get("availability_every", 6))
        self.round_no = 0

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
        if self.autobuy.warm_alive:
            return
        if time.monotonic() - self.warm_failed_at < 120:
            return  # 刚失败过，别每轮都重试
        try:
            self.autobuy.warm(self.warm_url or self._buy_url(self.parts[0]))
        except Exception as e:
            self.warm_failed_at = time.monotonic()
            self.log(f"[预热] 失败（不影响监控）：{type(e).__name__}: {e}")

    def _want_availability(self) -> bool:
        """这一轮要不要顺带查一下发货状态。"""
        if not self.pickup_on:
            return True            # 门店监控关着的话，它就是唯一的信息来源
        if self.avail_every <= 0:
            return False           # 显式关掉
        return (self.round_no - 1) % self.avail_every == 0   # 第一轮先打个底

    def run(self) -> None:
        self._ensure_warm()
        self.round_no += 1
        want_avail = self._want_availability()
        avail: dict[str, Availability] = {}
        pickup: dict[str, list[StorePickup]] = {}
        for parts in self.part_groups.values():
            if want_avail:
                avail.update(self.client.availability(parts))
            if self.pickup_on:
                pickup.update(self.client.pickup(parts, location=self.location))

        for part in self.parts:
            if want_avail:
                self._check_buyable(part, avail.get(part))
            if self.pickup_on:
                self._check_pickup(part, pickup.get(part) or [])

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
            # 把「刚放货的那几家」按顺序交给自动下单——写死一家的话，
            # 另外三家放货时会卡在选店那一步。
            self.hit(f"{prefix}：{label}", "\n".join(lines), self._buy_url(part),
                     in_stock=[s.store_name for s in fresh])
        self.state.set(f"pickup:{part}", cur)

    def _buy_url(self, part: str) -> str:
        slug = self.slug_of.get(part, "")
        return self.client.buy_url(slug, part) if slug else self.client.base + "/shop/buy-iphone"
