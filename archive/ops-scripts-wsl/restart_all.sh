#!/bin/bash
echo "=== 1. Kill bridge ==="
for pid in $(pgrep -f bridge.py 2>/dev/null); do
    kill -9 $pid 2>/dev/null
done
sleep 2

echo "=== 2. Kill stale docker exec processes ==="
for pid in $(pgrep -f 'docker exec.*agent_runner' 2>/dev/null); do
    kill -9 $pid 2>/dev/null
done

echo "=== 3. Kill agents inside container ==="
docker exec feishu-claude-agent bash -c '
for pid in $(ls /proc 2>/dev/null | grep -E "^[0-9]+$"); do
    cmdline=$(tr "\0" " " < /proc/$pid/cmdline 2>/dev/null)
    case "$cmdline" in
        *agent_runner_opencode*|*"opencode serve"*)
            echo "killing container pid $pid: $cmdline"
            kill -9 $pid 2>/dev/null
            ;;
    esac
done
'
sleep 1

echo "=== 4. Copy latest runner to container ==="
docker cp /home/liu/projects/claudeWorkSpace/feishu-bridge/agent_runner_opencode.py feishu-claude-agent:/app/agent_runner_opencode.py
echo "copied OK"

echo "=== 5. Start bridge ==="
export FEISHU_ENV_FILE=/home/liu/.secrets/feishu-bridge.env
cd /home/liu/projects/claudeWorkSpace/feishu-bridge
nohup .venv/bin/python3 bridge.py &>/tmp/br_final.log &
BPID=$!
sleep 5
echo "Bridge PID: $BPID"
tail -5 bridge.log

echo ""
echo "=== 6. Verify container has instrumented runner ==="
docker exec feishu-claude-agent python3 -c "import sys; sys.path.insert(0,'/app'); import agent_runner_opencode; print('phase' in dir(agent_runner_opencode) and 'ok' or 'NO PHASE FUNC')"
