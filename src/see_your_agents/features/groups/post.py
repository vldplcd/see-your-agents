"""Writing into a group: who gets the text, the header each member sees, and how it reaches them.

Every member gets a signed message (`Message.sender` = the group) through its provider: the route is planned
when the post is logged (`Provider.route(session, signed=True)`) and taken in a thread (`Provider.send`).
The log names the delivery by the provider's route (`Route.route`, then `Sent.route`), for example:
  peer     Claude Code, running: a message under the group's name by the courier (SendMessage); logs written
           before providers say `courier`
  resume   Claude Code, closed but with a transcript and a folder: one turn, with the flags of the feature
           that launched it
  queue    Codex: `codex queue`, a turn in the same thread
  ""       refused: the provider's reason (waits on a permission prompt, held by another process, busy, no
           unique name…), not delivered
A member whose program cannot message other sessions (no Cap.PEER: Codex) is told to answer in plain text.

The header and the sender are for agents: English, never catalog strings. A refusal that is a Problem is kept
in the log as its key (`problem`), so the feed words it in the reader's language.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING

from ...core import providers
from ...core.providers.base import MAX_TEXT, Message, Provider, SendError, Session
from ...i18n import Problem, request_lang
from . import groups, handles, store
from .groups import Fleet

if TYPE_CHECKING:
    from ...app.server import App

SUMMARY_LEN = 60
# How a group's header and sender name start: feed.py finds a resumed member's answer by the first.
# The courier copies the header verbatim, so it stays plain: a bracketed first block, no aside after the text.
HEADER_START = "[Group “{title}”"
SENDER = "Group “{title}”"
_threads: list[threading.Thread] = []


def resolve_targets(group: dict, fleet: Fleet, text: str) -> list[str]:
    """@handle -> those members, @all -> everyone, no mention -> the supervisor if it is a member: live, by
    the courier; stopped, by one resume turn that `start_resume` runs with the supervisor's own config.
    The supervisor's handle and @all work in English and in every UI language."""
    by_handle = {groups.handle_of(group, fleet, sid): sid for sid in group["members"]}
    if fleet.supervisor_id in group["members"]:
        by_handle.update(dict.fromkeys(handles.supervisor_words(), fleet.supervisor_id))
    words = handles.mentions(text)
    if handles.everyone_words() & set(words):
        return list(group["members"])
    known = [by_handle[w] for w in words if w in by_handle]
    if known:
        return list(dict.fromkeys(known))
    if words:
        lang = request_lang()
        listed = ", ".join(f"@{groups.handle_of(group, fleet, sid, lang)}" for sid in group["members"])
        raise Problem("groups.no_such_handle", handle=words[0], members=listed)
    sup = fleet.supervisor_id
    if sup and sup in group["members"]:
        return [sup]
    raise Problem("groups.no_supervisor", HTTPStatus.CONFLICT)


def _address(fleet: Fleet, session_id: str) -> str:
    """How a member that messages peers reaches this one: its SendMessage name, or why there is none."""
    if not fleet.messages_peers(session_id):
        return "SendMessage does not reach it"
    return fleet.name(session_id) or "not running"


def header(group: dict, fleet: Fleet, session_id: str) -> str:
    """Who the member is in the group and who else is there; SendMessage only for a program that has it."""
    start = HEADER_START.format(title=group["title"])
    me = f"{start}. You are @{groups.handle_of(group, fleet, session_id)}. "
    if not fleet.messages_peers(session_id):
        people = ", ".join(f"@{groups.handle_of(group, fleet, sid)}" for sid in group["members"])
        return (
            f"{me}Members: {people}\n"
            "Reply in plain text: the group sees your reply. You cannot message the other members: "
            "put what they need to know in your reply.]"
        )
    people = ", ".join(
        f"@{groups.handle_of(group, fleet, sid)} ({_address(fleet, sid)})" for sid in group["members"]
    )
    return (
        f"{me}Members: {people}\n"
        "Reply in plain text in your session: the group sees your reply. "
        'To message a member directly: SendMessage(to="<name>").]'
    )


def message_for(group: dict, fleet: Fleet, session_id: str, text: str) -> str:
    return f"{header(group, fleet, session_id)}\n\n{text}"


def member(app: App | None, fleet: Fleet, session_id: str) -> tuple[Provider, Session] | None:
    """The member's provider and session, or None when neither the index nor the live list knows it."""
    live = fleet.live.get(session_id)
    rec = app.index.record(session_id) if app is not None else None
    if rec is None and live is None:
        return None
    rec = rec or {"session_id": session_id, "cwd": live.cwd, "provider": live.provider}
    return providers.by_id(rec["provider"]), Session.of(rec, live)


def plan_route(app: App | None, fleet: Fleet, session_id: str) -> tuple[str, str | Problem]:
    """(route, refusal): the provider's route, or "" when the text must not go and the refusal says why."""
    found = member(app, fleet, session_id)
    if found is None:
        return "", Problem("groups.session_not_found")
    provider, session = found
    route = provider.route(session, signed=True)
    if not route.can_send or not route.route:
        return "", route.refusal() if route.reason else Problem("groups.cannot_continue")
    return route.route, ""


@dataclass(frozen=True)
class Job:
    group_id: str
    post_id: str
    title: str
    session_id: str
    name: str
    route: str
    message: str
    summary: str


def _result(
    job: Job, ok: bool | None, msg_id: str = "", error: str | BaseException = "", route: str = ""
) -> dict:
    """A delivery for the log; a Problem refusal also keeps its key, for the reader's language."""
    at = store.now() if ok is not None else ""
    out = {
        "session_id": job.session_id,
        "name": job.name,
        "route": route or job.route,
        "ok": ok,
        "msg_id": msg_id,
        "error": str(error),
        "at": at,
    }
    if isinstance(error, Problem):
        params = {k: v if isinstance(v, int | float | str) else str(v) for k, v in error.params.items()}
        out["problem"] = {"key": error.key, "params": params}
    return out


def _run(app: App, fleet: Fleet, job: Job) -> None:
    """One delivery, in its own thread; its outcome replaces the pending one in the log."""
    try:
        found = member(app, fleet, job.session_id)
        if found is None:
            raise Problem("groups.session_not_found")
        provider, session = found
        message = Message(job.message, sender=SENDER.format(title=job.title), summary=job.summary)
        sent = provider.send(session, message)
        outcome = _result(job, True, sent.msg_id, route=sent.route)
    except (SendError, Problem) as exc:
        outcome = _result(job, False, error=exc)
    except Exception as exc:  # a delivery thread must always report, or the post stays pending forever
        outcome = _result(job, False, error=Problem("groups.delivery_crashed", detail=repr(exc)))
    store.finish_delivery(job.group_id, job.post_id, outcome)


def post(app: App, group: dict, fleet: Fleet, text: object) -> dict:
    """Log the user's post, start one delivery thread per target and return the entry, deliveries pending."""
    if not isinstance(text, str) or not text.strip():
        raise Problem("groups.empty_text")
    if len(text) > MAX_TEXT:
        raise Problem("groups.text_too_long", max=MAX_TEXT)
    text = text.strip()
    targets = resolve_targets(group, fleet, text)
    summary = " ".join(text.split())[:SUMMARY_LEN]
    entry = {"id": store.new_id(), "ts": store.now(), "kind": "post", "text": text, "targets": targets}
    jobs, deliveries = [], []
    for sid in targets:
        route, refusal = plan_route(app, fleet, sid)
        job = Job(
            group["id"], entry["id"], group["title"], sid, fleet.name(sid), route,
            message_for(group, fleet, sid, text), summary,
        )  # fmt: skip
        if route:
            jobs.append(job)
            deliveries.append(_result(job, None))
        else:
            deliveries.append(_result(job, False, error=refusal))
    entry["deliveries"] = deliveries
    store.append(group["id"], entry)
    for job in jobs:
        thread = threading.Thread(target=_run, args=(app, fleet, job), daemon=True)
        _threads.append(thread)
        thread.start()
    return entry


def join_all(timeout: float = 5.0) -> None:
    """Wait for the delivery threads started so far (tests; the server never needs it)."""
    while _threads:
        _threads.pop().join(timeout)


def supervisor_post(group: dict, fleet: Fleet, text: str) -> dict:
    """The supervisor's own entry in the feed; it is not delivered to anyone."""
    if not text.strip():
        raise Problem("groups.empty_text")
    entry = {
        "id": store.new_id(),
        "ts": store.now(),
        "kind": "supervisor",
        "text": text.strip(),
        "session_id": fleet.supervisor_id or "",
    }
    store.append(group["id"], entry)
    return entry
