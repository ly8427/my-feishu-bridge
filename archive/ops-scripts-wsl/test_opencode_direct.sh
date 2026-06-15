#!/bin/bash
# Test opencode serve directly with deepseek

# Find running opencode serve port
PORT=$(docker exec feishu-claude-agent bash -c 'cat /tmp/opencode_serve_*.log 2>/dev/null | grep "listening on" | tail -1 | grep -oP "127.0.0.1:\K[0-9]+"')
echo "Port: $PORT"

# Test 1: health check
echo "=== Test 1: GET /session ==="
docker exec feishu-claude-agent curl -sS "http://127.0.0.1:$PORT/session" 2>&1 | head -5
echo ""

# Test 2: create session
echo "=== Test 2: POST /session ==="
SES=$(docker exec feishu-claude-agent curl -sS -X POST "http://127.0.0.1:$PORT/session" -H "Content-Type: application/json" -d '{"agent":"build","title":"test"}' 2>&1)
echo "$SES"
SID=$(echo "$SES" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
echo "Session ID: $SID"
echo ""

# Test 3: send prompt
echo "=== Test 3: POST prompt_async ==="
docker exec feishu-claude-agent curl -sS -X POST "http://127.0.0.1:$PORT/session/$SID/prompt_async" \
  -H "Content-Type: application/json" \
  -d '{"agent":"build","parts":[{"type":"text","text":"Reply OK"}]}' 2>&1
echo ""

# Test 4: get SSE events (brief)
echo "=== Test 4: GET /event (5 seconds) ==="
timeout 10 docker exec feishu-claude-agent curl -sS -N "http://127.0.0.1:$PORT/event" -H "Accept: text/event-stream" 2>&1 &
CURLPID=$!
sleep 8
kill $CURLPID 2>/dev/null
wait $CURLPID 2>/dev/null
echo ""
echo "Done"
