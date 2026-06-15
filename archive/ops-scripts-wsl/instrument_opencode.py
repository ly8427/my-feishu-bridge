#!/usr/bin/env python3
"""
Add timestamped phase logging to agent_runner_opencode.py.
Phase markers are emitted as valid JSON via emit() — safe for bridge.py.
Also patches bridge.py to log unknown message types so phases are visible.
"""
import sys

RUNNER = "/home/liu/projects/claudeWorkSpace/feishu-bridge/agent_runner_opencode.py"
BRIDGE = "/home/liu/projects/claudeWorkSpace/feishu-bridge/bridge.py"

# ==================== PART 1: agent_runner_opencode.py ====================

with open(RUNNER, "r", encoding="utf-8") as f:
    content = f.read()

# --- Patch 1: add _t0 global + phase() function right after emit() ---
emit_block = '''def emit(obj: dict) -> None:
    """Write one JSON line to stdout, flushed, serialized."""
    with _stdout_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
        sys.stdout.flush()'''

phase_code = '''


_t0 = 0.0  # process start time, set in run()


def phase(name: str, **extra) -> None:
    """Emit a timestamped phase marker as valid JSON."""
    elapsed = round(time.time() - _t0, 3) if _t0 else 0.0
    p = {"type": "_phase", "name": name, "elapsed": elapsed}
    if extra:
        p["extra"] = extra
    emit(p)'''

assert emit_block in content, "emit block not found!"
content = content.replace(emit_block, emit_block + phase_code, 1)
print("P1 OK: _t0 + phase()")

# --- Patch 2: add _t0 = time.time() at top of run() ---
old_run = "def run(prompt: str, resume: str | None) -> int:\n    global _server_proc, _server_port"
new_run = "def run(prompt: str, resume: str | None) -> int:\n    global _t0, _server_proc, _server_port\n    _t0 = time.time()"
assert old_run in content, "run() def not found!"
content = content.replace(old_run, new_run, 1)
print("P2 OK: _t0 init in run()")

# --- Patch 3: phase markers at key points ---

# 3a: start of _sse_reader
old = "def _sse_reader(prompt: str, resume: str | None) -> None:\n    \"\"\"Subscribe to opencode SSE events and translate to bridge protocol.\"\"\"\n    global _session_id, _server_port"
new = "def _sse_reader(prompt: str, resume: str | None) -> None:\n    \"\"\"Subscribe to opencode SSE events and translate to bridge protocol.\"\"\"\n    global _session_id, _server_port\n    phase(\"sse_reader_enter\", prompt_len=len(prompt))"
assert old in content, "sse_reader def not found!"
content = content.replace(old, new, 1)
print("P3a OK")

# 3b: server ready (after wait loop)
old = "    # 2) Create or resume session"
new = "    phase(\"server_ready\")\n    # 2) Create or resume session"
assert old in content, "server ready comment not found!"
content = content.replace(old, new, 1)
print("P3b OK")

# 3c: session created
old = "        _session_id = data[\"id\"]\n        emit({\"type\": \"session\", \"session_id\": _session_id})"
new = "        _session_id = data[\"id\"]\n        emit({\"type\": \"session\", \"session_id\": _session_id})\n        phase(\"session_created\")"
assert old in content, "session created not found!"
content = content.replace(old, new, 1)
print("P3c OK")

# 3d: SSE connected (right after conn.getresponse())
old = "        conn.request(\"GET\", \"/event\",\n                     headers={\"Accept\": \"text/event-stream\"})\n        resp = conn.getresponse()"
new = old + "\n        phase(\"sse_connected\")"
assert old in content, "SSE GET /event not found!"
content = content.replace(old, new, 1)
print("P3d OK")

# 3e: prompt sent
old = "        # 5) Now send the prompt — SSE is already listening\n        _api(\"POST\", f\"/session/{_session_id}/prompt_async\", {\n            \"agent\": \"build\",\n            \"parts\": [{\"type\": \"text\", \"text\": prompt}],\n        })"
new = "        # 5) Now send the prompt — SSE is already listening\n        _api(\"POST\", f\"/session/{_session_id}/prompt_async\", {\n            \"agent\": \"build\",\n            \"parts\": [{\"type\": \"text\", \"text\": prompt}],\n        })\n        phase(\"prompt_sent\")"
assert old in content, "prompt_async not found!"
content = content.replace(old, new, 1)
print("P3e OK")

# Write back
with open(RUNNER, "w", encoding="utf-8") as f:
    f.write(content)
print("Part 1 done:", RUNNER)


# ==================== PART 2: bridge.py ====================

with open(BRIDGE, "r", encoding="utf-8") as f:
    bcontent = f.read()

# Add debug logging for unknown message types in _render_loop
# Find the elif chain ending and add else clause
old_chain = "        elif mtype == \"error\":"
end_of_chain = old_chain + "\n            job.render(text=f\"❌ 出错: {msg.get('message','')[:200]}\")"
new_chain = old_chain + "\n            job.render(text=f\"❌ 出错: {msg.get('message','')[:200]}\")\n        else:\n            log.info(\"agent diagnostic: %s\", str(msg)[:300])"
assert end_of_chain in bcontent, "error elif not found!"
bcontent = bcontent.replace(end_of_chain, new_chain, 1)
print("Bridge patch OK: log unknown msg types")

# Also add debug logging for non-JSON lines (skipped by json.JSONDecodeError)
old_decode = "        except json.JSONDecodeError:\n            continue"
new_decode = "        except json.JSONDecodeError:\n            log.debug(\"agent stderr: %s\", raw[:200])\n            continue"
assert old_decode in bcontent, "JSONDecodeError continue not found!"
bcontent = bcontent.replace(old_decode, new_decode, 1)
print("Bridge patch OK: log non-JSON lines")

with open(BRIDGE, "w", encoding="utf-8") as f:
    f.write(bcontent)
print("Part 2 done:", BRIDGE)
print("\nAll instrumentation applied. Restart bridge to take effect.")
