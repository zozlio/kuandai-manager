#!/bin/bash
# 在飞牛 NAS 上部署 kuandai-manager
# 用法（在 NAS 上执行）: bash deploy_nas.sh <APP_DIR> <DATA_DIR> <PORT>
set -e

APP_DIR="${1:-/vol1/1000/docker/kuandai-manager}"
DATA_DIR="${2:-/vol1/1000/docker/kuandai-data}"
PORT="${3:-18080}"
IMAGE="kuandai-manager:latest"
CONTAINER="kuandai-manager"

echo "==> 目录准备"
mkdir -p "$APP_DIR" "$DATA_DIR"

echo "==> 解压源码"
cd "$APP_DIR"
if [ -f /tmp/kuandai-deploy.tar.gz ]; then
  tar -xzf /tmp/kuandai-deploy.tar.gz --strip-components=1 -C "$APP_DIR"
elif [ -f ./kuandai-deploy.tar.gz ]; then
  tar -xzf ./kuandai-deploy.tar.gz --strip-components=1 -C "$APP_DIR"
else
  echo "!! 找不到 kuandai-deploy.tar.gz，跳过解压"
fi

echo "==> 检查 Docker"
docker --version

echo "==> 构建镜像"
cd "$APP_DIR"
docker build -t "$IMAGE" .

echo "==> 清理同名旧容器"
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  docker rm -f "$CONTAINER" || true
fi

echo "==> 启动容器（端口 $PORT，数据目录 $DATA_DIR）"
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  -p "$PORT":8080 \
  -v "$DATA_DIR":/data \
  -e AI_BASE_URL="${AI_BASE_URL:-https://api.openai.com/v1}" \
  -e AI_API_KEY="${AI_API_KEY:-}" \
  -e AI_MODEL="${AI_MODEL:-gpt-4o}" \
  -e VISION_TIMEOUT="${VISION_TIMEOUT:-300}" \
  "$IMAGE"

echo "==> 等待健康检查"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "==> 部署成功：http://<NAS_IP>:$PORT"
    docker ps --filter "name=$CONTAINER"
    exit 0
  fi
  sleep 2
done

echo "==> 健康检查未通过，日志如下："
docker logs --tail 60 "$CONTAINER" || true
exit 1
