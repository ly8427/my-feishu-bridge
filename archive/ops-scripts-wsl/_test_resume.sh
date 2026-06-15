#!/usr/bin/env bash
set -u
ENVFILE=/home/liu/.secrets/feishu-bridge.env
set -a; . "$ENVFILE"; set +u

echo "=== Reproduce: claude engine with the polluted session id ==="
echo "START: $(date '+%H:%M:%S')"
timeout 30 docker exec -i \
  -e "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL" \
  -e "ANTHROPIC_AUTH_TOKEN=$ANTHROPIC_AUTH_TOKEN" \
  -e "SAFE_TOOLS=Read,Grep,Glob,WebSearch,WebFetch,TodoWrite,NotebookRead" \
  -e "CONFIRM_TIMEOUT=30" \
  -e "WORKSPACE_DIR=$WORKSPACE_DIR" \
  feishu-claude-agent \
  python3 /app/agent_runner.py --prompt "回复:好" --resume ses_13984e29affed8wsQ4zhOIxj2U 2>&1 | head -10
echo ""
echo "=== And WITHOUT resume (should work) ==="
timeout 30 docker exec -i \
  -e "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL" \
  -e "ANTHROPIC_AUTH_TOKEN=$ANTHROPIC_AUTH_TOKEN" \
  -e "SAFE_TOOLS=Read,Grep,Glob" \
  -e "CONFIRM_TIMEOUT=30" \
  -e "WORKSPACE_DIR=$WORKSPACE_DIR" \
  feishu-claude-agent \
  python3 /app/agent_runner.py --prompt "回复:好" 2>&1 | head -5
