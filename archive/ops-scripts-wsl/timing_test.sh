#!/bin/bash
set -e
ENVFILE=/home/liu/.secrets/feishu-bridge.env
API_KEY=$(grep '^OPENCODE_API_KEY=' $ENVFILE | cut -d= -f2)

echo "=== TEST 1: direct GLM-5.2 API latency (non-stream) ==="
START=$(date +%s.%N)
curl -sS -o /tmp/glm_resp.json -w "http_code=%{http_code} total=%{time_total}s connect=%{time_connect}s ttfb=%{time_starttransfer}s\n" \
  -X POST "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"glm-5.2\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: OK\"}],\"stream\":false}"
END=$(date +%s.%N)
echo "wall: $(echo "$END - $START" | bc)s"
echo "--- response (first 500 chars) ---"
head -c 500 /tmp/glm_resp.json
echo ""
echo ""
echo "=== TEST 2: opencode serve cold-start time ==="
PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
echo "starting opencode serve on port $PORT ..."
START=$(date +%s.%N)
opencode serve --port $PORT --hostname 127.0.0.1 > /tmp/oc_serve_test.log 2>&1 &
SERVE_PID=$!
# wait until listening or 30s
for i in $(seq 1 60); do
  if curl -sS -o /dev/null "http://127.0.0.1:$PORT/session" 2>/dev/null; then
    END=$(date +%s.%N)
    echo "opencode serve ready after: $(echo "$END - $START" | bc)s"
    break
  fi
  sleep 0.5
done
echo "--- serve log ---"
head -c 800 /tmp/oc_serve_test.log
echo ""
echo "leaving server running for test 3 (PID $SERVE_PID)"
echo "PORT=$PORT"
echo "PID=$SERVE_PID" > /tmp/oc_test_state.txt
