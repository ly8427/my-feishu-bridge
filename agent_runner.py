#!/usr/bin/env python3
"""
agent_runner.py — runs INSIDE the Docker container.

Drives one Claude Code turn via the Claude Agent SDK and speaks a tiny
newline-delimited JSON protocol over stdio so the host bridge can render
progress to Feishu and answer confirmation prompts.

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
    python3 agent_runner.py --prompt "<text>" [--resume <session_id>]

Environment:
    ANTHROPIC_API_KEY   required
    SAFE_TOOLS          comma-separated tools auto-allowed (read-only)
    CONFIRM_TIMEOUT     seconds to wait for a confirm_reply (default 300)
"""
import argparse
import asyncio
import json
import os
import sys
import uuid

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ResultMessage,
    PermissionResultAllow,
    PermissionResultDeny,
)

SAFE_TOOLS = {
    t.strip()
    for t in os.environ.get(
        # WebSearch/WebFetch intentionally NOT auto-allowed (S3): they are egress
        # channels that could exfiltrate a leaked secret without a tap.
        "SAFE_TOOLS", "Read,Grep,Glob,TodoWrite,NotebookRead"
    ).split(",")
    if t.strip()
}
CONFIRM_TIMEOUT = float(os.environ.get("CONFIRM_TIMEOUT", "300"))

# Working directory the agent is scoped to. Read-family tools are auto-approved
# ONLY for paths inside this tree; anything outside (/proc, /root, the mounted
# opencode auth.json, host secrets) falls through to an explicit Feishu confirm
# even though the tool is nominally "safe". This closes the zero-confirmation
# credential read (S1): SAFE_TOOLS ∋ Read is no longer a blank cheque to read
# /proc/self/environ or /root/.local/share/opencode/auth.json.
WORKSPACE_DIR = os.path.realpath(os.environ.get("WORKSPACE_DIR", os.getcwd()))

# Read-family tools that take a path and can reveal file content or layout.
_PATH_SCOPED_READERS = {"Read", "NotebookRead", "Grep", "Glob"}


def _path_arg(tool_name: str, tool_input: dict) -> str | None:
    """Extract the filesystem path a read-family tool will touch, if any.

    Grep/Glob without an explicit path default to the cwd (WORKSPACE_DIR), which
    is in-scope, so None → treated as safe.
    """
    if tool_name == "Read":
        return tool_input.get("file_path")
    if tool_name == "NotebookRead":
        return tool_input.get("notebook_path")
    if tool_name in ("Grep", "Glob"):
        return tool_input.get("path")
    return None


def _within_workspace(path: str | None) -> bool:
    """True if `path` resolves to somewhere inside WORKSPACE_DIR.

    realpath() resolves symlinks and '..', so a symlink inside the workspace
    that points at /root or /proc is correctly judged out-of-scope. A missing
    path (Grep/Glob defaulting to cwd) is in-scope.
    """
    if not path:
        return True
    target = path if os.path.isabs(path) else os.path.join(WORKSPACE_DIR, path)
    target = os.path.realpath(target)
    try:
        return os.path.commonpath([WORKSPACE_DIR, target]) == WORKSPACE_DIR
    except ValueError:
        # e.g. different drive on Windows — treat as out-of-scope.
        return False

_stdout_lock = asyncio.Lock()
# Pending confirmations: id -> Future[bool]
_pending: dict[str, asyncio.Future] = {}


async def emit(obj: dict) -> None:
    """Write one JSON line to stdout, flushed, serialized."""
    async with _stdout_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


async def _stdin_reader() -> None:
    """Read confirm_reply lines from the host and resolve waiting futures."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            msg = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "confirm_reply":
            fut = _pending.get(msg.get("id"))
            if fut and not fut.done():
                fut.set_result(bool(msg.get("allow")))


def _truncate(s: str, limit: int) -> str:
    """Trim to limit, marking how much was hidden so a tap is never blind."""
    s = s or ""
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n…(+{len(s) - limit} 字符已省略)"


def _brief_for(tool_name: str, tool_input: dict) -> str:
    """
    Human-readable description of what a tool call will ACTUALLY do.

    Security-critical: this text is what the user sees on the Feishu confirm
    card. It must reflect the real effect — never just a path — so an approval
    is an informed one, not a blind signature. Large content is bounded with an
    explicit "omitted" marker rather than silently cut.
    """
    if tool_name == "Bash":
        # Show the full command (bounded, but generous). The dangerous tail of a
        # long command must never be hidden behind a silent cut.
        cmd = tool_input.get("command", "")
        out = _truncate(cmd, 2000)
        desc = tool_input.get("description")
        return f"# {desc}\n{out}" if desc else out

    if tool_name in ("Write", "NotebookEdit"):
        path = tool_input.get("file_path", tool_input.get("notebook_path", "?"))
        content = tool_input.get("content", tool_input.get("new_source", ""))
        return f"{tool_name} → {path}\n--- 将写入内容 ---\n{_truncate(content, 1500)}"

    if tool_name == "Edit":
        path = tool_input.get("file_path", "?")
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        return (
            f"Edit → {path}\n--- 替换前 ---\n{_truncate(old, 800)}"
            f"\n--- 替换后 ---\n{_truncate(new, 800)}"
        )

    if tool_name == "MultiEdit":
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

    if tool_name in _PATH_SCOPED_READERS:
        p = _path_arg(tool_name, tool_input)
        flag = "" if _within_workspace(p) else "  ⚠️ 工作区外读取"
        shown = p or "(工作区)"
        if tool_name == "Grep":
            shown = f"{shown}  pattern: {_truncate(tool_input.get('pattern', ''), 200)}"
        return f"{tool_name} → {shown}{flag}"

    return f"{tool_name} {_truncate(json.dumps(tool_input, ensure_ascii=False), 600)}"


async def can_use_tool(tool_name: str, tool_input: dict, context) -> object:
    """
    Permission gate. Read-only tools in SAFE_TOOLS run automatically, EXCEPT
    read-family tools whose path escapes WORKSPACE_DIR — those need an explicit
    Feishu confirm so an agent can't silently read host secrets (/proc, /root,
    the mounted auth.json). Everything else asks the host and blocks until a
    reply or timeout.
    """
    if tool_name in SAFE_TOOLS:
        if tool_name in _PATH_SCOPED_READERS and not _within_workspace(
            _path_arg(tool_name, tool_input)
        ):
            pass  # out-of-workspace read → fall through to confirmation below
        else:
            return PermissionResultAllow()

    cid = "c_" + uuid.uuid4().hex[:8]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending[cid] = fut
    await emit(
        {
            "type": "confirm_request",
            "id": cid,
            "tool": tool_name,
            "detail": _brief_for(tool_name, tool_input),
        }
    )
    try:
        allow = await asyncio.wait_for(fut, timeout=CONFIRM_TIMEOUT)
    except asyncio.TimeoutError:
        return PermissionResultDeny(
            message=f"No confirmation within {int(CONFIRM_TIMEOUT)}s; denied for safety."
        )
    finally:
        _pending.pop(cid, None)

    if allow:
        return PermissionResultAllow()
    return PermissionResultDeny(message="Denied by user via Feishu.")


async def run(prompt: str, resume: str | None) -> int:
    options = ClaudeAgentOptions(
        cwd=os.environ.get("WORKSPACE_DIR", os.getcwd()),
        can_use_tool=can_use_tool,
        permission_mode="default",
        # S8: load ONLY user-level settings (the container's own ~/.claude,
        # which is fresh/empty). The SDK default is ["user","project"], which
        # would pull in <WORKSPACE_DIR>/.claude/settings*.json — and since
        # WORKSPACE_DIR is bind-mounted from the host, that file is the HOST's
        # Claude Code config (e.g. 81 permissions.allow entries, including
        # 'Bash(docker rm *)', 'Bash(rm -rf ...)', 'Bash(curl ...)'). Those
        # project/local rules could auto-allow matching tool calls and bypass
        # our can_use_tool gate. Excluding "project"/"local" makes this
        # deterministic regardless of workspace trust state.
        setting_sources=["user"],
        resume=resume,
        # Model selection. The Claude Agent SDK does NOT read ANTHROPIC_MODEL
        # from the environment — model is ignored unless passed here explicitly.
        # bridge.py injects these via `docker exec -e`; forward only the set ones.
        model=os.environ.get("ANTHROPIC_MODEL") or None,
        fallback_model=os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL") or None,
        effort=os.environ.get("CLAUDE_CODE_EFFORT_LEVEL") or None,
    )
    # Identity system prompt (set AFTER options, so we can use the resolved model).
    #
    # PROBLEM: When a non-Claude model (e.g. deepseek-v4-pro) is driven through
    # the Claude Agent SDK's stream-json agent mode (what ClaudeSDKClient uses),
    # the model self-identifies as "Claude Opus" even though the underlying model
    # is deepseek. Verified end-to-end: model=deepseek-v4-pro reaches the relay
    # endpoint correctly (deepseek is actually doing the reasoning), but the
    # model's *self-reported identity* is overridden to Claude.
    #
    # ROOT CAUSE (best current understanding — see investigation notes, may be
    # refined in a later session): it is NOT simply the literal string
    # "You are a Claude agent, built on Anthropic's Claude Agent SDK." Both the
    # `-p` (print) mode and the SDK agent mode inject that exact line into the
    # request's `system` field, yet `-p` mode answers "deepseek" while agent mode
    # answers "Claude". The differentiator is the surrounding *agent framework*:
    #   - agent mode (stream-json) loads 51 permission rules, 17 bundled skills,
    #     the full tool set, and the Claude Code operating protocol. This dense
    #     "you are a Claude Code agent" framing steers the model's self-identity.
    #   - `-p` mode sends a bare {model, messages, system} with no framework, so
    #     the single Claude line is too weak to override the model's real identity.
    # Mechanism captured via `claude --debug-file`: agent-mode startup applies
    # permission rules, skills, tools, and the agent protocol before the first
    # request, none of which appear in -p mode.
    #
    # WHY THIS FIX WORKS: appending an explicit, model-specific identity prompt
    # (naming the real model AND explicitly denying Claude) gives the model a
    # strong counter-signal that survives the agent framework's framing.
    # Verified: with this, deepseek-v4-pro answers "I am deepseek-v4-pro" in the
    # full SDK agent path. A vague "report your real identity" is NOT enough —
    # the framework wins; the statement must name the model and deny Claude.
    #
    # OPEN QUESTIONS (for a later deep-dive):
    #   1. Is identity truly decided by framework density, or by something more
    #      specific in the agent-mode system/messages (e.g. an injected assistant
    #      turn, or tool descriptions that frame Claude)? -p vs agent body diff
    #      was not fully captured (SDK mode's raw HTTP body eluded sniffing).
    #   2. Could a cleaner fix disable the agent framework's Claude framing at the
    #      source (CLI flag / setting) instead of overriding via system_prompt?
    #   3. Does this reassert itself under `--resume` (session history carries
    #      prior Claude self-reports)? Current mitigation: bridge clears session
    #      on engine/auth changes, but not on model-only changes.
    _model_name = os.environ.get("ANTHROPIC_MODEL") or ""
    if _model_name:
        options.system_prompt = (
            f"You are {_model_name}, the model configured for this session. "
            f"Answer the user's request directly and truthfully. When asked about "
            f"your identity or what model you are, state that you are {_model_name} "
            f"— do not claim to be Claude or any Anthropic product. Keep all file "
            f"operations within the current working directory."
        )

    stdin_task = asyncio.create_task(_stdin_reader())
    final_text_parts: list[str] = []
    session_id: str | None = resume
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            final_text_parts.append(block.text)
                            await emit({"type": "text", "text": block.text})
                        elif isinstance(block, ToolUseBlock):
                            await emit(
                                {
                                    "type": "tool",
                                    "name": block.name,
                                    "brief": _brief_for(block.name, block.input),
                                }
                            )
                elif isinstance(message, ResultMessage):
                    session_id = getattr(message, "session_id", session_id) or session_id
                    if session_id:
                        await emit({"type": "session", "session_id": session_id})
        await emit(
            {
                "type": "result",
                "ok": True,
                "text": "".join(final_text_parts),
                "session_id": session_id,
            }
        )
        return 0
    except Exception as exc:  # surface any failure to the host as a clean line
        # The SDK's ProcessError wraps a `claude` CLI exit and reports
        # "Check stderr output for details" — which the host never sees. Pull
        # every detail off the exception (stderr/stdout/returncode when present)
        # and its cause chain, so a failure is actually diagnosable instead of
        # a dead end. This is how we'll learn the real trigger of intermittent
        # exit-1s (upstream 5xx, auth, resume errors, …) without re-running.
        parts = [f"{type(exc).__name__}: {exc}"]
        for _attr in ("stderr", "stdout", "message", "process_error", "returncode", "command"):
            _v = getattr(exc, _attr, None)
            if _v:
                parts.append(f"{_attr}: {str(_v)[:1200]}")
        _cause = exc.__cause__ or exc.__context__
        if _cause is not None and _cause is not exc:
            parts.append(f"caused_by: {type(_cause).__name__}: {_cause}")
        await emit({"type": "error", "message": "\n".join(parts)})
        return 1
    finally:
        stdin_task.cancel()


def _list_sessions() -> int:
    """List all sessions for the workspace directory and print as JSON array.

    Uses the Claude Agent SDK's session storage to enumerate transcripts.
    Each entry: {session_id, created_at, updated_at, message_count}
    """
    cwd = os.environ.get("WORKSPACE_DIR", os.getcwd())
    try:
        from claude_agent_sdk import list_sessions
        sessions = list_sessions(directory=cwd)
        result = []
        # SDKSessionInfo fields: session_id, summary, last_modified (int epoch),
        # file_size, custom_title, first_prompt, git_branch, cwd, tag, created_at (int epoch)
        from datetime import datetime, timezone
        def _ts_to_str(ts):
            """Convert epoch timestamp to YYYY-MM-DD HH:MM string.
            SDK returns milliseconds; detect and divide if > 1e12."""
            if not ts:
                return ""
            if ts > 1e12:  # milliseconds
                ts = ts / 1000
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            except (ValueError, OSError):
                return ""
        for s in sessions or []:
            sid = getattr(s, "session_id", None)
            if not sid:
                continue
            created = getattr(s, "created_at", None)
            modified = getattr(s, "last_modified", None)
            created_str = _ts_to_str(created)
            modified_str = _ts_to_str(modified)
            # Use first_prompt as a title hint (truncated)
            title = (getattr(s, "first_prompt", None) or getattr(s, "summary", None) or "")[:60]
            entry = {
                "session_id": sid,
                "created_at": created_str,
                "updated_at": modified_str,
                "title": title,
                "cwd": getattr(s, "cwd", None),
            }
            result.append(entry)
        # Sort by last_modified descending (most recent first)
        result.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Emit a JSON error so the bridge can display it cleanly
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


def main() -> None:
    ap = argparse.ArgumentParser()
    # --prompt is required UNLESS --list-sessions is given
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--resume", default=None)
    ap.add_argument("--list-sessions", action="store_true",
                    help="List all sessions for the workspace and exit (JSON output)")
    args = ap.parse_args()

    if args.list_sessions:
        sys.exit(_list_sessions())

    if not args.prompt:
        ap.error("--prompt is required (unless --list-sessions is used)")

    rc = asyncio.run(run(args.prompt, args.resume or None))
    sys.exit(rc)


if __name__ == "__main__":
    main()
