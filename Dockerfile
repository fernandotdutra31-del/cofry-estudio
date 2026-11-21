# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS node_runtime
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data PORT=8000 HF_HOME=/data/huggingface OMP_NUM_THREADS=2
WORKDIR /app
RUN --mount=type=secret,id=build_ca \
    --mount=type=bind,from=node_runtime,source=/usr/local/bin/node,target=/tmp/node-runtime \
    --mount=type=bind,source=.,target=/tmp/project \
    apt-get update && apt-get install -y --no-install-recommends ffmpeg fontconfig fonts-dejavu-core ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/* \
    && install -m 0755 /tmp/node-runtime /usr/local/bin/node \
    && groupadd --gid 10001 cofry && useradd --uid 10001 --gid 10001 --no-create-home cofry \
    && mkdir -p /data && chown cofry:cofry /data \
    && if [ -f /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi \
    && pip install --no-cache-dir -r /tmp/project/requirements.txt \
    && cp -r /tmp/project/cofry /app/cofry \
    && chmod -R a+rX /app/cofry \
    && install -m 0755 /tmp/project/container-entrypoint.sh /app/container-entrypoint.sh
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health', timeout=3)"
ENTRYPOINT ["/bin/sh", "/app/container-entrypoint.sh"]
