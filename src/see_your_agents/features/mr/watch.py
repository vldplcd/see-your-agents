"""MR states for the cards: a cache the board reads without waiting, and a background refresher that fills it.

The board only reads memory. Building a card notes which MRs it links to; the refresher (one daemon thread,
started by the first card that links to a GitLab MR) asks glab about the ones that are due, a bounded number
of calls per cycle: an open MR every minute while a live or recent session shows it, every 15 minutes while
only older ones do; a merged or closed MR never again. MRs no board has shown for a few minutes are not
asked about, so a server nobody looks at makes no calls.

A stopped session in Recently closed whose MRs are all merged is marked done (`core/done.py`), once.

Errors are kept as specs (`glab.GlabError.spec()`) and worded in the request's language as a card is built.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ... import cache_dir
from ...core import done
from ...core.providers.base import LiveSession
from ...core.sessions import RECENT
from ...i18n import request_lang, t
from . import glab

if TYPE_CHECKING:
    from ...app.server import App

OPEN_S = 60.0
COLD_S = 15 * 60.0
CYCLE_S = 10.0
CALLS_PER_CYCLE = 12
SEEN_S = 5 * 60.0  # an MR no board has shown for this long is not refreshed


@dataclass
class Seen:
    ref: glab.Ref
    at: float  # last time a card showed it
    hot_at: float  # last time a live or recent session's card showed it


@dataclass
class Stopped:
    keys: list[str]
    last_activity: str
    at: float


@dataclass
class HostPause:
    until: float
    why: glab.GlabError
    login: bool


def _recent(last_activity: str, now: float) -> bool:
    try:
        ts = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    return now - ts.timestamp() <= RECENT.total_seconds()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z") if t else ""


class Watcher:
    def __init__(
        self,
        run: glab.Run = glab.run,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], float] = time.time,
        background: bool = True,
    ):
        self.run, self.which, self.clock = run, which, clock
        self.background = background  # False: no thread, whoever holds the watcher calls cycle()
        self.app: App | None = None  # set by the feature's setup: liveness is checked again before a mark
        self._lock = threading.Lock()
        self._loaded = False
        self._states: dict[str, dict] = {}  # Ref.key -> fetched state with checked_at (epoch s)
        self._auto: dict[str, list[str]] = {}  # session id -> the MR keys it was marked done for
        self._seen: dict[str, Seen] = {}
        self._stopped: dict[str, Stopped] = {}  # by session id
        self._hosts: dict[str, HostPause] = {}
        self._glab: bool | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- the board's side: memory only ------------------------------------------------------------
    def card(self, rec: dict, live: LiveSession | None, refs: list[tuple[str, glab.Ref]]) -> dict:
        if not self.available():
            items = [{"url": url, "iid": ref.iid, "state": ""} for url, ref in refs]
            return {"items": items, "all_merged": False, "off": t(request_lang(), "mr.no_glab")}
        now = self.clock()
        hot = live is not None or _recent(rec.get("last_activity", ""), now)
        with self._lock:
            self._load()
            for _, ref in refs:
                seen = self._seen.get(ref.key)
                self._seen[ref.key] = Seen(ref, now, now if hot else (seen.hot_at if seen else 0.0))
            if live is None:
                keys = [ref.key for _, ref in refs]
                self._stopped[rec["session_id"]] = Stopped(keys, rec.get("last_activity", ""), now)
            else:
                self._stopped.pop(rec["session_id"], None)
            items = [self._item(url, ref, now) for url, ref in refs]
        self._start()
        return {"items": items, "all_merged": all(i["state"] == "merged" for i in items), "off": ""}

    def available(self) -> bool:
        if self._glab is None:
            self._glab = self.which("glab") is not None
        return self._glab

    def _item(self, url: str, ref: glab.Ref, now: float) -> dict:
        s = self._states.get(ref.key, {})
        pause = self._hosts.get(ref.host)
        lang = request_lang()
        return {
            "url": url,
            "iid": ref.iid,
            "state": s.get("state", ""),
            "draft": s.get("draft", False),
            "title": s.get("title", ""),
            "pipeline": s.get("pipeline", ""),
            "approved": s.get("approved"),
            "approvals_left": s.get("approvals_left"),
            "approvals_required": s.get("approvals_required"),
            "unresolved": s.get("unresolved"),
            "checked_at": _iso(s.get("checked_at", 0.0)),
            "error": glab.words(s.get("error"), lang),
            "off": pause.why.text(lang) if pause and pause.login and pause.until > now else "",
        }

    # --- the refresher ----------------------------------------------------------------------------
    def _start(self) -> None:
        with self._lock:
            if self._thread is not None or not self.background:
                return
            self._thread = threading.Thread(target=self._loop, name="sya-mr", daemon=True)
        self._thread.start()

    def stop(self, wait_s: float = 5.0) -> None:
        """End the refresher after its current call."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(wait_s)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception as exc:  # the next cycle tries again; the board keeps the last states
                print(f"mr refresh failed: {exc!r}")
            self._stop.wait(CYCLE_S)

    def cycle(self) -> int:
        """Ask about the MRs that are due, at most CALLS_PER_CYCLE calls, then close finished sessions.
        Returns the number of calls made."""
        now = self.clock()
        with self._lock:
            self._load()
            due = self._due(now)
        calls, failed_hosts = 0, set()
        for ref in due:
            if calls + glab.MAX_CALLS > CALLS_PER_CYCLE or self._stop.is_set():
                break
            if ref.host in failed_hosts:
                continue
            try:
                state, n = glab.fetch(self.run, ref)
                error = None
            except glab.GlabError as exc:
                state, n, error = {}, 1, exc
            calls += n
            at = self.clock()
            with self._lock:
                if error is None:
                    self._states[ref.key] = {**state, "checked_at": at}
                    continue
                # keep what was known, say why it is not fresh
                self._states[ref.key] = {
                    **self._states.get(ref.key, {}),
                    "error": error.spec(),
                    "checked_at": at,
                }
                if error.host:
                    failed_hosts.add(ref.host)
                    self._hosts[ref.host] = HostPause(
                        at + (COLD_S if error.login else OPEN_S), error, error.login
                    )
        if calls:
            self._save()
        self._close_done(now)
        return calls

    def _due(self, now: float) -> list[glab.Ref]:
        """Never asked first, then the MRs of live and recent sessions, then the longest unchecked."""
        due = []
        for key, seen in self._seen.items():
            pause = self._hosts.get(seen.ref.host)
            if now - seen.at > SEEN_S or (pause and pause.until > now):
                continue
            s = self._states.get(key)
            if s and s.get("state") in glab.TERMINAL and not s.get("error"):
                continue
            hot = now - seen.hot_at <= SEEN_S
            checked = s.get("checked_at", 0.0) if s else 0.0
            if s and now - checked < (OPEN_S if hot else COLD_S):
                continue
            due.append((s is not None, not hot, checked, seen.ref))
        due.sort(key=lambda d: d[:3])
        return [d[3] for d in due]

    def _close_done(self, now: float) -> None:
        """Mark done the stopped sessions of Recently closed whose MRs are all merged, once per MR set: a
        session the user picks up again stays on the board when it stops again."""
        with self._lock:
            ready = [
                (sid, s)
                for sid, s in self._stopped.items()
                if now - s.at <= SEEN_S
                and s.keys
                and self._auto.get(sid) != sorted(s.keys)
                and all(self._states.get(k, {}).get("state") == "merged" for k in s.keys)
                and _recent(s.last_activity, now)
            ]
        if not ready or self.app is None:
            return
        marks = done.load()
        ready = [(sid, s) for sid, s in ready if not done.applies(marks.get(sid, ""), s.last_activity)]
        if not ready:
            return
        live = self.app.live()  # the card saw it stopped up to a cycle ago; a running session is never marked
        for sid, s in ready:
            if sid not in live:
                done.mark(sid, True)
                with self._lock:
                    self._auto[sid] = sorted(s.keys)
        self._save()

    # --- the cache file: states survive a restart, so merged MRs are never asked about again ---------
    def _path(self):
        return cache_dir() / "mr.json"

    def _load(self) -> None:
        """Under self._lock, once."""
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self._path().read_text())
        except (OSError, ValueError):
            return
        states = data.get("states") if isinstance(data, dict) else None
        auto = data.get("auto_done") if isinstance(data, dict) else None
        if isinstance(states, dict):
            self._states = {k: v for k, v in states.items() if isinstance(v, dict)}
        if isinstance(auto, dict):
            self._auto = {k: v for k, v in auto.items() if isinstance(v, list)}

    def _save(self) -> None:
        with self._lock:
            payload = json.dumps({"states": self._states, "auto_done": self._auto}, ensure_ascii=False)
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".mr-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(payload)
                os.replace(tmp, path)
            except BaseException:
                os.unlink(tmp)
                raise


WATCHER = Watcher()


def card_fields(rec: dict, live: LiveSession | None) -> dict:
    """`mr` on a card that links to GitLab MRs: their states from the cache, never a call."""
    prs = rec.get("prs") or []
    refs = [(p["url"], ref) for p in prs if isinstance(p, dict) and (ref := glab.ref_of(p.get("url")))]
    return {"mr": WATCHER.card(rec, live, refs)} if refs else {}


def setup(app: App) -> None:
    WATCHER.app = app
