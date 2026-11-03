"""New session: start a background session of an agent program in a project folder from the page.

GET  /api/launch             recent project folders and the programs that can start here: each one's options,
                             default model and the folders it trusts, the default provider first
GET  /api/launch/dirs?path=  subfolders for the folder field's suggestions
POST /api/launch             {provider?, cwd, prompt, name?, worktree, and its choices: model?, effort?, …}
                             -> {ok, job_id, name, worktree, session_id, attach}
POST /api/launch/fork        {session_id, prompt?} -> the same: Fork, a new session from a copy of this one's
                             history; without a prompt it waits

A program is an available provider with Cap.LAUNCH (`programs()`); a request without `provider` goes to the
default one. Its `options()`, `trusted()` and `start(LaunchRequest)` do the rest, with the limits and the free
name; the folder rule is the contract's `project_dir`. A fork goes to the session's own provider (`fork`,
Cap.FORK); cards say whether it may (`can_fork`: the cap, the transcript, the folder rule). Here: which
folders and programs to offer and the request's types. A refusal is {ok: false, error} in the request's
language, whoever words it: this module (a Problem) or the program (a LaunchError).
"""

from __future__ import annotations

import functools
import json
import os
import time
from collections import Counter
from dataclasses import asdict
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

from ...app.replies import Reply, json_reply
from ...core import providers
from ...core.providers.base import (
    Cap,
    LaunchError,
    LaunchRequest,
    LiveSession,
    Provider,
    RequestError,
    Session,
    Started,
    project_dir,
)
from ...i18n import Problem, message, request_lang

if TYPE_CHECKING:
    from email.message import Message

    from ...app.server import App

ROUTE = "/api/launch"
DIRS = "/api/launch/dirs"
FORK = "/api/launch/fork"
RECENT = 15
MAX_DIRS = 50
FOLDER_CHECK_S = 60  # a card's folder is checked again this often; the fork itself checks it anyway


def recent_folders(app: App) -> list[dict]:
    """Distinct working folders of indexed sessions, latest activity first, where a session may start; which
    program trusts which of them is in `describe_program()`."""
    records = sorted(app.index.records(), key=lambda r: r.get("last_activity") or "", reverse=True)
    resolved: dict[str, Path | None] = {}  # each recorded cwd checked once
    counts: Counter[Path] = Counter()
    latest: dict[Path, str] = {}  # in order of recency
    for rec in records:
        raw = rec.get("cwd") or ""
        if raw not in resolved:
            try:
                resolved[raw] = project_dir(raw)
            except RequestError:  # gone, outside home, hidden (.claude/worktrees/...), unreadable
                resolved[raw] = None
        if path := resolved[raw]:
            counts[path] += 1
            latest.setdefault(path, rec.get("last_activity", ""))
    return [
        {"path": str(p), "name": p.name, "sessions": counts[p], "last_activity": latest[p]}
        for p in list(latest)[:RECENT]
    ]


def programs() -> list[Provider]:
    """The providers New session can start here, in registry order: the default one first."""
    return [p for p in providers.available() if Cap.LAUNCH in p.caps]


def describe_program(provider: Provider, folders: list[Path]) -> dict:
    """What the dialog shows for one program: its options and which of the offered folders it trusts."""
    trusted = provider.trusted(folders)
    return {
        "id": provider.id,
        "title": provider.title,
        "caps": sorted(provider.caps),
        **asdict(provider.options()),
        "trusted": [str(f) for f in folders if f in trusted],
    }


def subfolders(raw: str) -> list[str]:
    """Up to MAX_DIRS folders under home that complete `raw` ("~/wo" -> ~/work, ~/workshop); none hidden."""
    home = Path.home().resolve()
    # strings, not Path: Path drops a trailing "/" (list this folder) and "/." (a hidden prefix)
    typed = os.path.join(str(home), os.path.expanduser(raw.strip() if raw.strip() not in ("", "~") else "~/"))
    head, _, prefix = typed.rpartition("/")
    try:
        folder = Path(head or "/").resolve()
        hidden = any(p.startswith(".") for p in folder.relative_to(home).parts)
    except (ValueError, OSError, RuntimeError):  # outside home, a NUL byte
        return []
    if hidden or prefix.startswith("."):
        return []
    try:
        with os.scandir(folder) as entries:
            names = [
                e.name
                for e in entries
                if not e.name.startswith(".") and e.name.lower().startswith(prefix.lower()) and e.is_dir()
            ]
    except OSError:
        return []
    return [str(folder / n) for n in sorted(names, key=str.lower)[:MAX_DIRS]]


def _text(data: dict, key: str) -> str:
    """A string field; missing is ""."""
    value = data.get(key, "")
    if not isinstance(value, str):
        raise Problem("launch.bad_text", field=key)
    return value


def launch(app: App, data: dict) -> Started:
    """Check the request's types and start; a Problem or a RequestError for a bad request."""
    if not isinstance(data.get("worktree", False), bool):
        raise Problem("launch.bad_worktree")
    wanted = _text(data, "provider") or providers.default().id
    provider = next((p for p in programs() if p.id == wanted), None)
    if provider is None:
        raise Problem("launch.no_program", program=wanted)
    request = LaunchRequest(
        _text(data, "cwd"),
        _text(data, "prompt"),
        _text(data, "name"),
        worktree=data.get("worktree", False),
        choices={key: _text(data, key) for key in provider.options().choices},
    )
    return provider.start(request, taken=[s.address for s in app.live().values()])


def can_fork(rec: dict, live: LiveSession | None) -> dict:
    """The card's `can_fork`: its provider forks sessions, the transcript is on disk to copy and its folder is
    one a session may start in. False hides Fork rather than offer what the server would refuse."""
    forks = Cap.FORK in providers.by_id(rec["provider"]).caps
    cwd = rec.get("cwd") or (live.cwd if live else "")
    on_disk = bool(rec.get("path")) and not rec.get("transcript_missing")
    return {"can_fork": forks and on_disk and _startable(cwd, int(time.monotonic() // FOLDER_CHECK_S))}


@functools.lru_cache(maxsize=4096)
def _startable(cwd: str, period: int) -> bool:
    """project_dir's verdict on `cwd`, asked once per folder and period: every card asks on every poll."""
    try:
        project_dir(cwd)
    except RequestError:
        return False
    return True


def fork(app: App, data: dict) -> Started:
    """Fork: the session's provider starts a copy of it, named after its live name or its own."""
    session_id, prompt = _text(data, "session_id"), _text(data, "prompt")
    running = app.live()
    live, record = running.get(session_id), app.index.record(session_id)
    if record is None:
        raise Problem("launch.fork.not_indexed")
    provider = providers.by_id(record["provider"])
    if Cap.FORK not in provider.caps:
        raise Problem("launch.fork.unsupported", program=provider.title)
    name = (live.name if live else "") or record.get("custom_name") or ""
    taken = [s.address for s in running.values()]
    return provider.fork(Session.of(record, live), name=name, prompt=prompt, taken=taken)


def handle_get(app: App, path: str, query: dict[str, str]) -> Reply | None:
    if path == ROUTE:
        folders = recent_folders(app)
        paths = [Path(f["path"]) for f in folders]
        found = [describe_program(p, paths) for p in programs()]
        return json_reply({"folders": folders, "home": str(Path.home().resolve()), "programs": found})
    if path == DIRS:
        return json_reply({"dirs": subfolders(query.get("path", ""))})
    return None


def handle_post(app: App, path: str, body: bytes, headers: Message) -> Reply | None:
    act = {ROUTE: launch, FORK: fork}.get(path)
    if act is None:
        return None
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = None
    try:
        if not isinstance(data, dict):
            raise Problem("app.bad_body")
        return json_reply({"ok": True, **asdict(act(app, data))})
    except LaunchError as exc:  # RequestError: a wrong request; else the program refused or did not answer
        wrong = isinstance(exc, RequestError)
        return _refused(exc, HTTPStatus.BAD_REQUEST if wrong else HTTPStatus.BAD_GATEWAY)
    except Problem as exc:  # this module's own checks
        return _refused(exc, exc.status)


def _refused(exc: Exception, status: int) -> Reply:
    return json_reply({"ok": False, "error": message(exc, request_lang())}, status)
