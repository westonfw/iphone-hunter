"""hunter2 的入口：**买手**这一侧。

盯库存的那一半在 `python -m scout` 里，这边只负责买。

    HUNTER_BUS_KEY=<所有部署一样的随机串> python -m hunter2 buy

部署一个新账号 = 把整个目录拷一份、改 config.json。订单记录、浏览器 profile、
日志目录都在各自的目录下，天然隔离。

单机不联机的用法在 `python -m hunter watch`——那条路不需要密钥，也不碰总线。
"""

from __future__ import annotations

import argparse
import secrets
import sys

from hunter.__main__ import ROOT, load_config
from hunter.logbook import setup as setup_logbook

from .bus import KEY_ENV, bus_key
from .buyer import Buyer


def cmd_buy(args) -> int:
    """只买不盯。一个请求都不花在巡检上，库存全靠探针喂。"""
    Buyer(load_config(), ROOT).loop()
    return 0


def cmd_key(args) -> int:
    """生成一个总线密钥。两边（所有部署）必须用同一个。"""
    print(f"{KEY_ENV}={secrets.token_urlsafe(32)}")
    print("\n把它放进每份部署的启动环境里（别写进 config.json——那个文件会被备份、"
          "同步、误提交）。所有部署必须用同一个值，否则互相不认。")
    return 0


def cmd_doctor(args) -> int:
    """检查这份部署能不能联机，以及有没有配成会互相打架的样子。"""
    cfg = load_config()
    link = dict(cfg.get("link") or {})
    ab = dict(cfg.get("autobuy") or {})
    ok = True

    if bus_key():
        print(f"✓ {KEY_ENV} 已设置")
    else:
        ok = False
        print(f"✗ 没有 {KEY_ENV} —— 不会联机，退化成单机。跑 "
              f"`python -m hunter2 key` 生成一个")

    bid = str(link.get("id") or "").strip()
    print(f"{'✓' if bid else '✗'} link.id = {bid or '(没写，会都叫 buyer)'}"
          + ("" if bid else "  ← 几份部署要各起一个名字，日志才分得清"))
    ok = ok and bool(bid)

    print(f"  link.buyer_offset = {link.get('buyer_offset', 0)}"
          f"   ← 几份部署要各不相同，否则多型号同时放货时会挤在同一个上")
    peers = [str(x) for x in (link.get("peers") or []) if x]
    print(f"  link.peers = {peers or '(广播)'}")
    # 写清端口的单播是合法的——同机多进程各绑各的端口，谁也吞不掉谁。
    # 只有光秃秃的地址才有歧义：几个进程绑同一个 UDP 端口时，单播只投给其中
    # 一个，而且不报任何错。
    bare = [x for x in peers if ":" not in x]
    if bare:
        ok = False
        print(f"  ✗ {'、'.join(bare)} 没写端口。同一台机器上有多个进程时，"
              f"单播只会投给其中一个，而且不报任何错。")
        print("    要么把 peers 留空走广播（每个进程都拿得到副本），"
              "要么给每份部署配不同的 link.port，"
              "并在 peers 里写成 192.168.1.9:48712 这样带端口的形式。")

    # 一主多子：买手同时是主程序的一条出口
    same = bool(link.get("same_exit_as_master"))
    print(f"  link.same_exit_as_master = {str(same).lower()}"
          + ("   ← 标了 true 就不开转发口。只有跟主程序同一个出口 IP 的那台该标，"
             "标错了主程序会白白少一条出口" if same else
             "   ← 会开一个转发口把出口 IP 借给主程序；跟主程序同 IP 的那台要标 true"))
    if not same:
        print(f"  link.proxy_port = {link.get('proxy_port', 0) or '(随机)'}")
    print(f"  link.enlist_every = {link.get('enlist_every', 20)}s"
          f"   ← 主程序 60s 判掉线")
    print(f"  link.master_stale = {link.get('master_stale', 90)}s"
          f"   ← 主程序这么久没心跳就叫醒你。不做保险丝，这是唯一的出路")

    from hunter.autobuy import stores_of
    print(f"  门店 = {'、'.join(stores_of(cfg)) or '(附近全部)'}"
          f"   ← 盯的就是买的，一份名单")

    print(f"  autobuy.apple_id = {ab.get('apple_id') or '(没写)'}")
    print(f"  autobuy.cdp_port = {ab.get('cdp_port') or '(默认 9222)'}"
          f"   ← 同一台机器上的几份部署必须各用一个端口，"
          f"否则是同一个 Chrome、同一份 cookie，账号会互相踢下线")
    print(f"  autobuy.max_orders = {ab.get('max_orders', 1)}   ← 每份部署各记各的")

    camp = dict(ab.get("camp") or {})
    if camp.get("enabled"):
        from hunter.monitor import watch_items
        items = watch_items(cfg)
        parts = [i["part"] for i in items]
        off = int(link.get("buyer_offset") or 0)
        part = parts[off % len(parts)] if parts else "(没有启用型号)"
        note = next((i.get("note") for i in items if i.get("part") == part), part)
        print(f"  autobuy.camp.enabled = true   ← 守株待兔：常驻蹲在结账页反复打 search")
        print(f"    蹲的型号 = {note}（{part}）"
              f"   ← 单账号一次只能蹲一个，由 buyer_offset 挑；只有它放货才有意义")
        print(f"    camp.cadence(热档) = {camp.get('cadence', 0)}s"
              f"   ← 报货后两发的最小间隔，0 = 一发回来立刻发下一发（服务端每会话 10s 放行一发）")
        print(f"    camp.idle_cadence(冷档) = {camp.get('idle_cadence', 120)}s"
              f"   ← 空闲时慢打 search 保温的间隔；凉了的会话一发要 20s，保温着才是 1~3s；90 跑 1.5 小时就 541，180 实测仍热；120 是折中")
        print(f"    camp.session_seconds = {camp.get('session_seconds', 1080)}s"
              f"   ← 多久重建会话，必须 < 20 分钟 TTL")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hunter2", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("buy", help="只买不盯——库存全靠探针喂，按账号加")
    b.set_defaults(func=cmd_buy)

    k = sub.add_parser("key", help="生成总线密钥")
    k.set_defaults(func=cmd_key)

    d = sub.add_parser("doctor", help="检查联机配置")
    d.set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    if args.cmd == "buy":
        # 长跑的那个才落盘。key / doctor 跑完就退，给它们开一份日志纯属噪音。
        try:
            log_cfg = load_config().get("logging") or {}
        except SystemExit:
            log_cfg = {}
        lb = setup_logbook(ROOT, log_cfg, command=args.cmd,
                           argv=argv or sys.argv[1:])
        if lb:
            req = f"，请求明细 {lb.req_path.name}" if lb.req_path else ""
            print(f"[日志] {lb.log_path}{req}")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
