#!/bin/bash
set -e

echo "=== Kill old processes ==="
for pid in $(pgrep -f bridge.py 2>/dev/null); do
    kill -9 $pid 2>/dev/null
done

docker exec feishu-claude-agent bash -c '
for pid in $(ls /proc | grep -E "^[0-9]+$"); do
    cmdline=$(tr "\0" " " < /proc/$pid/cmdline 2>/dev/null)
    case "$cmdline" in
        *agent_runner*|*"opencode serve"*)
            echo "killing container pid $pid"
            kill -9 $pid 2>/dev/null
            ;;
    esac
done
'
sleep 2

echo "=== Verify env ==="
grep -E 'OPENCODE_MODEL|OPENCODE_API_KEY' /home/liu/.secrets/feishu-bridge.env

echo "=== Start bridge ==="
export FEISHU_ENV_FILE=/home/liu/.secrets/feishu-bridge.env
cd /home/liu/projects/claudeWorkSpace/feishu-bridge
nohup .venv/bin/python3 bridge.py &>/tmp/br_ds.log &
BPID=$!
sleep 5

echo "=== Status ==="
echo "Bridge PID: $BPID"
tail -5 bridge.log
echo ""
echo "Ready. Test: /engine opencode then create file"
