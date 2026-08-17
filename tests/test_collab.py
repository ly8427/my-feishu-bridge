#!/usr/bin/env python3
"""test_collab.py — automated collab tests (no human, no real Feishu cards).

Layers:
  A  — pure functions: parse_spec / parse_verdict / command_blocked
  B1 — pipeline state machine with a MOCKED _start_turn: asserts the
       parameters each step passes (engine/model/hold_lock/no_resume/
       persist_session/tools_mode/run_id...)
  B2 — integration with REAL _start_turn + FakePopen: exercises the genuine
       drain/render/done/T1 threading; asserts real _active_chats held for
       the whole pipeline then released, session_store NOT polluted by the
       reviewer, ok:false error path, stop path (container-kill helper called
       with the right run_id), gate wiring, and the deterministic injection
       defenses (delimiters + contract placement in the composed prompts).

Usage: cd <bridge dir> && FEISHU_ENV_FILE=~/.secrets/feishu-bridge.env \
       .venv/bin/python3 tests/test_collab.py
"""
import json
import os
import queue as queue_mod
import sys
import threading
import time
import types
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not os.environ.get("FEISHU_ENV_FILE"):
    os.environ["FEISHU_ENV_FILE"] = os.path.expanduser("~/.secrets/feishu-bridge.env")

import bridge  # noqa: E402
import collab  # noqa: E402
import session_store  # noqa: E402

collab.init(bridge)

PASS = 0
FAIL = 0
CARDS: list[str] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def last_card():
    return CARDS[-1] if CARDS else ""


def patch_cards():
    CARDS.clear()
    bridge._post_card = lambda chat_id, text, buttons=None: CARDS.append(text) or "fake_mid"
    bridge._patch_card = lambda mid, text, buttons=None: CARDS.append(text)


def flush_workers():
    bridge._card_workers.shutdown(wait=True)
    from concurrent.futures import ThreadPoolExecutor
    bridge._card_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="card")


def send(text, chat="test_collab"):
    ev = SimpleNamespace(
        sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=bridge.ALLOWED_USER_ID)),
        message=SimpleNamespace(chat_id=chat, message_type="text",
                                content=json.dumps({"text": text})),
    )
    bridge.on_message(SimpleNamespace(event=ev))
    flush_workers()


# ══════════════════════════════════════════════ A. pure functions
def test_parse_spec():
    s, e = collab.parse_spec('impl=pi/deepseek review=pi/glm rounds=3 gate=true "写脚本 a=b"')
    check("A1 full spec", not e and s["impl_engine"] == "pi" and s["impl_provider"] == "deepseek"
          and s["review_provider"] == "glm" and s["rounds"] == 3 and s["gate"] is True
          and s["task"] == "写脚本 a=b", f"{e} {s}")
    s, e = collab.parse_spec('"解析 a=b c=d 的脚本"')
    check("A2 quoted key=value stays task", not e and s["task"] == "解析 a=b c=d 的脚本"
          and s["impl_engine"] == "claude", f"{e} {s}")
    s, e = collab.parse_spec("review=claude \"x\"")
    check("A3 review whitelist rejects claude", s is None and "白名单" in e, f"{s} {e}")
    s, e = collab.parse_spec("impl=opencode \"x\"")
    check("A4 impl whitelist rejects opencode", s is None and "白名单" in e)
    s, e = collab.parse_spec("rounds=9 \"x\"")
    check("A5 rounds bounds", s is None)
    s, e = collab.parse_spec("gate=yes \"x\"")
    check("A6 gate value", s is None)
    s, e = collab.parse_spec("bogus=1 \"x\"")
    check("A7 unknown key", s is None and "未知参数" in e)
    s, e = collab.parse_spec("")
    check("A8 empty → usage", s is None and "用法" in e)
    s, e = collab.parse_spec("裸任务词")
    check("A9 unquoted fallback task", not e and s["task"] == "裸任务词", f"{e}")


def test_parse_verdict():
    v, i, ok = collab.parse_verdict('意见...\n```json\n{"verdict":"approve","issues":[]}\n```')
    check("A10 fence approve", v == "approve" and ok)
    v, i, ok = collab.parse_verdict('x\n```json\n{"verdict":"revise","issues":["p1","p2"]}\n```')
    check("A11 fence revise issues", v == "revise" and i == ["p1", "p2"] and ok)
    txt = '先 {"verdict":"approve","issues":[]} 又 ```json\n{"verdict":"revise","issues":["later"]}\n```'
    v, i, ok = collab.parse_verdict(txt)
    check("A12 LAST candidate wins", v == "revise" and i == ["later"], f"{v} {i}")
    v, i, ok = collab.parse_verdict('文字 {"verdict":"approve","issues":[]} 文字')
    check("A13 flat object tier", v == "approve" and ok)
    v, i, ok = collab.parse_verdict('{"verdict": broken')
    check("A14 malformed → fail-safe revise", v == "revise" and not ok and i)
    v, i, ok = collab.parse_verdict("")
    check("A15 empty → fail-safe", v == "revise" and not ok)
    v, i, ok = collab.parse_verdict("垃圾" * 5000)
    check("A16 issues capped 2000", len(i[0]) == collab.ISSUES_MAX)


def test_command_blocked():
    chat = "cb_chat"
    send("/engine pi", chat)  # ensure engine var irrelevant here
    check("A17 not blocked without pipeline",
          not collab.command_blocked(chat, "/new"))
    fake = SimpleNamespace(stop=lambda: None)
    with collab._registry_lock:
        collab._registry[chat] = {"pipeline": fake, "done": False}
    for cmd in ("/new", "/reset", "/engine claude", "/provider deepseek",
                "/model x", "/resume 1", "/resume clear", "/tools safe", "/thinking high"):
        check(f"A18 blocked {cmd.split()[0]}", collab.command_blocked(chat, cmd))
    check("A19 plain text not blocked", not collab.command_blocked(chat, "普通消息"))
    check("A20 /collab status not blocked", not collab.command_blocked(chat, "/collab status"))
    with collab._registry_lock:
        del collab._registry[chat]


# ══════════════════════════════════════════════ B1. mocked _start_turn
def test_b1_pipeline_params():
    calls = []

    class FakeJob:
        def __init__(self, result, ok=True, sid="sid-"):
            self.result_text, self.ok, self.session_id = result, ok, sid
            self.done = threading.Event(); self.done.set()
            self.run_id = ""

    seq = {"n": 0}

    def fake_start(chat_id, prompt, engine, **kw):
        calls.append({"engine": engine, "prompt": prompt, **kw})
        seq["n"] += 1
        if seq["n"] % 2 == 1:  # impl
            return FakeJob("impl output round %d" % ((seq["n"] + 1) // 2), sid="impl-sid")
        # review: first revise, second approve
        verdict = "revise" if seq["n"] == 2 else "approve"
        return FakeJob(json.dumps({"verdict": verdict, "issues": ["问题A"]}))

    orig = bridge._start_turn
    bridge._start_turn = fake_start
    patch_cards()
    try:
        spec, err = collab.parse_spec('impl=pi/deepseek review=pi/zhipu rounds=2 "任务X"')
        assert not err
        p = collab.CollabPipeline("b1_chat", spec)
        p._run()  # synchronous, no thread
    finally:
        bridge._start_turn = orig

    impl_calls = [c for c in calls if c["engine"] == "pi" and not c["no_resume"]]
    rev_calls = [c for c in calls if c["no_resume"]]
    check("B1 step count (2 impl + 2 review)", len(calls) == 4, str(len(calls)))
    check("B1 impl hold_lock", all(c["hold_lock"] for c in calls))
    check("B1 impl resume+persist", impl_calls and all(c["persist_session"] for c in impl_calls)
          and all(not c["no_resume"] for c in impl_calls))
    check("B1 impl model from spec provider", impl_calls[0]["model"].startswith(
        bridge._PROVIDERS["deepseek"]["models"][0].rsplit("/", 1)[-1][:6]) or
        impl_calls[0]["model"] == bridge._PROVIDERS["deepseek"]["models"][0].rsplit("/", 1)[-1],
        impl_calls[0]["model"])
    check("B1 review cold", rev_calls and all(c["no_resume"] and not c["persist_session"]
                                              for c in rev_calls))
    check("B1 review tools safe", all(c["tools_mode"] == "safe" for c in rev_calls))
    check("B1 review persona injected", all(c["system_prompt"] and
          "IMPL_OUTPUT" in c["system_prompt"] and '"verdict"' in c["system_prompt"]
          for c in rev_calls))
    check("B1 review contract is LAST line",
          all(c["system_prompt"].strip().splitlines()[-1].startswith('{"verdict"')
              for c in rev_calls))
    check("B1 run_id present", all(c.get("run_id") for c in calls))
    check("B1 feedback prompt carries issues",
          "问题A" in calls[2]["prompt"] if len(calls) > 2 else False, calls[2]["prompt"][:80])
    check("B1 final approved", p.status == "approved", p.status)
    check("B1 delimiters around impl output",
          "<IMPL_OUTPUT_START>" in calls[1]["prompt"], calls[1]["prompt"][:120])


# ══════════════════════════════════════════════ B2. real threads + FakePopen
class FakeProc:
    """Popen stand-in streaming preset JSON lines. Behavior per step is chosen
    by inspecting the docker cmd (engine script + prompt marker)."""

    def __init__(self, cmd, verdicts, block=False):
        self.cmd = cmd
        self.block = block
        self.killed = threading.Event()
        self.stdin = SimpleNamespace(write=lambda s: None, flush=lambda: None,
                                     close=lambda: None)
        self._q = queue_mod.Queue()
        prompt = ""
        if "--prompt" in cmd:
            prompt = cmd[cmd.index("--prompt") + 1]
        is_review = "agent_runner_pi.py" in " ".join(cmd) and "协作审查 ·" in prompt
        if self.block:
            return  # no output until kill
        lines = []
        if is_review:
            v = verdicts["list"].pop(0)
            lines = [
                json.dumps({"type": "session", "session_id": "review-cold-sid-0001"}),
                json.dumps({"type": "text", "text": "审查中…"}),
                json.dumps({"type": "result", "ok": True, "session_id": "review-cold-sid-0001",
                            "text": '意见\n```json\n{"verdict":"%s","issues":["问题1"]}\n```' % v}),
            ]
        else:
            n = getattr(FakeProc, "impl_n", 0) + 1
            FakeProc.impl_n = n
            lines = [
                json.dumps({"type": "session", "session_id": "impl-sid-000%d" % n}),
                json.dumps({"type": "text", "text": "实现输出"}),
                json.dumps({"type": "result", "ok": True, "session_id": "impl-sid-000%d" % n,
                            "text": "IMPL_RESULT_%d" % n}),
            ]
        for ln in lines:
            self._q.put(ln + "\n")
        self._q.put(None)  # EOF

    # stdout protocol: iterate lines
    @property
    def stdout(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        if self.killed.is_set():
            raise StopIteration
        item = self._q.get()
        if item is None:
            raise StopIteration
        return item

    def poll(self):
        return None if not self._q.empty() or not self.killed.is_set() else 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed.set()
        self._q.put(None)


def run_b2(spec_text, review_verdicts, chat, stop_early=False, gate=False):
    """Drive a REAL CollabPipeline through real _start_turn with FakePopen."""
    patch_cards()
    session_store.clear(f"pi:{chat}")
    session_store.clear(f"claude:{chat}")
    kill_calls = []
    # SHARED holder: FakePopen instances pop from this dict so verdicts
    # sequence correctly across steps (a plain list would be copied per call).
    verdicts = {"list": list(review_verdicts)}
    orig_popen, orig_kill = bridge.subprocess.Popen, collab.kill_container_processes
    bridge.subprocess.Popen = lambda cmd, **kw: FakeProc(cmd, verdicts)
    collab.kill_container_processes = lambda rid, cont: kill_calls.append(rid)

    spec, err = collab.parse_spec(spec_text + (f' gate={str(gate).lower()}' if gate else ""))
    assert not err, err
    p = collab.CollabPipeline(chat, spec)
    collab._register(chat, p)
    t = threading.Thread(target=p._run, daemon=True)
    t.start()
    if stop_early:
        deadline = time.time() + 10
        while time.time() < deadline and not any("实现中" in c or "实现（" in c for c in CARDS):
            time.sleep(0.05)
        held = chat in bridge._active_chats
        p.stop()
        t.join(20)
        return p, kill_calls, held
    if gate:
        # wait for gate card then tap 继续
        deadline = time.time() + 10
        while time.time() < deadline:
            with bridge._gate_lock:
                if bridge._gate_waiters:
                    gid, cb = next(iter(bridge._gate_waiters.items()))
                    bridge._gate_waiters.pop(gid)
                    cb(True)
                    break
            time.sleep(0.05)
    t.join(60)
    bridge.subprocess.Popen = orig_popen
    collab.kill_container_processes = orig_kill
    return p, kill_calls, chat in bridge._active_chats


def test_b2_happy_and_locks():
    FakeProc.impl_n = 0
    before_pi = session_store.get("pi:b2_chat")
    p, kills, _ = run_b2('impl=pi/deepseek review=pi/zhipu rounds=2 "任务B2"',
                         review_verdicts=["revise", "approve"], chat="b2_chat")
    check("B2 approved after revise→approve", p.status == "approved", p.status)
    check("B2 T1 released", "b2_chat" not in bridge._active_chats)
    after_pi = session_store.get("pi:b2_chat")
    check("B2 impl session persisted", after_pi and after_pi.startswith("impl-sid"),
          str(after_pi))
    check("B2 review did NOT overwrite session", after_pi != "review-cold-sid-0001")
    check("B2 registry marked done", collab._registry["b2_chat"]["done"])
    check("B2 no container-kill on clean run", kills == [], str(kills))


def test_b2_ok_false_error():
    # patch: make impl result ok=False by rewriting queue on impl steps
    orig_init = FakeProc.__init__

    def err_init(self, cmd, verdicts, block=False):
        orig_init(self, cmd, verdicts, block)
        prompt = cmd[cmd.index("--prompt") + 1] if "--prompt" in cmd else ""
        if "协作审查 ·" not in prompt:  # impl or feedback step → inject failure
            # replace queued lines with an ok:false result
            items = []
            while True:
                try:
                    items.append(self._q.get_nowait())
                except queue_mod.Empty:
                    break
            self._q.put(json.dumps({"type": "session", "session_id": "impl-err-sid"}) + "\n")
            self._q.put(json.dumps({"type": "result", "ok": False,
                                    "text": "", "error": "模型调用失败"}) + "\n")
            self._q.put(None)

    FakeProc.__init__ = err_init
    try:
        p, _, _ = run_b2('impl=pi/deepseek review=pi/zhipu rounds=2 "任务E"',
                         review_verdicts=["approve"], chat="b2e_chat")
    finally:
        FakeProc.__init__ = orig_init
    check("B2 ok:false → error status", p.status == "error" and "模型调用失败" in p.error,
          f"{p.status} {p.error}")
    check("B2 error path releases T1", "b2e_chat" not in bridge._active_chats)


def test_b2_stop_path():
    orig_popen = bridge.subprocess.Popen
    kill_calls = []
    bridge.subprocess.Popen = lambda cmd, **kw: FakeProc(cmd, [], block=True)
    collab_kill_orig = collab.kill_container_processes
    collab.kill_container_processes = lambda rid, cont: kill_calls.append(rid)
    patch_cards()
    try:
        spec, err = collab.parse_spec('impl=pi/deepseek review=pi/zhipu rounds=2 "任务S"')
        p = collab.CollabPipeline("b2s_chat", spec)
        collab._register("b2s_chat", p)
        t = threading.Thread(target=p._run, daemon=True)
        t.start()
        deadline = time.time() + 10
        while time.time() < deadline and "b2s_chat" not in bridge._active_chats:
            time.sleep(0.05)
        held = "b2s_chat" in bridge._active_chats
        p.stop()
        t.join(20)
    finally:
        bridge.subprocess.Popen = orig_popen
        collab.kill_container_processes = collab_kill_orig
    check("B2 T1 held during run", held)
    check("B2 stop → container kill called with run_id",
          len(kill_calls) == 1 and kill_calls[0], str(kill_calls))
    check("B2 stop status", p.status == "stopped", p.status)
    check("B2 stop releases T1", "b2s_chat" not in bridge._active_chats)


def test_b2_gate():
    FakeProc.impl_n = 0
    p, _, _ = run_b2('impl=pi/deepseek review=pi/zhipu rounds=2 "任务G"',
                     review_verdicts=["revise", "approve"], chat="b2g_chat", gate=True)
    check("B2 gate continue → approved", p.status == "approved", p.status)
    check("B2 gate card posted", any("门控" in c for c in CARDS))
    check("B2 gate waiter cleared", not bridge._gate_waiters)


def test_b2_bridge_dispatch():
    patch_cards()
    send("/collab bogus \"x\"")
    # unknown key parse error handled inside handle_collab (worker thread)
    time.sleep(0.3)
    check("B2 dispatch invalid → card", any(("未知参数" in c or "❌" in c) for c in CARDS),
          last_card()[:80])
    send("/collab status")
    time.sleep(0.3)
    check("B2 status card", any("协作流水线" in c or "没有协作流水线记录" in c for c in CARDS))


def main():
    for fn in (test_parse_spec, test_parse_verdict, test_command_blocked,
               test_b1_pipeline_params, test_b2_happy_and_locks,
               test_b2_ok_false_error, test_b2_stop_path, test_b2_gate,
               test_b2_bridge_dispatch):
        print(f"=== {fn.__name__} ===")
        try:
            fn()
        except Exception as exc:
            global FAIL
            FAIL += 1
            print(f"  ❌ EXCEPTION: {type(exc).__name__}: {exc}")
    print(f"\n=== RESULT: {PASS} passed, {FAIL} failed ===")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
