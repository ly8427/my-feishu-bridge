#!/usr/bin/env python3
"""
agent_runner_pi.py — runs INSIDE the Docker container.

Drives one pi coding agent turn via `pi -p --mode json` subprocess and speaks
the same newline-delimited JSON protocol as agent_runner.py so the host bridge
can render progress to Feishu.

Protocol (stdout, agent -> host):
  {"type":"session","session_id":"..."}
  {"type":"text","text":"..."}
  {"type":"tool","name":"read","brief":"..."}
  {"type":"result","ok":true,"text":"...","session_id":"..."}
  {"type":"error","message":"..."}

Invocation:
    python3 agent_runner_pi.py --prompt "<text>" [--resume <session_id>]

Environment (injected by bridge.py via `docker exec -e`):
    PI_PROVIDERS               comma-separated provider names
    PI_<NAME>_BASE_URL         relay endpoint for provider <NAME>
    PI_<NAME>_API_KEY_ENV      env var name holding the API key (e.g. DEEPSEEK_API_KEY)
    PI_<NAME>_MODELS           comma-separated model IDs
    PI_<NAME>_API              API protocol (default: anthropic-messages)
    PI_ACTIVE_PROVIDER         active provider name with -relay suffix (e.g. deepseek-relay)
    PI_ACTIVE_MODEL            active model ID (normalized, no provider/ prefix)
    PI_USE_BEARER              set to "1" to use Authorization: Bearer instead of x-api-key
    <NAME>_API_KEY             actual API key value (referenced by models.json as $<NAME>_API_KEY)
    WORKSPACE_DIR              working directory
    SAFE_TOOLS                 comma-separated tools auto-allowed (read-only)
    CONFIRM_TIMEOUT            seconds to wait for a confirm_reply (default 300)
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time


def emit(obj: dict) -> None:
    """Write one JSON line to stdout, flushed."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ------------------------------------------------------------------ models.json
def _write_models_json() -> bool:
    """Generate ~/.pi/agent/models.json from PI_* env vars.

    Maps bridge providers to pi custom providers with correct relay base URLs.
    Uses $ENV_VAR references for API keys (no secrets on disk).
    Returns False if the active provider has no valid entry.

    Fixes: R1-1 (models.json bridging), P2 (PID-unique tmp),
           P3 (merge active model), P8 (tmp cleanup), P9 (authHeader).
    """
    providers = {}
    for name in os.environ.get("PI_PROVIDERS", "").split(","):
        name = name.strip()
        # Env var name safety: only allow alphanumeric + hyphens
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            continue
        P = f"PI_{name.upper()}"
        base_url = os.environ.get(f"{P}_BASE_URL", "")
        key_env = os.environ.get(f"{P}_API_KEY_ENV", "")
        api = os.environ.get(f"{P}_API", "anthropic-messages")
        models_str = os.environ.get(f"{P}_MODELS", "")
        models = [{"id": m.strip()} for m in models_str.split(",") if m.strip()]
        if base_url and models:
            prov_key = f"{name}-relay"
            providers[prov_key] = {
                "baseUrl": base_url,
                "api": api,
                "apiKey": f"${key_env}",  # $ENV reference, not a hardcoded secret
                "models": models,
            }
            # P9: Bearer fallback using pi's official authHeader field
            if os.environ.get("PI_USE_BEARER") == "1":
                providers[prov_key]["authHeader"] = True

    # P3: Ensure active model is in the provider's models list
    active = os.environ.get("PI_ACTIVE_PROVIDER", "")
    active_mdl = os.environ.get("PI_ACTIVE_MODEL", "")
    if active and active_mdl and active in providers:
        ids = [m["id"] for m in providers[active]["models"]]
        if active_mdl not in ids:
            providers[active]["models"].append({"id": active_mdl})

    # Check: active provider must have a valid entry
    if active and active not in providers:
        emit({"type": "error",
              "message": f"pi provider '{active}' not configured (models may be empty)"})
        return False

    # P2/P8: PID-unique tmp + atomic replace + cleanup
    path = os.path.expanduser("~/.pi/agent/models.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"providers": providers}, f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


# ------------------------------------------------------------------ pi command
def _build_pi_cmd(resume: str | None) -> list[str]:
    """Build the pi command line. Prompt is NOT in argv (goes via stdin, P5/P6)."""
    provider = os.environ.get("PI_ACTIVE_PROVIDER", "")
    model = os.environ.get("PI_ACTIVE_MODEL", "")
    session_dir = os.path.expanduser("~/.pi/agent/sessions")

    cmd = ["pi", "-p", "--mode", "json", "--approve",
           "--session-dir", session_dir]

    # Model with "/" → only pass --model, no --provider (avoid conflict)
    if model and "/" in model:
        cmd += ["--model", model]
    else:
        if provider:
            cmd += ["--provider", provider]
        if model and provider:
            cmd += ["--model", f"{provider}/{model}"]
        elif model:
            cmd += ["--model", model]

    # Model-identity system prompt (mirrors agent_runner.py): without this the
    # agent framework's framing makes the model self-identify as "pi" instead
    # of the real underlying model.
    if model:
        model_id = model.rsplit("/", 1)[-1]
        cmd += ["--append-system-prompt",
                f"You are {model_id}, the model configured for this session. "
                f"When asked about your identity or what model you are, state "
                f"that you are {model_id} — do not claim to be pi, Claude, or "
                f"any other product."]

    # Collab reviewer persona (APPENDED after the identity prompt so the JSON
    # verdict contract stays the LAST instruction — injection defense).
    _extra_sp = os.environ.get("PI_EXTRA_SYSTEM_PROMPT", "")
    if _extra_sp:
        cmd += ["--append-system-prompt", _extra_sp]

    # Thinking level (v2): /thinking <level> → PI_THINKING env → --thinking flag
    thinking = os.environ.get("PI_THINKING", "")
    if thinking:
        cmd += ["--thinking", thinking]

    # Tool policy (v2): safe = read-only allowlist; full = default (all tools).
    # pi -p mode has no per-call confirmation, so this is the coarse gate.
    if os.environ.get("PI_TOOLS_MODE", "full") == "safe":
        cmd += ["--tools", "read,grep,find,ls"]

    if resume:
        cmd += ["--session", resume]

    return cmd


# ------------------------------------------------------------------ list sessions
def _list_sessions() -> int:
    """List pi sessions from ~/.pi/agent/sessions/*.jsonl and print as JSON.

    File format: first line is the session header ({"type":"session","id":...,
    "timestamp":...,"cwd":...}); subsequent lines are tree entries (message /
    model_change / ...). We extract: session_id (header id == filename uuid),
    created_at (header timestamp), updated_at (last entry timestamp), title
    (first user message text), model (last model_change modelId).
    Output: JSON array sorted by updated_at desc, same shape as the claude
    runner's --list-sessions.
    """
    session_dir = os.path.expanduser("~/.pi/agent/sessions")
    result = []
    try:
        names = os.listdir(session_dir)
    except FileNotFoundError:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    for fname in names:
        if not fname.endswith(".jsonl"):
            continue
        header = None
        last_ts = ""
        title = ""
        model = ""
        try:
            with open(os.path.join(session_dir, fname), encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = entry.get("type")
                    if t == "session":
                        header = entry
                    elif t == "message":
                        if entry.get("role") == "user" and not title:
                            for block in entry.get("content", []):
                                if isinstance(block, dict) and block.get("type") == "text":
                                    title = (block.get("text") or "").strip()[:60]
                                    break
                        ts = entry.get("timestamp", "")
                        if ts:
                            last_ts = ts
                    elif t == "model_change":
                        model = entry.get("modelId", "") or model
        except OSError:
            continue  # unreadable file: skip, don't fail the listing
        if not header:
            continue
        created = (header.get("timestamp") or "")[:16].replace("T", " ")
        updated = (last_ts or header.get("timestamp") or "")[:16].replace("T", " ")
        result.append({
            "session_id": header.get("id", ""),
            "created_at": created,
            "updated_at": updated,
            "title": title,
            "model": model.rsplit("/", 1)[-1] if model else "",
        })
    result.sort(key=lambda x: x["updated_at"], reverse=True)
    print(json.dumps(result, ensure_ascii=False))
    return 0


# ------------------------------------------------------------------ event helpers
def _extract_assistant_text(messages: list) -> str:
    """Extract text from the last assistant message in agent_end.messages (R1-3)."""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            parts = []
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return "".join(parts)
    return ""


def _check_stop(messages: list) -> tuple[bool, str]:
    """Check stopReason of the last assistant message (R2-3).

    max_tokens is treated as OK (normal truncation, not an error).
    Returns (ok, error_message).
    """
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            reason = msg.get("stopReason", "")
            if reason == "error":
                err = msg.get("errorMessage") or msg.get("error", "unknown error")
                return False, f"pi error: {err}"
            return True, ""  # max_tokens, normal, etc. are all OK
    return True, ""


# ------------------------------------------------------------------ main run
# Timeouts: prevent a stuck pi process from permanently blocking the bridge's
# per-chat turn lock. Without these, a long-running tool call (e.g. ls -laR on
# a huge directory) or a network hang would block the chat forever.
_TURN_TIMEOUT = float(os.environ.get("PI_TURN_TIMEOUT", "600"))   # 10 min total
_IDLE_TIMEOUT = float(os.environ.get("PI_IDLE_TIMEOUT", "120"))   # 2 min no output


def run(prompt: str, resume: str | None) -> int:
    if not _write_models_json():
        return 1

    cmd = _build_pi_cmd(resume)
    cwd = os.environ.get("WORKSPACE_DIR", os.getcwd())

    env = dict(os.environ)
    env["PI_SKIP_VERSION_CHECK"] = "1"   # reduce network calls
    env["PI_TELEMETRY"] = "0"            # reduce non-JSON output

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,    # P5: PIPE, not inherited
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,   # R2-3: drained by thread below
        text=True,
        bufsize=1,
        cwd=cwd,                  # R1-2: cwd via Popen, not --cwd flag
        env=env,
        errors="replace",         # DS r5: non-UTF-8 won't crash
    )

    # R2-3: stderr drain thread (prevents pipe buffer deadlock)
    def _drain_stderr():
        for line in proc.stderr:
            sys.stderr.write(line)
    threading.Thread(target=_drain_stderr, daemon=True).start()

    # Timeout watchdog: kill pi if total time or idle time exceeds limits.
    # Runs in a separate thread because `for line in proc.stdout` blocks —
    # the idle check can't happen in the main loop when there's no output.
    _last_output = [time.time()]  # mutable container for thread communication
    _killed_by_timeout = [""]     # reason string if watchdog killed pi

    def _watchdog():
        start = time.time()
        while proc.poll() is None:
            time.sleep(5)
            now = time.time()
            if now - start > _TURN_TIMEOUT:
                _killed_by_timeout[0] = f"pi total time exceeded {_TURN_TIMEOUT:.0f}s"
                proc.kill()
                return
            if now - _last_output[0] > _IDLE_TIMEOUT:
                _killed_by_timeout[0] = f"pi no output for {_IDLE_TIMEOUT:.0f}s"
                proc.kill()
                return

    threading.Thread(target=_watchdog, daemon=True).start()

    _session_id = resume or ""
    _sent_result = False

    try:
        # P5/P6: Write prompt via stdin then close → EOF → pi's readPipedStdin completes
        # P7: Inside try block — BrokenPipeError if pi exits immediately
        try:
            proc.stdin.write(prompt + "\n")
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # pi already exited; P4 fallback will fire

        for line in proc.stdout:
            _last_output[0] = time.time()  # refresh idle timer on any output
            line = line.strip()
            if not line:
                continue
            # P4: Protect json.loads against non-JSON stdout lines
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                sys.stderr.write(f"[non-JSON stdout] {line[:200]}\n")
                continue

            t = event.get("type")

            if t == "session":
                # R2-2: Cache session_id from header event["id"]
                _session_id = event.get("id", _session_id)
                emit({"type": "session", "session_id": _session_id})

            elif t == "message_update":
                ev = event.get("assistantMessageEvent", {})
                # R1-3: Filter by event type, not contentIndex
                if ev.get("type") == "text_delta":
                    emit({"type": "text", "text": ev.get("delta", "")})

            elif t == "tool_execution_start":
                emit({"type": "tool",
                      "name": event.get("toolName", "?"),
                      "brief": json.dumps(event.get("args", {}), ensure_ascii=False)[:120]})

            elif t == "agent_end":
                messages = event.get("messages", [])
                text = _extract_assistant_text(messages)
                ok, err = _check_stop(messages)
                if ok:
                    emit({"type": "result", "ok": True,
                          "text": text, "session_id": _session_id})
                else:
                    emit({"type": "error", "message": err})
                _sent_result = True

    finally:
        proc.wait()
        # Watchdog killed pi — emit a clear timeout error
        if _killed_by_timeout[0]:
            emit({"type": "error", "message": _killed_by_timeout[0]})
            _sent_result = True
        # P4: Fallback — process ended without sending result/error
        if not _sent_result:
            emit({"type": "error",
                  "message": f"pi exited with code {proc.returncode}, no result produced"})

    return 0 if proc.returncode == 0 else 1


# ------------------------------------------------------------------ entry point
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default=None)  # not required with --list-sessions
    ap.add_argument("--resume", default=None)
    ap.add_argument("--list-sessions", action="store_true",
                    help="List pi sessions and exit (JSON output)")
    args = ap.parse_args()
    try:
        if args.list_sessions:
            sys.exit(_list_sessions())
        if not args.prompt:
            ap.error("--prompt is required (unless --list-sessions is used)")
        sys.exit(run(args.prompt, args.resume or None))
    except SystemExit:
        raise
    except Exception as exc:
        # P10: Top-level exception guard — any uncaught error emits a clean error line
        emit({"type": "error", "message": f"runner internal error: {exc}"})
        sys.exit(1)


if __name__ == "__main__":
    main()
