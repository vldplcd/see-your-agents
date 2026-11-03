"""Keep the Mac awake: while a session works, keep one `caffeinate -i -w <server pid>` running.

GET  /api/awake  -> {available, enabled, active}
POST /api/awake  {enabled: bool} -> the same

Only on macOS with `caffeinate` on PATH; elsewhere `available` is false and the page shows no switch. The
switch is the user's (config_dir()/awake.json, on by default). The child never outlives the server: `-w`
ends it when the server's process ends, however it ends (the watchdog starts a new one, which starts its
own child); the loop stops it once nothing works or the switch goes off, and so does exit.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ... import config_dir
from ...app.replies import Reply, json_reply
from ...core.providers.base import LiveSession
from ...core.sessions import column_for
from ...i18n import Problem

if TYPE_CHECKING:
    from email.message import Message

    from ...app.server import App

ROUTE = "/api/awake"
FILE = "awake.json"
CHECK_EVERY_S = 10.0
STOP_WAIT_S = 5.0
_file_lock = threading.Lock()


def available() -> bool:
    return sys.platform == "darwin" and shutil.which("caffeinate") is not None


def enabled() -> bool:
    try:
        saved = json.loads((config_dir() / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not isinstance(saved, dict) or saved.get("enabled") is not False


def set_enabled(on: bool) -> None:
    path = config_dir() / FILE
    with _file_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"enabled": on}), encoding="utf-8")
        os.replace(tmp, path)


def working(live: dict[str, LiveSession]) -> bool:
    """Some session is in Working: busy, not waiting on you."""
    now = datetime.now(UTC)
    return any(column_for(s, "", now) == "working" for s in live.values())


class Keeper:
    """At most one `caffeinate -i -w <this pid>` child: hold(True) starts it unless it runs, hold(False) stops
    it. After close() (exit) it starts nothing more."""

    def __init__(self, spawn: Callable[..., subprocess.Popen] = subprocess.Popen) -> None:
        self.spawn = spawn
        self.proc: subprocess.Popen | None = None
        self.closed = False
        self.lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def hold(self, on: bool) -> None:
        with self.lock:
            if on and not self.closed and not self.active:
                self._start()
            elif not on:
                self._stop()

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self._stop()

    def _start(self) -> None:
        # -w: the assertion ends with this process, even when it is killed and cannot stop the child
        argv = [shutil.which("caffeinate") or "caffeinate", "-i", "-w", str(os.getpid())]
        try:
            self.proc = self.spawn(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            print(f"caffeinate: {exc}", file=sys.stderr)
            self.proc = None

    def _stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


keeper = Keeper()


def tick(app: App) -> None:
    keeper.hold(enabled() and working(app.live()))  # the switch first: off, nobody asks `claude agents`


def run(app: App) -> None:
    """Feature.background: the server's loop, in its own thread for as long as the server runs."""
    if not available():
        return
    atexit.register(keeper.close)
    while True:
        time.sleep(CHECK_EVERY_S)  # first: a server that stops at once asks `claude agents` nothing
        try:
            tick(app)
        except Exception as exc:  # the next tick tries again; the server must not lose the loop
            print(f"awake: {exc!r}", file=sys.stderr)


def state() -> dict:
    return {"available": available(), "enabled": enabled(), "active": keeper.active}


def handle_get(app: App, path: str, query: dict[str, str]) -> Reply | None:
    return json_reply(state()) if path == ROUTE else None


def handle_post(app: App, path: str, body: bytes, headers: Message) -> Reply | None:
    if path != ROUTE:
        return None
    try:
        on = json.loads(body or b"{}").get("enabled")
    except (ValueError, AttributeError):
        on = None
    if not isinstance(on, bool):
        raise Problem("awake.bad_enabled")
    set_enabled(on)
    if available():
        tick(app)  # off stops the child now; on starts it if a session works
    return json_reply(state())
