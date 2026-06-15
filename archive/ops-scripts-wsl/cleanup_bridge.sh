#!/bin/bash
# Kill all bridge and stray agent processes
for pid in $(pgrep -f bridge.py 2>/dev/null); do
    kill -9 $pid 2>/dev/null
done
for pid in $(pgrep -f 'docker exec.*agent_runner' 2>/dev/null); do
    kill -9 $pid 2>/dev/null
done
sleep 2
echo "cleanup done"
pgrep -f 'bridge.py|agent_runner' && echo "WARNING: still running" || echo "all clear"
