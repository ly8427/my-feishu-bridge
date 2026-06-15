#!/bin/bash
echo "=== latest opencode serve logs ==="
docker exec feishu-claude-agent bash -c 'ls -t /tmp/opencode_serve_*.log 2>/dev/null | head -3' | while read f; do
    echo "--- $f ---"
    docker exec feishu-claude-agent cat "$f" 2>/dev/null
    echo ""
done

echo "=== agent processes ==="
docker exec feishu-claude-agent bash -c 'for pid in $(ls /proc | grep -E "^[0-9]+$"); do tr "\0" " " < /proc/$pid/cmdline 2>/dev/null; echo; done' | grep -E 'opencode|agent' | head -10

echo ""
echo "=== Check if port is open ==="
docker exec feishu-claude-agent bash -c 'cat /proc/net/tcp 2>/dev/null | head -5'
