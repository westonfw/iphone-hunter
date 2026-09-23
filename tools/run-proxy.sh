#!/usr/bin/env bash
# 起代理：监控池走 ips.txt，每个账号一个固定口、一份 buyer-*.txt。
#
#   tools/run-proxy.sh                 # 8080 轮换口 + 按文件名顺序 8081、8082…固定口
#   tools/run-proxy.sh --allow 1.2.3.4 # 额外参数原样透传给 rotating-proxy.py
#
# 读同目录的 proxy.env（可选，chmod 600）：
#   ROTPROXY_PASS=强密码            必填（也可以直接 export）
#   ROTPROXY_USER=hunter            默认 hunter
#   ROTPROXY_PORT=8080              轮换口
#   ROTPROXY_BUYER_BASE=8081        第一个固定口，之后按 buyer-*.txt 文件名顺序 +1
#   ROTPROXY_ALLOW="1.2.3.4 5.6.7.0/24"   免密来源，空格分隔（买手机器的公网 IP）
set -euo pipefail
cd "$(dirname "$0")"

[ -f proxy.env ] && set -a && . ./proxy.env && set +a
: "${ROTPROXY_PASS:?请在 tools/proxy.env 里写 ROTPROXY_PASS=强密码，或先 export ROTPROXY_PASS}"
USER_="${ROTPROXY_USER:-hunter}"
PORT="${ROTPROXY_PORT:-8080}"
BASE="${ROTPROXY_BUYER_BASE:-8081}"

[ -f ips.txt ] || { echo "没有 tools/ips.txt（监控池）。cp ips.txt.example ips.txt 后填真实 IP，或用 list-ips.py -o ips.txt 生成。" >&2; exit 2; }

args=(--port "$PORT" --user "$USER_" --ips ips.txt)
i=0
for f in buyer-*.txt; do
  [ -e "$f" ] || break            # 一个 buyer-*.txt 都没有时 glob 原样返回
  args+=(--buyer "$((BASE + i))=$f")
  i=$((i + 1))
done
[ "$i" -gt 0 ] || echo "提示：没有 buyer-*.txt，只起轮换口。要让结账走代理，每个账号建一份（见 buyer-a.txt.example）。" >&2
for a in ${ROTPROXY_ALLOW:-}; do args+=(--allow "$a"); done

echo "[run-proxy] 轮换口 $PORT · 固定口 $i 个（从 $BASE 起，按 buyer-*.txt 文件名顺序）"
exec python3 -u rotating-proxy.py "${args[@]}" "$@"
