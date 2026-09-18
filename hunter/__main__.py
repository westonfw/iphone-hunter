"""命令行入口：python -m hunter <子命令>"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import PY_CMD
from .apple import REGIONS, AppleClient, Blocked, NotLive, Stock
from .autobuy import (DEFAULT_CDP_PORT, AutoBuy, AutoBuyUnavailable, _store_list,
                      cdp_candidates, inspect_checkout, launch_debug_chrome, probe_cdp,
                      windows_chrome)
from .logbook import setup as setup_logbook
from .monitor import LaunchWatcher, StockWatcher, watch_items
from .session import SessionProbe, parse_duration
from .notify import Broadcaster

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
EXAMPLE = ROOT / "config.example.json"


def load_config() -> dict:
    if not CONFIG.exists():
        if EXAMPLE.exists():
            CONFIG.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"没找到 config.json，已从模板生成：{CONFIG}\n请先填好通知渠道再启动监控。")
        else:
            raise SystemExit(f"缺少配置文件：{CONFIG}")
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"config.json 格式错误：{e}")


def save_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 子命令 ----------

def cmd_parts(args) -> int:
    cfg = load_config()
    client = AppleClient(args.region or cfg.get("region", "cn"), proxy=cfg.get("proxy") or None)
    try:
        skus = client.catalog(args.slug)
    except NotLive as e:
        print(f"✗ {e}")
        print(f"  机型没上线时用：python -m hunter launch --slug {args.slug}")
        return 1
    except Blocked as e:
        print(f"✗ 请求被 Apple 拦截：{e}（换个网络或稍后再试）")
        return 1

    print(f"{args.slug} 共 {len(skus)} 个配置：\n")
    print(f"{'PART NUMBER':<16} {'价格':>10}  型号")
    print("-" * 68)
    for s in skus:
        price = f"{s.price:,.0f}" if s.price else "-"
        print(f"{s.part:<16} {price:>10}  {s.name}")

    if args.save:
        keep = [s for s in skus if not args.filter or args.filter.lower() in s.name.lower()]
        if not keep:
            print(f"\n✗ 没有型号匹配 --filter {args.filter}，没有写入")
            return 1
        cfg.setdefault("watch", [])
        exists = {w["part"] for w in cfg["watch"]}
        added = 0
        for s in keep:
            if s.part in exists:
                continue
            cfg["watch"].append({"part": s.part, "note": s.name, "model_slug": s.slug})
            added += 1
        save_config(cfg)
        print(f"\n✓ 已写入 config.json：新增 {added} 个，当前监控 {len(cfg['watch'])} 个配置")
    return 0


def cmd_check(args) -> int:
    cfg = load_config()
    client = AppleClient(args.region or cfg.get("region", "cn"), proxy=cfg.get("proxy") or None)
    parts = args.parts or [w["part"] for w in cfg.get("watch", [])]
    if not parts:
        print("✗ 没有要查的型号：给出 part number，或先跑 `parts <机型> --save`")
        return 1
    try:
        result = client.availability(parts)
    except Blocked as e:
        print(f"✗ 请求被 Apple 拦截：{e}")
        return 1
    for part in parts:
        av = result.get(part)
        print(f"{part:<16} {av.describe() if av else '状态未知（接口无返回）'}   {av.name if av else ''}")

    loc = args.location or (cfg.get("pickup") or {}).get("location", "")
    if not loc:
        print("\n（想看门店取货，加 --location <邮编> 或填 config.pickup.location）")
        return 0
    try:
        pickup = client.pickup(parts, location=loc)
    except Blocked as e:
        print(f"\n✗ 门店查询被拦截：{e}")
        return 1
    for part in parts:
        stores = pickup.get(part) or []
        ready = [s for s in stores if s.state is Stock.AVAILABLE]
        unknown = [s for s in stores if s.state is Stock.UNKNOWN]
        head = result.get(part).name if result.get(part) else part
        if unknown and len(unknown) == len(stores):
            print(f"\n{head} 门店取货：状态未知 — {unknown[0].reason}")
            continue
        print(f"\n{head} 门店取货：{len(ready)}/{len(stores)} 家可取")
        for s in ready:
            print(f"  ✓ {s.store_number} {s.store_name}（{s.city}） {s.quote}")
    return 0


def cmd_stores(args) -> int:
    cfg = load_config()
    client = AppleClient(args.region or cfg.get("region", "cn"), proxy=cfg.get("proxy") or None)
    watch = watch_items(cfg) or (cfg.get("watch") or [])
    sample = args.part or (watch[0]["part"] if watch else None)
    if not sample:
        print("✗ 需要一个 part number 才能带出门店列表：加 --part，或先跑 `parts <机型> --save`")
        return 1
    try:
        stores = client.nearby_stores(args.location, sample)
    except Blocked as e:
        print(f"✗ 请求被拦截：{e}")
        return 1
    real = [s for s in stores if s.store_number]
    if not real:
        print(f"✗ {args.location} 附近没查到门店：{stores[0].reason if stores else '空结果'}")
        print("  中国大陆要用邮政编码（如 100000 北京 / 200000 上海），中文城市名会被拒。")
        return 1
    print(f"{args.location} 附近 {len(real)} 家直营店：\n")
    for s in real:
        print(f"  {s.store_number:<6} {s.store_name:<14} {s.city:<8} {s.address}")
    print("\n把想盯的门店编号填进 config.json 的 pickup.stores 就只监控这几家。")
    return 0


def cmd_launch(args) -> int:
    cfg = load_config()
    slugs = args.slug or (cfg.get("launch_watch") or {}).get("slugs") or []
    if not slugs:
        print("✗ 没有要盯的机型：用 --slug iphone-18-pro 或在 config.json 的 launch_watch.slugs 里填")
        return 1
    LaunchWatcher(cfg, ROOT, slugs, sprint=args.sprint).loop()
    return 0


def cmd_watch(args) -> int:
    StockWatcher(load_config(), ROOT, sprint=args.sprint).loop()
    return 0


def cmd_connect(args) -> int:
    """诊断能不能挂到你自己已登录的 Chrome 上。"""
    cfg = load_config()
    ab = cfg.get("autobuy") or {}
    port = int(args.port or ab.get("cdp_port", DEFAULT_CDP_PORT))
    cands = cdp_candidates(ab.get("cdp_url", ""), port)

    print(f"探测调试端口 {port}：\n")
    found = None
    for url in cands:
        info = probe_cdp(url)
        if info:
            print(f"  ✓ {url}  →  {info.get('Browser', '?')}")
            found = found or url
        else:
            print(f"  ✗ {url}  无应答")

    if found:
        print(f"\n✓ 可以挂上去。把 config.json 的 autobuy.cdp_url 设成 {found!r} 锁定这个地址，")
        print("  或者留空让它每次自动探测。接着跑：")
        print(f"      {PY_CMD} -m hunter rehearse")
        return 0

    if args.launch:
        ok, msg = launch_debug_chrome(port=port)
        print(f"\n{'✓' if ok else '✗'} {msg}")
        if ok:
            print("\n浏览器已打开购物袋页。**现在在里面登录你的 Apple ID**（只需这一次），")
            print("顺便确认收货地址和付款方式都在。然后跑：")
            print(f"      {PY_CMD} -m hunter rehearse")
        return 0 if ok else 1

    print("\n✗ 没找到开着调试端口的 Chrome。")
    print("\n最省事的办法——让工具替你开一个：\n")
    print(f"      {PY_CMD} -m hunter connect --launch\n")
    print("说明：Chrome 136 起，--remote-debugging-port 对**默认 profile 直接失效**")
    print("（防止攻击者挂上真实 profile 偷 cookie），所以你日常在用的那个 Chrome 挂不上去，")
    print("必须用一个独立 profile。--launch 会开一个独立 profile 的 Chrome 并停在购物袋页，")
    print("你在里面登录一次 Apple ID 就行。")
    print("\nApple 的收货地址和付款方式是存在你的 Apple ID 账号里的、不在浏览器里，")
    print("所以换 profile 不会丢——登录一次，该有的全都有，而且这个 profile 会一直留着。")
    if windows_chrome():
        print("\n（已检测到 Windows Chrome，--launch 会用它；WSL 镜像网络已生效，端口能直连。）")
    return 1


def cmd_rehearse(args) -> int:
    """抢购前的彩排：验证选择器还有效，并把 Apple ID 登录态存进 profile。"""
    cfg = load_config()
    client = AppleClient(args.region or cfg.get("region", "cn"), proxy=cfg.get("proxy") or None)
    watch = watch_items(cfg) or (cfg.get("watch") or [])
    if args.part:
        part, slug = args.part, (args.slug or "")
        if not slug:
            for w in watch:
                if w["part"] == part:
                    slug = w.get("model_slug", "")
    elif watch:
        part, slug = watch[0]["part"], watch[0].get("model_slug", "")
    else:
        print("✗ 需要一个目标：加 --part <PART> --slug <机型>，或先跑 `parts <机型> --save`")
        return 1
    if not slug:
        print(f"✗ 不知道 {part} 属于哪个机型页面，加 --slug")
        return 1

    ab = dict(cfg.get("autobuy") or {})
    ab["enabled"] = True
    ab.setdefault("region", args.region or cfg.get("region", "cn"))
    if args.show:
        ab["headless"] = False
    url = client.buy_url(slug, part)

    print("彩排会做这些事：打开机型页 → 选掉「折抵换购」和「AppleCare+」→ 检查加购按钮是否可点。")
    print("它**不会**点加购，也不会创建订单。\n")
    try:
        r = AutoBuy(ab, ROOT).rehearse(url)
    except AutoBuyUnavailable as e:
        print(f"✗ {e}")
        return 1
    print(f"\n{'✓' if r.ok else '✗'} {r.stage}")
    if r.detail:
        print(f"  {r.detail}")
    if r.ok:
        print("\n下一步：确认那个 Chrome 里已经登录 Apple ID、收货地址、发票都在，")
        print("并把 config.json 的 autobuy.payment_method 设成扫码付的一种：")
        print("  支付宝 / 微信支付 / 花呗分期 / 微信分付 / 招商银行 / 中国建设银行 / 工商银行")
        print("  （银行分期还要设 installment_months，如 24。信用卡 Visa/Mastercard 会即时扣款，不支持。）")
        print("确认无误后把 autobuy.enabled 改成 true：命中后会点「现在下单」创建待付款订单，付款仍是你自己来。")
    return 0 if r.ok else 1


def _target_from_args(args, cfg):
    watch = watch_items(cfg) or (cfg.get("watch") or [])
    if args.part:
        part, slug = args.part, (args.slug or "")
        if not slug:
            for w in watch:
                if w["part"] == part:
                    slug = w.get("model_slug", "")
    elif watch:
        part, slug = watch[0]["part"], watch[0].get("model_slug", "")
    else:
        raise SystemExit("✗ 需要一个目标：加 --part <PART> --slug <机型>，或先跑 `parts <机型> --save`")
    if not slug:
        raise SystemExit(f"✗ 不知道 {part} 属于哪个机型页面，加 --slug")
    return part, slug


def cmd_buy(args) -> int:
    """真跑一次下单：加购 → 现在下单 → 停在待付款。不经过库存监控。"""
    cfg = load_config()
    ab = dict(cfg.get("autobuy") or {})
    if args.stop_at_review:
        ab["stop_at_review"] = True
    # 拿来做 A/B：同一条链路分别用发包和点页面各跑一次，才知道快车道到底快不快。
    if args.no_fast_path:
        ab["fast_path"] = False
    if args.store:
        ab["pickup_store_numbers"] = [args.store.upper()]
    if not args.confirm:
        pay = ab.get("payment_method") or "支付宝"
        stores = _store_list(ab.get("pickup_stores"), ab.get("pickup_store_name"))
        store = " > ".join(stores) if stores else "（未填，结账时请手动选）"
        last4 = ab.get("id_last4") or ""
        print("这会在你已登录的 Chrome 里走完整结账向导：")
        print("  自提 → 身份证后四位 → 付款方式 → 检查订单 → Review 确认下单")
        if ab.get("stop_at_review"):
            print("**本次只走到 Review 页就停，不会点「立即下单」，不产生订单。**")
        else:
            print("会生成一笔待付款订单，不会代你付款；超时未付订单会被取消。")
        print(f"  支付方式：{pay}")
        print(f"  取货门店（按优先级）：{store}")
        print(f"  身份证后四位：{'已填' if last4 else '未填（PickupContact 会卡住）'}")
        if not last4:
            print("\n先在 config.json 的 autobuy.id_last4 填身份证后 4 位（可含 X）。")
        print("\n确认后加上 --confirm：")
        print("      python -m hunter buy --confirm")
        return 1

    part, slug = _target_from_args(args, cfg)
    if (ab.get("delivery") or "pickup") == "pickup":
        last4 = "".join(str(ab.get("id_last4") or "").split()).upper()
        if len(last4) != 4:
            print("✗ 到店取货必须先在 config.json 的 autobuy.id_last4 填身份证后 4 位。")
            return 1

    client = AppleClient(args.region or cfg.get("region", "cn"), proxy=cfg.get("proxy") or None)
    ab["enabled"] = True
    ab.setdefault("region", args.region or cfg.get("region", "cn"))
    url = client.buy_url(slug, part)
    print(f"目标：{part}（{slug}）")
    print(f"购买页：{url}")
    print("开始创建待付款订单……\n")
    try:
        r = AutoBuy(ab, ROOT).buy(url)
    except AutoBuyUnavailable as e:
        print(f"✗ {e}")
        return 1
    print(f"\n{'✓' if r.ok else '✗'} {r.stage}")
    if r.order_id:
        print(f"  订单号：{r.order_id}")
    if r.detail:
        print(f"  {r.detail}")
    if r.url:
        print(f"  {r.url}")
    if r.ok:
        print("\n请到打开的 Chrome 标签页完成付款。不要关那个窗口。")
    return 0 if r.ok else 1


def cmd_inspect(args) -> int:
    """只读查看结账页的配送方式控件，用来定位「到店取货」的选择器。"""
    cfg = load_config()
    ab = dict(cfg.get("autobuy") or {}); ab["enabled"] = True
    print("只读检查：打开购物袋/结账页，只列出配送方式相关的控件。")
    print("不点击、不修改、不提交，也不会打印你的收货信息。\n")
    try:
        found = inspect_checkout(ab, ROOT)
    except AutoBuyUnavailable as e:
        print(f"✗ {e}")
        return 1
    if not found:
        print("没找到配送方式控件——购物袋可能是空的，或者要先点『结账』才会出现这一步。")
        return 1
    print(f"找到 {len(found)} 个配送相关控件：\n")
    for f in found:
        print(f"  [{f['tag']}] {f['text']!r}")
        print(f"        data-autom={f['data_autom']!r} id={f['id']!r} name={f['name']!r}")
        if f["aria"]:
            print(f"        aria-label={f['aria']!r}")
    return 0


def cmd_test(args) -> int:
    cfg = load_config()
    client = AppleClient(cfg.get("region", "cn"), proxy=cfg.get("proxy") or None)
    bc = Broadcaster.from_config(cfg)
    bc.send(
        "🚨 iphone-hunter 测试通知",
        "这是一条测试。真到抢购时，通知长这样，点开直接进购买页。",
        client.base + "/shop/buy-iphone",
        critical=True,
        # 测试通知必须发出去：半夜测试收不到会让人以为渠道配错了，回头去乱改配置
        wake=True,
    )
    print("\n已发送。手机/桌面没收到就去 config.json 检查对应渠道的 enabled 和密钥。")
    return 0


def cmd_session(args) -> int:
    """长时间记录会话状态，回答「挂着到底能挂多久、靠什么续」。"""
    cfg = load_config()
    ab = cfg.get("autobuy") or {}

    from .logbook import DailyFile, current
    lb = current()
    sink = None
    if lb is not None:
        jar = DailyFile(lb.dir, "session-probe", ".samples.jsonl")
        def sink(rec, _jar=jar):
            _jar.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        print(f"[探针] 采样落盘：{jar.path_for()}")

    notifier = None
    if args.notify:
        notifier = Broadcaster.from_config(cfg)

    probe = SessionProbe(
        ab.get("cdp_url", ""),
        int(args.port or ab.get("cdp_port", DEFAULT_CDP_PORT)),
        every=parse_duration(args.every, 300.0),
        act=args.act,
        login_every=parse_duration(args.login_every, 0.0) if args.login_every else 0.0,
        hours=float(args.hours or 0),
        notifier=notifier,
        sink=sink,
    )
    return probe.run()


def cmd_fastpath(args) -> int:
    """在已经打开的结账页上跑一遍六步发包，停在 Review。**不下单。**"""
    from .fastpath import FastCheckout
    cfg = load_config()
    ab = cfg.get("autobuy") or {}
    pk = cfg.get("pickup") or {}
    stores = [s for s in (ab.get("pickup_store_numbers") or pk.get("stores") or [])
              if str(s).upper().startswith("R")]
    if not stores:
        raise SystemExit("配置里没有门店编号（形如 R581）。填 pickup.stores 或 "
                         "autobuy.pickup_store_numbers。名字（五角场）不行——"
                         "selectStore 只认编号。")
    store = (args.store or stores[0]).upper()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(f"没装 playwright。用虚拟环境跑：{PY_CMD} -m hunter ...")

    for url in cdp_candidates(ab.get("cdp_url", ""), int(ab.get("cdp_port", DEFAULT_CDP_PORT))):
        if probe_cdp(url):
            break
    else:
        raise SystemExit("没找到开着调试端口的 Chrome。先跑：hunter connect --launch")

    with sync_playwright() as pw:
        ctx = pw.chromium.connect_over_cdp(url).contexts[0]
        pages = [p for p in ctx.pages if "/shop/checkout" in (p.url or "")]
        if not pages:
            raise SystemExit("没有打开着的结账页。先在浏览器里走到结账第一步"
                             "（购物袋 → 结账），再跑这个命令。")
        page = pages[0]
        print(f"结账页：{page.url[:90]}")
        print(f"门店 {store} / 付款 {ab.get('payment_method')} "
              f"{ab.get('installment_months')} 期 / 取货时段 "
              f"{args.time or ab.get('pickup_time') or 'earliest'}\n")
        fc = FastCheckout(
            store=store,
            id_last4=str(ab.get("id_last4") or ""),
            last_name=str(ab.get("pickup_last_name") or ""),
            first_name=str(ab.get("pickup_first_name") or ""),
            email=str(ab.get("pickup_email") or ""),
            phone=str(ab.get("pickup_phone") or ""),
            city=args.city or str(ab.get("pickup_city") or "上海"),
            state=args.state or str(ab.get("pickup_state") or "上海"),
            district=args.district or str(ab.get("pickup_district") or "杨浦区"),
            pickup_time=args.time or str(ab.get("pickup_time") or ""),
            payment_label=str(ab.get("payment_method") or "招商银行"),
            installment_months=int(ab.get("installment_months") or 24),
            # 默认只走到 Review。真要下单必须显式 --confirm——这条命令是拿来
            # 验链路的，别让人手一滑就创建了真实订单。
            place_order=bool(args.confirm),
        )
        if not args.confirm:
            print("（只走到 Review，不下单。要真下单加 --confirm）\n")
        ok, stage, detail = fc.run(page)
        print(f"\n{'✅' if ok else '❌'} {stage}\n   {detail}")
        if ok and not args.confirm:
            # 别让人自己刷：标签页的 URL 还挂着上一步的 _s= 锚点，F5 等于带着
            # 那个锚点重开，页面回到那一步，看着就像流程从头走了一遍。
            if fc.show_review(page):
                print("\n浏览器已经停在 Review 页上了（没有下单）。")
            else:
                print(f"\n没能把标签页带过去，自己开 {FastCheckout.review_url(page)} 看。")
        return 0 if ok else 1


def cmd_har(args) -> int:
    """解析 Chrome 导出的 HAR，输出结账请求清单（只看字段名和长度）。"""
    from .harscan import scan
    return scan(Path(args.path))


def cmd_record(args) -> int:
    """挂到已登录 Chrome，记录结账点击和步骤 URL。"""
    from .record import run as record_run
    cfg = load_config()
    watch = watch_items(cfg) or (cfg.get("watch") or [])
    url = ""
    part = (getattr(args, "part", "") or "").strip()
    slug = (getattr(args, "slug", "") or "").strip()
    # 人已经在结账流程中间时，跳转会把他导航走、白白丢掉当前会话。
    # --attach 就是「别动页面，只挂钩子，从这里开始录」。
    if getattr(args, "attach", False):
        part = slug = ""
        watch = []
    if part or watch:
        client = AppleClient(args.region or cfg.get("region", "cn"), proxy=cfg.get("proxy") or None)
        if part:
            # 录制经常要拿一个**有货的**型号来走，而监控列表第一条往往正是
            # 那个抢不到的。写死 watch[0] 等于逼你改配置才能录一次。
            url = client.buy_url(slug or (watch[0].get("model_slug", "") if watch else ""), part)
        else:
            url = client.buy_url(watch[0].get("model_slug", ""), watch[0]["part"])
    try:
        return record_run(cfg, ROOT, buy_url=url)
    except AutoBuyUnavailable as e:
        print(f"✗ {e}")
        return 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m hunter",
        description="盯 Apple 官网，新 iPhone 一开卖就叫醒你",
    )
    p.add_argument("--region", choices=sorted(REGIONS), help="覆盖 config.json 里的区域")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("parts", help="列出某机型全部配置的 part number")
    sp.add_argument("slug", help="机型页面标识，如 iphone-18-pro")
    sp.add_argument("--save", action="store_true", help="写进 config.json 的监控列表")
    sp.add_argument("--filter", help="只保存名称包含该关键词的配置，如 '512GB'")
    sp.set_defaults(func=cmd_parts)

    sc = sub.add_parser("check", help="查一次当前库存后退出")
    sc.add_argument("parts", nargs="*", help="part number；不给就查 config 里的监控列表")
    sc.add_argument("--location", help="邮政编码，顺带查该地区门店取货情况")
    sc.set_defaults(func=cmd_check)

    ss = sub.add_parser("stores", help="列出某邮编附近的直营店编号")
    ss.add_argument("location", help="邮政编码，如 100000")
    ss.add_argument("--part", help="用哪个 part 带出门店列表，默认取监控列表第一个")
    ss.set_defaults(func=cmd_stores)

    sl = sub.add_parser("launch", help="盯新机型购买页何时上线（发布会当天用这个）")
    sl.add_argument("--slug", action="append", help="可重复，如 --slug iphone-18-pro")
    sl.add_argument("--sprint", action="store_true", help="冲刺模式，用更短的轮询间隔")
    sl.set_defaults(func=cmd_launch)

    sw = sub.add_parser("watch", help="持续监控已知型号的库存")
    sw.add_argument("--sprint", action="store_true", help="冲刺模式，用更短的轮询间隔")
    sw.set_defaults(func=cmd_watch)

    scn = sub.add_parser("connect", help="诊断能否挂到你已登录的 Chrome")
    scn.add_argument("--port", type=int, help=f"调试端口，默认 {DEFAULT_CDP_PORT}")
    scn.add_argument("--launch", action="store_true",
                     help="没探测到就直接开一个带调试端口的 Chrome（独立 profile）")
    scn.set_defaults(func=cmd_connect)

    sr = sub.add_parser("rehearse", help="彩排自动下单流程（不会真的下单）")
    sr.add_argument("--part", help="目标 part number，默认取监控列表第一个")
    sr.add_argument("--slug", help="机型页面标识")
    sr.add_argument("--show", action="store_true", help="显示浏览器界面（用来登录 Apple ID）")
    sr.set_defaults(func=cmd_rehearse)

    sb = sub.add_parser("buy", help="立刻加购并创建待付款订单（真会下单，需 --confirm）")
    sb.add_argument("--part", help="目标 part number，默认取监控列表第一个")
    sb.add_argument("--slug", help="机型页面标识")
    sb.add_argument("--no-fast-path", action="store_true",
                    help="本次不走发包快车道，改点页面（用来对比两条路的耗时）")
    sb.add_argument("--store", default="",
                    help="本次指定取货门店编号，如 R359；不给就用配置里的优先级")
    sb.add_argument("--stop-at-review", action="store_true",
                    help="只走到 Review 页就停，不点「立即下单」（测试整条链路用）")
    sb.add_argument("--confirm", action="store_true",
                    help="确认：会加购并点「现在下单」，生成待付款订单，付款仍由你自己完成")
    sb.set_defaults(func=cmd_buy)

    si = sub.add_parser("inspect-checkout", help="只读查看结账页的配送方式控件")
    si.set_defaults(func=cmd_inspect)

    st = sub.add_parser("test", help="发一条测试通知，验证渠道配置")
    st.set_defaults(func=cmd_test)

    sp2 = sub.add_parser("session-probe",
                         help="长时间记录登录/会话状态，测它到底能挂多久")
    sp2.add_argument("--every", default="5m", help="采样间隔，如 30s/5m/1h（默认 5m，零请求）")
    sp2.add_argument("--act", default="none", choices=["none", "xhr", "nav"],
                     help="每次采样后的保活动作：none=纯观测 / xhr=发个接口请求 / nav=真实导航跑JS")
    sp2.add_argument("--login-every", default="", help="多久探一次登录态，如 30m（要发请求，默认关）")
    sp2.add_argument("--hours", type=float, default=0, help="跑多少小时后自动收工（默认不限）")
    sp2.add_argument("--notify", action="store_true", help="登录态掉了就推送提醒")
    sp2.add_argument("--port", type=int, default=0, help="CDP 端口")
    sp2.set_defaults(func=cmd_session)

    sf = sub.add_parser("fastpath",
                        help="在已打开的结账页上跑六步发包，停在 Review（不下单）")
    sf.add_argument("--store", default="", help="门店编号，如 R581；不给就用配置里第一个")
    sf.add_argument("--city", default="", help="覆盖配置里的城市")
    sf.add_argument("--state", default="", help="覆盖配置里的省/直辖市")
    sf.add_argument("--district", default="", help="覆盖配置里的区（搜索门店用）")
    sf.add_argument("--time", default="",
                    help="取货时段：earliest（默认）/ latest / HH:MM（当天不早于它的第一档）")
    sf.add_argument("--confirm", action="store_true",
                    help="真的提交订单（创建待付款订单，仍需你自己扫码付款）；"
                         "不给这个开关就只走到 Review")
    sf.set_defaults(func=cmd_fastpath)

    sh = sub.add_parser("har", help="解析 Chrome 导出的 HAR，看结账每一步发了什么")
    sh.add_argument("path", help="HAR 文件路径")
    sh.set_defaults(func=cmd_har)

    sr = sub.add_parser("record", help="挂到 Chrome 记录你的人工结账操作")
    sr.add_argument("--part", default="", help="要录的 part number，如 MG6X4CH/A；不给就用监控列表第一条")
    sr.add_argument("--slug", default="", help="机型 slug，如 iphone-17；不给就沿用监控列表里的")
    sr.add_argument("--attach", action="store_true",
                    help="只挂钩子不跳转——人已经在结账流程中间时用这个")
    sr.set_defaults(func=cmd_record)

    args = p.parse_args(argv)

    # 日志要在跑命令之前接上，之后所有 print 都会同时落盘。
    # 配置读不出来（首次运行、格式错）也不该拦住命令本身，那就用默认值。
    try:
        log_cfg = (load_config().get("logging") or {})
    except SystemExit:
        log_cfg = {}
    lb = setup_logbook(ROOT, log_cfg, command=args.cmd, argv=argv or sys.argv[1:])
    if lb:
        req = f"，请求明细 {lb.req_path.name}" if lb.req_path else ""
        print(f"[日志] {lb.log_path}{req}")

    outcome = ""
    try:
        rc = args.func(args)
        outcome = f"退出码 {rc}"
        return rc
    except KeyboardInterrupt:
        outcome = "被 Ctrl+C 中断"
        raise
    except SystemExit as e:
        # argparse / 各命令用 SystemExit("说明") 报错退出。默认那条说明只会
        # 打到真正的 stderr，绕过日志——手动打一遍，日志里才留得下原因。
        if isinstance(e.code, str):
            outcome = e.code
            print(e.code)
            return 1
        outcome = f"退出码 {e.code}"
        raise
    except BaseException as e:
        outcome = f"{type(e).__name__}: {e}"
        raise
    finally:
        if lb:
            lb.close(outcome)


if __name__ == "__main__":
    sys.exit(main())
