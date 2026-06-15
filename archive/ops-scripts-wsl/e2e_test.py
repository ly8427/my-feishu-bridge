#!/usr/bin/env python3
"""
End-to-end test: runs agent_runner_opencode directly and checks if
confirm_request is emitted for dangerous tools (Write, Bash, Edit).

Usage: wsl -d Ubuntu-24.04 -e python3 /mnt/c/Users/liu/ZCodeProject/e2e_test.py
"""
import subprocess, sys, os, json, time, threading

ENV_FILE = "/home/liu/.secrets/feishu-bridge.env"

def load_env():
    """Load env vars from .env file."""
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

def run_test(label: str, prompt: str, expected_tools: list = None):
    """Run agent_runner_opencode with a prompt and capture output."""
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"PROMPT: {prompt}")
    print(f"{'='*60}")

    # Build docker exec command like bridge does
    cmd = ["docker", "exec", "-i"]
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        if os.environ.get(var):
            cmd += ["-e", f"{var}={os.environ[var]}"]
    for var in ("OPENCODE_API_KEY", "OPENCODE_API_URL", "OPENCODE_MODEL", "ZHIPU_API_KEY"):
        if os.environ.get(var):
            cmd += ["-e", f"{var}={os.environ[var]}"]
    if os.environ.get("OPENCODE_API_KEY") and not os.environ.get("ZHIPU_API_KEY"):
        cmd += ["-e", f"ZHIPU_API_KEY={os.environ['OPENCODE_API_KEY']}"]
    if os.environ.get("OPENCODE_BIN"):
        cmd += ["-e", f"OPENCODE_BIN={os.environ['OPENCODE_BIN']}"]

    cmd += [
        "-e", "SAFE_TOOLS=Read,Grep,Glob,WebSearch,WebFetch,TodoWrite,NotebookRead",
        "-e", "CONFIRM_TIMEOUT=300",
        "-e", f"WORKSPACE_DIR={os.environ.get('WORKSPACE_DIR', '/home/liu/projects/claudeWorkSpace')}",
        os.environ.get("CONTAINER_NAME", "feishu-claude-agent"),
        "python3", "/app/agent_runner_opencode.py", "--prompt", prompt,
    ]

    print(f"CMD: {' '.join(cmd[:6])} ... --prompt '{prompt[:40]}...'")
    t0 = time.time()

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    events = []
    confirm_requests = []
    replied_confirm_ids = set()
    errors = []
    text_output = []
    session_id = None
    phase_markers = []

    # Auto-reply thread: when confirm_request detected, reply via stdin
    def _auto_replier():
        while proc.poll() is None:
            for _, cr in list(confirm_requests):
                cid = cr["id"]
                if cid not in replied_confirm_ids:
                    reply = json.dumps({"type": "confirm_reply", "id": cid, "allow": True}) + "\n"
                    try:
                        proc.stdin.write(reply)
                        proc.stdin.flush()
                        print(f"  🔄 AUTO-REPLIED: allow {cid}")
                        replied_confirm_ids.add(cid)
                    except:
                        pass
            time.sleep(0.1)

    replier = threading.Thread(target=_auto_replier, daemon=True)
    replier.start()

    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            elapsed = time.time() - t0
            try:
                msg = json.loads(line)
                mtype = msg.get("type", "")
                if mtype == "_phase":
                    phase_markers.append((elapsed, msg))
                    print(f"  [{elapsed:6.2f}s] PHASE: {msg['name']}")
                elif mtype == "confirm_request":
                    confirm_requests.append((elapsed, msg))
                    print(f"  [{elapsed:6.2f}s] ✅ CONFIRM_REQUEST: tool={msg.get('tool')} id={msg.get('id')}")
                elif mtype == "error":
                    errors.append((elapsed, msg))
                    print(f"  [{elapsed:6.2f}s] ❌ ERROR: {msg.get('message','')[:120]}")
                elif mtype == "result":
                    print(f"  [{elapsed:6.2f}s] 🏁 RESULT: ok={msg.get('ok')} text={msg.get('text','')[:60]}")
                elif mtype == "session":
                    session_id = msg.get("session_id")
                elif mtype == "tool":
                    print(f"  [{elapsed:6.2f}s] 🔧 TOOL: {msg.get('name')} - {msg.get('brief','')[:80]}")
                elif mtype == "text":
                    text_output.append(msg.get("text", ""))
                events.append((elapsed, mtype, msg))
            except json.JSONDecodeError:
                # stderr debug lines
                print(f"  [{elapsed:6.2f}s] 📝 STDERR: {line[:120]}")
    except Exception as e:
        print(f"  EXCEPTION reading stdout: {e}")

    total_time = time.time() - t0
    proc.wait(timeout=5)

    # Summary
    print(f"\n--- RESULTS for '{label}' ({total_time:.1f}s) ---")
    print(f"  Total events: {len(events)}")
    print(f"  Phase markers: {len(phase_markers)}")
    print(f"  Confirm requests: {len(confirm_requests)}")
    print(f"  Errors: {len(errors)}")
    print(f"  Session ID: {session_id or 'NONE'}")
    print(f"  Text output chars: {sum(len(t) for t in text_output)}")

    if confirm_requests:
        for elapsed, cr in confirm_requests:
            print(f"  ✅ CONFIRM CARD would appear at +{elapsed:.1f}s: {cr.get('tool')}")

    if not confirm_requests:
        print(f"  ⚠️  NO CONFIRM REQUEST emitted! Tool executed without user confirmation.")
        print(f"  This is the BUG: dangerous tools should trigger confirm_request.")
    else:
        print(f"  ✅ OK: {len(confirm_requests)} confirm request(s) emitted")

    # Auto-reply to confirm so the agent can finish
    for _, cr in confirm_requests:
        cid = cr["id"]
        reply = json.dumps({"type": "confirm_reply", "id": cid, "allow": True}) + "\n"
        try:
            proc.stdin.write(reply)
            proc.stdin.flush()
        except:
            pass

    return {
        "confirm_requests": confirm_requests,
        "errors": errors,
        "total_time": total_time,
        "session_id": session_id,
    }


def main():
    load_env()
    print(f"Model: {os.environ.get('OPENCODE_MODEL', '?')}")
    print(f"Container: {os.environ.get('CONTAINER_NAME', 'feishu-claude-agent')}")

    # Test 1: Simple file creation (should trigger Write confirm)
    r1 = run_test(
        "Create file (should trigger Write confirm)",
        "在当前目录创建文件 e2e_test1.txt，写入 hello world"
    )

    # Check if file was created
    print("\n--- Checking file creation ---")
    subprocess.run(
        ["docker", "exec", os.environ.get("CONTAINER_NAME", "feishu-claude-agent"),
         "cat", f"{os.environ.get('WORKSPACE_DIR', '/home/liu/projects/claudeWorkSpace')}/e2e_test1.txt"],
        stderr=subprocess.DEVNULL
    )

    # Summary
    print("\n" + "="*60)
    print("FINAL SUMMARY")
    print("="*60)
    if r1["confirm_requests"]:
        print("✅ Test 1 PASS: confirm_request was emitted")
    else:
        print("❌ Test 1 FAIL: no confirm_request emitted (the bug!)")

    if r1["errors"]:
        print(f"⚠️  Errors during test: {len(r1['errors'])}")
        for _, err in r1["errors"]:
            print(f"   - {err.get('message', '?')[:100]}")


if __name__ == "__main__":
    main()
