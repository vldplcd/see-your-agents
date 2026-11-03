"""`sya serve` as a small parent: it runs the dashboard as a child process and starts it again on request.

The child exits with RESTART (75) to be started again, so new Python code takes effect (an accepted build, a
rollback). When the restart follows an accept, the parent waits up to HEALTH_WAIT_S for GET /api/health; a
version that does not answer is reverted (`git revert -m 1` of the accepted merge, found in
config_dir()/builds.json), marked rolled_back there and started again. A version that died because another
process holds the port is not to blame: that is reported, not reverted.

A stop signal (Ctrl+C, SIGTERM, SIGHUP) goes on to the child, each time one comes. After the first nothing
is started and nothing is reverted; the child gets STOP_WAIT_S to finish, then it is killed.

The parent imports only the stdlib and the path helpers: it has to outlive a broken feature. `cli.py` execs
into this module before anything else is loaded, so not even the feature registry is in the parent's memory.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from .. import config_dir

RESTART = 75  # EX_TEMPFAIL: "start me again"
ENV = "SYA_WATCHDOG"  # set for the child: a restart request has someone to answer it
HEALTH_WAIT_S = 15.0
ACCEPT_WINDOW_S = 120.0  # an accept this recent is what the restart was for
STOP_WAIT_S = 10.0
MAX_RESTARTS, RESTART_WINDOW_S = 5, 60.0  # a child that keeps asking for restarts is broken
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
_no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def say(text: str) -> None:
    print(f"sya: {text}", file=sys.stderr, flush=True)


def child_command(argv: list[str]) -> list[str]:
    return [sys.executable, "-m", "see_your_agents.app.cli", *argv, "--child"]


def exec_parent(port: int, argv: list[str]) -> None:
    """Replace this process (which has the whole registry loaded) with a bare watchdog."""
    os.execv(
        sys.executable,
        [sys.executable, "-m", "see_your_agents.app.watchdog", f"--port={port}", "--", *argv],
    )


def under_watchdog() -> bool:
    return os.environ.get(ENV) == "1"


def exit_with_parent(interval: float = 1.0) -> None:
    """Child side: stop when the watchdog is gone (SIGKILL, a crash) instead of holding the port forever."""
    if not under_watchdog():
        return
    parent = os.getppid()

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(interval)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=watch, daemon=True).start()


def stop_on_signals() -> None:
    """Child side: SIGTERM and SIGHUP end the server as Ctrl+C does, through an exception, so `atexit` runs
    (it stops a running preview). Python's default for them kills the process without it. The first signal
    is enough: the watchdog passes on every one, uv repeats Ctrl+C, and a repeat would cut cleanup short."""

    def handler(signum, frame) -> None:
        for s in SIGNALS:
            signal.signal(s, signal.SIG_IGN)
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    for s in SIGNALS:
        signal.signal(s, handler)


def healthy(port: int, proc: subprocess.Popen, stopping: threading.Event) -> bool:
    deadline = time.monotonic() + HEALTH_WAIT_S
    while time.monotonic() < deadline and not stopping.is_set():
        if proc.poll() is not None:
            return False
        try:
            with _no_proxy.open(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                if json.load(resp).get("ok") is True:
                    return True
        except (OSError, ValueError, AttributeError):
            pass
        time.sleep(0.3)
    return False


def port_taken(port: int) -> bool:
    """Another process holds the port: a version that could not bind it is not broken."""
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as the server binds: TIME_WAIT is free
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def stop(proc: subprocess.Popen, sig: int | None = signal.SIGTERM) -> None:
    """Send `sig` (None: it is already on its way), wait STOP_WAIT_S, then kill."""
    if proc.poll() is not None:
        return
    if sig is not None:
        proc.send_signal(sig)
    try:
        proc.wait(STOP_WAIT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def wait_child(proc: subprocess.Popen, stopping: threading.Event) -> int:
    """The child's exit code. After a stop signal it has STOP_WAIT_S left: a child that ignores signals must
    not keep the watchdog running."""
    while proc.poll() is None and not stopping.is_set():
        time.sleep(0.2)  # not stopping.wait(): forward() sets it from a signal handler on this very thread
    stop(proc, None)
    return proc.returncode


# ---- the accepted build, read from the builder's file -----------------------------------------


def _builds_path() -> Path:
    return config_dir() / "builds.json"


def _builds() -> list[dict]:
    try:
        data = json.loads(_builds_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    builds = data.get("builds") if isinstance(data, dict) else None
    return [b for b in builds if isinstance(b, dict)] if isinstance(builds, list) else []


def _age_s(ts: object) -> float:
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(str(ts))).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def last_accept() -> dict | None:
    """The build accepted in the last ACCEPT_WINDOW_S, if any: the restart is there to load it."""
    accepted = [
        b
        for b in _builds()
        if b.get("status") == "accepted" and b.get("merge_sha") and b.get("checkout")
        if _age_s(b.get("accepted_at")) < ACCEPT_WINDOW_S
    ]
    return max(accepted, key=lambda b: str(b.get("accepted_at")), default=None)


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=60,
        stdin=subprocess.DEVNULL, env={**os.environ, "GIT_EDITOR": "true"},
    )  # fmt: skip


def _mark_rolled_back(build_id: str, reason: dict, revert_sha: str) -> None:
    """The child is stopped here, so nothing else writes the file meanwhile. `reason` is the builder's
    stored-error shape, {"key", "params"}: the card words it in its reader's language."""
    path = _builds_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for b in data.get("builds", []):
        if isinstance(b, dict) and b.get("id") == build_id:
            now = datetime.now(UTC).isoformat()
            b.update(status="rolled_back", error=reason, revert_sha=revert_sha, updated_at=now)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".builds-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def why_reverted(crashed: int | None) -> tuple[str, dict]:
    """Why a new version is reverted: English for the terminal, and a key of the builder's catalog with its
    params for builds.json."""
    if crashed is not None:
        return f"the new version crashed on start (code {crashed})", {
            "key": "builder.error.crashed",
            "params": {"code": crashed},
        }
    return f"the new version did not answer in {HEALTH_WAIT_S:.0f} s", {
        "key": "builder.error.no_answer",
        "params": {"n": round(HEALTH_WAIT_S)},
    }


def rollback(build: dict, reason: dict) -> str:
    """Revert the accepted merge if it is still the tip of the build's branch; returns what happened."""
    repo, merge = build["checkout"], build["merge_sha"]
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if head != merge:
        return f"not rolling back: HEAD {head[:8]} is no longer the merge {merge[:8]}"
    current, base = _git(repo, "branch", "--show-current").stdout.strip(), build.get("base_branch")
    if current != base:  # a revert on another branch would leave the build in its own
        return f"not rolling back: the checkout is on '{current or 'detached HEAD'}', the build in '{base}'"
    done = _git(repo, "revert", "-m", "1", "--no-edit", merge)
    if done.returncode:
        _git(repo, "revert", "--abort")
        return f"git revert failed: {(done.stderr or done.stdout).strip()[:300]}"
    revert_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _mark_rolled_back(str(build.get("id")), reason, revert_sha)
    return f"rolled back {merge[:8]} with commit {revert_sha[:8]}"


# ---- the loop ---------------------------------------------------------------------------------


def _exit_code(code: int) -> int:
    return code if code >= 0 else 128 - code


def supervise(child: list[str], port: int) -> int:
    """Run `child` until it exits with anything but RESTART; returns the exit code for the parent."""
    proc: subprocess.Popen | None = None
    stopped_by = 0
    stopping = threading.Event()

    def forward(signum, frame) -> None:
        nonlocal stopped_by
        stopped_by = stopped_by or signum
        stopping.set()
        if proc is not None and proc.poll() is None:
            proc.send_signal(signum)  # every one, not only the first; our child ignores repeats itself

    previous = {s: signal.signal(s, forward) for s in SIGNALS}
    restarts: list[float] = []
    verify, first = None, True
    try:
        while not stopping.is_set():  # nothing starts after a stop signal
            cmd = child if first else [*child, "--no-open"]
            first = False
            # its own session: Ctrl+C reaches the parent only, which passes it on
            proc = subprocess.Popen(cmd, env={**os.environ, ENV: "1"}, start_new_session=True)
            if stopping.is_set():  # it came while the child was starting, before forward() could reach it
                proc.send_signal(stopped_by)
            elif verify and not healthy(port, proc, stopping) and not stopping.is_set():
                crashed = proc.poll()
                stop(proc)
                if crashed is not None and port_taken(port):
                    say(
                        f"the new version did not start: port {port} is taken by another process. Not "
                        "rolling the build back: free the port and run sya serve again"
                    )
                    return _exit_code(crashed)
                why, reason = why_reverted(crashed)
                say(f"{why}; {rollback(verify, reason)}")
                verify = None
                continue
            verify = None
            code = wait_child(proc, stopping)
            if stopping.is_set() or code != RESTART:
                return _exit_code(code)
            now = time.monotonic()
            restarts = [t for t in restarts if now - t < RESTART_WINDOW_S] + [now]
            if len(restarts) > MAX_RESTARTS:
                say(f"the server asks for a restart too often ({MAX_RESTARTS} times a minute); stopping")
                return 1
            verify = last_accept()
            say("restarting the server" + (" with the accepted build" if verify else ""))
        return 128 + stopped_by  # stopped between two children: none is left to give a code
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
        if proc is not None:
            stop(proc)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sya-watchdog")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER, help="sya's own arguments, after --")
    args = parser.parse_args(argv)
    rest = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    sys.exit(supervise(child_command(rest), args.port))


if __name__ == "__main__":
    main()
