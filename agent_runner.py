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
        "SAFE_TOOLS", "Read,Grep,Glob,WebSearch,WebFetch,TodoWrite,NotebookRead"
    ).split(",")
    if t.strip()
}
CONFIRM_TIMEOUT = float(os.environ.get("CONFIRM_TIMEOUT", "300"))

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

    return f"{tool_name} {_truncate(json.dumps(tool_input, ensure_ascii=False), 600)}"


async def can_use_tool(tool_name: str, tool_input: dict, context) -> object:
    """
    Permission gate. Read-only tools in SAFE_TOOLS run automatically.
    Everything else asks the host (Feishu) and blocks until a reply or timeout.
    """
    if tool_name in SAFE_TOOLS:
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
        await emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1
    finally:
        stdin_task.cancel()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--resume", default=None)
    args = ap.parse_args()
    rc = asyncio.run(run(args.prompt, args.resume or None))
    sys.exit(rc)


if __name__ == "__main__":
    main()
