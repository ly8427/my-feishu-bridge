#!/bin/bash
# Stop manual bridge, start via systemd
PIDFILE=/home/liu/projects/claudeWorkSpace/feishu-bridge/bridge.pid
if [ -f $PIDFILE ]; then
    kill $(cat $PIDFILE) 2>/dev/null
    echo "Killed manual bridge"
fi
sleep 1
systemctl --user daemon-reload
systemctl --user start feishu-bridge
sleep 3
systemctl --user status feishu-bridge --no-pager
