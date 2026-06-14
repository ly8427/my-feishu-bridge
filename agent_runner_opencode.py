#!/usr/bin/env python3
"""
agent_runner_opencode.py — runs INSIDE the Docker container.

Drives one OpenCode turn via the opencode serve REST API + SSE events,
and speaks the same newline-delimited JSON protocol as agent_runner.py
so the host bridge can render progress to Feishu and answer confirmation prompts.

Protocol (one JSON object per line):

  stdout (agent -> host):
    {"type":"session","session_id":"..."}      once known
    {"type":"text","text":"..."}               assistant text chunk
    {"type":"tool","name":"Bash","brief":"..."} a tool is about to run (info)
    {"type":"confirm_request","id":"c1","tool":"Bash","detail":"rm -rf ..."}
    {"type":"result","ok":true,"text":"...","session_id":"..."}
    {"type":"error","message":"..."}

  stdin (host -> agent), only in response to confirm_request:
    {"type":"confirm_reply","id":"c1","allow":true}

Invocation:
    python3 agent_runner_opencode.py --prompt "<text>" [--resume <session_id>]

Environment:
    OPENCODE_BIN           path to opencode binary (default: opencode)
    OPENCODE_API_KEY       API key for the selected provider (required)
    OPENCODE_API_URL       provider endpoint URL (default: zhipu coding plan)
    OPENCODE_MODEL         provider/model format (default: zhipuai-coding-plan/glm-5.1)
    SAFE_TOOLS             comma-separated tools auto-allowed (read-only)
    CONFIRM_TIMEOUT        seconds to wait for a confirm_reply (default 300)
    WORKSPACE_DIR          working directory
"""

import argparse
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid


SAFE_TOOLS = {
    t.strip()
    for t in os.environ.get(
        "SAFE_TOOLS", "Read,Grep,Glob,WebSearch,WebFetch,TodoWrite,NotebookRead"
    ).split(",")
    if t.strip()
}
CONFIRM_TIMEOUT = float(os.environ.get("CONFIRM_TIMEOUT", "300"))
OPENCODE_BIN = os.environ.get("OPENCODE_BIN", "opencode")
OPENCODE_API_KEY = os.environ.get("OPENCODE_API_KEY") \
    or os.environ.get("ZHIPU_API_KEY")  # backward compat
OPENCODE_API_URL = os.environ.get(
    "OPENCODE_API_URL", "https://open.bigmodel.cn/api/coding/paas/v4"
)
OPENCODE_MODEL = os.environ.get(
    "OPENCODE_MODEL", "zhipuai-coding-plan/glm-5.1"
)  # provider/model format


_stdout_lock = threading.Lock()
_stop_event = threading.Event()
_server_proc = None
_server_port = 0
_session_id = ""
_confirm_waiters: dict[str, threading.Event] = {}
_confirm_results: dict[str, bool] = {}
_tool_inputs: dict[str, dict] = {}  # callID -> tool input (pending state)
_pending_perms: dict[str, tuple[str, str]] = {}  # confirm_id -> (session_id, permission_id)


def _free_port() -> int:
    """Find an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def emit(obj: dict) -> None:
    """Write one JSON line to stdout, flushed, serialized."""
    with _stdout_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _truncate(s: str, limit: int) -> str:
    """Trim to limit, marking how much was hidden so a tap is never blind."""
    s = s or ""
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n…(+{len(s) - limit} 字符已省略)"


def _normalize_tool_input(tool_input: dict) -> dict:
    """Convert camelCase keys from OpenCode SSE to snake_case expected by _brief_for."""
    CAMEL_TO_SNAKE = {
        "filePath": "file_path",
        "notebookPath": "notebook_path",
        "oldString": "old_string",
        "newString": "new_string",
    }
    result = {CAMEL_TO_SNAKE.get(k, k): v for k, v in tool_input.items()}
    if "edits" in result:
        result["edits"] = [
            {CAMEL_TO_SNAKE.get(ek, ek): ev for ek, ev in e.items()}
            for e in result["edits"]
        ]
    return result


def _brief_for(tool_name: str, tool_input: dict) -> str:
    """
    Human-readable description of what a tool call will ACTUALLY do.
    Security-critical: this text is what the user sees on the Feishu confirm card.
    """
    if tool_name == "bash":
        cmd = tool_input.get("command", "")
        out = _truncate(cmd, 2000)
        desc = tool_input.get("description")
        return f"# {desc}\n{out}" if desc else out

    if tool_name in ("write", "notebookedit"):
        path = tool_input.get("file_path", tool_input.get("notebook_path", "?"))
        content = tool_input.get("content", tool_input.get("new_source", ""))
        return f"Write → {path}\n--- 将写入内容 ---\n{_truncate(content, 1500)}"

    if tool_name == "edit":
        path = tool_input.get("file_path", "?")
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        return (
            f"Edit → {path}\n--- 替换前 ---\n{_truncate(old, 800)}"
            f"\n--- 替换后 ---\n{_truncate(new, 800)}"
        )

    if tool_name == "multiedit":
        path = tool_input.get("file_path", "?")
        edits = tool_input.get("edits", [])
        parts = [f"MultiEdit → {path}  ({len(edits)} 处修改)"]
        for i, e in enumerate(edits[:5], 1):
            parts.append(
                f"[{i}] 替换前: {_truncate(e.get('old_string',''), 200)}"
                f"\n    替换后: {_truncate(e.get('new_string',''), 200)}"
            )
        if len(edits) > 5:
            parts.append(f"…(还有 {len(edits) - 5} 处未显示)")
        return "\n".join(parts)

    return f"{tool_name} {_truncate(json.dumps(tool_input, ensure_ascii=False), 600)}"


def _api(method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
    """Make a REST API call to the local opencode server."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", _server_port, timeout=10)
        headers = {"Content-Type": "application/json"}
        body_bytes = json.dumps(body).encode("utf-8") if body else None
        conn.request(method, path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()
        if resp.status == 204:
            return resp.status, None
        return resp.status, json.loads(data) if data else None
    except Exception as exc:
        emit({"type": "error", "message": f"API call failed: {exc}"})
        return 0, None


def _reply_permission(session_id: str, permission_id: str, response: str) -> None:
    """Reply to an opencode permission request (once/always/reject)."""
    _api(
        "POST",
        f"/session/{session_id}/permissions/{permission_id}",
        {"response": response},
    )


def _stdin_reader() -> None:
    """Read confirm_reply lines from stdin via os.read with select."""
    import os as _os
    import select as _sel
    fd = sys.stdin.fileno()
    buf = b""
    while not _stop_event.is_set():
        try:
            ready, _, _ = _sel.select([fd], [], [], 1.0)
        except (ValueError, OSError):
            break
        if not ready:
            continue
        try:
            data = _os.read(fd, 4096)
            if not data:
                break
        except (BlockingIOError, OSError):
            continue
        buf += data
        while b"\n" in buf:
            line_bytes, buf = buf.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "confirm_reply":
                cid = msg.get("id")
                allow = bool(msg.get("allow"))
                _confirm_results[cid] = allow
                # Reply to opencode permission if this was an approval gate
                perm_info = _pending_perms.pop(cid, None)
                if perm_info:
                    psid, pid = perm_info
                    _reply_permission(psid, pid, "once" if allow else "reject")
                ev = _confirm_waiters.get(cid)
                if ev:
                    ev.set()


def _sse_reader(prompt: str, resume: str | None) -> None:
    """Subscribe to opencode SSE events and translate to bridge protocol."""
    global _session_id, _server_port

    # 1) Wait for server to be ready
    deadline = time.time() + 30
    while time.time() < deadline and not _stop_event.is_set():
        try:
            conn = http.client.HTTPConnection("127.0.0.1", _server_port, timeout=2)
            conn.request("GET", "/session")
            conn.getresponse()
            conn.close()
            break
        except Exception:
            time.sleep(0.5)
    else:
        emit({"type": "error", "message": "OpenCode server did not become ready"})
        return

    # 2) Create or resume session
    if resume:
        _session_id = resume
        emit({"type": "session", "session_id": _session_id})
    else:
        status, data = _api("POST", "/session", {"agent": "build", "title": "feishu-bridge"})
        if status != 200 or not data:
            emit({"type": "error", "message": "Failed to create session"})
            return
        _session_id = data["id"]
        emit({"type": "session", "session_id": _session_id})

    # 3) Define event handler (must be before SSE connection)
    final_text_parts: list[str] = []
    last_busy = False
    seen_error = False
    cancelled = False
    _user_msg_ids: set[str] = set()
    _reasoning_part_ids: set[str] = set()
    _tool_names_by_call: dict[str, str] = {}

    def _on_event(evt: dict) -> bool:
        """Process one event. Returns True to stop reading (session finished)."""
        nonlocal last_busy, seen_error, cancelled
        etype = evt.get("type", "")
        props = evt.get("properties", {})

        if etype == "message.updated":
            info = props.get("info", {})
            if info.get("role") == "user":
                _user_msg_ids.add(info["id"])
            return False

        if etype == "message.part.delta":
            part_id = props.get("partID", "")
            field = props.get("field", "")
            delta = props.get("delta", "")
            if field == "text" and delta and part_id not in _reasoning_part_ids:
                final_text_parts.append(delta)
                emit({"type": "text", "text": delta})
            return False

        if etype == "message.part.updated":
            part = props.get("part", {})

            ptype = part.get("type", "")

            if ptype == "text":
                txt = part.get("text", "")
                msg_id = part.get("messageID", "")
                if not txt or msg_id in _user_msg_ids or part.get("synthetic"):
                    return False
                return False

            elif ptype == "reasoning":
                _reasoning_part_ids.add(part.get("id", ""))
                return False

            elif ptype == "tool":
                state = part.get("state", {})
                status_val = state.get("status", "")
                tool_name = part.get("tool", "")
                call_id = part.get("callID", "")

                if status_val == "pending":
                    _tool_names_by_call[call_id] = tool_name
                    return False

                if status_val == "running":
                    tool_input = _normalize_tool_input(state.get("input", {}))
                    _tool_inputs[call_id] = tool_input
                    _tool_names_by_call[call_id] = tool_name
                    emit({
                        "type": "tool",
                        "name": tool_name.capitalize(),
                        "brief": _brief_for(tool_name, tool_input)[:120],
                    })
                    return False

                if status_val == "completed":
                    return False

                if status_val == "error":
                    err_msg = state.get("error", "")
                    emit({
                        "type": "error",
                        "message": f"{tool_name.capitalize()}: {err_msg}",
                    })
                    return False

                return False

            elif ptype in ("step-start", "step-finish", "reasoning", "snapshot", "patch"):
                return False

            return False

        elif etype == "permission.asked":
            perm_id = props.get("id", "")
            perm_type = props.get("permission", props.get("type")) or ""
            perm_session = props.get("sessionID", _session_id)
            perm_title = props.get("title", "")
            metadata = props.get("metadata", {}) or {}
            call_id = props.get("callID")
            if not call_id:
                tool_info = props.get("tool", {}) or {}
                call_id = tool_info.get("callID", "")

            # Use actual tool name if available, otherwise permission type
            actual_tool = _tool_names_by_call.get(call_id, perm_type)
            tool_name = actual_tool.capitalize() if actual_tool else (perm_type.capitalize() or "Tool")

            # Auto-allow read-only tools
            if perm_type and perm_type.lower() in SAFE_TOOLS:
                _reply_permission(perm_session, perm_id, "once")
                return False

            # Build detail: prefer tool input, fallback to permission metadata
            tool_input = _tool_inputs.get(call_id, {})
            if tool_input:
                detail = _brief_for(actual_tool, tool_input)
            elif isinstance(metadata, dict):
                parts = [perm_title] if perm_title else [f"# {perm_type}" if perm_type else "# Permission"]
                if metadata.get("command"):
                    parts.append(f"命令: {metadata['command']}")
                if metadata.get("filepath"):
                    parts.append(f"文件: {metadata['filepath']}")
                if metadata.get("description"):
                    parts.append(f"{metadata['description']}")
                detail = "\n".join(parts)
            else:
                detail = perm_title or perm_type or "unknown"

            cid = "c_" + uuid.uuid4().hex[:8]
            event = threading.Event()
            _confirm_waiters[cid] = event
            # Store permission info so stdin_reader can reply directly
            _pending_perms[cid] = (perm_session, perm_id)
            emit({
                "type": "confirm_request",
                "id": cid,
                "tool": tool_name,
                "detail": detail,
            })

            # Non-blocking: stdin_reader thread handles the reply (calls
            # _reply_permission).  SSE reader stays alive so streaming
            # continues even while the user ponders the confirm card.
            #
            # Timeout guard: reject automatically after CONFIRM_TIMEOUT secs
            # unless the user already replied.
            def _auto_reject():
                if cid in _pending_perms:
                    psid, pid = _pending_perms.pop(cid)
                    _reply_permission(psid, pid, "reject")
            _timer = threading.Timer(CONFIRM_TIMEOUT, _auto_reject)
            _timer.daemon = True
            _timer.start()
            return False

        elif etype == "session.status":
            status_obj = props.get("status", {})
            stype = status_obj.get("type", "")

            if stype == "busy":
                last_busy = True
            elif stype == "idle" and last_busy:
                emit({
                    "type": "result",
                    "ok": True,
                    "text": "".join(final_text_parts).strip() or "OK",
                    "session_id": _session_id,
                })
                return True
            elif stype == "error":
                seen_error = True
                emit({
                    "type": "result",
                    "ok": False,
                    "text": status_obj.get("message", "Session error"),
                    "session_id": _session_id,
                })
                return True
            return False

        elif etype == "session.error":
            error_obj = props.get("error", {})
            err_data = error_obj.get("data", {}) if isinstance(error_obj, dict) else {}
            msg = err_data.get("message", str(error_obj))
            emit({"type": "error", "message": msg})
            return False

        return False

    # 4) Open SSE connection BEFORE sending prompt (so permission.asked
    # events are not missed)
    try:
        ws_dir = os.environ.get("WORKSPACE_DIR", os.getcwd())
        conn = http.client.HTTPConnection("127.0.0.1", _server_port, timeout=600)
        conn.request("GET", "/event",
                     headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()

        # 5) Now send the prompt — SSE is already listening
        _api("POST", f"/session/{_session_id}/prompt_async", {
            "agent": "build",
            "parts": [{"type": "text", "text": prompt}],
        })

        # 6) Read SSE events
        line_buf = b""
        while not _stop_event.is_set():
            chunk = resp.read(4096)
            if not chunk:
                time.sleep(0.05)
                continue
            line_buf += chunk
            while b"\n\n" in line_buf:
                raw, line_buf = line_buf.split(b"\n\n", 1)
                raw_text = raw.decode("utf-8", errors="replace").strip()
                for line in raw_text.split("\n"):
                    line = line.strip()
                    if line.startswith("data: "):
                        data_str = line[6:]
                        try:
                            evt = json.loads(data_str)
                            if _on_event(evt):
                                return
                        except json.JSONDecodeError:
                            pass

        conn.close()
    except Exception as exc:
        if not _stop_event.is_set():
            emit({"type": "error", "message": f"SSE connection error: {exc}"})

    # Final result if no explicit idle seen
    if not cancelled and not seen_error:
        emit({
            "type": "result",
            "ok": True,
            "text": "".join(final_text_parts).strip() or "✅ 完成",
            "session_id": _session_id,
        })


def _write_opencode_config() -> None:
    """Generate opencode.json from environment variables."""
    import os.path as _osp

    if not OPENCODE_API_KEY:
        emit({
            "type": "error",
            "message": "OPENCODE_API_KEY not set — cannot start opencode serve"
        })
        return

    provider = OPENCODE_MODEL.split("/")[0]
    model = OPENCODE_MODEL.split("/", 1)[-1]

    # provider name to env var mapping (for built-in providers)
    _env_map = {
        "zhipuai-coding-plan": "ZHIPU_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
    }

    config: dict = {
        "model": OPENCODE_MODEL,
        "agent": {
            "build": {
                "permission": {
                    "edit": "ask",
                    "bash": {"*": "ask"},
                    "webfetch": "ask",
                    "external_directory": {"*": "ask"},
                }
            }
        },
    }

    # For built-in providers, just set the env var — the provider is auto-detected.
    # Adding explicit provider config can conflict with built-in definitions.
    if provider in _env_map:
        os.environ.setdefault(_env_map[provider], OPENCODE_API_KEY)

    config_dir = _osp.join(_osp.expanduser("~"), ".config", "opencode")
    os.makedirs(config_dir, exist_ok=True)
    config_path = _osp.join(config_dir, "opencode.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def run(prompt: str, resume: str | None) -> int:
    global _server_proc, _server_port

    _write_opencode_config()

    _server_port = _free_port()
    workspace = os.environ.get("WORKSPACE_DIR", os.getcwd())

    import tempfile
    _log_file = open(tempfile.mktemp(suffix=".log", prefix="opencode_serve_"), "w")
    _server_proc = subprocess.Popen(
        [
            OPENCODE_BIN, "serve",
            "--port", str(_server_port),
            "--hostname", "127.0.0.1",
        ],
        cwd=workspace,
        stdout=_log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )

    def _cleanup():
        _stop_event.set()
        if _server_proc:
            _server_proc.send_signal(signal.SIGTERM)
            try:
                _server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _server_proc.kill()

    stdin_thread = threading.Thread(target=_stdin_reader, daemon=True)
    stdin_thread.start()

    try:
        _sse_reader(prompt, resume)
        return 0
    except Exception as exc:
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1
    finally:
        _cleanup()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--resume", default=None)
    args = ap.parse_args()
    rc = run(args.prompt, args.resume or None)
    sys.exit(rc)


if __name__ == "__main__":
    main()
