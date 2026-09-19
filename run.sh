#!/usr/bin/env bash
# 有启用型号时只盯库存；未配置型号时盯上架。显式传 --sprint 才冲刺。
set -euo pipefail
cd "$(dirname "$0")"
PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3
if "$PY" -c 'import json,sys; from hunter.monitor import watch_items; sys.exit(0 if watch_items(json.load(open("config.json"))) else 1)'; then
  exec "$PY" -m hunter watch "$@"
else
  exec "$PY" -m hunter launch "$@"
fi
