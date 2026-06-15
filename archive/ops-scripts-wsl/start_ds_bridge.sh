#!/bin/bash
# Kill everything old
pkill -9 -f bridge.py 2>/dev/null
sleep 1

# Verify config
echo "=== Config ==="
grep -E 'OPENCODE_MODEL|OPENCODE_API_KEY' /home/liu/.secrets/feishu-bridge.env

# Start bridge
export FEISHU_ENV_FILE=/home/liu/.secrets/feishu-bridge.env
cd /home/liu/projects/claudeWorkSpace/feishu-bridge
nohup .venv/bin/python3 bridge.py &>/tmp/br_ds.log &
sleep 5

echo "=== Started ==="
ps aux | grep -v grep | grep bridge
tail -5 /home/liu/projects/claudeWorkSpace/feishu-bridge/bridge.log 2>/dev/null
cat /tmp/br_ds.log 2>/dev/null | tail -5
