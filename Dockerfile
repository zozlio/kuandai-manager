# 基础镜像。默认走国内镜像站（国内直连 Docker Hub 经常被拦或极慢）；
# 要用官方源，构建时覆盖：
#   docker build --build-arg PYTHON_IMAGE=python:3.12-slim .
ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.13-slim
FROM ${PYTHON_IMAGE}

# pip 源。默认用阿里云镜像站（官方源在国内慢），要用官方源就覆盖：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple/ \
#                --build-arg PIP_TRUSTED_HOST=pypi.org .
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

LABEL org.opencontainers.image.title="kuandai-manager"
LABEL org.opencontainers.image.description="宽带账号台账 / 充值提醒 / AI 助手"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    TZ=Asia/Shanghai \
    DATA_DIR=/data

WORKDIR /app

# apt 源。默认用阿里云镜像站（官方源在国内慢），留空则用官方源：
#   docker build --build-arg DEBIAN_MIRROR= .
ARG DEBIAN_MIRROR=mirrors.aliyun.com

RUN set -eux; \
    if [ -n "${DEBIAN_MIRROR}" ]; then \
      sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends tzdata curl; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY app /app/app
COPY static /app/static
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /data \
    && groupadd -r app && useradd -r -g app -u 10001 -d /app -s /sbin/nologin app \
    && chown -R app:app /app /data

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
