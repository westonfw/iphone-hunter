"""hunter2 的入口。跟 `python -m hunter watch` 做的是同一件事，多一条局域网总线。

部署一个新账号 = 把整个目录拷一份、改 config.json。订单记录、浏览器 profile、
日志目录都在各自的目录下，天然隔离。

    HUNTER_BUS_KEY=<两边一样的随机串> python -m hunter2 watch

没配 HUNTER_BUS_KEY 就退化成单机，行为跟 hunter watch 完全一致。
"""

from __future__ import annotations

import argparse
import secrets

from hunter.__main__ import ROOT, load_config

from .bus import KEY_ENV, bus_key
from .buyer import Buyer
from .link import LinkedWatcher
from .sensor import Sensor


def cmd_watch(args) -> int:
    """又盯又买（单机也能跑）。想把两件事拆到不同进程/机器上，用 sense / buy。"""
    LinkedWatcher(load_config(), ROOT, sprint=args.sprint).loop()
    return 0


def cmd_sense(args) -> int:
    """只盯不买。不需要账号，所以可以按出口 IP 随便加。"""
    Sensor(load_config(), ROOT, sprint=args.sprint).loop()
    return 0


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
    print(f"  link.peers = {link.get('peers') or '(广播)'}")

    print(f"  autobuy.apple_id = {ab.get('apple_id') or '(没写)'}")
    print(f"  autobuy.cdp_port = {ab.get('cdp_port') or '(默认 9222)'}"
          f"   ← 同一台机器上的几份部署必须各用一个端口，"
          f"否则是同一个 Chrome、同一份 cookie，账号会互相踢下线")
    print(f"  autobuy.max_orders = {ab.get('max_orders', 1)}   ← 每份部署各记各的")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hunter2", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="又盯又买（单机模式），同时接入总线")
    w.add_argument("--sprint", action="store_true", help="冲刺模式（开卖前十分钟）")
    w.set_defaults(func=cmd_watch)

    se = sub.add_parser("sense", help="只盯不买——不需要账号，按出口 IP 加")
    se.add_argument("--sprint", action="store_true", help="冲刺模式")
    se.set_defaults(func=cmd_sense)

    b = sub.add_parser("buy", help="只买不盯——库存全靠探针喂，按账号加")
    b.set_defaults(func=cmd_buy)

    k = sub.add_parser("key", help="生成总线密钥")
    k.set_defaults(func=cmd_key)

    d = sub.add_parser("doctor", help="检查联机配置")
    d.set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
