#!/usr/bin/env bash
# EvoHarness 数字员工单活启动（Linux 包装脚本，DE-23）。
# 真正的检查/单活/看护在 ops/supervisor.py；本脚本只负责定位 Python 并拉起主管。
# 用法：./ops/start.sh [--once] [--db sqlite:///data/agent.db] [--cwd feishu-daily-agent]
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
cwd="${CWD:-$repo/feishu-daily-agent}"
db="${DB:-}"

if [ -x "$repo/feishu-daily-agent/.venv/bin/python" ]; then
  python="$repo/feishu-daily-agent/.venv/bin/python"
else
  python="${PYTHON:-python3}"
fi

args=(start --cwd "$cwd")
[ -n "$db" ] && args+=(--db "$db")
for arg in "$@"; do
  args+=("$arg")
done

cd "$repo"
# 部署者通过环境变量提供 DATABASE_URL / FEISHU_TOKEN_SECRET / FEISHU_* 配置；
# 本脚本不读也不写任何凭据。
exec "$python" -m ops.supervisor "${args[@]}"
