#!/bin/bash
export FEISHU_ENV_FILE=/home/liu/.secrets/feishu-bridge.env
cd /home/liu/projects/claudeWorkSpace/feishu-bridge

# Kill any existing bridge via PID file
if [ -f bridge.pid ]; then
    OLD=$(cat bridge.pid)
    kill $OLD 2>/dev/null
    sleep 1
    kill -9 $OLD 2>/dev/null
    echo "Killed old bridge PID $OLD"
fi

# Start new bridge (nohup + disown keeps it alive)
nohup .venv/bin/python3 bridge.py &>/tmp/br_safe.log &
disown
sleep 5

# Verify
NEW=$(cat bridge.pid 2>/dev/null)
if kill -0 $NEW 2>/dev/null; then
    echo "Bridge running: PID $NEW"
    tail -3 bridge.log
else
    echo "FAILED to start!"
    cat /tmp/br_safe.log | tail -20
fi
