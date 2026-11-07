#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "${DATA_DIR}"

# 宿主机挂载进来的目录可能属于 root，先修好权限再降权运行。
if [ "$(id -u)" = "0" ]; then
  chown -R app:app "${DATA_DIR}" 2>/dev/null || true
  if command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid=app --regid=app --init-groups "$@"
  fi
  # 没有 setpriv 时用 su，并把参数原样透传（避免 * 被通配符展开）
  exec su -s /bin/sh app -c 'exec "$@"' sh "$@"
fi

exec "$@"
