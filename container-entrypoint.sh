#!/bin/sh
set -eu

if [ "$(id -u)" -eq 0 ]; then
    # Railway and Render can mount an empty volume owned by root.
    mkdir -p "$DATA_DIR"
    chown cofry:cofry "$DATA_DIR"
    exec gosu cofry /bin/sh "$0" "$@"
fi

exec uvicorn cofry.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 \
    --timeout-graceful-shutdown 20 --no-access-log "$@"
