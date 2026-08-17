#!/bin/sh
# Start the self-hosted pmxt sidecar, then the API.
#
# Both processes live in one container so they deploy and scale as one unit.
# The sidecar binds to 127.0.0.1:3847 and is not reachable from outside the
# container; only uvicorn listens on $PORT.
#
# If either process dies, this script exits and Cloud Run replaces the
# instance. A container serving /scan without a working sidecar would report
# venue failures it cannot explain, which is worse than being replaced.
#
# POSIX sh throughout: the base image's /bin/sh is dash, which has no
# `wait -n`. Hence the explicit supervision loop below.

set -eu

: "${PORT:=8080}"
: "${PMXT_ACCESS_TOKEN:=}"

if [ -z "$PMXT_ACCESS_TOKEN" ]; then
    # The sidecar invents a random token when none is supplied, which the API
    # would then not know. Generate one both processes can see.
    PMXT_ACCESS_TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    export PMXT_ACCESS_TOKEN
fi

log() {
    printf '{"severity":"%s","message":"%s","logger":"entrypoint"}\n' "$1" "$2"
}

log INFO "starting pmxt sidecar"
node /opt/pmxt/node_modules/pmxt-core/dist/server/index.js &
SIDECAR_PID=$!

log INFO "starting api"
uvicorn main:app --host 0.0.0.0 --port "$PORT" --workers 1 --timeout-keep-alive 65 &
API_PID=$!

shutdown() {
    log INFO "shutting down"
    kill -TERM "$SIDECAR_PID" "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT

# Supervise both. `kill -0` tests liveness without signalling.
while true; do
    if ! kill -0 "$SIDECAR_PID" 2>/dev/null; then
        log ERROR "pmxt sidecar exited — stopping the instance so it is replaced"
        kill -TERM "$API_PID" 2>/dev/null || true
        wait "$API_PID" 2>/dev/null || true
        exit 1
    fi
    if ! kill -0 "$API_PID" 2>/dev/null; then
        log ERROR "api exited — stopping the instance so it is replaced"
        kill -TERM "$SIDECAR_PID" 2>/dev/null || true
        exit 1
    fi
    sleep 2
done
