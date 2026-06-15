#!/bin/bash
BPID=$(pgrep -f bridge.py | head -1)
echo "bridge PID: $BPID"
kill -USR1 $BPID
sleep 2
tail -80 /home/liu/projects/claudeWorkSpace/feishu-bridge/bridge.log > /tmp/tdump.txt
cat /tmp/tdump.txt
