#!/bin/bash
# Find latest opencode serve port
PORT=$(docker exec feishu-claude-agent bash -c 'cat $(ls -t /tmp/opencode_serve_*.log | head -1) | grep -o "127.0.0.1:[0-9]*" | cut -d: -f2')
echo "Port: $PORT"

# 1) Start SSE listener in background (connect BEFORE creating session)
echo "=== Starting SSE listener ==="
timeout 15 docker exec feishu-claude-agent curl -sS -N "http://127.0.0.1:$PORT/event" \
  -H "Accept: text/event-stream" > /tmp/sse_output.txt 2>&1 &
SSE_PID=$!
sleep 1

# 2) Create session
SES=$(docker exec feishu-claude-agent curl -sS -X POST "http://127.0.0.1:$PORT/session" \
  -H "Content-Type: application/json" \
  -d '{"agent":"build","title":"sse-test"}')
echo "Session: $SES"
SID=$(echo "$SES" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
echo "SID: $SID"

# 3) Send prompt
echo "=== Sending prompt ==="
docker exec feishu-claude-agent curl -sS -X POST "http://127.0.0.1:$PORT/session/$SID/prompt_async" \
  -H "Content-Type: application/json" \
  -d '{"agent":"build","parts":[{"type":"text","text":"Reply OK"}]}'
echo ""

# 4) Wait for SSE events
echo "=== Waiting for SSE events ==="
sleep 10

# 5) Show results
echo "=== SSE output ==="
cat /tmp/sse_output.txt 2>/dev/null | head -20
kill $SSE_PID 2>/dev/null
echo ""
echo "Done"
