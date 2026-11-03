"""Preview: the build's dashboard from its worktree, on its own port, with a copy of the user's config.

It runs code the user has not accepted yet, so it starts only from their click, one at a time. Its config is
a copy (without builds.json and the builder's files: a preview has no builds of its own to act on) and its
cache is its own, so the index there is built afresh. A refusal is a PreviewError or the sandbox's
SandboxError: Problems (builder.*) the page reads in its language.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from ... import config_dir
from ...core.env import clean_env
from ...i18n import Problem
from . import agent

PORTS = range(8766, 8800)
READY_WAIT_S = 90.0  # the first `uv run` in a worktree creates its venv
STOP_WAIT_S = 10.0
_no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_lock = threading.Lock()
_start_lock = threading.Lock()  # one start at a time: a second click waits, then replaces the first
_running: dict | None = None  # {"id", "port", "url", "proc"}


class PreviewError(Problem):
    def __init__(self, key: str, /, **params: object):
        super().__init__(key, 502, **params)


def _free_port() -> int:
    for port in PORTS:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as the server binds: TIME_WAIT is free
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise PreviewError("builder.preview.no_port", first=PORTS.start, last=PORTS.stop - 1)


def _answers(port: int) -> bool:
    try:
        with _no_proxy.open(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
            return json.load(resp).get("ok") is True
    except (OSError, ValueError, AttributeError):
        return False


def _folders(worktree: Path) -> tuple[Path, Path, Path]:
    """The preview's config, cache and log, under the build's temp root: the only place outside the worktree
    the sandboxed preview may write. Copying the user's config (minus secrets) happens here, in the
    dashboard, not in the sandboxed child, so the child never reads config_dir()."""
    root = agent.tmp_root(worktree) / "preview"
    config, cache = root / "config", root / "cache"
    shutil.rmtree(root, ignore_errors=True)
    if config_dir().is_dir():
        # No secrets: the preview runs code the user has not accepted, and makes its own MCP tokens.
        # mcp-tokens.json holds the control token; supervisor/ holds it again and the Jira credentials.
        skip = shutil.ignore_patterns("builds.json", "builder", "mcp-tokens.json", "supervisor")
        shutil.copytree(config_dir(), config, ignore=skip)
    config.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    return config, cache, root / "preview.log"


def current() -> dict | None:
    with _lock:
        if _running and _running["proc"].poll() is None:
            return {"id": _running["id"], "url": _running["url"]}
        return None


def start(build: dict, deny_port: int) -> dict:
    """Start the preview of `build` (stopping any other) and wait until it answers. `deny_port` is the
    dashboard's own port: the sandboxed preview may reach other loopback ports but not that one."""
    with _start_lock:
        return _start(build, deny_port)


def _start(build: dict, deny_port: int) -> dict:
    stop()
    uv = shutil.which("uv")
    if not uv:
        raise PreviewError("builder.preview.no_uv")
    worktree = Path(build["worktree"])
    if not worktree.is_dir():
        raise PreviewError("builder.preview.no_worktree")
    config, cache, log = _folders(worktree)
    port = _free_port()
    env = clean_env(**agent.confined_env(worktree, SYA_CONFIG_DIR=str(config), SYA_CACHE_DIR=str(cache)))
    # It runs the build's code, so under sandbox-exec like the checks; fail closed if it cannot be confined
    # (SandboxError, 502).
    serve = [uv, "run", "sya", "serve", "--no-open", "--port", str(port)]
    argv = agent.confine(serve, worktree, deny_port)
    with log.open("wb") as out:
        proc = subprocess.Popen(
            argv, cwd=worktree, env=env,
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
        )  # fmt: skip
    global _running
    url = f"http://127.0.0.1:{port}/"
    with _lock:
        _running = {"id": build["id"], "port": port, "url": url, "proc": proc}
    deadline = time.monotonic() + READY_WAIT_S
    while time.monotonic() < deadline and proc.poll() is None:
        if _answers(port):
            return {"id": build["id"], "url": url}
        time.sleep(0.5)
    stop()
    tail = log.read_text(encoding="utf-8", errors="replace")[-1500:].strip()
    if tail:
        raise PreviewError("builder.preview.not_up", output=tail)
    raise PreviewError("builder.preview.no_answer")


def stop(build_id: str | None = None) -> bool:
    """Stop the running preview (only the one of `build_id`, when given); True if one was running."""
    global _running
    with _lock:
        running = _running
        if running is None or (build_id and running["id"] != build_id):
            return False
        _running = None
    proc: subprocess.Popen = running["proc"]
    if proc.poll() is None:
        _signal_group(proc.pid, signal.SIGTERM)
        try:
            proc.wait(STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            _signal_group(proc.pid, signal.SIGKILL)
            proc.wait()
    return True


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


atexit.register(stop)  # a restart or Ctrl+C must not leave a preview holding its port
