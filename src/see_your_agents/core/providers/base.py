"""The provider contract: what the dashboard needs from an agent program (Claude Code, Codex…).

A provider is a folder in core/providers/ with a `Provider` subclass and a line in providers/__init__.py.
Nothing outside it runs the program or reads its files: the index, the board and the features ask the
session's provider (`providers.by_id(record["provider"])`) or the registry. What a program cannot do is
missing from its `caps`, and the page hides it. How to add one: docs/providers.md.
"""

from __future__ import annotations

import os
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import ClassVar, TypedDict

from ...i18n import Problem, request_lang, t


class Cap(StrEnum):
    """What a provider can do beyond the minimum (transcripts, live state). Without a cap the page hides the
    control or the field, it does not show it empty."""

    TASKS = "tasks"  # the session's task list is on disk: progress on the card
    COST = "cost"  # the transcript records the cost in dollars (and the lines changed); without it, tokens
    PEER = "peer"  # live sessions message each other: the courier, groups, the supervisor's SendMessage
    # The prompt itself reaches the dashboard (what is asked), so the page may answer it. That a session waits
    # needs no cap: any provider says so in LiveSession.status / waiting_for, and the board shows "Needs you".
    PERMISSION_WAIT = "permission_wait"
    BACKGROUND = "background"  # the program runs a session in the background itself (`claude --bg`)
    NAME = "name"  # a session gets a name at launch
    ATTACH = "attach"  # a terminal command opens the session: "Open in terminal"
    STOP = "stop"  # the dashboard stops a running session's process: "Finish"
    FORK = "fork"  # a new session starts with a copy of this one's history: "Fork"
    LAUNCH = "launch"  # "New session" starts one: the program to pick when more than one can
    FILES = "files"  # a message from the chat may carry files (on the routes that take them): the clip


class Kind(StrEnum):
    """LiveSession.kind: how the session runs. Plain strings compare equal to these."""

    INTERACTIVE = "interactive"
    BACKGROUND = "background"


class Status(StrEnum):
    """The LiveSession.status values the board sorts by; a provider maps its program's states onto these and
    may pass others through, which the board treats as "your move"."""

    BUSY = "busy"
    SHELL = "shell"  # running a shell command
    IDLE = "idle"
    WAITING = "waiting"  # on a permission prompt or a dialog
    BLOCKED = "blocked"  # needs the user


class TaskItem(TypedDict):
    id: str
    subject: str
    active_form: str  # "Running the tests": what the card shows while it is in progress
    status: str  # pending | in_progress | completed


class Event(TypedDict):
    """A line of the session's history the index stores for search: a prompt, a plan, a peer message…"""

    ts: str
    kind: str
    text: str


@dataclass
class Digest:
    """What was in one session, as its provider's parser read the transcript. A field a provider has no
    data for stays empty."""

    session_id: str
    path: str
    cwd: str = ""
    project: str = ""
    git_branch: str = ""
    worktree: str = ""
    ai_title: str = ""
    custom_name: str = ""
    started_at: str = ""
    last_activity: str = ""
    first_prompt: str = ""
    last_prompt: str = ""
    last_prompt_at: str = ""
    n_prompts: int = 0
    n_replies: int = 0  # assistant messages with text; the chat's unread badge counts from it
    n_tool_calls: int = 0
    last_assistant_text: str = ""
    pending_tool: str = ""
    last_turn_aborted: bool = False  # the last turn was interrupted, not finished (Codex: turn_aborted)
    recap: str = ""
    recap_at: str = ""
    cost_usd: float = 0.0
    tokens: int = 0  # tokens the session used: what a program without Cap.COST shows instead
    # How full the context window is: the tokens the latest request carried, of the window the model has.
    # context_window 0: the provider cannot tell, and the page shows nothing.
    context_tokens: int = 0
    context_window: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    models: list[str] = field(default_factory=list)
    jira_keys: list[str] = field(default_factory=list)
    jira_base: str = ""
    prs: list[dict] = field(default_factory=list)
    tasks: list[TaskItem] = field(default_factory=list)
    plans: list[dict] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    agents: list[dict] = field(default_factory=list)
    messages_out: list[dict] = field(
        default_factory=list
    )  # cross-session SendMessage: ts, to, summary, text, msg_id, ok
    # peer messages received: ts, from, text, msg_id, and reply / reply_at, the session's last text after it
    messages_in: list[dict] = field(default_factory=list)
    # turns the program's own queue started (Codex: `codex queue`), not peer messages: ts, msg_id (the turn's
    # id, what `send` returned as Sent.msg_id), reply / reply_at (the turn's latest answer)
    queued_turns: list[dict] = field(default_factory=list)
    top_tools: list[list] = field(default_factory=list)
    # skill:<name>, agent:<type>, mcp:<server>, mcp_tool:<server>/<tool>, command:<name> -> [count, last ts]
    tool_usage: dict[str, list] = field(default_factory=dict)
    continued_in: str = ""
    events: list[Event] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.custom_name or self.ai_title or self.first_prompt[:80] or self.session_id[:8]

    def to_record(self) -> dict:
        """Everything except the event log, which is stored separately."""
        rec = asdict(self)
        rec.pop("events")
        rec["title"] = self.title
        return rec


@dataclass
class LiveSession:
    """A session whose process runs now, as its provider sees it."""

    session_id: str
    kind: str  # a Kind
    status: str  # a Status, or the program's own word for it
    waiting_for: str = ""
    name: str = ""
    pid: int | None = None
    cwd: str = ""
    status_since_ms: int | None = None
    updated_at: str = ""  # ISO time of the last state change the program recorded
    needs: str = ""  # background agents: what they need from you
    detail: str = ""  # background agents: one-line state
    suggested_reply: str = ""
    job_id: str = ""  # the program's handle for a background run (`claude attach <job_id>`)
    name_source: str = ""  # user (-n / --name), derived (folder + id), auto
    host: str = ""  # what holds the session, where the program tells: Codex daemon | vscode | exec | tui
    provider: str = field(kw_only=True)  # the provider's id

    @property
    def address(self) -> str:
        """The name a peer message finds the session by. Derived names are addresses too (a peer's reply
        goes to one); a name the program made up ("auto") is not known to be one, so it gives ""."""
        return "" if self.name_source == "auto" else self.name


@dataclass(frozen=True)
class Session:
    """One session as a provider acts on it: facts from the index record and the live state, if any."""

    session_id: str
    cwd: str = ""
    transcript_missing: bool = False
    live: LiveSession | None = None
    last_turn_aborted: bool = False  # Digest.last_turn_aborted as of the last parse

    @classmethod
    def of(cls, record: dict, live: LiveSession | None) -> Session:
        cwd = record.get("cwd") or (live.cwd if live else "")
        aborted = bool(record.get("last_turn_aborted"))
        return cls(record["session_id"], cwd, record.get("transcript_missing", False), live, aborted)


# ---- writing to a session ---------------------------------------------------------------------

MAX_TEXT = 20_000  # one message from the dashboard


@dataclass(frozen=True)
class Route:
    """How a message from the dashboard would reach a session now, and whether it may go."""

    route: str | None  # the provider's own route name ("resume", "peer"…); None when nothing reaches it
    can_send: bool
    # For the user: why this way, or why not. A catalog key (web/i18n, app.provider.*) with `params`, put
    # into words by why() when it is shown; "" says nothing.
    reason: str
    target: int | str | None = None  # what the route delivers to: a shell pid, a tmux pane, a peer's name
    alt: str | None = None  # a second route the user may pick instead (peer -> takeover)
    pending: bool = False  # a reply the dashboard started (a resume turn, a delivery) is still under way
    params: Mapping[str, object] = field(default_factory=dict)  # the reason's placeholders

    def why(self, lang: str | None = None) -> str:
        """The reason in words: in `lang`, else in the language of the request being answered."""
        return t(lang or request_lang(), self.reason, **self.params) if self.reason else ""

    def refusal(self) -> SendRefused:
        """What sending along this route raises when it may not go: its reason, worded at the edge."""
        return SendRefused(self.reason, **self.params)

    def public(self) -> dict[str, str | bool | None]:
        """The card's `delivery` field, the reason in the request's language; `pending` goes to the card as
        `running_reply`."""
        return {"route": self.route, "can_send": self.can_send, "reason": self.why(), "alt": self.alt}


@dataclass(frozen=True)
class Message:
    """Text from the dashboard to a session: what the user wrote or explicitly chose to send."""

    text: str
    files: tuple[str, ...] = ()  # the dashboard's uploads, absolute; the provider mentions or refuses them
    takeover: bool = False  # the user agreed to close the session in its terminal and continue it here
    # Who signs it. "" — the user, in the chat: the provider may type it into the session's terminal. A name
    # (a group, the morning review): a live session must see it as a message from that sender.
    sender: str = ""
    summary: str = ""  # one line for the recipient's list of messages; "" — the start of the text


@dataclass(frozen=True)
class Sent:
    route: str  # the route the message took
    log: str = ""  # a resume turn's output file
    msg_id: str = ""  # a message to a live session: the id its answer will carry


class SendError(Problem):
    """Nothing reached the session: the program or a tool failed. Every writing operation reports failure
    this way, never by a return value. A Problem: a catalog key (web/i18n, app.provider.*) and its params,
    worded at the edge (the page in its language; an agent and the log in English). The class fixes the
    HTTP status."""

    STATUS: ClassVar[int] = HTTPStatus.BAD_GATEWAY

    def __init__(self, key: str, /, **params: object):
        super().__init__(key, self.STATUS, **params)


class SendRefused(SendError):
    """The session's state forbids it now: busy, waiting on a prompt, not the process it was, unconfirmed."""

    STATUS = HTTPStatus.CONFLICT


class SendInvalid(SendError):
    """The message cannot go this way at all (files to a route that carries only text)."""

    STATUS = HTTPStatus.BAD_REQUEST


class SendTimeout(SendError):
    """The program did not do its part in time (a terminal's process did not exit)."""

    STATUS = HTTPStatus.GATEWAY_TIMEOUT


# ---- starting a session ----------------------------------------------------------------------

MAX_PROMPT = 20_000
MAX_NAME = 80


class LaunchError(Problem):
    """Why a session did not start: the program refused or did not answer. A Problem with a catalog key
    (web/i18n, app.provider.*), worded at the edge like SendError."""

    STATUS: ClassVar[int] = HTTPStatus.BAD_GATEWAY

    def __init__(self, key: str, /, **params: object):
        super().__init__(key, self.STATUS, **params)


class RequestError(LaunchError):
    """The request itself is wrong (folder, option, text): nothing was started."""

    STATUS = HTTPStatus.BAD_REQUEST


@dataclass(frozen=True)
class LaunchRequest:
    """A new session. The fields are what every program understands; the rest is `choices`, by the keys of
    the provider's `options().choices` (Claude: model, effort, permission_mode; Codex: model, effort,
    sandbox), and the provider refuses a key or a value its options do not list."""

    cwd: str  # as typed, "~/work/x" or absolute: start() checks it with project_dir
    prompt: str
    name: str = ""  # Cap.NAME: the session's name, its peer address; "" takes the folder's name
    worktree: bool = False  # work in a new git worktree, named after the session
    choices: Mapping[str, str] = field(
        default_factory=dict
    )  # "" or a missing key keeps the program's default


@dataclass(frozen=True)
class Started:
    job_id: str  # the program's handle for the run (Claude: the short id of `claude attach / stop`)
    name: str
    worktree: str = ""  # the worktree's name, "" when the session works in cwd itself
    session_id: str = ""  # "" when the program did not tell it in time
    attach: str = ""  # Cap.ATTACH: the command that opens it in a terminal


@dataclass(frozen=True)
class LaunchOptions:
    """What a start may choose: whitelists by LaunchRequest.choices key, and the model the program uses when
    none is chosen ("" when unknown). A fresh mapping each time: callers may keep or change it."""

    choices: Mapping[str, tuple[str, ...]]
    default_model: str = ""
    labels: Mapping[str, str] = field(default_factory=dict)  # a value's name for people: a model's own name
    # model -> the efforts it supports, narrower than choices["effort"]; a model missing here takes those
    efforts: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    defaults: Mapping[str, str] = field(default_factory=dict)  # what "" gives for a key other than model
    note: str = ""  # one line under the options: what the program does with them
    untrusted_note: str = ""  # why a folder outside trusted() would be refused


def checked_task(prompt: str) -> str:
    """A start's first message, stripped; RequestError when it is too long or carries a NUL."""
    prompt = prompt.strip()
    if len(prompt) > MAX_PROMPT:
        raise RequestError("app.provider.task_too_long", max=MAX_PROMPT)
    if "\x00" in prompt:
        raise RequestError("app.provider.task_nul")
    return prompt


def project_dir(raw: str) -> Path:
    """The folder rule every provider's start shares; "New session" offers only folders that pass it.
    `raw` as an existing project folder inside home: not home itself, nothing hidden (~/.ssh). The path is
    held against home before anything on disk is touched, and again once symlinks are followed."""
    outside = RequestError("app.provider.folder_outside")
    try:
        typed = Path(raw).expanduser()
    except RuntimeError as exc:  # ~unknownuser
        raise RequestError("app.provider.folder_bad", error=str(exc)) from exc
    if not typed.is_absolute():
        raise RequestError("app.provider.folder_relative")
    typed, home = Path(os.path.normpath(typed)), Path(os.path.normpath(Path.home()))
    if typed == home or not typed.is_relative_to(home):
        raise outside
    try:
        cwd, home = typed.resolve(), home.resolve()
        hidden = cwd.is_relative_to(home) and any(p.startswith(".") for p in cwd.relative_to(home).parts)
        if hidden:
            raise RequestError("app.provider.folder_hidden")
        if cwd == home or not cwd.is_relative_to(home) or not cwd.is_dir():
            raise outside
    except (OSError, ValueError, RuntimeError) as exc:  # a name too long, no access, a NUL byte, a loop
        raise RequestError("app.provider.folder_unreadable", error=str(exc)) from exc
    return cwd


# ---- reading a transcript as a chat -----------------------------------------------------------

# A `compact` message's text: for an agent reading the thread; the page puts its own words there.
COMPACTED = "Context compacted"


class Thread(ABC):
    """One transcript read as chat messages, extended as the file grows: the chat panel's view of a session.

    A message is a dict with `id` (its index: the page merges and pages by it), `at`, `kind` and the kind's
    fields: you {text, images}, agent {text}, tools {calls: [{name, summary, error}]}, question {items,
    answer}, plan {text, path}, recap {text}, compact {text}, peer_in {from, text, msg_id}, peer_out {to,
    text, summary, ok}. The chat keeps one Thread per transcript and holds `lock` while it syncs and reads.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.messages: list[dict] = []

    @abstractmethod
    def sync(self, path: Path) -> None:
        """Read what the file gained since the last call; start over when it was rewritten."""

    def first_mutable(self) -> int:
        """The oldest message a later line may still change (a tool call without its result): a client
        re-reads from there."""
        return len(self.messages)

    def image(self, path: Path, key: str, n: int) -> tuple[str, bytes] | None:
        """(media type, bytes) of the image a `you` message lists as `image/<key>/<n>`."""
        return None


class Provider(ABC):
    """One agent program. Reading (the four abstract operations) is required; everything else has a default
    that refuses or gives nothing, so a read-only provider is a valid first step."""

    id: ClassVar[str]  # [a-z][a-z0-9-]{0,31} (providers.ID), stored with every indexed session
    title: ClassVar[str]  # for people: "Claude Code"
    caps: ClassVar[frozenset[Cap]]
    parser_version: ClassVar[int]  # bump when `parse` changes: the index re-parses this provider's sessions

    @abstractmethod
    def available(self) -> bool:
        """The program or its data is on this machine. An unavailable provider is skipped everywhere."""

    @abstractmethod
    def transcripts(self) -> dict[str, Path]:
        """Every transcript on disk by session id; the index re-parses those whose size or mtime changed."""

    @abstractmethod
    def parse(self, path: Path) -> Digest:
        """One transcript as a Digest. Must not fail on an unknown entry: skip it."""

    @abstractmethod
    def live(self) -> dict[str, LiveSession]:
        """Sessions running now by id; {} when the program cannot tell. Called every couple of seconds."""

    def names(self) -> dict[str, str]:
        """Session names the program keeps outside its transcripts, by session id (Codex: thread names).
        The index reads them each time it hands out records, so a rename shows before the transcript
        changes; a name here is the session's title."""
        return {}

    def tasks(self, session: Session) -> list[TaskItem] | None:
        """Cap.TASKS: the session's current task list from disk, if it keeps one there."""
        return None

    def attach_command(self, session: Session) -> str:
        """Cap.ATTACH: a shell command that opens the session in a terminal; "" when there is none."""
        return ""

    def thread(self) -> Thread | None:
        """A new reader of one transcript as a chat; None when the chat cannot show this program's."""
        return None

    # ---- starting: the defaults refuse ---------------------------------------------------------

    def options(self) -> LaunchOptions:
        """What "New session" may choose for this program."""
        return LaunchOptions({})

    def trusted(self, folders: Iterable[Path]) -> set[Path]:
        """The folders where a start will not be refused for want of the program's trust in them."""
        return set(folders)

    def start(self, request: LaunchRequest, taken: Iterable[str] = ()) -> Started:
        """A new session on the request's first task, in the background. `taken`: names live sessions
        answer to (Cap.NAME: the new one gets -2, -3… past them). RequestError for a wrong request,
        LaunchError when the program refused."""
        raise LaunchError("app.provider.no_launch", program=self.title)

    def fork(self, session: Session, name: str = "", prompt: str = "", taken: Iterable[str] = ()) -> Started:
        """Cap.FORK: a new background session that starts with a copy of this one's history; the session
        itself stays as it is. `name`: the session's own name ("" — its folder's), the fork is named after
        it and unique past `taken`; `prompt`: the fork's first message, "" leaves it waiting. RequestError
        and LaunchError as for start()."""
        raise LaunchError("app.provider.no_fork", program=self.title)

    # ---- writing: the defaults refuse ----------------------------------------------------------

    def route(self, session: Session, signed: bool = False) -> Route:
        """How a message would reach the session now: one the user writes in the chat, or with `signed`, one
        from someone else (Message.sender). Asked for every card on each poll: keep it cheap."""
        return Route(None, False, "app.provider.no_write", params={"program": self.title})

    def send(self, session: Session, message: Message) -> Sent:
        """Deliver along `route(session, signed=bool(message.sender))`."""
        raise self.route(session, bool(message.sender)).refusal()

    def resume(self, session: Session, message: Message) -> Sent:
        """One turn in a session that is not running, in the background."""
        raise SendRefused("app.provider.no_resume", program=self.title)

    def stop(self, session: Session) -> None:
        """Stop a running session's process, only once it is known to be the session's own."""
        raise SendRefused("app.provider.no_stop", program=self.title)

    def focus(self, session: Session) -> None:
        """Bring the window the session runs in to the front."""
        raise SendRefused("app.provider.no_focus", program=self.title)
