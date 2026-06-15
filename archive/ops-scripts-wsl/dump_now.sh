#!/bin/bash
BPID=$(pgrep -f bridge.py | head -1)
echo "bridge PID: $BPID"
kill -USR1 $BPID
sleep 2
# Show only relevant info
tail -80 /home/liu/projects/claudeWorkSpace/feishu-bridge/bridge.log | grep -E 'SIGUSR1|Thread-|_drain|_render|card_|asyncio|confirm card|agent diagnostic|turn from|prompt_sent|00:2[3-5]'
