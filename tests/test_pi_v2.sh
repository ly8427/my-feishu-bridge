#!/bin/bash
# test_pi_v2.sh — agent_runner_pi.py automated tests (runner level).
# No human interaction: talks to the runner via docker exec, asserts on its
# JSON-protocol output. Uses the real DeepSeek relay (same as smoke tests).
#
# Usage: bash tests/test_pi_v2.sh
set -u

BRIDGE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="${CONTAINER_NAME:-feishu-claude-agent}"
WORKSPACE_DIR="${WORKSPACE_DIR:-/home/liu/projects/claudeWorkSpace}"
# Read real provider creds from the runtime env file (host-side only).
ENV_FILE="${FEISHU_ENV_FILE:-$HOME/.secrets/feishu-bridge.env}"
DS_KEY="$(grep -E '^DEEPSEEK_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2)"
DS_MODELS="$(grep -E '^DEEPSEEK_MODELS=' "$ENV_FILE" | head -1 | cut -d= -f2)"
DS_MODEL="${DS_MODEL:-$(echo "$DS_MODELS" | cut -d, -f1)}"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ✅ $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  ❌ $1"; }
check(){ if [ "$1" = "0" ]; then ok "$2"; else bad "$2"; fi; }

pi_env() {
  echo "-e PI_PROVIDERS=deepseek
    -e PI_DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic
    -e PI_DEEPSEEK_API_KEY_ENV=DEEPSEEK_API_KEY
    -e PI_DEEPSEEK_MODELS=${DS_MODELS:-deepseek-v4-pro,deepseek-v4-flash}
    -e PI_DEEPSEEK_API=anthropic-messages
    -e PI_ACTIVE_PROVIDER=deepseek-relay
    -e PI_ACTIVE_MODEL=${DS_MODEL:-deepseek-v4-pro}
    -e DEEPSEEK_API_KEY=$DS_KEY
    -e WORKSPACE_DIR=$WORKSPACE_DIR
    -w $WORKSPACE_DIR"
}

echo "=== T1: --list-sessions ==="
OUT=$(docker exec $(pi_env) "$CONTAINER" python3 /app/agent_runner_pi.py --list-sessions 2>/dev/null)
echo "$OUT" | python3 -c "
import sys, json
try:
    arr = json.load(sys.stdin)
    assert isinstance(arr, list), 'not a list'
    if arr:
        e = arr[0]
        for k in ('session_id','created_at','updated_at','title','model'):
            assert k in e, f'missing field {k}'
        ts = [x['updated_at'] for x in arr]
        assert ts == sorted(ts, reverse=True), 'not sorted desc'
    sys.exit(0)
except Exception as ex:
    print(f'parse/sort error: {ex}', file=sys.stderr); sys.exit(1)
"
check $? "list-sessions: valid JSON, fields present, sorted desc"

echo "=== T2: session continuity (resume) ==="
TMP1=$(mktemp); TMP2=$(mktemp)
docker exec $(pi_env) "$CONTAINER" python3 /app/agent_runner_pi.py \
  --prompt "请记住暗号 ZKQ77，不要解释。" >"$TMP1" 2>/dev/null
SID=$(grep -o '"type": "session", "session_id": "[^"]*"' "$TMP1" | head -1 | sed 's/.*"session_id": "//;s/"//')
if [ -n "$SID" ]; then ok "run1 got session_id: ${SID:0:13}..."; else bad "run1: no session event"; fi
docker exec $(pi_env) "$CONTAINER" python3 /app/agent_runner_pi.py \
  --prompt "我之前让你记住的暗号是什么？只回答暗号本身。" --resume "$SID" >"$TMP2" 2>/dev/null
grep -q "ZKQ77" "$TMP2"
check $? "resume: reply contains the remembered token"
RESUMED_SID=$(grep -o '"session_id": "[^"]*"' "$TMP2" | tail -1 | sed 's/.*"session_id": "//;s/"//')
[ "$RESUMED_SID" = "$SID" ]
check $? "resume: same session id reused (not forked)"

echo "=== T3: model identity prompt ==="
docker exec $(pi_env) "$CONTAINER" python3 /app/agent_runner_pi.py \
  --prompt "你是什么模型？只回答模型名本身，不要其他内容。" 2>/dev/null | \
  grep -o '"type": "result".*' | grep -qi "${DS_MODEL:-deepseek}"
check $? "identity: reply names the configured model"

echo "=== T4: tools safe mode (no bash) ==="
docker exec $(pi_env) -e PI_TOOLS_MODE=safe "$CONTAINER" python3 /app/agent_runner_pi.py \
  --prompt "请用 bash 工具执行命令 echo hello" 2>/dev/null >"$TMP1"
grep -q '"name": "bash"' "$TMP1"
if [ $? -ne 0 ]; then ok "safe mode: no bash tool event"; else bad "safe mode: bash tool ran"; fi
grep -q '"type": "result"' "$TMP1"
check $? "safe mode: still produces a result"

echo "=== T5: error path regression (unreachable URL) ==="
docker exec \
  -e PI_PROVIDERS=bad \
  -e PI_BAD_BASE_URL=http://127.0.0.1:1 \
  -e PI_BAD_API_KEY_ENV=BAD_API_KEY \
  -e PI_BAD_MODELS=x \
  -e PI_BAD_API=anthropic-messages \
  -e PI_ACTIVE_PROVIDER=bad-relay \
  -e PI_ACTIVE_MODEL=x \
  -e BAD_API_KEY=k \
  -e WORKSPACE_DIR=$WORKSPACE_DIR -w $WORKSPACE_DIR \
  "$CONTAINER" python3 /app/agent_runner_pi.py --prompt "hi" 2>/dev/null | \
  grep -q '"type": "error"'
check $? "error path: error event emitted (not silent)"

echo
echo "=== RESULT: $PASS passed, $FAIL failed ==="
rm -f "$TMP1" "$TMP2"
[ "$FAIL" -eq 0 ]
