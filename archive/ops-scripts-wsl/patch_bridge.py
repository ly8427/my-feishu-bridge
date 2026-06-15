#!/usr/bin/env python3
"""Patch bridge.py: log unknown message types + non-JSON stderr lines."""
import sys

BRIDGE = "/home/liu/projects/claudeWorkSpace/feishu-bridge/bridge.py"

with open(BRIDGE, "r", encoding="utf-8") as f:
    bcontent = f.read()

# Patch A: add else clause to log unknown message types
# The last elif in _render_loop is "elif mtype == \"error\":"
old_error = 'elif mtype == "error":\n            job.render(f"\\n\\n\u274c \u51fa\u9519: {msg.get(\'message\')}")'
new_error = old_error + '\n        else:\n            log.info("agent diagnostic: %s", str(msg)[:300])'
assert old_error in bcontent, "error elif block not found!"
bcontent = bcontent.replace(old_error, new_error, 1)
print("Patch A OK: log unknown msg types")

# Patch B: log non-JSON (stderr) lines instead of silently skipping them
old_decode = "except json.JSONDecodeError:\n            continue"
new_decode = 'except json.JSONDecodeError:\n            log.debug("agent stderr: %s", raw[:200])\n            continue'
assert old_decode in bcontent, "JSONDecodeError continue not found!"
bcontent = bcontent.replace(old_decode, new_decode, 1)
print("Patch B OK: log non-JSON lines")

with open(BRIDGE, "w", encoding="utf-8") as f:
    f.write(bcontent)
print("Done:", BRIDGE)
