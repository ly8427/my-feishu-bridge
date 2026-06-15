#!/usr/bin/env python3
"""
cli.py — cross-platform manager for the feishu-bridge.

Replaces the grab-bag of host-specific shell scripts (restart_all.sh,
kill_all_bridge.sh, etc.) with a single entry point that works on
Windows / macOS / Linux. Pure-Python: no bash, pgrep, /proc, or systemd
assumption. Anything the agent_readme.md needs to run automatically goes
through here so an agent drives the bridge by `subprocess` instead of
spelling out platform shell.

Subcommands:
  check      Environment self-check (docker? credentials? container? bridge?)
  up         Build & start the agent container (docker compose up -d --build)
  down       Stop & remove the agent container (docker compose down)
  start      Start the bridge.py host process (--detach to background it)
  stop       Stop the running bridge.py (PID-lock based, cross-platform)
  restart    stop then start
  status     Show bridge process + agent container state
  logs       Tail bridge.log (and optionally --container agent logs)
  doctor     Deep diagnosis with suggested fixes (same checks as check, verbose)

All docker interaction goes through the `docker` executable on PATH
(Docker Desktop on Win/Mac, docker engine on Linux). The bridge PID lock
file (bridge.pid, written by bridge.py) is the single source of truth for
"bridge running?" — never ps/pgrep.

Exit codes: 0 = ok, 1 = check failed / actionable problem, 2 = usage error.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------- paths
HERE = Path(__file__).resolve().parent
BRIDGE_PY = HERE / "bridge.py"
COMPOSE_FILE = HERE / "docker" / "docker-compose.yml"
ENV_EXAMPLE = HERE / ".env.example"
BRIDGE_LOG = HERE / "bridge.log"
PID_FILE = HERE / "bridge.pid"


# ------------------------------------------------------------------- helpers
def _env_file() -> Path:
    """Resolve the secrets env file the same way bridge.py does.

    Order: $FEISHU_ENV_FILE → ./​.env → ~/.secrets/feishu-bridge.env (the
    standard location the README and the systemd unit both point at).
    """
    p = os.environ.get("FEISHU_ENV_FILE")
    if p:
        return Path(p).expanduser()
    local = HERE / ".env"
    if local.exists():
        return local
    return Path.home() / ".secrets" / "feishu-bridge.env"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command, capturing output. Never raises — caller inspects rc."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 60)
    try:
        return subprocess.run(cmd, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found on PATH")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{cmd[0]}: timed out")


def _ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


def _fail(msg: str) -> None:
    print(f"  \033[31m✗\033[0m {msg}")


def _warn(msg: str) -> None:
    print(f"  \033[33m!\033[0m {msg}")


def _info(msg: str) -> None:
    print(f"    {msg}")


def _is_windows() -> bool:
    return sys.platform == "win32"


def _python() -> str:
    """Python interpreter to launch bridge.py with (this process's exe)."""
    return sys.executable or "python3"


# ----------------------------------------------------- check building blocks
def _check_python() -> tuple[bool, str]:
    ver = sys.version_info
    ok = ver >= (3, 10)
    return ok, f"Python {ver.major}.{ver.minor}.{ver.micro} ({sys.executable})"


def _check_docker() -> tuple[bool, str]:
    r = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if r.returncode != 0:
        return False, f"docker not usable: {r.stderr.strip()[:160]}"
    return True, f"docker server {r.stdout.strip()}"


def _check_compose() -> tuple[bool, str]:
    # `docker compose` (v2 plugin) first; fall back to legacy `docker-compose`.
    r = _run(["docker", "compose", "version", "--short"])
    if r.returncode == 0:
        return True, f"docker compose v2 {r.stdout.strip()}"
    r2 = _run(["docker-compose", "version", "--short"])
    if r2.returncode == 0:
        return True, f"docker-compose v1 {r2.stdout.strip()}"
    return False, "docker compose plugin not found"


def _env_keys() -> dict[str, str]:
    """Load KEY=VALUE from the env file (does NOT touch os.environ)."""
    path = _env_file()
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _check_credentials() -> tuple[bool, str]:
    path = _env_file()
    if not path.exists():
        return False, f"env file missing: {path}"
    keys = _env_keys()
    required = ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "ALLOWED_USER_ID"]
    missing = [k for k in required if not keys.get(k) or keys[k].startswith("xxx")]
    if missing:
        return False, f"env file present but missing/placeholder: {', '.join(missing)}"
    return True, f"env file ok: {path}"


def _container_name() -> str:
    return _env_keys().get("CONTAINER_NAME") or os.environ.get(
        "CONTAINER_NAME", "feishu-claude-agent"
    )


def _check_container() -> tuple[bool, str]:
    name = _container_name()
    r = _run(["docker", "inspect", "-f", "{{.State.Status}}", name])
    state = r.stdout.strip()
    if r.returncode != 0 or not state:
        return False, f"container '{name}' not found (run: python cli.py up)"
    if state != "running":
        return False, f"container '{name}' state is '{state}' (not running)"
    return True, f"container '{name}' running"


def _bridge_pid() -> int | None:
    """Return the PID written by bridge.py, or None if no PID file / stale.

    NOTE: a stale PID file can remain if a previous instance was killed with
    SIGKILL before bridge.py learned to release its lock. We treat the PID as
    "live" only if the process actually exists. Cross-platform liveness uses
    no signals: on Windows we can't signal arbitrary PIDs safely, so we probe
    via the lock-holding semantics instead — see _bridge_running().
    """
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    """Is the given PID a live process? Cross-platform (no ps/pgrep).

    os.kill(pid, 0) is supported on all platforms: it raises ProcessLookupError
    if the PID is gone, PermissionError if it exists but is not ours. Both mean
    "alive from our perspective" for a bridge we started ourselves.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by another user — still "alive".
        return True
    except OSError:
        return False


def _bridge_running() -> bool:
    """Is the bridge host process alive? Determined without ps/pgrep.

    Two probes, most reliable first:
      1. Advisory-lock probe (the mechanism the NEW bridge.py uses): if the lock
         is busy, a bridge holding it is live.
      2. PID-liveness fallback for bridges started by an OLDER bridge.py (or by
         systemd) that wrote a PID file but never took the lock. This keeps
         `stop`/`status` honest during the transition before a restart.
    """
    pid = _bridge_pid()
    if pid is None:
        return False
    if pid == os.getpid():
        return False

    # Probe 1: advisory lock (the new bridge.py's source of truth).
    try:
        fh = open(PID_FILE, "a+")
    except OSError:
        # Can't even open the PID file — fall back to PID liveness.
        return _pid_alive(pid)
    try:
        if _is_windows():
            import msvcrt
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                lock_free = True
            except OSError:
                lock_free = False  # held by a live bridge
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                lock_free = True
            except OSError:
                lock_free = False
    finally:
        fh.close()
    if not lock_free:
        return True
    # Lock is free — either no bridge, or an old bridge that doesn't hold it.
    # Fall back to checking whether the recorded PID is actually alive.
    return _pid_alive(pid)


def _check_bridge() -> tuple[bool, str]:
    if _bridge_running():
        pid = _bridge_pid()
        return True, f"bridge.py running (PID {pid})"
    return False, "bridge.py not running (start: python cli.py start --detach)"


def _check_opencode_auth() -> tuple[bool, str]:
    """OpenCode auth.json — only required when ENGINE=opencode is in use."""
    keys = _env_keys()
    engine = (os.environ.get("ENGINE") or keys.get("ENGINE") or "claude").lower()
    if engine != "opencode":
        return True, f"engine is '{engine}' — opencode auth not required"
    default_auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    auth = Path(os.environ.get("OPENCODE_AUTH_FILE", str(default_auth))).expanduser()
    if auth.exists():
        return True, f"opencode auth.json present: {auth}"
    return False, f"ENGINE=opencode but auth.json missing: {auth} (run: opencode auth login)"


def _all_checks() -> list[tuple[str, tuple[bool, str]]]:
    return [
        ("Python >= 3.10", _check_python()),
        ("Docker daemon", _check_docker()),
        ("Docker compose", _check_compose()),
        ("Credentials (.env)", _check_credentials()),
        ("Agent container", _check_container()),
        ("Bridge process", _check_bridge()),
        ("OpenCode auth (if needed)", _check_opencode_auth()),
    ]


# ----------------------------------------------------------------- commands
def cmd_check(args) -> int:
    print("feishu-bridge environment check:")
    all_ok = True
    for name, (ok, detail) in _all_checks():
        if ok:
            _ok(f"{name}: {detail}")
        else:
            all_ok = False
            _fail(f"{name}: {detail}")
    print()
    if all_ok:
        print("\033[32mAll checks passed — bridge is ready.\033[0m")
        return 0
    print("\033[33mSome checks failed. Run `python cli.py doctor` for fixes.\033[0m")
    return 1


def cmd_doctor(args) -> int:
    print("feishu-bridge doctor — diagnosis & suggested fixes:\n")
    all_ok = True
    for name, (ok, detail) in _all_checks():
        if ok:
            _ok(f"{name}: {detail}")
        else:
            all_ok = False
            _fail(f"{name}: {detail}")
            _fix_hint(name, detail)
    print()
    return 0 if all_ok else 1


def _fix_hint(name: str, detail: str) -> None:
    """Print the self-heal action for a failed check (agent_readme §2 mirror)."""
    hints = {
        "Python >= 3.10": "Install Python 3.10+ — https://www.python.org/downloads/  "
                          "(Linux: sudo apt install python3 python3-venv)",
        "Docker daemon": "Install Docker Desktop (Win/Mac) or docker engine (Linux). "
                         "Start Docker Desktop, or: sudo systemctl start docker",
        "Docker compose": "Docker Desktop bundles compose v2. On Linux: "
                          "sudo apt install docker-compose-plugin",
        "Credentials (.env)": f"cp .env.example \"{_env_file()}\"  then fill in "
                              "FEISHU_APP_ID / FEISHU_APP_SECRET / ALLOWED_USER_ID "
                              "(see agent_readme.md §2.3)",
        "Agent container": "Build & start it:  python cli.py up",
        "Bridge process": "Start the bridge:  python cli.py start --detach",
        "OpenCode auth (if needed)": "On the host: opencode auth login  "
                                     "(choose your provider, paste the API key)",
    }
    action = hints.get(name)
    if action:
        _info(f"FIX → {action}")


def _compose_cmd() -> list[str]:
    """Return the docker compose invocation prefix for this machine."""
    if _run(["docker", "compose", "version"]).returncode == 0:
        return ["docker", "compose", "-f", str(COMPOSE_FILE)]
    return ["docker-compose", "-f", str(COMPOSE_FILE)]


def _load_env_into_environ() -> None:
    """Export the env file into os.environ so compose can interpolate ${VARS}.

    compose uses ${WORKSPACE_DIR} etc. at parse time; it does not read our
    secrets file. We therefore surface the keys the same way the systemd unit
    would via EnvironmentFile. Values already set in the real env win.
    """
    for k, v in _env_keys().items():
        os.environ.setdefault(k, v)


def cmd_up(args) -> int:
    if not COMPOSE_FILE.exists():
        _fail(f"compose file missing: {COMPOSE_FILE}")
        return 1
    _load_env_into_environ()
    print(f"Building & starting agent container ({_container_name()}) ...")
    # Stream output (no capture) so build progress is visible.
    r = subprocess.run(_compose_cmd() + ["up", "-d", "--build"], env=os.environ)
    if r.returncode != 0:
        _fail("compose up failed")
        return 1
    _ok("agent container up")
    # Quick verify.
    ok, detail = _check_container()
    ( _ok if ok else _fail)(f"verify: {detail}")
    return 0 if ok else 1


def cmd_down(args) -> int:
    _load_env_into_environ()
    print("Stopping agent container ...")
    r = subprocess.run(_compose_cmd() + ["down"], env=os.environ)
    if r.returncode != 0:
        _fail("compose down failed")
        return 1
    _ok("agent container down")
    return 0


def _spawn_bridge(detach: bool) -> int:
    """Launch bridge.py. If detach, redirect output to bridge.log and background."""
    env = dict(os.environ)
    # The bridge loads its own env file via FEISHU_ENV_FILE; make sure it's set.
    env.setdefault("FEISHU_ENV_FILE", str(_env_file()))
    if detach:
        # Detached, logs to bridge.log. Cross-platform: no nohup/disown.
        flags = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        ) if _is_windows() else 0
        log_fh = open(BRIDGE_LOG, "ab")
        try:
            proc = subprocess.Popen(
                [_python(), str(BRIDGE_PY)],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                creationflags=flags,
                close_fds=True,
            )
        finally:
            # Popen dup'd the fd on POSIX; on Windows close_fds handles it.
            if not _is_windows():
                log_fh.close()
        # Give it a moment to either stay up or exit on a config error.
        time.sleep(2)
        rc = proc.poll()
        if rc is not None:
            _fail(f"bridge exited immediately (rc={rc}); see {BRIDGE_LOG}")
            return 1
        _ok(f"bridge started detached (PID {proc.pid}); logging to {BRIDGE_LOG}")
        return 0
    else:
        # Foreground: inherit tty, Ctrl-C / SIGTERM propagate naturally.
        print(f"Starting bridge in foreground (Ctrl-C to stop). Log also → {BRIDGE_LOG}")
        return subprocess.call([_python(), str(BRIDGE_PY)], env=env)


def cmd_start(args) -> int:
    if _bridge_running():
        _warn(f"bridge already running (PID {_bridge_pid()}); not starting another.")
        return 0
    ok, detail = _check_container()
    if not ok:
        _fail(f"agent container not ready: {detail}")
        _info("Start it first:  python cli.py up")
        return 1
    return _spawn_bridge(detach=args.detach)


def cmd_stop(args) -> int:
    if not _bridge_running():
        _info("bridge not running (no live lock). Nothing to stop.")
        # Clean up a stale PID file if present.
        if PID_FILE.exists():
            try:
                PID_FILE.unlink()
            except OSError:
                pass
        return 0
    pid = _bridge_pid()
    if pid is None:
        _warn("bridge lock held but no PID readable; cannot stop cleanly.")
        return 1
    # Cross-platform termination. SIGTERM is what bridge.py listens for and
    # exits cleanly on. On Windows, os.kill(pid, SIGTERM) is supported (it
    # maps to TerminateProcess); bridge.py's atexit still releases the lock.
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as exc:
        _fail(f"could not stop bridge PID {pid}: {exc}")
        return 1
    # Wait for the lock to release (bridge atexit clears it).
    for _ in range(50):  # up to ~5s
        if not _bridge_running():
            _ok(f"bridge stopped (was PID {pid})")
            return 0
        time.sleep(0.1)
    _warn(f"bridge PID {pid} sent SIGTERM but lock still held after 5s — may need a second stop.")
    return 1


def cmd_restart(args) -> int:
    rc1 = cmd_stop(args)
    time.sleep(1)
    rc2 = cmd_start(args)
    return rc2 if rc1 == 0 else rc1


def cmd_status(args) -> int:
    print("feishu-bridge status:")
    # Bridge
    if _bridge_running():
        _ok(f"bridge.py: RUNNING (PID {_bridge_pid()})")
    else:
        _fail("bridge.py: NOT RUNNING")
    # Container
    name = _container_name()
    r = _run(["docker", "inspect", "-f", "{{.State.Status}} (up {{.State.StartedAt}})", name])
    if r.returncode == 0 and r.stdout.strip():
        _ok(f"container {name}: {r.stdout.strip()}")
    else:
        _fail(f"container {name}: not found / not running")
    # Env file
    ok, detail = _check_credentials()
    ( _ok if ok else _fail)(f"credentials: {detail}")
    return 0


def cmd_logs(args) -> int:
    if args.container:
        return subprocess.call(["docker", "logs", "--tail", str(args.n), _container_name()])
    # Tail bridge.log (cross-platform: just print last N lines, no `tail` dep).
    if not BRIDGE_LOG.exists():
        _fail(f"no bridge log at {BRIDGE_LOG}")
        return 1
    try:
        with open(BRIDGE_LOG, "rb") as f:
            if args.follow:
                f.seek(0, os.SEEK_END)
                # Simple follow loop; Ctrl-C exits.
                try:
                    while True:
                        chunk = f.readline()
                        if chunk:
                            sys.stdout.buffer.write(chunk)
                            sys.stdout.buffer.flush()
                        else:
                            time.sleep(0.5)
                except KeyboardInterrupt:
                    pass
            else:
                # last N lines
                lines = f.readlines()
                for line in lines[-args.n:]:
                    sys.stdout.buffer.write(line)
    except OSError as exc:
        _fail(str(exc))
        return 1
    return 0


# ---------------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Cross-platform manager for feishu-bridge.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="environment self-check")
    sub.add_parser("doctor", help="deep diagnosis with suggested fixes")
    sub.add_parser("up", help="build & start the agent container")
    sub.add_parser("down", help="stop & remove the agent container")
    sub.add_parser("restart", help="stop then start the bridge")
    sub.add_parser("status", help="show bridge + container state")

    p_start = sub.add_parser("start", help="start the bridge host process")
    p_start.add_argument("--detach", action="store_true", help="run in background")
    sub.add_parser("stop", help="stop the running bridge")

    p_logs = sub.add_parser("logs", help="tail logs")
    p_logs.add_argument("-n", type=int, default=50, help="number of lines (bridge log)")
    p_logs.add_argument("-f", "--follow", action="store_true", help="follow the bridge log")
    p_logs.add_argument("--container", action="store_true", help="show agent container logs instead")

    args = parser.parse_args()
    dispatch = {
        "check": cmd_check, "doctor": cmd_doctor, "up": cmd_up, "down": cmd_down,
        "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
        "status": cmd_status, "logs": cmd_logs,
    }
    try:
        return dispatch[args.cmd](args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
