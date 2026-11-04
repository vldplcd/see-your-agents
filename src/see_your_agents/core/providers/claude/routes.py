"""Writing to Claude Code sessions: the route a message takes, a resume turn, stop, focus.

Routes of a message the user writes in the chat (decided per session, see `route_for`):
  vscode  the session is a live `claude` in a VS Code terminal: the bridge extension pastes the text
  tmux    a live `claude` inside tmux: tmux paste-buffer (bracketed) + Enter
  attach  a background session (`claude --bg`): the text is typed into it through `claude attach <job>`, as
          the user's own words (attach.py)
  peer    a live, named `claude` nothing can type into: the courier sends the text as a peer message (text
          only; the agent answers in its own session)
  resume  not running: one `claude -p --resume <id>` turn, appended to the same transcript
  takeover  a live, idle `claude` in a terminal nothing can type into: stop it there (SIGTERM), then resume;
          only with an explicit confirmation in the request, since it closes the terminal's session.
          Offered as the secondary action (`alt`) next to `peer`, and the main one for an unnamed session
A signed message (`Message.sender`: a group, the morning review) is never typed into a terminal: a live
session gets it by courier under the sender's name, a stopped one by a resume turn. Nor is a text Claude Code
would run as a command ("/", "!", "#" first): a named session gets it by courier.
Text is never sent to a session that waits on a permission prompt or dialog: it would land in the dialog.
A background session has no terminal of its own: the prompt is answered after `claude attach <job id>`.

What only the dashboard knows comes through `plug`, once, from the feature that wires Claude in (the chat's
`Feature.setup`): the VS Code bridge, every live session (a peer name must be unique), and the flags a feature
asks for a resume of a session it launched. Core never imports that feature.
"""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .... import cache_dir
from ....i18n import message as message_of
from ....i18n import request_lang
from ...uploads import session_uploads
from ..base import (
    Kind,
    LiveSession,
    Message,
    Route,
    SendError,
    SendInvalid,
    SendRefused,
    SendTimeout,
    Sent,
    Session,
    Status,
)
from . import ID, attach, courier
from .live import registry_by_pid
from .paths import claude_home
from .transcript import REPLY_NOTE

# How a chat message reaches a session by courier. Transcripts before it was English say «Дашборд»: the chat
# shows a message from either as the user's own.
SENDER = "Dashboard"
SUMMARY_LEN = 60
SNAPSHOT_TTL_S = 5.0
START_TOLERANCE_S = 2.0
EXIT_WAIT_S = 10.0
WAITING = {Status.WAITING, Status.BLOCKED}
BACKGROUND = {"bg", Kind.BACKGROUND}
# Reasons and errors are catalog keys (web/i18n/*.json), worded where they are shown.
NOT_THE_PROCESS = "app.provider.claude.not_the_process"
UNPLUGGED = "app.provider.claude.unplugged"
NO_CLAUDE = "app.provider.claude.no_claude"
WAITING_PROMPT = "app.provider.claude.waiting"
NAMESAKES = "app.provider.claude.namesakes"
COMMAND_TEXT = "app.provider.claude.command_text"


class Terminals(Protocol):
    """A way into terminals this provider cannot type into by itself: the chat's VS Code bridge module."""

    def shells(self) -> frozenset[int] | None:
        """Shell pids it can type into now; None when no bridge answers at all."""

    def send(self, shell_pid: int, text: str) -> None:
        """Paste the text into that shell's terminal and press Enter; raises SendError."""

    def focus(self, shell_pid: int) -> None:
        """Bring that terminal's window to the front; raises SendError."""


@dataclass
class _Host:
    terminals: Terminals | None = None
    live: Callable[[], Mapping[str, LiveSession]] | None = None
    resume_args: Callable[[str], Sequence[str]] | None = None  # None: not plugged, and a resume refuses


_host = _Host()


def plug(
    terminals: Terminals | None,
    live: Callable[[], Mapping[str, LiveSession]],
    resume_args: Callable[[str], Sequence[str]],
) -> None:
    """Give the routes the dashboard's side: the bridge, every live session, the flags for a resume."""
    global _host
    _host = _Host(terminals, live, resume_args)
    reset_caches()


def unplug() -> None:
    global _host
    _host = _Host()
    reset_caches()


def _fleet() -> list[LiveSession]:
    """Claude's own live sessions as the dashboard sees them: a peer name must be unique among these."""
    return [s for s in (_host.live() if _host.live else {}).values() if s.provider == ID]


# ---- process snapshot -------------------------------------------------------------------------


@dataclass
class Snapshot:
    parents: dict[int, int]  # pid -> ppid, every process
    registry: dict[int, dict]  # ~/.claude/sessions/<pid>.json by pid
    shells: set[int]  # shell pids of terminals that a plugged-in bridge can type into
    bridge_alive: bool


_snap_lock = threading.Lock()
_snap_cache: tuple[float, Path, Snapshot] | None = None


def _ps_parents() -> dict[int, int]:
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    pairs = (line.split() for line in out.stdout.splitlines())
    return {int(a): int(b) for a, b in (p for p in pairs if len(p) == 2 and all(x.isdigit() for x in p))}


def snapshot() -> Snapshot:
    """Everything route detection needs, taken once per few seconds however many cards ask."""
    global _snap_cache
    home = claude_home()
    with _snap_lock:
        if _snap_cache and _snap_cache[1] == home and time.monotonic() - _snap_cache[0] < SNAPSHOT_TTL_S:
            return _snap_cache[2]
        shells = _host.terminals.shells() if _host.terminals else None
        snap = Snapshot(_ps_parents(), registry_by_pid(home), set(shells or ()), shells is not None)
        _snap_cache = (time.monotonic(), home, snap)
        return snap


def reset_caches() -> None:
    global _snap_cache
    with _snap_lock:
        _snap_cache = None


def _ancestors(pid: int, parents: dict[int, int], limit: int = 6) -> list[int]:
    chain = []
    while len(chain) < limit and (pid := parents.get(pid, 0)) > 1:
        chain.append(pid)
    return chain


def _tmux_target(reg: dict) -> str:
    """The registry's `tmux` field names the pane; its exact shape is not documented, so accept both."""
    field = reg.get("tmux")
    if isinstance(field, str):
        return field
    if isinstance(field, dict):
        for key in ("paneId", "pane", "target", "pane_id"):
            if isinstance(field.get(key), str) and field[key]:
                return field[key]
    return ""


# ---- turns and deliveries in flight -----------------------------------------------------------

_runs: dict[str, subprocess.Popen] = {}
_sending: set[str] = set()  # sessions with a chat message on its way by courier or attach
_runs_lock = threading.Lock()


def busy(session_id: str) -> bool:
    """A reply turn (`claude -p --resume`) runs for the session. A courier on its way is not one: the session
    still takes messages, so the page keeps its input open (`_chat_by_courier` refuses a second courier)."""
    with _runs_lock:
        proc = _runs.get(session_id)
        if proc is not None and proc.poll() is not None:
            del _runs[session_id]
            proc = None
        return proc is not None


# ---- route detection --------------------------------------------------------------------------


def _resume_blocker(cwd: str, transcript_missing: bool) -> str:
    if transcript_missing or not cwd or not _is_dir(cwd):
        return "app.provider.claude.no_transcript"
    return "" if shutil.which("claude") else NO_CLAUDE


def _is_dir(path: str) -> bool:
    """A folder the disk can stat: is_dir() raises ENAMETOOLONG and the like, the board takes it as none."""
    try:
        return Path(path).is_dir()
    except OSError:
        return False


def route_for(
    live: LiveSession | None, cwd: str, transcript_missing: bool, is_running: bool, snap: Snapshot
) -> Route:
    """The route of a chat message: the user's own words, typed into the terminal when one is reachable."""
    if is_running:
        return Route("resume", False, "app.provider.claude.replying")
    if live is None:
        if blocker := _resume_blocker(cwd, transcript_missing):
            return Route(None, False, blocker)
        return Route("resume", True, "app.provider.claude.resume")
    if live.kind in BACKGROUND:
        return _background_route(live)
    reg = snap.registry.get(live.pid or 0, {})
    route = Route(None, True, "")
    if tmux := _tmux_target(reg):
        route = Route("tmux", True, "app.provider.claude.tmux", tmux)
    elif shell := next((p for p in _ancestors(live.pid or 0, snap.parents) if p in snap.shells), None):
        route = Route("vscode", True, "app.provider.claude.vscode", shell)
    if live.waiting_for or live.status in WAITING:
        return Route(route.route, False, WAITING_PROMPT, route.target)
    if route.route:
        return route
    # Nothing can type into its terminal. The courier reaches a named session whatever it is doing;
    # taking it over needs the agent to have finished its turn.
    if live.name:
        can_take_over = live.status == Status.IDLE and not _resume_blocker(cwd, False)
        reason = "app.provider.claude.peer"
        return Route("peer", True, reason, live.name, "takeover" if can_take_over else None)
    if live.status != Status.IDLE:
        return Route(None, False, "app.provider.claude.working")
    if blocker := _resume_blocker(cwd, False):
        return Route(None, False, blocker)
    reason = "app.provider.claude.takeover" if snap.bridge_alive else "app.provider.claude.takeover_no_bridge"
    return Route("takeover", True, reason)


def _background_route(live: LiveSession) -> Route:
    """A `claude --bg` session: the text is typed into it by `claude attach`; a permission prompt waits for a
    terminal. Without its job id only the courier reaches it, by name.

    Seen with Claude Code 2.1.317: on a permission prompt `claude agents` gives status "waiting" and
    waitingFor "permission prompt" (state "blocked"); a job that ended its turn with a question is only
    "blocked", with its `needs` ("confirm: …") and no waitingFor, and a message answers it."""
    if live.waiting_for or live.status == Status.WAITING:
        return Route(None, False, "app.provider.claude.bg_waiting", params={"attach": _attach_command(live)})
    if live.job_id and shutil.which("claude"):
        return Route("attach", True, "app.provider.claude.bg_attach", live.job_id)
    return _background_peer(live)


def _background_peer(live: LiveSession) -> Route:
    if not live.address:
        return Route(None, False, "app.provider.claude.bg_unnamed", params={"attach": _attach_command(live)})
    return Route("peer", True, "app.provider.claude.bg_peer", live.address)


def _attach_command(live: LiveSession) -> str:
    return f"claude attach {live.job_id}" if live.job_id else "claude attach"


def _namesakes(name: str) -> int:
    return sum(other.name.casefold() == name.casefold() for other in _fleet())


def _signed_route(live: LiveSession) -> Route:
    """A signed message to a live session goes by courier, whatever terminal it runs in."""
    # a background job that ended its turn with a question is only "blocked": a message answers it
    on_prompt = live.status == Status.WAITING if live.kind in BACKGROUND else live.status in WAITING
    if live.waiting_for or on_prompt:
        return Route(None, False, WAITING_PROMPT)
    if not live.address:
        return Route(None, False, "app.provider.claude.no_name")
    if _namesakes(live.address) > 1:
        return Route(None, False, NAMESAKES, params={"name": live.name})
    return Route("peer", True, "app.provider.claude.signed_peer", live.address)


def route(session: Session, signed: bool = False) -> Route:
    pending = busy(session.session_id)
    if signed and session.live is not None:
        found = _signed_route(session.live)
    else:
        found = route_for(session.live, session.cwd, session.transcript_missing, pending, snapshot())
    return replace(found, pending=pending)


# ---- process identity -------------------------------------------------------------------------


def _parse_start(text: str, tz) -> datetime | None:
    try:
        return datetime.strptime(" ".join(text.split()), "%a %b %d %H:%M:%S %Y").replace(tzinfo=tz)
    except ValueError:
        return None


def verify_pid(pid: int | None, proc_start: str | None, comm: str = "claude") -> None:
    """Raise unless `pid` is still the process the registry described.

    The registry's procStart is UTC while `ps -o lstart=` prints local time, so both are parsed to instants.
    """
    if not pid or not proc_start:
        raise SendRefused(NOT_THE_PROCESS)
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise SendRefused(NOT_THE_PROCESS) from None
    if len(out.split()) < 6:
        raise SendRefused(NOT_THE_PROCESS)
    start_text, name = " ".join(out.split()[:5]), out.split(None, 5)[5]
    actual = _parse_start(start_text, datetime.now().astimezone().tzinfo)
    expected = _parse_start(proc_start, UTC)
    if not actual or not expected or abs((actual - expected).total_seconds()) > START_TOLERANCE_S:
        raise SendRefused(NOT_THE_PROCESS)
    if Path(name).name != comm:
        raise SendRefused(NOT_THE_PROCESS)


def verify_live(live: LiveSession) -> None:
    reg = registry_by_pid(claude_home()).get(live.pid or 0, {})
    if reg.get("sessionId") != live.session_id:
        raise SendRefused(NOT_THE_PROCESS)
    verify_pid(live.pid, reg.get("procStart"))


# ---- sending ----------------------------------------------------------------------------------


def compose(message: Message) -> str:
    """The text plus `@/abs/path` lines: Claude Code expands an @-mention into the file's content
    without a tool call (checked with claude -p --resume)."""
    return message.text + "".join(f"\n@{p}" for p in message.files)


def _tmux_paste(target: str, text: str) -> None:
    tmux = shutil.which("tmux")
    if not tmux:
        raise SendError("app.provider.claude.no_tmux")
    buf = f"sya-{secrets.token_hex(4)}"
    try:
        subprocess.run([tmux, "load-buffer", "-b", buf, "-"], input=text, text=True, check=True, timeout=5)
        subprocess.run([tmux, "paste-buffer", "-p", "-d", "-b", buf, "-t", target], check=True, timeout=5)
        time.sleep(0.15)
        subprocess.run([tmux, "send-keys", "-t", target, "Enter"], check=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SendError("app.provider.claude.tmux_failed", error=str(exc)) from exc


def resume(session: Session, message: Message) -> Path:
    """One `claude -p --resume` turn in the background, with the flags the session's own feature asks for and
    the session's uploads readable; returns its log."""
    exe = shutil.which("claude")
    if not exe:
        raise SendError(NO_CLAUDE)
    if _host.resume_args is None:
        raise SendRefused(UNPLUGGED)
    session_id = session.session_id
    extra = list(_host.resume_args(session_id))  # before the lock: the supervisor writes its files here
    uploads = session_uploads(session_id)
    log = cache_dir() / "runs" / f"{session_id}-{datetime.now():%Y%m%d-%H%M%S}.log"
    with _runs_lock:
        existing = _runs.get(session_id)
        if existing is not None and existing.poll() is None:
            raise SendRefused("app.provider.claude.reply_running")
        log.parent.mkdir(parents=True, exist_ok=True)
        # --resume takes an optional value: joined with "=", an id that starts with "-" cannot become a flag
        cmd = [exe, "-p", f"--resume={session_id}", *extra]
        if uploads.is_dir():
            cmd += ["--add-dir", str(uploads)]
        with log.open("wb") as out:
            _runs[session_id] = subprocess.Popen(
                [*cmd, "--", compose(message)],
                cwd=session.cwd,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    return log


def _terminate(pid: int | None) -> None:
    """SIGTERM and wait for the process to go: a resume must not run next to the terminal's claude."""
    if not pid:
        raise SendRefused("app.provider.claude.no_process")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + EXIT_WAIT_S
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise SendTimeout("app.provider.claude.no_exit", s=f"{EXIT_WAIT_S:.0f}")


def _by_courier(to: str, message: Message, sender: str) -> courier.Delivery:
    summary = message.summary or " ".join(message.text.split())[:SUMMARY_LEN]
    text = (REPLY_NOTE if sender == SENDER else "") + compose(message)  # a group's header says it already
    return courier.deliver(to=to, text=text, summary=summary, sender=sender)


def _signed_by_courier(to: str, message: Message) -> Sent:
    result = _by_courier(to, message, message.sender)
    if not result.ok:
        raise result.problem()
    return Sent("peer", msg_id=result.msg_id)


@contextmanager
def _one_at_a_time(session_id: str) -> Iterator[None]:
    """One chat message on its way per session, in the request's own thread. The page sends a session's
    messages in order, so a second one at once comes only from another tab."""
    with _runs_lock:
        if session_id in _sending:
            raise SendRefused("app.provider.claude.sending")
        _sending.add(session_id)
    try:
        yield
    finally:
        with _runs_lock:
            _sending.discard(session_id)


def _chat_by_courier(session_id: str, to: str, message: Message) -> Sent:
    """The courier for a chat message (3-10 s)."""
    with _one_at_a_time(session_id):
        result = _by_courier(to, message, SENDER)
    if not result.ok:  # in the request's thread: the reason is worded in its language
        raise SendError(
            "app.provider.claude.not_delivered", reason=message_of(result.problem(), request_lang())
        )
    return Sent("peer", msg_id=result.msg_id)


def send(session: Session, message: Message) -> Sent:
    found = route(session, signed=bool(message.sender))
    if not found.can_send:
        raise found.refusal()
    if found.route == "resume":
        return Sent("resume", str(resume(session, message)))
    live = session.live
    assert live is not None  # every other route is to a live session
    if message.sender:
        return _signed_by_courier(str(found.target), message)
    if found.route == "attach" and not attach.typeable(message.text):
        found = _background_peer(live)
        if not found.can_send:
            raise SendRefused(COMMAND_TEXT)
    if found.route == "attach":
        return _by_attach(live, message)
    if found.route == "peer" and not message.takeover:
        if message.files:
            raise SendInvalid("app.provider.claude.text_only")
        if live.kind not in BACKGROUND:  # claude's daemon owns a background session; the name finds it
            verify_live(live)
        name = str(found.target)
        if _namesakes(name) > 1:
            raise SendRefused(NAMESAKES, name=name)
        return _chat_by_courier(session.session_id, name, message)
    if found.route == "peer" and found.alt != "takeover":
        raise SendRefused("app.provider.claude.no_takeover")
    if found.route in ("takeover", "peer"):
        if not message.takeover:
            raise SendRefused("app.provider.claude.confirm_takeover")
        verify_live(live)
        _terminate(live.pid)
        return Sent("takeover", str(resume(session, message)))
    verify_live(live)
    if found.route == "vscode":
        if _host.terminals is None:
            raise SendError("app.provider.claude.no_bridge")
        _host.terminals.send(int(found.target), compose(message))
    else:
        _tmux_paste(str(found.target), compose(message))
    return Sent(found.route)


def _by_attach(live: LiveSession, message: Message) -> Sent:
    """Typed into a background session (1-2 s): the user's own words, with the files as @-mentions."""
    exe = shutil.which("claude")
    if not exe:
        raise SendError(NO_CLAUDE)
    with _one_at_a_time(live.session_id):
        attach.type_into(exe, live.job_id, live.session_id, compose(message))
    return Sent("attach")


def stop(session: Session) -> None:
    """A background session through `claude stop <job>`; a terminal's process by SIGTERM, once it is known
    to be the registered one."""
    live = session.live
    if live is None:
        raise SendRefused("app.provider.claude.not_running")
    if live.kind in BACKGROUND:
        exe = shutil.which("claude")
        if not live.job_id or not exe:
            raise SendRefused("app.provider.claude.cant_stop")
        try:
            subprocess.run([exe, "stop", live.job_id], check=True, capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SendError("app.provider.claude.stop_failed", error=str(exc)) from exc
        return
    verify_live(live)
    os.kill(live.pid, signal.SIGTERM)


def focus(session: Session) -> None:
    """The VS Code window with the session's terminal, through the bridge."""
    found = route(session)
    if found.route != "vscode" or _host.terminals is None:
        raise SendRefused("app.provider.claude.no_window")
    _host.terminals.focus(int(found.target))
