"""主程序的入口。

    HUNTER_BUS_KEY=<跟所有买手一样的随机串> python -m scout

**它必须先启动。** 买手不自己巡检，主程序不在就等于全体失明——所以买手一边
等心跳一边报警，而不是自己兜底。

跟买手放在不同的包里，是因为它是另一种程序：不要账号、不开浏览器、没有订单
状态。config.json 共用一份（它只读 `watch` / `pickup` / `pacing` / `link`）。
"""

from __future__ import annotations

import argparse
import sys

from hunter.__main__ import ROOT, load_config
from hunter.logbook import setup as setup_logbook

from .main import Scout


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scout", description=__doc__)
    ap.add_argument("--sprint", action="store_true",
                    help="冲刺模式（开卖前十分钟），所有出口一起提速")
    args = ap.parse_args(argv)
    cfg = load_config()
    # 日志要在跑起来之前接上：主程序现在是唯一的眼睛，放货那几秒到底看见了什么，
    # 事后只能从它的日志里找。
    lb = setup_logbook(ROOT, cfg.get("logging") or {}, command="scout",
                       argv=argv or sys.argv[1:])
    if lb:
        req = f"，请求明细 {lb.req_path.name}" if lb.req_path else ""
        print(f"[日志] {lb.log_path}{req}")
    Scout(cfg, ROOT, sprint=args.sprint).loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
