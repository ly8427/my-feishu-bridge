#!/usr/bin/env bash
# Pre-start gate for the feishu-bridge systemd user service.
# Docker here is Docker Desktop (Windows-side), surfaced into WSL via
# /var/run/docker.sock. After a Windows login it can take 20-40s before the
# socket is live. This script blocks until Docker responds, then makes sure
# the agent container is running, so the bridge never starts into a dead socket.
set -euo pipefail

CONTAINER="${CONTAINER_NAME:-feishu-claude-agent}"
MAX_WAIT="${MAX_WAIT:-180}"   # seconds to wait for the Docker daemon

# 1) Wait for the Docker daemon (socket) to answer.
waited=0
until docker info >/dev/null 2>&1; do
    if [ "$waited" -ge "$MAX_WAIT" ]; then
        echo "wait-for-docker: Docker did not become ready within ${MAX_WAIT}s" >&2
        exit 1
    fi
    sleep 3
    waited=$((waited + 3))
done
echo "wait-for-docker: Docker ready after ${waited}s"

# 2) Ensure the agent container is up (Docker Desktop's restart policy usually
#    handles this, but start it explicitly if it's stopped/absent).
state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo missing)"
case "$state" in
    running)  echo "wait-for-docker: container '$CONTAINER' already running" ;;
    missing)  echo "wait-for-docker: container '$CONTAINER' missing — start it via docker/docker-compose.yml first" >&2; exit 1 ;;
    *)        echo "wait-for-docker: starting container '$CONTAINER' (was: $state)"; docker start "$CONTAINER" >/dev/null ;;
esac

exit 0
