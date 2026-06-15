#!/bin/bash
# Kill all bridge-related processes aggressively
echo "Killing all bridge processes..."
for sig in TERM KILL; do
    for pid in $(ps aux | grep -E '[b]ridge\.py|[p]ython.*bridge' | awk '{print $2}'); do
        kill -$sig $pid 2>/dev/null
        echo "killed $pid"
    done
    sleep 1
done
# Also kill leftover nohup shells
pkill -9 -f "nohup.*bridge" 2>/dev/null
sleep 2
echo "Remaining:"
ps aux | grep -v grep | grep -E 'bridge|python.*feishu' || echo "all clean"
