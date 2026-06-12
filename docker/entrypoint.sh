#!/bin/sh
# POSIX-compliant container entrypoint: wait for Redis socket readiness, then exec PID 1.
set -eu

REDIS_HOST="${REDIS_HOST:-datastore}"
REDIS_PORT="${REDIS_PORT:-6379}"
MAX_WAIT_SECONDS=30
POLL_INTERVAL_SECONDS=2

echo "[entrypoint] Initializing telemetry pipeline container"
echo "[entrypoint] Waiting for datastore socket at ${REDIS_HOST}:${REDIS_PORT}"

elapsed=0
while [ "${elapsed}" -lt "${MAX_WAIT_SECONDS}" ]; do
    if python -c "
import socket
import sys

host = '${REDIS_HOST}'
port = int('${REDIS_PORT}')
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(2)
try:
    sock.connect((host, port))
    sys.exit(0)
except OSError:
    sys.exit(1)
finally:
    sock.close()
"; then
        echo "[entrypoint] Datastore reachable after ${elapsed}s"
        echo "[entrypoint] Handing off to application (exec python app/pipeline.py)"
        exec python app/pipeline.py
    fi

    echo "[entrypoint] Datastore unavailable (${elapsed}s / ${MAX_WAIT_SECONDS}s); retrying in ${POLL_INTERVAL_SECONDS}s"
    sleep "${POLL_INTERVAL_SECONDS}"
    elapsed=$((elapsed + POLL_INTERVAL_SECONDS))
done

echo "[entrypoint] ERROR: datastore did not respond on port ${REDIS_PORT} within ${MAX_WAIT_SECONDS}s"
exit 1
