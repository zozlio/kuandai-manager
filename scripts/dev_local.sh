#!/usr/bin/env bash
# 本地预览：在自己电脑上跑起来看，不用打包、不用上传 NAS。
#
#   ./scripts/dev_local.sh          # 启动，浏览器打开 http://127.0.0.1:18099
#   ./scripts/dev_local.sh stop     # 停掉
#
# 数据存在项目下的 .localdata/ 目录（已加入 .gitignore），跟正式数据完全分开。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-18099}"
HOST="${HOST:-127.0.0.1}"
DATA_DIR="${DATA_DIR:-$ROOT/.localdata}"
PY="$ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "没找到虚拟环境，先建一个："
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

mkdir -p "$DATA_DIR"

if [ "${1:-start}" = "stop" ]; then
  pkill -f "uvicorn app.main:app --host $HOST --port $PORT" && echo "已停掉" || echo "本来就没在跑"
  exit 0
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 $PORT 已经被占了，先停掉：./scripts/dev_local.sh stop"
  exit 1
fi

echo "本地预览启动中…  地址 http://$HOST:$PORT"
echo "数据目录 $DATA_DIR"
echo "第一次打开会让你注册一个账户，注册完就能进。"

DATA_DIR="$DATA_DIR" "$PY" -m uvicorn app.main:app \
  --host "$HOST" --port "$PORT" --log-level warning
