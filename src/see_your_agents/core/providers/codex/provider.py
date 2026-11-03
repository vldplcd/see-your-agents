"""Codex CLI as a provider: its threads are on the board, have a status and open in the chat; the chat writes
to them through `codex queue` (queue.py) and "New session" starts one with `codex exec`
(launch.py). A thread the daemon hosts shows that it waits on an approval or an answer (daemon.py)."""

from __future__ import annotations

import shlex
import shutil
import threading
from collections.abc import Iterable
from pathlib import Path

from ..base import (
    Cap,
    Digest,
    LaunchOptions,
    LaunchRequest,
    LiveSession,
    Message,
    Provider,
    Route,
    Sent,
    Session,
    Started,
    Thread,
)
from . import ID, launch, queue, rollout
from .daemon import Waits
from .live import Live
from .names import thread_names
from .paths import codex_home
from .thread import CodexThread


class Codex(Provider):
    id = ID
    title = "Codex"
    caps = frozenset({Cap.ATTACH, Cap.LAUNCH})
    parser_version = rollout.PARSER_VERSION

    def __init__(self) -> None:
        self._live = Live()
        self._waits = Waits()
        self._paths: dict[str, Path] = {}  # live thread id -> its rollout, found once
        self._paths_lock = threading.Lock()

    def available(self) -> bool:
        return shutil.which("codex") is not None or codex_home().is_dir()

    def transcripts(self) -> dict[str, Path]:
        return rollout.transcripts(codex_home())

    def parse(self, path: Path) -> Digest:
        return rollout.parse_rollout(path)

    def names(self) -> dict[str, str]:
        return thread_names(codex_home())

    def live(self) -> dict[str, LiveSession]:
        home = codex_home()
        return self._waits.apply(home, self._live.read(home, lambda tid: self._rollout(home, tid)))

    def _rollout(self, home: Path, tid: str) -> Path | None:
        with self._paths_lock:
            path = self._paths.get(tid)
            if path is None or not path.exists():  # not written yet, or archived since
                path = rollout.find_rollout(home, tid)
                if path is not None:
                    self._paths[tid] = path
            return path

    def attach_command(self, session: Session) -> str:
        if self._rollout(codex_home(), session.session_id) is None:
            return ""  # no prompt yet: Codex has nothing to resume
        resume = f"codex resume {session.session_id}"
        return f"cd {shlex.quote(session.cwd)} && {resume}" if session.cwd else resume

    def thread(self) -> Thread:
        return CodexThread()

    def options(self) -> LaunchOptions:
        return launch.options()

    def trusted(self, folders: Iterable[Path]) -> set[Path]:
        return launch.trusted(folders)

    def start(self, request: LaunchRequest, taken: Iterable[str] = ()) -> Started:
        return launch.start(request)

    def route(self, session: Session, signed: bool = False) -> Route:
        return queue.route(session, signed)

    def send(self, session: Session, message: Message) -> Sent:
        return queue.send(session, message)

    def resume(self, session: Session, message: Message) -> Sent:
        return queue.send(session, message)  # a stopped thread takes the same queue: the daemon resumes it
