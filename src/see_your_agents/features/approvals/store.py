"""Permission prompts waiting for an answer from the page, in memory: a request lives as long as its hook.

Each request belongs to one hook process (hook.py), which polls for it every couple of seconds. It ends when
the page decides it (the hook takes the decision on its next poll), when the hook withdraws it (the terminal
answered first, the hook's time is up, Claude Code killed it), when the hook stops polling, or when the
session no longer looks like it waits on a prompt (answered in the terminal without a transcript to tell).
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ...core.providers.base import LiveSession, Status

MAX_PENDING = 64
STALE_S = 6.0  # the session has not looked like waiting on a prompt for this long: answered elsewhere
SILENT_S = 15.0  # the hook has not polled for this long: it is gone (killed by a timeout, Esc, a stop)
ENDED_KEEP_S = 600.0  # a hook asking about an ended id hears «gone», not «unknown» (which makes it ask again)


def waiting(live: LiveSession | None) -> bool:
    """The session shows a prompt or a dialog right now (`claude agents`: status waiting, waitingFor set)."""
    return live is not None and (live.status == Status.WAITING or bool(live.waiting_for))


@dataclass
class Request:
    id: str
    session_id: str
    tool: str
    view: dict  # what the page shows: summary, text, plan, questions
    created: float
    polled: float
    seen_waiting: float
    decision: dict | None = None

    def public(self) -> dict:
        return {"id": self.id, "tool": self.tool, **self.view}


class Store:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._cv = threading.Condition()
        self._items: dict[str, Request] = {}
        self._ended: dict[str, tuple[float, str]] = {}  # id -> (when, how): delivered, withdrawn, dropped…

    def add(self, session_id: str, tool: str, view: dict) -> Request | None:
        """A new request, or None when too many wait already."""
        now = self._clock()
        with self._cv:
            if len(self._items) >= MAX_PENDING:
                return None
            req = Request(secrets.token_urlsafe(16), session_id, tool, view, now, now, now)
            self._items[req.id] = req
            return req

    def get(self, rid: str) -> Request | None:
        with self._cv:
            return self._items.get(rid)

    def _end(self, rid: str, how: str) -> None:
        self._items.pop(rid, None)
        self._ended[rid] = (self._clock(), how)
        self._cv.notify_all()

    def wait(self, rid: str, timeout: float) -> tuple[str, dict | None]:
        """The hook's poll: ("decided", decision) once, then "gone"; "pending" after `timeout`; "unknown" for
        an id this process never had (the dashboard restarted)."""
        with self._cv:
            end = self._clock() + timeout
            while True:
                req = self._items.get(rid)
                if req is None:
                    return ("gone" if rid in self._ended else "unknown"), None
                req.polled = self._clock()
                if req.decision is not None:
                    self._end(rid, "delivered")
                    return "decided", req.decision
                if (left := end - self._clock()) <= 0:
                    return "pending", None
                self._cv.wait(left)

    def decide(self, rid: str, decision: dict) -> bool:
        """The page's answer; False when the request is no longer open (first answer wins)."""
        with self._cv:
            req = self._items.get(rid)
            if req is None or req.decision is not None:
                return False
            req.decision = decision
            self._cv.notify_all()
            return True

    def settle(self, rid: str, timeout: float) -> bool:
        """After `decide`: True once the hook took the decision. A decision nobody takes within `timeout` is
        revoked and the request ends: its hook is gone, and the page must not say the answer arrived."""
        with self._cv:
            end = self._clock() + timeout
            while (ended := self._ended.get(rid)) is None:
                if (left := end - self._clock()) <= 0:
                    self._end(rid, "unclaimed")
                    return False
                self._cv.wait(left)
            return ended[1] == "delivered"

    def withdraw(self, rid: str) -> None:
        with self._cv:
            if rid in self._items:
                self._end(rid, "withdrawn")

    def sweep(self, live: Mapping[str, LiveSession]) -> None:
        """Drop what nobody waits for any more: a silent hook, a session that stopped showing a prompt."""
        now = self._clock()
        with self._cv:
            for req in list(self._items.values()):
                if waiting(live.get(req.session_id)):
                    req.seen_waiting = now
                if now - req.polled > SILENT_S or (req.decision is None and now - req.seen_waiting > STALE_S):
                    self._end(req.id, "dropped")
            for rid, (at, _how) in list(self._ended.items()):
                if now - at > ENDED_KEEP_S:
                    del self._ended[rid]

    def open_for(self, session_id: str) -> list[Request]:
        """The session's requests the page can still answer, oldest first."""
        with self._cv:
            return [r for r in self._items.values() if r.session_id == session_id and r.decision is None]
