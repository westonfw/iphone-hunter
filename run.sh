#!/usr/bin/env bash
# 开卖当天的一键启动：盯上架 + 盯库存/门店，两个循环同时跑。
set -euo pipefail
cd "$(dirname "$0")"

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3     # 没建虚拟环境就退回系统 python（自动下单会不可用）

"$PY" -m hunter launch --sprint &
LAUNCH_PID=$!
trap 'kill $LAUNCH_PID 2>/dev/null || true' EXIT

if "$PY" -c "import json,sys; sys.exit(0 if json.load(open('config.json')).get('watch') else 1)" 2>/dev/null; then
  "$PY" -m hunter watch --sprint
else
  echo "config.json 的 watch 还是空的，本次只监控机型上架。"
  wait $LAUNCH_PID
fi
