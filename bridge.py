#!/usr/bin/env python3
"""
bridge.py — runs on the HOST. No public IP needed (Feishu WebSocket long connection).

Flow:
  Feishu DM --> ws event --> whitelist check --> docker exec agent_runner.py
  agent stdout JSON lines --> update a Feishu card (streaming)
  confirm_request --> send a card with 允许/拒绝 buttons
  button tap --> card action callback --> write confirm_reply to agent stdin

Security posture (the "必做 + Docker + 危险操作确认" tier):
  - Only ALLOWED_USER_ID may drive the bot; all others ignored + logged.
  - Secrets read from .env via FEISHU_ENV_FILE (keep outside mount to avoid leaking).
  - The agent runs inside a container mounting ONLY WORKSPACE_DIR.
  - Mutating tools require an explicit Feishu tap; read-only tools auto-run.
  - docker exec only receives necessary env vars, not the full host environment.
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    P2ImMessageReceiveV1,
)

# ---------------------------------------------------------------- config
def _load_env() -> None:
    path = os.environ.get(
        "FEISHU_ENV_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    )
    if not os.path.exists(path):
        sys.exit(
            "Missing .env — set FEISHU_ENV_FILE or copy .env.example to .env"
        )
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()

APP_ID = os.environ["FEISHU_APP_ID"]
APP_SECRET = os.environ["FEISHU_APP_SECRET"]
ALLOWED_USER_ID = os.environ["ALLOWED_USER_ID"]
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "feishu-claude-agent")
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/home/user/projects")
CONFIRM_TIMEOUT = os.environ.get("CONFIRM_TIMEOUT", "300")
SAFE_TOOLS = os.environ.get("SAFE_TOOLS", "Read,Grep,Glob,WebSearch,WebFetch,TodoWrite,NotebookRead")
DEFAULT_ENGINE = os.environ.get("ENGINE", "claude")

# per-chat engine preference (chat_id -> "claude" | "opencode")
_chat_engine: dict[str, str] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(os.path.dirname(__file__), "bridge.log"))],
)
log = logging.getLogger("bridge")

import session_store  # local module

client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()

# confirm_id -> callable(allow: bool) that pushes the reply into the agent stdin
_confirm_waiters: dict[str, "Job"] = {}
_confirm_lock = threading.Lock()


# ---------------------------------------------------------------- Feishu helpers
def _post_card(chat_id: str, text: str, buttons: list | None = None) -> str | None:
    """Create a new interactive card; returns message_id for later patching."""
    card = _build_card(text, buttons)
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.create(req)
    if not resp.success():
        log.error("post_card failed: %s %s", resp.code, resp.msg)
        return None
    return resp.data.message_id


def _patch_card(message_id: str, text: str, buttons: list | None = None) -> None:
    """Update an existing card in place (streaming UX)."""
    card = _build_card(text, buttons)
    req = (
        PatchMessageRequest.builder()
        .message_id(message_id)
        .request_body(PatchMessageRequestBody.builder().content(json.dumps(card, ensure_ascii=False)).build())
        .build()
    )
    resp = client.im.v1.message.patch(req)
    if not resp.success():
        log.error("patch_card failed: %s %s", resp.code, resp.msg)


def _build_card(text: str, buttons: list | None) -> dict:
    # Feishu cap: keep text bounded so patch never rejects an oversized payload.
    body = text if len(text) <= 9000 else (text[:9000] + "\n…(truncated)")
    elements = [{"tag": "markdown", "content": body or " "}]
    if buttons:
        elements.append({"tag": "action", "actions": buttons})
    return {"config": {"wide_screen_mode": True}, "elements": elements}


def _confirm_buttons(confirm_id: str) -> list:
    return [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "✅ 允许"},
            "type": "primary",
            "value": {"action": "confirm", "id": confirm_id, "allow": True},
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🚫 拒绝"},
            "type": "danger",
            "value": {"action": "confirm", "id": confirm_id, "allow": False},
        },
    ]


# ---------------------------------------------------------------- job (one turn)
@dataclass
class Job:
    chat_id: str
    proc: subprocess.Popen
    card_msg_id: str | None = None
    transcript: str = ""
    confirm_card_id: str | None = None  # message_id of the pending confirm card
    lock: threading.Lock = field(default_factory=threading.Lock)

    def render(self, extra: str = "") -> None:
        with self.lock:
            shown = (self.transcript + extra).strip() or "🤔 思考中…"
            if self.card_msg_id:
                _patch_card(self.card_msg_id, shown)

    def answer_confirm(self, confirm_id: str, allow: bool) -> None:
        try:
            self.proc.stdin.write(json.dumps({"type": "confirm_reply", "id": confirm_id, "allow": allow}) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            log.warning("confirm reply could not be delivered (process gone)")


def _drain_job(job: Job) -> None:
    """Read newline-JSON from the agent and reflect it onto Feishu cards."""
    for raw in job.proc.stdout:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        mtype = msg.get("type")

        if mtype == "text":
            job.transcript += msg["text"]
            job.render()
        elif mtype == "tool":
            extra = f"\n\n> 🔧 {msg['name']}: `{msg.get('brief','')[:120]}`"
            job.transcript += extra
            job.render()
        elif mtype == "session":
            session_store.put(job.chat_id, msg["session_id"])
        elif mtype == "confirm_request":
            cid = msg["id"]
            with _confirm_lock:
                _confirm_waiters[cid] = job
            detail = msg.get("detail", "")
            # Show the full prepared detail (agent already bounds it with explicit
            # "omitted" markers). Keep under Feishu's card limit; _build_card also
            # guards the total. This is what the user signs off on — don't re-trim.
            if len(detail) > 7000:
                detail = detail[:7000] + "\n…(detail 过长已截断)"
            card_text = (
                f"⚠️ **需要确认危险操作**\n\n"
                f"工具: `{msg.get('tool')}`\n\n"
                f"```\n{detail}\n```\n\n"
                f"在 {CONFIRM_TIMEOUT}s 内选择(超时自动拒绝):"
            )
            job.confirm_card_id = _post_card(job.chat_id, card_text, _confirm_buttons(cid))
        elif mtype == "result":
            final = msg.get("text", "").strip()
            if final and final not in job.transcript:
                job.transcript += "\n" + final
            job.render()
        elif mtype == "error":
            job.render(f"\n\n❌ 出错: {msg.get('message')}")

    job.proc.wait()


def _start_turn(chat_id: str, prompt: str, engine: str | None = None) -> None:
    engine = engine or _chat_engine.get(chat_id, DEFAULT_ENGINE)
    resume = session_store.get(chat_id)
    # OpenCode sessions are ephemeral — each runner starts a fresh server,
    # so cross-runner resume is meaningless.
    if engine == "opencode":
        resume = None
    # Forward whichever Claude auth vars are set on the host. This machine uses
    # a relay (ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN); an official key works too.
    cmd = ["docker", "exec", "-i"]
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        if os.environ.get(var):
            cmd += ["-e", f"{var}={os.environ[var]}"]
    if engine == "opencode":
        for var in ("OPENCODE_API_KEY", "OPENCODE_API_URL", "OPENCODE_MODEL", "ZHIPU_API_KEY"):
            if os.environ.get(var):
                cmd += ["-e", f"{var}={os.environ[var]}"]
        # Also set ZHIPU_API_KEY from OPENCODE_API_KEY so built-in provider auto-detects
        if os.environ.get("OPENCODE_API_KEY") and not os.environ.get("ZHIPU_API_KEY"):
            cmd += ["-e", f"ZHIPU_API_KEY={os.environ['OPENCODE_API_KEY']}"]
        if os.environ.get("OPENCODE_BIN"):
            cmd += ["-e", f"OPENCODE_BIN={os.environ['OPENCODE_BIN']}"]
    cmd += [
        "-e", f"SAFE_TOOLS={SAFE_TOOLS}",
        "-e", f"CONFIRM_TIMEOUT={CONFIRM_TIMEOUT}",
        "-e", f"WORKSPACE_DIR={WORKSPACE_DIR}",
        CONTAINER_NAME,
    ]
    if engine == "opencode":
        cmd += [
            "python3", "/app/agent_runner_opencode.py", "--prompt", prompt,
        ]
    else:
        cmd += [
            "python3", "/app/agent_runner.py", "--prompt", prompt,
        ]
    if resume:
        cmd += ["--resume", resume]

    _passthrough_vars = (
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
        "PATH", "HOME", "DOCKER_HOST", "TERM",
    )
    if engine == "opencode":
        _passthrough_vars = _passthrough_vars + (
            "OPENCODE_API_KEY", "OPENCODE_API_URL", "OPENCODE_MODEL", "ZHIPU_API_KEY",
        )
    _clean_env = {
        k: os.environ[k]
        for k in _passthrough_vars
        if k in os.environ
    }
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=_clean_env,
    )
    job = Job(chat_id=chat_id, proc=proc)
    job.card_msg_id = _post_card(chat_id, "🤔 已收到,正在处理…")
    threading.Thread(target=_drain_job, args=(job,), daemon=True).start()


# ---------------------------------------------------------------- event handlers
def on_message(data: P2ImMessageReceiveV1) -> None:
    ev = data.event
    sender_id = ev.sender.sender_id.open_id
    chat_id = ev.message.chat_id

    if sender_id != ALLOWED_USER_ID:
        log.warning("IGNORED message from non-whitelisted sender: %s", sender_id)
        return

    if ev.message.message_type != "text":
        _post_card(chat_id, "目前只支持文本指令。")
        return

    try:
        text = json.loads(ev.message.content).get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return
    if not text:
        return

    # simple slash commands
    if text in ("/new", "/reset"):
        session_store.clear(chat_id)
        _post_card(chat_id, "🧹 已清除会话上下文,下条消息将开启新会话。")
        return
    if text.startswith("/engine "):
        eng = text.split(" ", 1)[1].strip().lower()
        if eng in ("claude", "opencode"):
            _chat_engine[chat_id] = eng
            session_store.clear(chat_id)  # sessions are engine-specific
            _post_card(chat_id, f"⚙️ 引擎已切换: {eng}")
        else:
            _post_card(chat_id, f"未知引擎: {eng}。可用: claude / opencode")
        return

    log.info("turn from %s in %s: %s", sender_id, chat_id, text[:80])
    _start_turn(chat_id, text)


def on_card_action(data) -> dict | None:
    """Confirmation button tapped. data.event.action.value carries our payload.

    Returns a toast in the callback response for INSTANT on-tap feedback (no
    round-trip lag), then also patches the card so the verdict persists.
    """
    try:
        value = data.event.action.value  # dict
        operator = data.event.operator.open_id
    except AttributeError:
        log.warning("card action: malformed payload, ignoring")
        return None

    log.info("card action received: operator=%s value=%s", operator, value)

    if operator != ALLOWED_USER_ID:
        log.warning("IGNORED card action from non-whitelisted: %s", operator)
        return {"toast": {"type": "error", "content": "无权限"}}
    if value.get("action") != "confirm":
        return None

    cid = value.get("id")
    allow = bool(value.get("allow"))
    with _confirm_lock:
        job = _confirm_waiters.pop(cid, None)

    if job is None:
        # Already handled, expired, or timed out before the tap landed.
        return {"toast": {"type": "info", "content": "该确认已失效或已处理"}}

    job.answer_confirm(cid, allow)
    verdict = "✅ 已允许" if allow else "🚫 已拒绝"
    if job.confirm_card_id:
        _patch_card(job.confirm_card_id, f"{verdict}(操作 `{cid}`)")
    # Instant toast feedback on the tapped button.
    return {"toast": {"type": "success" if allow else "info", "content": verdict}}


def main() -> None:
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )
    ws = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, log_level=lark.LogLevel.INFO)
    log.info("Bridge up. Whitelisted user: %s | container: %s", ALLOWED_USER_ID, CONTAINER_NAME)
    ws.start()  # blocks, maintains the long connection


if __name__ == "__main__":
    main()
