"""命令行入口：python -m hunter <子命令>"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .apple import REGIONS, AppleClient, Blocked, NotLive, Stock
from .autobuy import (DEFAULT_CDP_PORT, AutoBuy, AutoBuyUnavailable, cdp_candidates,
                      inspect_checkout, launch_debug_chrome, probe_cdp, windows_chrome)
from .monitor import LaunchWatcher, StockWatcher
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
    watch = cfg.get("watch") or []
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
        print("✗ 没有要盯的机型：用 --slug iphone-18 或在 config.json 的 launch_watch.slugs 里填")
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
        print("      .venv/bin/python -m hunter rehearse")
        return 0

    if args.launch:
        ok, msg = launch_debug_chrome(port=port)
        print(f"\n{'✓' if ok else '✗'} {msg}")
        if ok:
            print("\n浏览器已打开购物袋页。**现在在里面登录你的 Apple ID**（只需这一次），")
            print("顺便确认收货地址和付款方式都在。然后跑：")
            print("      .venv/bin/python -m hunter rehearse")
        return 0 if ok else 1

    print("\n✗ 没找到开着调试端口的 Chrome。")
    print("\n最省事的办法——让工具替你开一个：\n")
    print("      .venv/bin/python -m hunter connect --launch\n")
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
    watch = cfg.get("watch") or []
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
    if args.show:
        ab["headless"] = False
    url = client.buy_url(slug, part)

    print("彩排会做这些事：打开机型页 → 选掉「折抵换购」和「AppleCare+」→ 检查加购按钮是否可点。")
    print("它**不会**点加购，也不会下单。\n")
    try:
        r = AutoBuy(ab, ROOT).rehearse(url)
    except AutoBuyUnavailable as e:
        print(f"✗ {e}")
        return 1
    print(f"\n{'✓' if r.ok else '✗'} {r.stage}")
    if r.detail:
        print(f"  {r.detail}")
    if r.ok:
        print("\n下一步：确认那个 Chrome 里已经登录 Apple ID、收货地址和付款方式都在。")
        print("确认无误后把 config.json 的 autobuy.enabled 改成 true。")
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
    bc = Broadcaster(cfg.get("notifiers"))
    bc.send(
        "🚨 iphone-hunter 测试通知",
        "这是一条测试。真到抢购时，通知长这样，点开直接进购买页。",
        client.base + "/shop/buy-iphone",
        critical=True,
    )
    print("\n已发送。手机/桌面没收到就去 config.json 检查对应渠道的 enabled 和密钥。")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m hunter",
        description="盯 Apple 官网，新 iPhone 一开卖就叫醒你",
    )
    p.add_argument("--region", choices=sorted(REGIONS), help="覆盖 config.json 里的区域")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("parts", help="列出某机型全部配置的 part number")
    sp.add_argument("slug", help="机型页面标识，如 iphone-17 / iphone-17-pro")
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
    sl.add_argument("--slug", action="append", help="可重复，如 --slug iphone-18 --slug iphone-18-pro")
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

    si = sub.add_parser("inspect-checkout", help="只读查看结账页的配送方式控件")
    si.set_defaults(func=cmd_inspect)

    st = sub.add_parser("test", help="发一条测试通知，验证渠道配置")
    st.set_defaults(func=cmd_test)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
