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
