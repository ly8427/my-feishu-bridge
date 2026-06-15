#!/bin/bash
echo "=== agent processes in container ==="
docker exec feishu-claude-agent bash -c 'cat /proc/*/cmdline 2>/dev/null | tr "\0" " " | grep -E "python|opencode"' 2>/dev/null || echo "no agent found"
echo ""
echo "=== opencode serve logs ==="
docker exec feishu-claude-agent bash -c 'ls -t /tmp/opencode_serve_*.log 2>/dev/null | head -3; for f in $(ls -t /tmp/opencode_serve_*.log 2>/dev/null | head -3); do echo "--- $f ---"; cat "$f"; done'
echo ""
echo "=== latest bridge.log lines ==="
tail -5 /home/liu/projects/claudeWorkSpace/feishu-bridge/bridge.log
