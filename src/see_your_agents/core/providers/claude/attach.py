"""Typing a chat message into a background session: `claude attach <job>` on a pseudo-terminal, the way the
user would in a terminal.

Claude Code records text typed there as the user's own (`origin.kind` "human"); a courier's message is a
peer's, and auto mode does not take a peer's request for the user's. Seen with Claude Code 2.1.318:
- one bracketed paste of about 1000 characters or of several lines collapses into "[Pasted text #N]" and
  reaches the agent as pasted content, so each line goes as pastes of at most PIECE characters, lines joined
  by LF (Ctrl+J; ESC CR does not work between pastes);
- a text that starts with "/" runs as a slash command even pasted: `typeable` says which texts not to type;
- with a permission dialog open, pastes and LF are ignored while CR answers its highlighted "Yes", so CR goes
  only after `claude agents` says the session is not on a prompt, asked again right before it;
- a text that ends in an @-mention (an attached file) keeps the file suggestions open, and CR picks one
  instead of sending: a space after the text closes them;
- every attach client shares the input box, and unsent text stays in it: CR goes only once the text is drawn,
  and the message counts as delivered only once the transcript has it, typed or queued mid-turn.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import struct
import subprocess
import termios
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..base import SendError, SendRefused, SendTimeout
from .live import on_prompt
from .paths import claude_home

PIECE = 200
ROWS, COLS = 60, 200
READY_S = 15.0  # the client starts and draws the session's input
SETTLE_S = 0.3  # after the prompt mark: what attach draws first is done
DRAWN_S = 5.0
RECORDED_S = 10.0
TAIL = 60  # the end of the text that must be drawn before Enter
PROMPT_MARK = "❯"
COMMAND_STARTS = ("/", "!", "#")  # a slash command, a shell line, a memory note
_ESCAPES = re.compile(
    r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78]"
)
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # tab and newline stay


def typeable(text: str) -> bool:
    """A text Claude Code would not take for a command."""
    return not text.lstrip().startswith(COMMAND_STARTS)


def keystrokes(text: str) -> bytes:
    """What the terminal sends for the text: each line as bracketed pastes of at most PIECE characters, LF
    between lines, a space at the end. A control character could end a paste or press a key: none is sent."""

    def line(s: str) -> bytes:
        pieces = (s[i : i + PIECE] for i in range(0, len(s), PIECE))
        return b"".join(b"\x1b[200~" + p.encode() + b"\x1b[201~" for p in pieces)

    return b"\n".join(line(s) for s in _CONTROLS.sub("", text).split("\n")) + b" "


def _squash(text: str) -> str:
    """Without whitespace: a terminal draws spaces and line wraps as cursor moves."""
    return "".join(text.split())


class _Client:
    """`claude attach <job>` on a pseudo-terminal, its output collected as it comes."""

    def __init__(self, exe: str, job_id: str) -> None:
        master, slave = os.openpty()
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        try:
            self.proc = subprocess.Popen(
                [exe, "attach", job_id],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env={**os.environ, "TERM": "xterm-256color"},
                start_new_session=True,
            )
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.fd, self.out = master, bytearray()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:  # the client exited
                return
            if not chunk:
                return
            self.out += chunk

    def screen(self, since: int = 0) -> str:
        return _ESCAPES.sub("", bytes(self.out[since:]).decode("utf-8", "replace"))

    def wait_for(self, done: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not done():
            if time.monotonic() > deadline or self.proc.poll() is not None:
                return done()
            time.sleep(0.05)
        return True

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view) :]

    def close(self) -> None:
        """Detach: the session runs on without this client."""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        os.close(self.fd)


def _transcript(session_id: str) -> Path | None:
    return next((claude_home() / "projects").glob(f"*/{session_id}.jsonl"), None)


def _size(path: Path | None) -> int:
    try:
        return path.stat().st_size if path else 0
    except OSError:
        return 0


def _typed(entry: dict) -> str | None:
    """What a human typed, as the transcript keeps it: a prompt, or one queued while the agent works."""
    if entry.get("type") == "user" and (entry.get("origin") or {}).get("kind") == "human":
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
        return content if isinstance(content, str) else None
    if entry.get("type") == "queue-operation" and entry.get("operation") == "enqueue":
        return entry.get("content") if isinstance(entry.get("content"), str) else None
    return None


def _recorded(path: Path | None, offset: int, text: str) -> bool:
    """The transcript, past `offset`, has the text as typed. A draft that sat in the input comes before it; a
    text the input collapsed into pasted content does not end with it."""
    if path is None:
        return False
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            lines = fh.read().splitlines()
    except OSError:
        return False
    want = _squash(text)
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        said = _typed(entry) if isinstance(entry, dict) else None
        if said is not None and _squash(said).endswith(want):
            return True
    return False


def _free(session_id: str, attach: str, typed: bool) -> None:
    """Raise unless the session is running and not on a prompt, where Enter would answer the dialog."""
    waiting = on_prompt(session_id)
    if waiting is None:
        raise SendRefused("app.provider.claude.not_running")
    if waiting:
        key = "app.provider.claude.attach_prompt" if typed else "app.provider.claude.bg_waiting"
        raise SendRefused(key, attach=attach)


def type_into(exe: str, job_id: str, session_id: str, text: str) -> None:
    """Type the text into the session and press Enter; returns once the session's transcript has it."""
    attach, text = f"claude attach {job_id}", _CONTROLS.sub("", text)  # as drawn and recorded
    path = _transcript(session_id)
    offset = _size(path)
    try:
        client = _Client(exe, job_id)
    except OSError as exc:
        raise SendError("app.provider.claude.attach_failed", error=str(exc)) from exc
    try:
        if not client.wait_for(lambda: PROMPT_MARK in client.screen(), READY_S):
            if client.proc.poll() is not None:
                client.reader.join(1.0)  # what it said before it exited
                lines = [ln.strip() for ln in client.screen().splitlines() if ln.strip()]
                raise SendError("app.provider.claude.attach_failed", error=lines[-1] if lines else "")
            raise SendTimeout("app.provider.claude.attach_no_input", s=f"{READY_S:.0f}")
        time.sleep(SETTLE_S)
        _free(session_id, attach, typed=False)
        mark, tail = len(client.out), _squash(text)[-TAIL:]
        client.write(keystrokes(text))
        if not client.wait_for(lambda: tail in _squash(client.screen(mark)), DRAWN_S):
            raise SendError("app.provider.claude.attach_not_drawn", s=f"{DRAWN_S:.0f}", attach=attach)
        _free(session_id, attach, typed=True)
        client.write(b"\r")

        def recorded() -> bool:
            nonlocal path
            path = path or _transcript(session_id)
            return _recorded(path, offset, text)

        if not client.wait_for(recorded, RECORDED_S):
            raise SendTimeout("app.provider.claude.attach_unconfirmed", s=f"{RECORDED_S:.0f}")
    except OSError as exc:  # the client exited while being written to
        raise SendError("app.provider.claude.attach_failed", error=str(exc)) from exc
    finally:
        client.close()
