#!/usr/bin/env python3
"""test_bridge_pi_v2.py — bridge-level automated tests (no human interaction).

Imports bridge.py WITHOUT starting the Feishu WS loop (main() is never called),
monkeypatches _post_card/_patch_card to record instead of sending real cards,
then drives on_message() with fabricated event objects and asserts on card
content + session_store state.

Usage:  cd <bridge dir> && FEISHU_ENV_FILE=~/.secrets/feishu-bridge.env \
        .venv/bin/python3 tests/test_bridge_pi_v2.py
"""
import json
import os
import sys
import types
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not os.environ.get("FEISHU_ENV_FILE"):
    os.environ["FEISHU_ENV_FILE"] = os.path.expanduser("~/.secrets/feishu-bridge.env")

import bridge  # noqa: E402  (module init loads env, providers; no WS)
import session_store  # noqa: E402

PASS = 0
FAIL = 0
CARDS: list[str] = []          # captured card texts
TEST_CHAT = "test_chat_pi_v2"

# ---- fixtures -------------------------------------------------------------
SESSIONS_BAK = None
if os.path.exists(session_store._PATH):
    with open(session_store._PATH, encoding="utf-8") as f:
        SESSIONS_BAK = f.read()


def setup():
    CARDS.clear()
    # Replace card senders with recorders (no real Feishu HTTP).
    bridge._post_card = lambda chat_id, text, buttons=None: CARDS.append(text) or "fake_mid"
    bridge._patch_card = lambda mid, text, buttons=None: CARDS.append(text)
    # Reset per-chat in-memory state for the test chat.
    bridge._chat_engine.pop(TEST_CHAT, None)
    bridge._chat_thinking.pop(TEST_CHAT, None)
    bridge._chat_tools.pop(TEST_CHAT, None)
    for ns in ("claude", "opencode", "pi"):
        session_store.clear(f"{ns}:{TEST_CHAT}")
    bridge._session_list_cache.pop(TEST_CHAT, None)


def teardown():
    for ns in ("claude", "opencode", "pi"):
        session_store.clear(f"{ns}:{TEST_CHAT}")
    bridge._session_list_cache.pop(TEST_CHAT, None)
    # Restore sessions.json exactly as it was.
    if SESSIONS_BAK is not None:
        with open(session_store._PATH, "w", encoding="utf-8") as f:
            f.write(SESSIONS_BAK)


def send(text: str):
    """Fabricate a P2ImMessageReceiveV1-like object and call on_message."""
    ev = SimpleNamespace(
        sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=bridge.ALLOWED_USER_ID)),
        message=SimpleNamespace(chat_id=TEST_CHAT, message_type="text",
                                content=json.dumps({"text": text})),
    )
    bridge.on_message(SimpleNamespace(event=ev))
    # Slash-command handlers run via _card_workers pool → flush it.
    bridge._card_workers.shutdown(wait=True)
    # The pool is one-shot after shutdown; rebuild for the next send.
    from concurrent.futures import ThreadPoolExecutor
    bridge._card_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="card")


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def last_card() -> str:
    return CARDS[-1] if CARDS else ""


# ---- tests ----------------------------------------------------------------
def test_engine_pi():
    send("/engine pi")
    check("engine switch", "引擎已切换: pi" in last_card())
    check("engine state", bridge._chat_engine.get(TEST_CHAT) == "pi")


def test_status():
    send("/status")
    c = last_card()
    check("status shows engine", "引擎: `pi`" in c, c[:120])
    check("status shows provider", "厂商:" in c)
    check("status shows model", "模型:" in c)
    check("status shows pi extras", "思考等级" in c and "工具模式" in c)


def test_thinking():
    send("/thinking high")
    check("thinking set", bridge._chat_thinking.get(TEST_CHAT) == "high")
    check("thinking card", "思考等级已设置" in last_card())
    send("/thinking bogus")
    check("thinking invalid rejected", "无效等级" in last_card())
    bridge._chat_thinking.pop(TEST_CHAT)


def test_tools():
    send("/tools safe")
    check("tools safe set", bridge._chat_tools.get(TEST_CHAT) == "safe")
    check("tools safe card", "只读模式" in last_card())
    send("/tools full")
    check("tools full set", bridge._chat_tools.get(TEST_CHAT) == "full")
    send("/tools bogus")
    check("tools invalid rejected", "无效模式" in last_card())


def test_model_validation():
    send("/model foo/bar")
    check("pi model with slash rejected", "不带 `/`" in last_card())
    send("/model has space")
    check("model with space rejected", "不能含空格" in last_card())
    send("/model deepseek-v4-flash")
    check("valid model accepted", "模型已切换" in last_card())
    bridge._chat_model.pop(TEST_CHAT)


def test_sessions_pi():
    send("/sessions")
    c = last_card()
    ok = ("会话列表" in c) or ("暂无 pi 历史会话" in c)
    check("pi sessions listed", ok, c[:120])
    cached = bridge._session_list_cache.get(TEST_CHAT, {})
    check("cache engine-keyed pi", cached.get("engine") == "pi")


def test_resume_by_index():
    cached = bridge._session_list_cache.get(TEST_CHAT, {})
    sessions = cached.get("sessions") or []
    send("/resume 1")
    if sessions:
        want = sessions[0]["session_id"]
        got = session_store.get(f"pi:{TEST_CHAT}")
        check("resume 1 stores first id", got == want, f"want={want} got={got}")
    else:
        check("resume with empty list handled", "超出范围" in last_card() or "无当前引擎" in last_card())


def test_resume_clear():
    send("/resume clear")
    check("resume clear", session_store.get(f"pi:{TEST_CHAT}") is None)


def test_resume_engine_guard():
    # Cache was built under pi; switching engines must invalidate it.
    bridge._chat_engine[TEST_CHAT] = "claude"
    send("/resume 1")
    check("cross-engine resume rejected", ("无当前引擎" in last_card()) or ("超出范围" in last_card()))
    bridge._chat_engine[TEST_CHAT] = "pi"


def test_new_clears_pi():
    session_store.put(f"pi:{TEST_CHAT}", "deadbeef-cafe")
    send("/new")
    check("/new clears pi ns", session_store.get(f"pi:{TEST_CHAT}") is None)


def main():
    setup()
    try:
        for fn in (test_engine_pi, test_status, test_thinking, test_tools,
                   test_model_validation, test_sessions_pi, test_resume_by_index,
                   test_resume_clear, test_resume_engine_guard, test_new_clears_pi):
            print(f"=== {fn.__name__} ===")
            fn()
    finally:
        teardown()
    print(f"\n=== RESULT: {PASS} passed, {FAIL} failed ===")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
