#!/bin/bash
# Kill stale agent processes inside container using /proc
docker exec feishu-claude-agent bash -c '
for pid in $(ls /proc | grep -E "^[0-9]+$"); do
    cmdline=$(tr "\0" " " < /proc/$pid/cmdline 2>/dev/null)
    case "$cmdline" in
        *agent_runner_opencode*|*"opencode serve"*)
            echo "killing $pid: $cmdline"
            kill -9 $pid 2>/dev/null
            ;;
    esac
done
echo "all stale agents killed"
'
sleep 1
echo "=== verify ==="
docker exec feishu-claude-agent bash -c 'for pid in $(ls /proc | grep -E "^[0-9]+$"); do tr "\0" " " < /proc/$pid/cmdline 2>/dev/null; echo; done' | grep -E 'agent|opencode' || echo "no agent processes"
