#!/bin/sh
set -eu
if [ -f .env ]; then
    set -a
    . ./.env
    set +a
fi
if [ -z "${COFRY_PYTHON:-}" ]; then
    if [ -x .venv/bin/python ]; then
        COFRY_PYTHON=.venv/bin/python
    else
        COFRY_PYTHON=python3
    fi
fi
host=0.0.0.0
port=${PORT:-8000}
while [ "$#" -gt 0 ]; do
    case "$1" in
        --host) host=$2; shift 2 ;;
        --port) port=$2; shift 2 ;;
        --strictPort) shift ;;
        *) echo "Opção desconhecida: $1" >&2; exit 2 ;;
    esac
done
exec "$COFRY_PYTHON" -m uvicorn cofry.main:app --host "$host" --port "$port" --workers 1 --no-access-log
