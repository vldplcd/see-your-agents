"""Which Codex threads run now, from disk only (docs/codex-cli-notes.md, «Live state»).

A thread is running while some process holds ~/.codex/thread-writer-locks/<thread id>.lock: the daemon (TUIs
are its clients), a VS Code window's own app-server, `codex exec`, or a TUI started with --no-daemon. `lsof`
names the holder; when lsof itself fails, a lock file that exists counts as held. A running thread is busy
while its rollout's last turn is open (task_started with no task_complete or turn_aborted after it), idle
otherwise. Waiting for an approval is not on disk.

Nothing here writes to ~/.codex or touches a lock: taking one, even to test it, could make Codex fail.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from ..base import Kind, LiveSession, Status
from . import ID
from .rollout import THREAD_ID, Replay, event_type, is_hidden, iso_ms

LOCK = re.compile(rf"({THREAD_ID})\.lock")
LSOF_TIMEOUT_S = 2.0
PS_TIMEOUT_S = 2.0
TURN_EVENTS = ("task_started", "task_complete", "turn_aborted")
LINE_MARKS = tuple(f'"{kind}"'.encode() for kind in (*TURN_EVENTS, "session_meta"))  # worth parsing
# Options of `codex` that take a value: the subcommand is the first word that is neither one nor its value.
VALUE_FLAGS = {"-c", "--config", "-m", "--model", "-p", "--profile", "-C", "--cd", "-s", "--sandbox", "-a",
               "--ask-for-approval", "-i", "--image", "--enable", "--disable", "--add-dir",
               "--remote"}  # fmt: skip
KIND = {"exec": Kind.BACKGROUND}  # daemon, vscode, tui: Kind.INTERACTIVE


def lock_files(home: Path) -> dict[str, Path]:
    """Lock files by thread id; the folder's own .coordination.lock is not a thread's."""
    folder = home / "thread-writer-locks"
    if not folder.is_dir():
        return {}
    return {m[1]: p for p in folder.glob("*.lock") if (m := LOCK.fullmatch(p.name))}


def holders(locks: dict[str, Path]) -> dict[str, int] | None:
    """{thread id: pid} of the locks some process holds; None when lsof cannot tell."""
    exe = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        out = subprocess.run(
            [exe, "-w", "-F", "pn", "--", *map(str, locks.values())],
            capture_output=True,
            text=True,
            timeout=LSOF_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode not in (0, 1):  # 1: some file is open by nobody
        return None
    by_name = {p.name: tid for tid, p in locks.items()}  # lsof prints the resolved path: match by name
    held: dict[str, int] = {}
    pid = 0
    for line in out.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid and (tid := by_name.get(Path(line[1:]).name)):
            held.setdefault(tid, pid)
    return held


def daemon_pid(home: Path) -> int:
    try:
        data = json.loads((home / "app-server-daemon" / "daemon.pid").read_text())
    except (OSError, ValueError):
        return 0
    pid = data.get("pid") if isinstance(data, dict) else data
    return pid if isinstance(pid, int) else 0


def subcommand(command: str) -> str:
    """`codex -c x=y exec …` -> exec; a TUI (`codex`, `codex --no-daemon`) -> ""."""
    words = iter(command.split()[1:])
    for word in words:
        if word in VALUE_FLAGS:
            next(words, None)
        elif not word.startswith("-"):
            return word
    return ""


def host_of(pid: int, command: str, daemon: int) -> str:
    """daemon | vscode (a VS Code window, or the desktop app, runs its own app-server) | exec | tui."""
    if pid == daemon or "--managed-daemon" in command:
        return "daemon"
    sub = subcommand(command)
    if sub == "app-server":
        return "vscode"
    return "exec" if sub in ("exec", "e") else "tui"


def command_lines(pids: set[int]) -> dict[int, str]:
    """The command lines of these processes; {} when ps fails."""
    if not pids:
        return {}
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=,command=", "-p", ",".join(map(str, sorted(pids)))],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in out.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit():
            found[int(pid)] = command.strip()
    return found


class TurnState:
    """Whether a rollout's last own turn is still open; a read takes only what the file gained since the
    last. A fork's replayed history ends on its parent's open turn: that one is not the fork's (`Replay`)."""

    def __init__(self) -> None:
        self.offset = 0
        self.busy = False
        self.since = ""  # the time of the last turn event
        self.replay = Replay()

    def read(self, path: Path) -> None:
        size = path.stat().st_size
        if size < self.offset:  # rewritten, not appended
            self.__init__()
        with path.open("rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read(size - self.offset)
        complete = chunk[: chunk.rfind(b"\n") + 1]  # a half-written last line waits for the next read
        for raw in complete.splitlines():
            if not any(mark in raw for mark in LINE_MARKS):
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict) or not self.replay.own(entry):
                continue
            if (kind := event_type(entry)) in TURN_EVENTS:
                self.busy = kind == "task_started"
                self.since = str(entry.get("timestamp") or self.since)
        self.offset += len(complete)


class Live:
    """Who runs now. The rollouts' turn state is kept between calls: the board asks every few seconds."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.turns: dict[Path, TurnState] = {}

    def read(self, home: Path, rollout_of: Callable[[str], Path | None]) -> dict[str, LiveSession]:
        """`rollout_of(thread id)`: the thread's rollout, None while it has none yet (it appears with the
        first prompt). A hidden thread (rollout.is_hidden: a subagent, a helper) is not shown, live or not."""
        locks = lock_files(home)
        if not locks:
            return {}
        held = holders(locks)
        if held is None:  # lsof failed: a lock that exists is held, by whom is unknown
            held, hosts = dict.fromkeys(locks, 0), {}
        else:
            daemon = daemon_pid(home)
            hosts = {pid: host_of(pid, cmd, daemon) for pid, cmd in command_lines(set(held.values())).items()}
        found: dict[str, LiveSession] = {}
        with self.lock:
            paths = {tid: rollout_of(tid) for tid in held}
            self.turns = {p: t for p, t in self.turns.items() if p in paths.values()}
            for tid, pid in held.items():
                path = paths[tid]
                if path is None or not is_hidden(path):
                    found[tid] = self._session(tid, pid, hosts.get(pid, ""), path)
        return found

    def _session(self, tid: str, pid: int, host: str, rollout: Path | None) -> LiveSession:
        status, since = Status.IDLE, ""
        if rollout is not None:
            turns = self.turns.setdefault(rollout, TurnState())
            with contextlib.suppress(OSError):  # archived or removed meanwhile: what was read stands
                turns.read(rollout)
            status, since = (Status.BUSY if turns.busy else Status.IDLE), turns.since
        return LiveSession(
            provider=ID,
            session_id=tid,
            kind=KIND.get(host, Kind.INTERACTIVE),
            status=status,
            pid=pid or None,
            host=host,
            updated_at=since,
            status_since_ms=iso_ms(since),
        )
