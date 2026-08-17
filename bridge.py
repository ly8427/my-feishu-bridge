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
import atexit
import signal
import json
import logging
import os
import subprocess
import sys
import threading
import time
import queue
from concurrent.futures import ThreadPoolExecutor
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
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR") or os.path.join(
    os.path.expanduser("~"), "projects"
)
CONFIRM_TIMEOUT = os.environ.get("CONFIRM_TIMEOUT", "300")
SAFE_TOOLS = os.environ.get("SAFE_TOOLS", "Read,Grep,Glob,WebSearch,WebFetch,TodoWrite,NotebookRead")
DEFAULT_ENGINE = os.environ.get("ENGINE", "claude")
# S7: cap the prompt length so a single huge message can't blow the docker exec
# argv or burn API tokens. Feishu single-message max is ~4k, so 8k is generous.
MAX_PROMPT_LEN = int(os.environ.get("MAX_PROMPT_LEN", "8000"))


# ---------------------------------------------------------------- provider table
def _parse_providers() -> tuple[dict[str, dict], str]:
    """Build a provider config table from PROVIDERS + <NAME>_BASE_URL/API_KEY/MODELS env vars.

    Falls back to a single implicit provider from the legacy ANTHROPIC_* vars if
    PROVIDERS is unset, preserving backward compatibility.
    """
    table: dict[str, dict] = {}
    names_str = os.environ.get("PROVIDERS", "")
    if names_str:
        names = [n.strip() for n in names_str.split(",") if n.strip()]
        for name in names:
            prefix = name.upper()
            base_url = os.environ.get(f"{prefix}_BASE_URL", "")
            api_key = os.environ.get(f"{prefix}_API_KEY", "")
            models_str = os.environ.get(f"{prefix}_MODELS", "")
            models = [m.strip() for m in models_str.split(",") if m.strip()]
            table[name] = {"base_url": base_url, "api_key": api_key, "models": models}
    else:
        # Backward compat: derive a single "default" provider from ANTHROPIC_* vars.
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
        api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY", "")
        model = os.environ.get("ANTHROPIC_MODEL", "")
        models = [model] if model else []
        table["default"] = {"base_url": base_url, "api_key": api_key, "models": models}

    default_name = os.environ.get("DEFAULT_PROVIDER", "")
    if default_name not in table:
        default_name = next(iter(table), "")
    return table, default_name


_PROVIDERS, _DEFAULT_PROVIDER = _parse_providers()


def _apply_provider(name: str, chat_id: str = "") -> None:
    """Inject a provider's credentials into os.environ as ANTHROPIC_* vars.

    This lets the existing _CLAUDE_FORWARD_VARS forwarding logic work unchanged —
    it reads ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN from
    os.environ, so we just overwrite those before each turn.
    """
    prov = _PROVIDERS.get(name)
    if not prov:
        return
    if prov["base_url"]:
        os.environ["ANTHROPIC_BASE_URL"] = prov["base_url"]
    if prov["api_key"]:
        os.environ["ANTHROPIC_AUTH_TOKEN"] = prov["api_key"]
        os.environ["ANTHROPIC_API_KEY"] = prov["api_key"]
    # If user has a per-chat model override, keep it; otherwise use provider default
    if chat_id and _chat_model.get(chat_id):
        os.environ["ANTHROPIC_MODEL"] = _chat_model[chat_id]
    elif prov["models"]:
        os.environ["ANTHROPIC_MODEL"] = prov["models"][0]


def _provider_env(provider: str, chat_id: str = "", model: str | None = None) -> dict[str, str]:
    """This turn's Anthropic auth/model vars from the provider table, computed
    WITHOUT touching global os.environ.

    S4: concurrent turns for different providers used to race on os.environ —
    thread A could overwrite ANTHROPIC_BASE_URL between thread B's write and its
    read, pairing B's api_key with A's base_url (key POSTed to the wrong
    vendor). Returning a local dict per turn removes the shared mutable state.
    Mirrors the model selection in _apply_provider.

    model: explicit per-turn override (collab steps). Takes priority over the
    chat's /model value so an orchestrated review step can never be silently
    re-targeted to whatever the user last set interactively.
    """
    prov = _PROVIDERS.get(provider, {})
    env: dict[str, str] = {}
    if prov.get("base_url"):
        env["ANTHROPIC_BASE_URL"] = prov["base_url"]
    if prov.get("api_key"):
        env["ANTHROPIC_AUTH_TOKEN"] = prov["api_key"]
        env["ANTHROPIC_API_KEY"] = prov["api_key"]
    mdl = model or _chat_model.get(chat_id) or (prov.get("models") or [""])[0]
    if mdl:
        env["ANTHROPIC_MODEL"] = mdl
    return env


# per-chat state: engine, provider, model overrides (in-memory, process lifetime)
_chat_engine: dict[str, str] = {}
_chat_provider: dict[str, str] = {}
_chat_model: dict[str, str] = {}
# pi-only per-chat options (v2): thinking level + tool policy
_chat_thinking: dict[str, str] = {}   # chat_id -> off/minimal/low/medium/high/xhigh/max
_chat_tools: dict[str, str] = {}      # chat_id -> "safe" | "full"

# Apply default provider at startup so the first turn uses correct credentials.
_apply_provider(_DEFAULT_PROVIDER)


def _effective_model(chat_id: str, provider: str, model: str | None = None) -> str:
    """Resolve the model that WILL actually be used on the next turn for this
    chat: an explicit per-turn override (collab) wins, then an explicit `/model`
    override, else the current provider's default (first) model. Mirrors the
    selection in _apply_provider.

    model: explicit per-turn override (collab steps) — priority over /model.

    Do NOT read os.environ["ANTHROPIC_MODEL"] for display — that var is only
    refreshed by _apply_provider at turn-start, so right after `/provider` it
    still holds the *previous* provider's model (e.g. shows deepseek-v4-pro
    even after switching to zhipu)."""
    if model:
        return model
    override = _chat_model.get(chat_id)
    if override:
        return override
    prov = _PROVIDERS.get(provider, {})
    if prov.get("models"):
        return prov["models"][0]
    return os.environ.get("ANTHROPIC_MODEL", "?")

# Anthropic/Claude-Code env vars forwarded into the agent container when the
# claude engine runs. Auth (BASE_URL/AUTH_TOKEN/API_KEY) is always relevant;
# the MODEL* and CLAUDE_CODE* vars let a relay-compatible endpoint pin which
# model the in-container claude CLI actually requests (e.g. a deepseek/glm
# model behind an anthropic-compatible relay). Only set vars are injected, so
# unset entries are harmless. These are injected both via `docker exec -e`
# AND into the Popen subprocess env — keep both sites in sync via this tuple.
_CLAUDE_FORWARD_VARS = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
)

# NOTE: _chat_engine is declared once at the per-chat state block above
# (alongside _chat_provider/_chat_model/_chat_thinking/_chat_tools); the
# duplicate declaration that used to live here was removed.

from logging.handlers import RotatingFileHandler

_LOG_PATH = os.path.join(os.path.dirname(__file__), "bridge.log")
# Rotating file handler (T4): bridge.log lives in the mounted workspace and the
# bridge runs indefinitely; cap it at 10MB × 3 backups so it can't fill the disk.
# The _RedactFilter is attached to every handler below, so rotated files are
# scrubbed too.
_file_handler = RotatingFileHandler(
    _LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), _file_handler],
)
log = logging.getLogger("bridge")


class _RedactFilter(logging.Filter):
    """Scrub credentials from every log line before it reaches any handler.

    The lark SDK logs the live WebSocket URL on every (re)connect, and that URL
    carries `access_key` + `ticket` as query params — i.e. real, reusable auth
    that lets anyone impersonate this bot. bridge.log lives inside the agent's
    mounted workspace and Read is auto-approved, so without this filter a
    prompt-injected agent could exfiltrate the connection credentials via the
    log file. Redact at the record level so ALL handlers (console + file) are
    covered with one filter, and match defensively (token prefixes, query
    params, open_id values).

    Exception: lines starting with "IGNORED ..." keep the full open_id. Those
    lines exist ONLY to tell the operator the sender's open_id during first-time
    whitelist setup (see agent_readme §2.3 — "发条消息, 看 bridge.log 里 IGNORED
    ... sender: ou_xxx, 把 ou_... 填回"). Redacting the ou_ there defeats the
    feature; access_key/ticket/tokens are still scrubbed on every line.
    """
    import re as _re
    # Always-on secrets: real reusable credentials. Scrubbed on every line.
    _SECRETS = _re.compile(
        r"("
        r"access_key=[A-Za-z0-9]+"          # feishu WS connection credential
        r"|ticket=[A-Za-z0-9-]+"            # feishu WS connection ticket
        r"|app_secret=[A-Za-z0-9]+"         # feishu app secret if ever logged
        r"|(?:FEISHU_APP_SECRET|ANTHROPIC_AUTH_TOKEN|ANTHROPIC_API_KEY|OPENCODE_API_KEY|ZHIPU_API_KEY|DEEPSEEK_API_KEY)=[A-Za-z0-9_\-]+"
        r"|[A-Z][A-Z0-9_]*_API_KEY=[A-Za-z0-9_\-]+"  # R2-5: generic API_KEY redaction (catches DEFAULT_API_KEY etc.)
        r"|sk-[A-Za-z0-9]{6,}"              # any Anthropic/relay bearer token
        r")"
    )
    # PII: open_id values. Scrubbed on routine lines, KEPT on IGNORED setup lines.
    _OPENID = _re.compile(r"ou_[A-Za-z0-9]{20,}")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # Secrets are always redacted.
        out = self._SECRETS.sub(
            lambda m: m.group(0).split("=", 1)[0] + "=***", msg
        )
        # open_id is redacted EXCEPT on the "IGNORED ..." setup-assist lines,
        # where the whole point of the log entry is to surface the sender's id.
        is_setup_line = out.lstrip().startswith("IGNORED ")
        if not is_setup_line:
            out = self._OPENID.sub("ou_***", out)
        if out != msg:
            record.msg = out
            record.args = ()
        return True


logging.getLogger().addFilter(_RedactFilter())
# A logger-level filter only fires when a record originates at THAT logger.
# The lark SDK emits its `connected to wss://...access_key=...` line from its
# own "Lark" logger; even though it propagates up to root, the filter on root
# is NOT re-checked during propagation. To cover records from any sub-logger
# (lark, asyncio, etc.), attach the filter to every HANDLER too — a handler
# filter is applied to every record that passes through it regardless of origin.
_redact = _RedactFilter()
for _h in logging.getLogger().handlers:
    _h.addFilter(_redact)



import session_store  # local module

client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .timeout(float(os.environ.get("LARK_TIMEOUT", "10")))
    .build()
)

# Worker pool for Feishu card API calls made from the lark ws event-loop
# thread (on_message / on_card_action). Running these synchronous HTTP calls
# inline would block the single-threaded lark loop and stall heartbeats; offloading
# to a pool keeps the loop responsive. (Per-turn streaming cards are handled by the
# _render_loop thread spawned per Job, not this pool.)
_card_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="card")

# confirm_id -> callable(allow: bool) that pushes the reply into the agent stdin
_confirm_waiters: dict[str, "Job"] = {}
_confirm_lock = threading.Lock()

# T1: chats with an in-flight turn. Prevents two concurrent `docker exec` agents
# from --resume-ing the same session_id (which corrupts its transcript). Guarded
# by _active_lock; the slot is released in _render_loop's finally.
_active_chats: set[str] = set()
_active_lock = threading.Lock()


def _release_chat(chat_id: str) -> None:
    """Release a chat's in-flight-turn slot (see T1)."""
    with _active_lock:
        _active_chats.discard(chat_id)


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
    engine: str  # "claude" | "opencode" | "pi" — namespaces session_store keys per engine
    out_q: queue.Queue  # raw stdout lines -> _render_loop (decouples read from HTTP)
    card_msg_id: str | None = None
    transcript: str = ""
    confirm_card_id: str | None = None  # message_id of the pending confirm card
    lock: threading.Lock = field(default_factory=threading.Lock)
    # --- collab turn-primitive fields (coordinator-facing) ---
    result_text: str = ""              # final text from the runner's result message
    ok: bool | None = None             # result.ok (None = no result seen); False = step error
    error: str = ""                    # error message if the turn ended in error
    session_id: str = ""               # ALWAYS captured from session events (rm cold sessions)
    done: threading.Event = field(default_factory=threading.Event)  # set in render finally
    holds_lock: bool = False           # True: this step did NOT acquire T1 (pipeline owns it)
    persist_session: bool = True       # False: capture session_id but skip session_store.put
    done_cb: object = None             # callable(job) fired once in render finally
    run_id: str = ""                   # COLLAB_RUN_ID for precise container-side process kill

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
    """READ-ONLY thread: pull newline-JSON from the agent stdout and enqueue it.

    Deliberately makes NO Feishu API calls here. A slow lark HTTP call inline would
    stall this read loop, fill the OS pipe buffer, and back-pressure the agent's
    stdout writes — which is exactly what blocked its `confirm_request` emit and
    caused multi-minute stalls. Rendering happens in _render_loop on another thread.
    """
    try:
        for raw in job.proc.stdout:
            raw = raw.strip()
            if raw:
                job.out_q.put(raw)
    finally:
        job.out_q.put(None)  # sentinel: process exited, stop the renderer
        job.proc.wait()


def _render_loop(job: Job) -> None:
    """Render thread wrapper: run the render body, then ALWAYS release the
    per-chat turn slot (T1) and drop any dangling confirm waiters for this job
    (T3) — even if the body raises — so a chat can never get stuck 'busy' and
    timed-out confirmations don't leak Job references.

    Collab: steps with holds_lock=True did NOT acquire T1 (the pipeline
    coordinator owns it), so they must not release it either.
    """
    try:
        _render_loop_body(job)
    finally:
        if not job.holds_lock:
            _release_chat(job.chat_id)
        with _confirm_lock:
            for _cid in [k for k, v in _confirm_waiters.items() if v is job]:
                _confirm_waiters.pop(_cid, None)
        # Collab turn-primitive: single guaranteed "turn over" signal.
        job.done.set()
        if job.done_cb is not None:
            try:
                job.done_cb(job)
            except Exception:
                log.exception("done_cb raised (ignored)")


def _render_loop_body(job: Job) -> None:
    """Consume the agent's JSON lines and reflect them onto Feishu.

    Runs independently of _drain_job, so a slow lark HTTP call only delays this
    card update — it never blocks the agent's stdout from being read.
    """
    while True:
        raw = job.out_q.get()
        if raw is None:  # sentinel from _drain_job: process ended
            break
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.info("agent stderr: %s", raw[:200])
            continue
        mtype = msg.get("type")

        if mtype == "text":
            with job.lock:
                job.transcript += msg["text"]
            job.render()
        elif mtype == "tool":
            extra = f"\n\n> 🔧 {msg['name']}: `{msg.get('brief','')[:120]}`"
            with job.lock:
                job.transcript += extra
            job.render()
        elif mtype == "session":
            # ALWAYS capture the id (collab deletes cold review session files
            # by it); only persist when this step owns the chat's session.
            job.session_id = msg.get("session_id", "") or job.session_id
            if job.persist_session:
                # Namespace by engine so OpenCode's 'ses_xxx' ids never reach
                # claude's --resume (which requires a UUID).
                session_store.put(f"{job.engine}:{job.chat_id}", msg["session_id"])
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
            log.info("confirm card posted: id=%s tool=%s", cid, msg.get("tool"))
        elif mtype == "result":
            final = msg.get("text", "").strip()
            # Collab: capture the result for the coordinator; ok=False (e.g.
            # opencode session.error) is a step error, not an empty result.
            job.result_text = final
            job.ok = bool(msg.get("ok", True))
            if not job.ok:
                job.error = msg.get("error", "result reported ok=false")
            if final and final not in job.transcript:
                with job.lock:
                    job.transcript += "\n" + final
            job.render()
        elif mtype == "error":
            job.error = msg.get("message", "")
            job.ok = False
            job.render(f"\n\n❌ 出错: {msg.get('message')}")
        else:
            log.info("agent diagnostic: %s", str(msg)[:300])


# Wrapper that logs exceptions swallowed by ThreadPoolExecutor
def _safe_start_turn(chat_id: str, text: str) -> None:
    try:
        _start_turn(chat_id, text)
    except Exception:
        log.exception("CRASH in _start_turn")
        # The T1 slot is acquired inside _start_turn before any failure point;
        # the render thread that normally releases it never started, so release
        # here to avoid wedging the chat in a permanent 'busy' state.
        # NOTE: this path serves NORMAL turns only (holds_lock is always False
        # here). Collab pipeline steps call _start_turn directly with their own
        # try/except, so this release can never drop the pipeline's T1 slot.
        _release_chat(chat_id)


# Internal prompt cap: on_message enforces MAX_PROMPT_LEN for user input, but
# collab composes prompts internally (task + feedback + staged refs) — enforce
# the same bound here so a composed prompt can never blow docker-exec argv.
_INTERNAL_PROMPT_MAX = 8000


def _start_turn(
    chat_id: str,
    prompt: str,
    engine: str | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    system_prompt: str | None = None,   # APPENDED to the identity prompt in the runner
    no_resume: bool = False,            # cold turn (collab reviewer): resume forced None
    persist_session: bool = True,       # False: capture session_id, skip session_store.put
    hold_lock: bool = False,            # True: T1 already held by the caller (pipeline)
    tools_mode: str | None = None,      # pi: "safe"|"full" (overrides per-chat /tools)
    safe_tools: str | None = None,      # claude: overrides SAFE_TOOLS for this turn
    confirm_timeout: str | None = None, # overrides CONFIRM_TIMEOUT for this turn
    pi_turn_timeout: str | None = None, # pi: overrides PI_TURN_TIMEOUT for this turn
    pi_idle_timeout: str | None = None, # pi: overrides PI_IDLE_TIMEOUT for this turn
    initial_card_text: str | None = None,  # per-step card text (collab step labeling)
    done_cb=None,                       # callable(job), fired once in render finally
    run_id: str | None = None,          # COLLAB_RUN_ID: precise container-side kill key
) -> Job:
    # T1: one in-flight turn per chat. Two concurrent turns would launch two
    # `docker exec` agents that --resume the SAME session_id and corrupt its
    # transcript. If a turn is already running, tell the user and bail; the slot
    # is released in _render_loop's finally (or by _safe_start_turn on crash).
    # hold_lock: the pipeline coordinator already acquired T1 atomically for
    # the whole pipeline — steps must not re-check (they'd trip on their own
    # pipeline's slot) and must not release it.
    if not hold_lock:
        with _active_lock:
            if chat_id in _active_chats:
                _post_card(chat_id, "⏳ 上一条指令还在处理中，请等它完成后再发一条。")
                return None  # type: ignore[return-value]
            _active_chats.add(chat_id)
    engine = engine or _chat_engine.get(chat_id, DEFAULT_ENGINE)
    resume = session_store.get(f"{engine}:{chat_id}")
    if no_resume:
        resume = None
    # OpenCode sessions are ephemeral — each runner starts a fresh server,
    # so cross-runner resume is meaningless.
    if engine == "opencode":
        resume = None
    # Internal composed prompts get the same bound as user input.
    if len(prompt) > _INTERNAL_PROMPT_MAX:
        prompt = prompt[:_INTERNAL_PROMPT_MAX] + "\n…(内部指令过长已截断)"
    provider = provider or _chat_provider.get(chat_id, _DEFAULT_PROVIDER)

    if engine == "pi":
        # ---- pi engine: inject PI_* env (no ANTHROPIC_* path) ----
        active_prov = provider
        active_mdl = _effective_model(chat_id, active_prov, model)
        if not active_mdl or active_mdl == "?":
            _post_card(chat_id, f"❌ pi 引擎需要为 `{active_prov}` 配置模型。用 `/model <name>` 设置。")
            # R2: Job isn't constructed yet, so gate on the hold_lock PARAMETER —
            # a collab step must never drop the pipeline's T1 slot. (Collab also
            # pre-validates models before starting, so this is defense in depth.)
            if not hold_lock:
                _release_chat(chat_id)
            return None  # type: ignore[return-value]
        # DS r8: normalize model — take last part after "/" for provider-config models
        active_mdl = active_mdl.rsplit("/", 1)[-1]
        cmd = ["docker", "exec", "-i"]
        cmd += ["-e", f"PI_PROVIDERS={','.join(_PROVIDERS.keys())}"]
        for prov_name, prov_cfg in _PROVIDERS.items():
            P = f"PI_{prov_name.upper()}"
            cmd += ["-e", f"{P}_BASE_URL={prov_cfg['base_url']}"]
            cmd += ["-e", f"{P}_API_KEY_ENV={prov_name.upper()}_API_KEY"]
            normalized = [m.rsplit("/", 1)[-1] for m in prov_cfg["models"]]
            cmd += ["-e", f"{P}_MODELS={','.join(normalized)}"]
            cmd += ["-e", f"{P}_API=anthropic-messages"]
            if prov_cfg.get("api_key"):
                cmd += ["-e", f"{prov_name.upper()}_API_KEY={prov_cfg['api_key']}"]
        cmd += ["-e", f"PI_ACTIVE_PROVIDER={active_prov}-relay"]
        cmd += ["-e", f"PI_ACTIVE_MODEL={active_mdl}"]
        if os.environ.get("PI_USE_BEARER"):
            cmd += ["-e", f"PI_USE_BEARER={os.environ['PI_USE_BEARER']}"]
        # v2: per-chat thinking level + tool policy (pi-only);
        # collab steps override via tools_mode without touching shared state
        if _chat_thinking.get(chat_id):
            cmd += ["-e", f"PI_THINKING={_chat_thinking[chat_id]}"]
        cmd += ["-e", f"PI_TOOLS_MODE={tools_mode or _chat_tools.get(chat_id, 'full')}"]
        # collab per-step extras: extra system prompt (APPENDED after the
        # identity prompt by the runner), precise kill key, timeout overrides
        if system_prompt:
            cmd += ["-e", f"PI_EXTRA_SYSTEM_PROMPT={system_prompt}"]
        if run_id:
            cmd += ["-e", f"COLLAB_RUN_ID={run_id}"]
        if pi_turn_timeout:
            cmd += ["-e", f"PI_TURN_TIMEOUT={pi_turn_timeout}"]
        if pi_idle_timeout:
            cmd += ["-e", f"PI_IDLE_TIMEOUT={pi_idle_timeout}"]
        cmd += [
            "-e", f"SAFE_TOOLS={SAFE_TOOLS}",
            "-e", f"CONFIRM_TIMEOUT={confirm_timeout or CONFIRM_TIMEOUT}",
            "-e", f"WORKSPACE_DIR={WORKSPACE_DIR}",
            "-w", WORKSPACE_DIR,
            CONTAINER_NAME,
        ]
        cmd += ["python3", "/app/agent_runner_pi.py", "--prompt", prompt]
        if resume:
            cmd += ["--resume", resume]
        _clean_env = {k: os.environ[k] for k in ("PATH", "HOME", "DOCKER_HOST", "TERM") if k in os.environ}
        if system_prompt:
            _clean_env["PI_EXTRA_SYSTEM_PROMPT"] = system_prompt
        if run_id:
            _clean_env["COLLAB_RUN_ID"] = run_id
    else:
        # ---- claude/opencode engine: ANTHROPIC_* path ----
        # S4: resolve this turn's provider auth/model into a LOCAL dict (no global
        # os.environ mutation) so concurrent turns can't cross-pair one provider's
        # api_key with another's base_url. Provider override wins; else fall back to
        # the host's static env. Then mirror AUTH_TOKEN→API_KEY (claude CLI reads
        # only API_KEY).
        _prov_env = _provider_env(provider, chat_id, model)
        _fwd: dict[str, str] = {}
        for var in _CLAUDE_FORWARD_VARS:
            val = _prov_env.get(var) or os.environ.get(var)
            if val:
                _fwd[var] = val
        if not _fwd.get("ANTHROPIC_API_KEY") and _fwd.get("ANTHROPIC_AUTH_TOKEN"):
            _fwd["ANTHROPIC_API_KEY"] = _fwd["ANTHROPIC_AUTH_TOKEN"]
        cmd = ["docker", "exec", "-i"]
        for var in _CLAUDE_FORWARD_VARS:
            if _fwd.get(var):
                cmd += ["-e", f"{var}={_fwd[var]}"]
        if engine == "opencode":
            for var in ("OPENCODE_API_KEY", "OPENCODE_API_URL", "OPENCODE_MODEL", "ZHIPU_API_KEY"):
                if os.environ.get(var):
                    cmd += ["-e", f"{var}={os.environ[var]}"]
            if os.environ.get("OPENCODE_API_KEY") and not os.environ.get("ZHIPU_API_KEY"):
                cmd += ["-e", f"ZHIPU_API_KEY={os.environ['OPENCODE_API_KEY']}"]
            if os.environ.get("OPENCODE_BIN"):
                cmd += ["-e", f"OPENCODE_BIN={os.environ['OPENCODE_BIN']}"]
        # collab per-step extras (claude path). COLLAB_SYSTEM_PROMPT is a v2
        # RESERVED pipe (v1 reviewer whitelist is pi-only) — wired now so the
        # claude reviewer later needs zero bridge changes.
        if system_prompt:
            cmd += ["-e", f"COLLAB_SYSTEM_PROMPT={system_prompt}"]
        if run_id:
            cmd += ["-e", f"COLLAB_RUN_ID={run_id}"]
        cmd += [
            "-e", f"SAFE_TOOLS={safe_tools or SAFE_TOOLS}",
            "-e", f"CONFIRM_TIMEOUT={confirm_timeout or CONFIRM_TIMEOUT}",
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
        _passthrough_vars = _CLAUDE_FORWARD_VARS + (
            "PATH", "HOME", "DOCKER_HOST", "TERM",
        )
        if engine == "opencode":
            _passthrough_vars = _passthrough_vars + (
                "OPENCODE_API_KEY", "OPENCODE_API_URL", "OPENCODE_MODEL", "ZHIPU_API_KEY",
            )
        _clean_env = {}
        for k in _passthrough_vars:
            if k in _CLAUDE_FORWARD_VARS:
                if _fwd.get(k):
                    _clean_env[k] = _fwd[k]
            elif k in os.environ:
                _clean_env[k] = os.environ[k]
        if system_prompt:
            _clean_env["COLLAB_SYSTEM_PROMPT"] = system_prompt
        if run_id:
            _clean_env["COLLAB_RUN_ID"] = run_id
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=_clean_env,
    )
    job = Job(chat_id=chat_id, proc=proc, engine=engine, out_q=queue.Queue(),
              holds_lock=hold_lock, persist_session=persist_session,
              done_cb=done_cb, run_id=run_id or "")
    job.card_msg_id = _post_card(chat_id, initial_card_text or "🤔 已收到,正在处理…")
    # Read thread: drains agent stdout into job.out_q (no HTTP, never stalls).
    # Render thread: consumes job.out_q and calls lark API to update cards.
    # Splitting these is what stops a slow lark HTTP call from back-pressuring
    # the agent's stdout (the root cause of multi-minute stalls).
    threading.Thread(target=_drain_job, args=(job,), daemon=True).start()
    log.info("turn started: engine=%s provider=%s model=%s", engine, provider,
             _effective_model(chat_id, provider, model))
    threading.Thread(target=_render_loop, args=(job,), daemon=True).start()
    return job


# ---------------------------------------------------------------- slash command helpers
def _help_text() -> str:
    return (
        "📖 **可用命令**\n\n"
        "`/new`, `/reset` — 清除会话,开启新对话\n"
        "`/sessions` — 列出所有历史会话\n"
        "`/resume <N|id>` — 恢复指定会话\n"
        "`/provider [name]` — 查看/切换厂商\n"
        "`/model [name]` — 查看/切换模型\n"
        "`/engine <name>` — 切换引擎 (claude/opencode/pi)\n"
        "`/status` — 查看当前状态\n"
        "`/thinking [level]` — pi 思考等级 (off~max)\n"
        "`/tools [safe|full]` — pi 工具模式 (只读/全部)\n"
        "`/help` — 显示此帮助\n\n"
        "直接发消息即与 AI 对话"
    )


# Cache for session listing to support /resume <序号>.
# v2: engine-keyed so a pi listing can never be consumed by a claude /resume
# (and vice versa) — the engine that produced the list must match the current
# engine at /resume time.
_session_list_cache: dict[str, dict] = {}  # chat_id -> {"engine": str, "sessions": list}


def _cache_sessions(chat_id: str, engine: str, sessions: list) -> None:
    _session_list_cache[chat_id] = {"engine": engine, "sessions": sessions}


def _cached_sessions(chat_id: str, engine: str) -> list | None:
    """Return cached session list ONLY if it was produced by the same engine."""
    entry = _session_list_cache.get(chat_id)
    if entry and entry.get("engine") == engine:
        return entry.get("sessions")
    return None


def _format_session_card(chat_id: str, engine: str, sessions: list) -> None:
    """Render a session-listing card (shared by claude + pi handlers)."""
    current = session_store.get(f"{engine}:{chat_id}")
    lines = [f"📂 **会话列表** ({len(sessions)} 个)", ""]
    for i, s in enumerate(sessions, 1):
        sid = s.get("session_id", "?")
        sid_short = sid[:13] + "..." if len(sid) > 16 else sid
        ts = s.get("updated_at", s.get("created_at", "?"))
        title = s.get("title", "")
        model = s.get("model", "")
        marker = " ← 当前" if sid == current else ""
        line = f"{i}. `{sid_short}` | {ts}"
        if model:
            line += f" | {model}"
        if title:
            line += f" | {title}"
        line += marker
        lines.append(line)
    lines.append("")
    if current:
        lines.append(f"当前活跃: `{current[:13]}...`")
    lines.append("用 `/resume <序号或session_id>` 恢复")
    _post_card(chat_id, "\n".join(lines))


def _handle_sessions_cmd(chat_id: str) -> None:
    """List Claude sessions from the container via agent_runner --list-sessions."""
    try:
        result = subprocess.run(
            ["docker", "exec", feishu_claude_agent_name(), "python3", "/app/agent_runner.py", "--list-sessions"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            err = result.stdout.strip()[:500] or result.stderr.strip()[:500]
            _post_card(chat_id, f"❌ 查询会话失败:\n```\n{err}\n```")
            return
        sessions = json.loads(result.stdout.strip())
        if not sessions:
            _post_card(chat_id, "📂 暂无历史会话。\n发条消息开始第一次对话吧。")
            return
        _cache_sessions(chat_id, "claude", sessions)
        _format_session_card(chat_id, "claude", sessions)
    except subprocess.TimeoutExpired:
        _post_card(chat_id, "❌ 查询超时(30s),请稍后重试")
    except Exception as e:
        _post_card(chat_id, f"❌ 查询出错: {e}")


def _handle_pi_sessions_cmd(chat_id: str) -> None:
    """List pi sessions from the container via agent_runner_pi --list-sessions (v2)."""
    try:
        result = subprocess.run(
            ["docker", "exec", feishu_claude_agent_name(), "python3", "/app/agent_runner_pi.py", "--list-sessions"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            err = result.stdout.strip()[:500] or result.stderr.strip()[:500]
            _post_card(chat_id, f"❌ 查询 pi 会话失败:\n```\n{err}\n```")
            return
        sessions = json.loads(result.stdout.strip())
        if not sessions:
            _post_card(chat_id, "📂 暂无 pi 历史会话。\n发条消息开始第一次对话吧。")
            return
        _cache_sessions(chat_id, "pi", sessions)
        _format_session_card(chat_id, "pi", sessions)
    except subprocess.TimeoutExpired:
        _post_card(chat_id, "❌ 查询超时(30s),请稍后重试")
    except Exception as e:
        _post_card(chat_id, f"❌ 查询出错: {e}")


def _handle_resume_cmd(chat_id: str, arg: str) -> None:
    """Resume a session by sequence number or full session_id (engine-aware, v2)."""
    engine = _chat_engine.get(chat_id, DEFAULT_ENGINE)

    if arg.lower() == "clear":
        session_store.clear(f"{engine}:{chat_id}")
        _post_card(chat_id, "✅ 已清除会话恢复,下条消息将开启新会话。")
        return

    # Determine target session_id
    target_sid = None
    if arg.isdigit():
        idx = int(arg) - 1
        sessions = _cached_sessions(chat_id, engine) or []
        if 0 <= idx < len(sessions):
            target_sid = sessions[idx].get("session_id")
        elif not sessions:
            _post_card(chat_id, "❌ 无当前引擎的会话列表。先发 `/sessions` 再用序号。")
            return
        else:
            _post_card(chat_id, f"❌ 序号 {arg} 超出范围。先用 `/sessions` 查看列表。")
            return
    else:
        target_sid = arg.strip()
        # If the provided ID is shorter than a full UUID, try prefix-matching
        # against cached sessions of the SAME engine only (v2: engine-keyed).
        if len(target_sid) < 36:
            sessions = _cached_sessions(chat_id, engine) or []
            matches = [s["session_id"] for s in sessions
                       if s.get("session_id", "").startswith(target_sid)]
            if len(matches) == 1:
                target_sid = matches[0]
            elif len(matches) > 1:
                _post_card(chat_id, f"❌ ID 前缀 `{target_sid}` 匹配了 {len(matches)} 个会话，请提供更完整的 ID。")
                return

    if not target_sid:
        _post_card(chat_id, "❌ 无效的 session_id")
        return

    session_store.put(f"{engine}:{chat_id}", target_sid)
    sid_short = target_sid[:13] + "..." if len(target_sid) > 16 else target_sid
    _post_card(chat_id, f"✅ 已设置会话恢复: `{sid_short}`\n下条消息将从该会话继续。")


def feishu_claude_agent_name() -> str:
    return CONTAINER_NAME


# ---------------------------------------------------------------- event handlers
def on_message(data: P2ImMessageReceiveV1) -> None:
    ev = data.event
    sender_id = ev.sender.sender_id.open_id
    chat_id = ev.message.chat_id

    if sender_id != ALLOWED_USER_ID:
        log.warning("IGNORED message from non-whitelisted sender: %s", sender_id)
        return

    if ev.message.message_type != "text":
        _card_workers.submit(_post_card, chat_id, "目前只支持文本指令。")
        return

    try:
        text = json.loads(ev.message.content).get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return
    if not text:
        return

    # ---- slash commands ----
    if text in ("/new", "/reset"):
        session_store.clear(f"claude:{chat_id}")
        session_store.clear(f"opencode:{chat_id}")
        session_store.clear(f"pi:{chat_id}")
        _card_workers.submit(_post_card, chat_id, "🧹 已清除会话上下文,下条消息将开启新会话。")
        return

    if text == "/help":
        _card_workers.submit(_post_card, chat_id, _help_text())
        return

    if text.startswith("/engine "):
        eng = text.split(" ", 1)[1].strip().lower()
        if eng in ("claude", "opencode", "pi"):
            _chat_engine[chat_id] = eng
            for ns in ("claude", "opencode", "pi"):
                session_store.clear(f"{ns}:{chat_id}")
            _card_workers.submit(_post_card, chat_id, f"⚙️ 引擎已切换: {eng}")
        else:
            _card_workers.submit(_post_card, chat_id, f"未知引擎: {eng}。可用: claude / opencode / pi")
        return

    if text == "/provider" or text.startswith("/provider "):
        parts = text.split(None, 1)
        if len(parts) < 2:
            # Show current provider + available list
            cur = _chat_provider.get(chat_id, _DEFAULT_PROVIDER)
            lines = [f"🏢 **厂商管理**", f"当前: `{cur}`", "", "**可用厂商:**"]
            for name, prov in _PROVIDERS.items():
                models = ", ".join(prov["models"]) or "(未配置)"
                marker = " ← 当前" if name == cur else ""
                lines.append(f"  • `{name}` (模型: {models}){marker}")
            lines.append("")
            lines.append("用 `/provider <name>` 切换")
            lines.append("⚠️ 切换厂商会清除当前会话(不同厂商 API 不兼容)")
            _card_workers.submit(_post_card, chat_id, "\n".join(lines))
        else:
            name = parts[1].strip().lower()
            if name in _PROVIDERS:
                _chat_provider[chat_id] = name
                # Clear session: different providers have incompatible APIs
                session_store.clear(f"claude:{chat_id}")
                session_store.clear(f"opencode:{chat_id}")
                session_store.clear(f"pi:{chat_id}")
                # Reset per-chat model so the new provider's default kicks in
                _chat_model.pop(chat_id, None)
                prov = _PROVIDERS[name]
                models = ", ".join(prov["models"]) or "(未配置)"
                cur_mdl = _effective_model(chat_id, name)
                _card_workers.submit(
                    _post_card, chat_id,
                    f"🏢 厂商已切换: `{name}`\n当前模型: `{cur_mdl}`\n可选: {models}\n会话已清除。\n用 `/model` 切换其他模型。"
                )
            else:
                available = ", ".join(_PROVIDERS.keys())
                _card_workers.submit(_post_card, chat_id, f"未知厂商: `{name}`。可用: {available}")
        return

    if text == "/model" or text.startswith("/model "):
        parts = text.split(None, 1)
        provider = _chat_provider.get(chat_id, _DEFAULT_PROVIDER)
        prov = _PROVIDERS.get(provider, {})
        cur_model = _effective_model(chat_id, provider)
        if len(parts) < 2:
            # Show current model + available list for current provider
            lines = [f"🤖 **模型管理**", f"厂商: `{provider}`", f"当前: `{cur_model}`", "", "**可用模型:**"]
            for m in prov.get("models", []):
                marker = " ← 当前" if m == cur_model else ""
                lines.append(f"  • `{m}`{marker}")
            lines.append("")
            lines.append("用 `/model <name>` 切换")
            lines.append("也可输入其他模型名自定义")
            _card_workers.submit(_post_card, chat_id, "\n".join(lines))
        else:
            model_name = parts[1].strip()
            # DS r5/r8: validate for pi engine + general safety
            engine = _chat_engine.get(chat_id, DEFAULT_ENGINE)
            if engine == "pi" and "/" in model_name:
                _card_workers.submit(_post_card, chat_id, "❌ pi 引擎请用不带 `/` 的模型名。")
                return
            if any(c in model_name for c in (" ", "\t", ",")):
                _card_workers.submit(_post_card, chat_id, "❌ 模型名不能含空格或逗号。")
                return
            _chat_model[chat_id] = model_name
            _card_workers.submit(_post_card, chat_id, f"🤖 模型已切换: `{model_name}`")
        return

    if text == "/sessions":
        engine = _chat_engine.get(chat_id, DEFAULT_ENGINE)
        if engine == "pi":
            _card_workers.submit(_handle_pi_sessions_cmd, chat_id)
        elif engine == "opencode":
            _card_workers.submit(_post_card, chat_id, "⚠️ opencode 会话为临时会话，不支持列出。")
        else:
            _card_workers.submit(_handle_sessions_cmd, chat_id)
        return

    if text.startswith("/resume"):
        parts = text.split(None, 1)
        if len(parts) < 2:
            _card_workers.submit(_post_card, chat_id, "用法: `/resume <序号或session_id>`\n先用 `/sessions` 查看列表\n或 `/resume clear` 清除恢复")
        else:
            arg = parts[1].strip()
            _card_workers.submit(_handle_resume_cmd, chat_id, arg)
        return

    if text == "/status":
        engine = _chat_engine.get(chat_id, DEFAULT_ENGINE)
        provider = _chat_provider.get(chat_id, _DEFAULT_PROVIDER)
        model = _effective_model(chat_id, provider)
        sess = session_store.get(f"{engine}:{chat_id}") or "无"
        if len(sess) > 16:
            sess = sess[:13] + "..."
        lines = [f"📊 **当前状态**", "",
                 f"引擎: `{engine}`",
                 f"厂商: `{provider}`",
                 f"模型: `{model}`",
                 f"会话: `{sess}`"]
        if engine == "pi":
            lines.append(f"思考等级: `{_chat_thinking.get(chat_id, '默认')}`")
            lines.append(f"工具模式: `{_chat_tools.get(chat_id, 'full')}`")
        _card_workers.submit(_post_card, chat_id, "\n".join(lines))
        return

    if text == "/thinking" or text.startswith("/thinking "):
        engine = _chat_engine.get(chat_id, DEFAULT_ENGINE)
        if engine != "pi":
            _card_workers.submit(_post_card, chat_id, "⚠️ `/thinking` 仅 pi 引擎支持。")
            return
        parts = text.split(None, 1)
        levels = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
        if len(parts) < 2:
            cur = _chat_thinking.get(chat_id, "默认")
            _card_workers.submit(_post_card, chat_id,
                f"🧠 **思考等级**: `{cur}`\n\n可用: {', '.join(levels)}\n用 `/thinking <level>` 切换")
        else:
            lvl = parts[1].strip().lower()
            if lvl in levels:
                _chat_thinking[chat_id] = lvl
                _card_workers.submit(_post_card, chat_id, f"🧠 思考等级已设置: `{lvl}`")
            else:
                _card_workers.submit(_post_card, chat_id,
                    f"❌ 无效等级 `{lvl}`。可用: {', '.join(levels)}")
        return

    if text == "/tools" or text.startswith("/tools "):
        engine = _chat_engine.get(chat_id, DEFAULT_ENGINE)
        if engine != "pi":
            _card_workers.submit(_post_card, chat_id, "⚠️ `/tools` 仅 pi 引擎支持。")
            return
        parts = text.split(None, 1)
        if len(parts) < 2:
            cur = _chat_tools.get(chat_id, "full")
            desc = {"full": "全部工具（bash/edit/write/read/grep/find/ls）",
                    "safe": "只读（read/grep/find/ls）——禁止 bash/edit/write"}
            _card_workers.submit(_post_card, chat_id,
                f"🔧 **工具模式**: `{cur}` — {desc.get(cur, '')}\n\n用 `/tools safe` 只读模式\n用 `/tools full` 全部工具")
        else:
            mode = parts[1].strip().lower()
            if mode in ("safe", "full"):
                _chat_tools[chat_id] = mode
                extra = "\n⚠️ 只读模式下 bash/edit/write 被禁用。" if mode == "safe" else ""
                _card_workers.submit(_post_card, chat_id, f"🔧 工具模式已设置: `{mode}`{extra}")
            else:
                _card_workers.submit(_post_card, chat_id, "❌ 无效模式。用 `/tools safe` 或 `/tools full`")
        return

    if len(text) > MAX_PROMPT_LEN:
        _card_workers.submit(
            _post_card, chat_id,
            f"指令过长（{len(text)} 字符），上限 {MAX_PROMPT_LEN}。请拆分或精简后再发。"
        )
        return

    log.info("turn from %s in %s: %s", sender_id, chat_id, text[:80])
    # Run _start_turn on a worker thread: it makes a synchronous _post_card call
    # ("已收到" card) that would otherwise block the lark ws event loop and stall
    # heartbeats. Offloading the whole turn-start keeps card_msg_id assignment
    # ordered before the read/render threads start.
    _card_workers.submit(_safe_start_turn, chat_id, text)


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

    log.info("card action: job=%s cid=%s", "found" if job else "NONE", cid)
    if job is None:
    # Already handled, expired, or timed out before the tap landed.
        return {"toast": {"type": "info", "content": "该确认已失效或已处理"}}

    # answer_confirm writes the agent's stdin (local IO, no network) — keep inline
    # so the agent unblocks the instant the user taps, before any card rendering.
    log.info("answering confirm: cid=%s allow=%s proc_alive=%s", cid, str(allow), str(job.proc.poll() is None))
    job.answer_confirm(cid, allow)
    verdict = "✅ 已允许" if allow else "🚫 已拒绝"
    if job.confirm_card_id:
        _card_workers.submit(_patch_card, job.confirm_card_id, f"{verdict}(操作 `{cid}`)")
    # Instant toast feedback on the tapped button.
    return {"toast": {"type": "success" if allow else "info", "content": verdict}}


PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.pid")

# Module-level handle to the singleton lock file. The OS releases this advisory
# lock (and thus the single-instance guarantee) only when the process truly
# exits — including under SIGKILL / a hard crash. This replaces the previous
# approach of killing the old instance, which relied on SIGKILL and so could
# not run on Windows.
_singleton_lockfh: object | None = None


def _acquire_singleton_lock() -> bool:
    """Acquire an exclusive, cross-platform lock on PID_FILE.

    Returns True if acquired (this is the only running instance), False if a
    live instance already holds the lock. Advisory-lock implementation:
      - Unix: fcntl.flock(LOCK_EX | LOCK_NB)
      - Win:  msvcrt.locking(LK_NBLCK)
    The OS frees the lock automatically on process death, so a stale PID file
    after a crash no longer blocks the next start.
    """
    global _singleton_lockfh
    # "a+" neither truncates an existing PID nor fails when it is absent.
    fh = open(PID_FILE, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                return False
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return False
    except Exception:
        # Locking unavailable on this platform — fall through rather than block.
        fh.close()
        return True
    _singleton_lockfh = fh
    return True


def _release_singleton_lock() -> None:
    """Release the singleton lock and clear the PID file on clean exit."""
    global _singleton_lockfh
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except OSError:
        pass
    if _singleton_lockfh is not None:
        try:
            _singleton_lockfh.close()
        except OSError:
            pass
        _singleton_lockfh = None


def _ensure_single_instance() -> None:
    """Ensure only one bridge instance runs; refuse to start otherwise.

    Cross-platform: no kill / no signals. If a live instance holds the lock we
    exit with a clear message so the operator stops it explicitly. This is
    safer than silently killing (the old path used SIGKILL — Windows-incompatible
    and risky against an instance mid-turn).
    """
    if not _acquire_singleton_lock():
        sys.exit(
            f"Another bridge instance is running (lock held on {PID_FILE}). "
            "Stop it first: python3 cli.py stop  (or: systemctl --user stop feishu-bridge)."
        )
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(_release_singleton_lock)


def _on_stop_signal(signum, _frame) -> None:
    """Hard-stop hook for SIGINT/SIGTERM (both exist on Win/Mac/Linux).

    We must exit explicitly. lark's ws.start() blocks on a socket recv and does
    NOT unwind when we install our own handler: registering replaces the
    default action (terminate on SIGTERM / raise KeyboardInterrupt on SIGINT),
    so merely logging here leaves the process — and its singleton lock — alive
    forever. That is exactly why `cli.py stop` kept reporting
    "lock still held after 5s".

    Release the singleton lock so a fresh instance can start immediately, then
    hard-exit. The OS also frees the fcntl advisory lock on process death, and
    the daemon drain/render threads die with the process.
    """
    log.info("Received signal %s — shutting down bridge", signum)
    try:
        _release_singleton_lock()
    finally:
        os._exit(0)


def _patch_websockets_no_native_ping() -> None:
    """Disable websockets library's native WS-level ping/pong.

    The lark SDK maintains its own protobuf-frame ping loop (_ping_loop), so the
    websockets library's built-in ping is redundant. Worse, the Feishu WS server
    does NOT respond to RFC 6455 ping frames, so websockets' ping times out after
    20s and forcibly closes the connection ('keepalive ping timeout'). We disable
    it by patching the defaults of websockets.connect.
    """
    try:
        import websockets
        _orig_connect = websockets.connect

        def _patched_connect(uri, **kwargs):
            # Disable native WS ping (Feishu server ignores RFC 6455 pings;
            # the lark SDK uses its own protobuf ping instead).
            kwargs.setdefault("ping_interval", None)
            kwargs.setdefault("ping_timeout", None)
            # Hard deadline on the handshake itself (default 10s, but be
            # explicit so a half-open TCP can't hang forever in recv).
            kwargs.setdefault("open_timeout", 20)
            return _orig_connect(uri, **kwargs)

        websockets.connect = _patched_connect
        log.info("Patched websockets.connect: native WS ping disabled")
    except Exception as e:
        log.warning("Failed to patch websockets (non-fatal): %s", e)


def _patch_requests_timeout() -> None:
    """Force a default timeout on every requests.post.

    The lark SDK's _get_conn_url() does a synchronous requests.post() to fetch
    the WS URL with NO timeout. If that call stalls (DNS/TCP/half-open), it
    blocks the single asyncio event-loop thread forever while holding the SDK's
    connection lock — freezing ping/recv/reconnect so no exception ever fires.
    That silent unbounded block is the root cause of the multi-hour zombie
    disconnects. Bounding it to 15s turns the stall into a raised exception that
    the SDK's normal reconnect path then handles. setdefault() so a caller's own
    timeout is always respected.
    """
    try:
        import requests
        _orig_post = requests.post

        def _post_with_timeout(url, *args, **kwargs):
            kwargs.setdefault("timeout", 15)
            return _orig_post(url, *args, **kwargs)

        requests.post = _post_with_timeout
        log.info("Patched requests.post: default timeout=15s")
    except Exception as e:
        log.warning("Failed to patch requests (non-fatal): %s", e)


# Max seconds the connection may go without receiving ANY WS frame before we
# assume it's frozen / half-open and force a restart. The lark SDK pings every
# ~120s and the server responds with a pong, so a healthy idle connection
# receives a frame at least that often; 600s is a ~5x margin.
_WATCHDOG_TIMEOUT = 600

# Liveness signal for the connection watchdog. Refreshed by _install_recv_tracker
# on every inbound WS frame (pong / control / event). Initialized to startup
# time by the watchdog so it is armed immediately — which also catches the
# "connected but never delivers a frame" failure mode.
_last_recv_ts: float = 0.0


def _install_recv_tracker(ws) -> None:
    """Wrap ws._handle_message so every received WS frame refreshes _last_recv_ts.

    The watchdog keys off _last_recv_ts instead of log activity: a healthy idle
    connection still receives the server's pong/heartbeats (~every 120s) so it is
    never mis-killed, while a frozen asyncio loop or a half-open connection that
    stops delivering frames is caught within _WATCHDOG_TIMEOUT. Done per-Client
    instance; the lark SDK reuses the same instance across its internal reconnects,
    so one wrap covers the whole process lifetime.
    """
    orig = ws._handle_message

    async def _tracked(msg):
        global _last_recv_ts
        _last_recv_ts = time.time()
        return await orig(msg)

    ws._handle_message = _tracked


def _start_connection_watchdog() -> None:
    """Daemon thread that force-restarts the process if the connection is dead.

    Unlike the old log-mtime watchdog, this keys off the WS recv timestamp
    (_last_recv_ts), refreshed on every inbound frame. That distinguishes a
    healthy idle connection (the server keeps pushing pong/heartbeats) from a
    truly dead one — so healthy idle connections are NOT mis-killed. A frozen
    asyncio loop (now structurally prevented by the requests/open_timeout bounds)
    or a half-open connection that silently stops delivering frames triggers an
    os._exit(1); systemd's Restart=on-failure brings us back with a fresh socket.
    """
    global _last_recv_ts
    _last_recv_ts = time.time()  # armed from boot; also catches connected-but-silent

    def _watchdog():
        time.sleep(30)  # let the initial connect settle before first check
        while True:
            try:
                if _last_recv_ts and time.time() - _last_recv_ts > _WATCHDOG_TIMEOUT:
                    idle = int(time.time() - _last_recv_ts)
                    print(
                        f"\n[WATCHDOG] No WS frame received for {idle}s "
                        f"(threshold {_WATCHDOG_TIMEOUT}s). "
                        f"Force-killing for systemd restart.\n",
                        file=sys.stderr, flush=True,
                    )
                    os._exit(1)
            except Exception:
                pass
            time.sleep(30)

    threading.Thread(target=_watchdog, daemon=True, name="conn-watchdog").start()
    log.info("Connection watchdog started (recv-based, timeout=%ds)", _WATCHDOG_TIMEOUT)


def _warn_if_source_in_workspace() -> None:
    """S2: the bridge's own source must not sit inside the agent-writable mount.

    WORKSPACE_DIR is bind-mounted RW into the container and the agent runs as
    root there. If bridge.py/cli.py live under WORKSPACE_DIR, a single approved
    Write/Edit lets the containerized agent rewrite the very code the HOST later
    executes (container→host code injection). We can't repair the mount from
    here, but we refuse to be silent: warn loudly, and hard-refuse to start when
    WORKSPACE_STRICT is set.
    """
    src = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    ws = os.path.realpath(WORKSPACE_DIR)
    try:
        inside = os.path.commonpath([ws, src]) == ws
    except ValueError:
        inside = False
    if not inside:
        return
    msg = (
        f"bridge source ({src}) is INSIDE WORKSPACE_DIR ({ws}), which is mounted "
        f"RW into the agent container — the agent could rewrite bridge code that "
        f"the host then runs. Point WORKSPACE_DIR at a dedicated subdirectory "
        f"that excludes the bridge source."
    )
    if os.environ.get("WORKSPACE_STRICT"):
        sys.exit(f"[REFUSING TO START — S2] {msg}  (unset WORKSPACE_STRICT to override.)")
    log.warning("⚠️  SECURITY (S2): %s", msg)


def main() -> None:
    _ensure_single_instance()
    _warn_if_source_in_workspace()
    # SIGINT (Ctrl-C) works everywhere; SIGTERM lets systemd `stop` and
    # `docker stop`-style supervisors shut us down cleanly. Neither uses the
    # old SIGKILL path, so this is Windows-compatible.
    signal.signal(signal.SIGINT, _on_stop_signal)
    signal.signal(signal.SIGTERM, _on_stop_signal)
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )

    # Disable the websockets library's native ping — it's the root cause of the
    # recurring 'keepalive ping timeout' disconnects (Feishu server ignores
    # RFC 6455 pings; the lark SDK uses its own protobuf ping instead).
    _patch_websockets_no_native_ping()
    _patch_requests_timeout()

    log.info("Bridge up. Whitelisted user: %s | container: %s", ALLOWED_USER_ID, CONTAINER_NAME)

    # Start a recv-based watchdog daemon thread. If the connection goes
    # _WATCHDOG_TIMEOUT seconds without receiving ANY WS frame, the asyncio
    # loop is frozen or the connection is half-open — force-kill the process so
    # systemd restarts it with a clean connection. Healthy idle connections are
    # not mis-killed: the server keeps pushing pong/heartbeats (~every 120s).
    _start_connection_watchdog()

    # The lark SDK's reconnect loop is bounded by a server-controlled
    # ReconnectCount. When retries are exhausted it raises
    # ServerUnreachableException and ws.start() never returns. We wrap it in an
    # outer loop so the bridge re-establishes a fresh connection forever.
    import time as _time
    attempt = 0
    while True:
        attempt += 1
        try:
            ws = lark.ws.Client(
                APP_ID, APP_SECRET,
                event_handler=handler, log_level=lark.LogLevel.INFO,
            )
            # Record every inbound frame for the recv-based watchdog. Done per
            # instance; the SDK reuses this same `ws` across internal reconnects.
            _install_recv_tracker(ws)
            ws.start()  # blocks until connection is lost and reconnects exhaust
            # If start() returns normally (shouldn't happen), loop and reconnect.
            log.warning("ws.start() returned unexpectedly, reconnecting (attempt %d)", attempt)
        except KeyboardInterrupt:
            raise
        except SystemExit:
            raise
        except Exception as e:
            # ServerUnreachableException or any other failure: back off and retry.
            log.error("ws connection lost (attempt %d): %s — retrying in 30s", attempt, e)
            _time.sleep(30)


if __name__ == "__main__":
    main()
