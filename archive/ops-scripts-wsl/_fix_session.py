#!/usr/bin/env python3
"""Fix cross-engine session pollution: namespace session_store keys by engine.

Problem: OpenCode writes session_id 'ses_xxx' into sessions.json; Claude engine
reads it back and passes to `claude --resume`, which rejects non-UUID and exits 1.

Fix: use '{engine}:{chat_id}' as the session_store key so the two engines never
share session ids.
"""
import sys

PATH = "/home/liu/projects/claudeWorkSpace/feishu-bridge/bridge.py"
src = open(PATH, encoding="utf-8").read()

def replace_once(src, old, new, label):
    n = src.count(old)
    if n != 1:
        sys.exit(f"FAIL [{label}]: expected 1 match, found {n}\n--- old ---\n{old[:300]}")
    return src.replace(old, new, 1)


# 1. Add `engine` field to Job dataclass
src = replace_once(
    src,
    "    chat_id: str\n"
    "    proc: subprocess.Popen\n"
    "    out_q: queue.Queue  # raw stdout lines -> _render_loop (decouples read from HTTP)\n",
    "    chat_id: str\n"
    "    proc: subprocess.Popen\n"
    "    engine: str  # \"claude\" | \"opencode\" — namespaces session_store keys per engine\n"
    "    out_q: queue.Queue  # raw stdout lines -> _render_loop (decouples read from HTTP)\n",
    "Job.engine field",
)

# 2. _render_loop: session_store.put uses namespaced key
src = replace_once(
    src,
    "        elif mtype == \"session\":\n"
    "            session_store.put(job.chat_id, msg[\"session_id\"])\n",
    "        elif mtype == \"session\":\n"
    "            # Namespace by engine so OpenCode's 'ses_xxx' ids never reach\n"
    "            # claude's --resume (which requires a UUID).\n"
    "            session_store.put(f\"{job.engine}:{job.chat_id}\", msg[\"session_id\"])\n",
    "render_loop put",
)

# 3. _start_turn: read with namespaced key
src = replace_once(
    src,
    "    resume = session_store.get(chat_id)\n",
    "    resume = session_store.get(f\"{engine}:{chat_id}\")\n",
    "start_turn get",
)

# 4. Job instantiation: pass engine
src = replace_once(
    src,
    "    job = Job(chat_id=chat_id, proc=proc, out_q=queue.Queue())\n",
    "    job = Job(chat_id=chat_id, proc=proc, engine=engine, out_q=queue.Queue())\n",
    "Job instantiation",
)

# 5. /new and /reset: clear BOTH engines' sessions for this chat
src = replace_once(
    src,
    "    if text in (\"/new\", \"/reset\"):\n"
    "        session_store.clear(chat_id)\n",
    "    if text in (\"/new\", \"/reset\"):\n"
    "        # Clear sessions for both engines (keys are namespaced).\n"
    "        session_store.clear(f\"claude:{chat_id}\")\n"
    "        session_store.clear(f\"opencode:{chat_id}\")\n",
    "/new clear",
)

# 6. /engine switch: clear BOTH engines' sessions for this chat
src = replace_once(
    src,
    "            session_store.clear(chat_id)  # sessions are engine-specific\n",
    "            session_store.clear(f\"claude:{chat_id}\")  # sessions are engine-specific\n"
    "            session_store.clear(f\"opencode:{chat_id}\")\n",
    "/engine clear",
)

open(PATH, "w", encoding="utf-8").write(src)
print("OK: session key namespacing applied (6 changes)")
