"""Claude Code as a provider: the contract of base.py over this package's modules."""

from __future__ import annotations

import shlex
import shutil
from collections.abc import Iterable
from pathlib import Path

from ..base import (
    Cap,
    Digest,
    Kind,
    LaunchOptions,
    LaunchRequest,
    LiveSession,
    Message,
    Provider,
    Route,
    Sent,
    Session,
    Started,
    TaskItem,
    Thread,
)
from . import ID, launch, live, routes, transcript
from .paths import claude_home
from .thread import ClaudeThread


class Claude(Provider):
    id = ID
    title = "Claude Code"
    caps = frozenset(Cap)
    parser_version = transcript.PARSER_VERSION

    def available(self) -> bool:
        return shutil.which("claude") is not None or claude_home().is_dir()

    def transcripts(self) -> dict[str, Path]:
        return transcript.transcripts(claude_home())

    def parse(self, path: Path) -> Digest:
        return transcript.parse_transcript(path)

    def live(self) -> dict[str, LiveSession]:
        return live.read_live(claude_home())

    def tasks(self, session: Session) -> list[TaskItem] | None:
        return live.read_tasks(claude_home(), session.session_id)

    def attach_command(self, session: Session) -> str:
        if session.live and session.live.kind == Kind.BACKGROUND and session.live.job_id:
            return f"claude attach {session.live.job_id}"
        if not session.cwd:
            return ""
        return f"cd {shlex.quote(session.cwd)} && claude --resume {session.session_id}"

    def thread(self) -> Thread:
        return ClaudeThread()

    def options(self) -> LaunchOptions:
        return launch.options()

    def trusted(self, folders: Iterable[Path]) -> set[Path]:
        return launch.trusted(folders)

    def start(self, request: LaunchRequest, taken: Iterable[str] = ()) -> Started:
        return launch.start(request, taken)

    def fork(self, session: Session, name: str = "", prompt: str = "", taken: Iterable[str] = ()) -> Started:
        return launch.fork(session, name=name, prompt=prompt, taken=taken)

    def route(self, session: Session, signed: bool = False) -> Route:
        return routes.route(session, signed)

    def send(self, session: Session, message: Message) -> Sent:
        return routes.send(session, message)

    def resume(self, session: Session, message: Message) -> Sent:
        return Sent("resume", str(routes.resume(session, message)))

    def stop(self, session: Session) -> None:
        routes.stop(session)

    def focus(self, session: Session) -> None:
        routes.focus(session)
