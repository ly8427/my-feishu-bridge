#!/bin/bash
set -e
ENV_FILE=/home/liu/.secrets/feishu-bridge.env
export $(grep -v '^#' $ENV_FILE | grep -v '^$' | xargs)

echo "=== Simulating bridge docker exec ==="
timeout 20 docker exec -i \
  -e ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL}" \
  -e ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN}" \
  -e OPENCODE_API_KEY="${OPENCODE_API_KEY}" \
  -e OPENCODE_API_URL="${OPENCODE_API_URL}" \
  -e OPENCODE_MODEL="${OPENCODE_MODEL}" \
  -e ZHIPU_API_KEY="${OPENCODE_API_KEY}" \
  -e SAFE_TOOLS="Read,Grep,Glob,WebSearch,WebFetch,TodoWrite,NotebookRead" \
  -e CONFIRM_TIMEOUT="300" \
  -e WORKSPACE_DIR="/home/liu/projects/claudeWorkSpace" \
  feishu-claude-agent \
  python3 /app/agent_runner_opencode.py --prompt "Reply OK" 2>&1
echo "EXIT=$?"
