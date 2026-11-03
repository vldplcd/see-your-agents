"""Writing to a Codex thread: `codex queue --thread <id> --message <text> --resume`, one route for every case.

The command hands the message to Codex's daemon, which resumes the thread and runs it as a turn. It exits 0
even when that fails («Warning: message was queued, but could not resume thread …»), so a message counts as
delivered only once the thread's rollout gains a turn started by the queue (`turn_attribution.turn_trigger`
"queue") that carries the text. `send` waits for that in the request's own thread, at most CONFIRM_S: the
daemon starts the turn within a couple of seconds, and a failure then reaches the composer as an error while
the text is still in it, instead of a pending bubble that silently expires. The turn's id is the send's
`Sent.msg_id`: a group finds the answer under it in the digest's `queued_turns`.

The route refuses before anything is queued where Codex would not run it (docs/notes/codex-cli-notes.md,
«Send»):
- a thread another process holds — VS Code's app-server, a `codex --no-daemon` TUI, a `codex exec` run: the
  daemon cannot take its writer lock, and the message would stay in Codex's queue (an exec run even takes it
  on its way out and aborts it);
- a busy thread: its queued message starts only when the current turn ends, past any bounded wait; one that
  waits on an approval or an answer (daemon.py) is busy too, and text never goes to a waiting prompt;
- a thread whose last turn was aborted (the parser's `last_turn_aborted`): Codex resumes it but starts no
  turn from the queue, and the message waits there until the thread is opened in a terminal.
Files are never carried: the command takes text only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ...env import clean_env
from ..base import Message, Route, SendError, SendInvalid, SendRefused, SendTimeout, Sent, Session, Status
from .paths import codex_home
from .rollout import event_type, find_rollout, prompt_of, queue_turn

QUEUE_TIMEOUT_S = 20.0
CONFIRM_S = 20.0
POLL_S = 0.25
ROUTE = "queue"
# Reasons and errors are catalog keys (web/i18n/*.json), worded where they are shown.
READY = "app.provider.codex.ready"
HELD = {
    "vscode": "app.provider.codex.held_vscode",
    "tui": "app.provider.codex.held_tui",
    "exec": "app.provider.codex.held_exec",
}
BUSY = "app.provider.codex.busy"
WAITING = "app.provider.codex.waiting"
ABORTED = "app.provider.codex.aborted"  # {sid}
SENDING = "app.provider.codex.sending"
NO_CODEX = "app.provider.codex.no_codex"
NOT_RESUMED = "could not resume thread"  # codex queue's warning: the message is queued, no turn started
HELD_ELSEWHERE = "already has an active writer"  # the reason it gives when another process holds the thread

_sending: set[str] = set()  # sessions with a message on its way: one at a time each
_lock = threading.Lock()


def route(session: Session, signed: bool = False) -> Route:
    """Every message takes the queue, signed or not: Codex has no other way in from outside."""
    if session.session_id in _sending:
        return Route(ROUTE, False, SENDING, pending=True)
    if session.transcript_missing:
        return Route(None, False, "app.provider.codex.no_transcript")
    if not shutil.which("codex"):
        return Route(None, False, NO_CODEX)
    live = session.live
    if live is not None and live.host in HELD:
        return Route(None, False, HELD[live.host])
    if live is not None and (live.status == Status.WAITING or live.waiting_for):
        return Route(None, False, WAITING)
    if live is not None and live.status == Status.BUSY:
        return Route(None, False, BUSY)
    if session.last_turn_aborted:
        return Route(None, False, ABORTED, params={"sid": session.session_id})
    return Route(ROUTE, True, READY)


def send(session: Session, message: Message) -> Sent:
    """Queue the text and wait for the turn it starts; SendError (or a subclass) whenever it did not start."""
    if message.files:
        raise SendInvalid("app.provider.codex.text_only")
    if "\x00" in message.text:
        raise SendInvalid("app.provider.codex.text_nul")
    found = route(session, bool(message.sender))
    if not found.can_send:
        raise found.refusal()
    sid = session.session_id
    with _lock:
        if sid in _sending:
            raise SendRefused(SENDING)
        _sending.add(sid)
    try:
        watch = QueuedTurnWatch(codex_home(), sid, message.text)
        _queue(sid, message.text)
        turn = watch.wait(CONFIRM_S)
    finally:
        with _lock:
            _sending.discard(sid)
    if turn is None:
        raise SendTimeout("app.provider.codex.not_started", s=f"{CONFIRM_S:.0f}", sid=sid)
    return Sent(ROUTE, msg_id=turn)  # the digest's queued_turns carry the answer under this id


def _queue(sid: str, text: str) -> None:
    """Run `codex queue`; raise on what its output says went wrong, exit code 0 or not."""
    exe = shutil.which("codex")
    if not exe:
        raise SendError(NO_CODEX)
    try:
        out = subprocess.run(
            [exe, "queue", f"--thread={sid}", f"--message={text}", "--resume"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=QUEUE_TIMEOUT_S,
            env=clean_env(),
        )  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        raise SendTimeout("app.provider.codex.queue_timeout", s=f"{QUEUE_TIMEOUT_S:.0f}") from exc
    except OSError as exc:
        raise SendError("app.provider.codex.queue_failed", error=str(exc)) from exc
    said = (out.stdout + "\n" + out.stderr).strip()
    if out.returncode and said:
        raise SendError("app.provider.codex.queue_said", output=said[-300:])
    if out.returncode:
        raise SendError("app.provider.codex.queue_exit", code=out.returncode)
    if HELD_ELSEWHERE in said:
        raise SendRefused("app.provider.codex.held_elsewhere")
    if NOT_RESUMED in said:
        raise SendError("app.provider.codex.not_resumed", output=said[-300:])


class QueuedTurnWatch:
    """Watches a thread's rollout from now on for a turn the queue started that carries the text. Made before
    the message is queued, so the turn cannot land before the point it reads from."""

    def __init__(self, home: Path, sid: str, text: str) -> None:
        self.home, self.sid, self.text = home, sid, text.strip()
        self.path = find_rollout(home, sid)  # None for a thread opened without a prompt: its turn writes one
        try:
            self.offset = self.path.stat().st_size if self.path else 0
        except OSError:  # archived or removed meanwhile
            self.path, self.offset = None, 0
        self.turn: str | None = None  # the id of the queue-started turn under way since the offset
        self.found: str | None = None  # the id of the one that carries the text

    def wait(self, seconds: float) -> str | None:
        """The turn's id ("" when Codex names none) once it appears within `seconds`, else None."""
        deadline = time.monotonic() + seconds
        while True:
            self.path = self.path or find_rollout(self.home, self.sid)
            if self.path is not None:
                self.read(self.path)
            if self.found is not None or time.monotonic() >= deadline:
                return self.found
            time.sleep(POLL_S)

    def read(self, path: Path) -> None:
        try:
            with path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
        except OSError:
            return
        complete = chunk[: chunk.rfind(b"\n") + 1]  # a half-written last line waits for the next read
        self.offset += len(complete)
        for raw in complete.splitlines():
            self.feed(raw)

    def feed(self, raw: bytes) -> None:
        try:
            entry = json.loads(raw)
        except ValueError:
            return
        if not isinstance(entry, dict):
            return
        if event_type(entry) == "task_started":
            self.turn = queue_turn(entry)
        elif self.turn is not None and (prompt := prompt_of(entry)) is not None and self.text in prompt:
            self.found = self.turn
