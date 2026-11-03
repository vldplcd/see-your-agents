"""Chat with sessions: the transcript as messages (GET /api/chat/<id>, its images).

Sending text or files into a session is delivery.py (POST /api/chat/<id>/<action>).

Reading: the session's provider parses its transcript into a flat list of messages (`Provider.thread`, the
contract's `Thread`). A message's id is its index, so a client can merge by id; the paging cursors are
indexes too. A reader is cached per file and extended from the last complete line when the file only grew.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

from ...app.replies import Reply, json_reply, text_reply
from ...core import providers
from ...core.providers.base import Thread

if TYPE_CHECKING:
    from ...app.server import App

DEFAULT_LIMIT, MAX_LIMIT = 60, 300
CACHED_THREADS = 6
ROUTE = re.compile(r"/api/chat/([^/]+)(?:/(image)/([^/]+)/(\d+))?")
# Raster types only, whatever a provider finds: an SVG served from our origin would run script with access
# to the API.
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def page(thread: Thread, session_id: str, query: dict[str, str]) -> dict:
    """Messages `before` a cursor (the newest by default) or all `after` one, up to `limit`."""
    end = len(thread.messages)
    limit = min(max(_int(query.get("limit"), DEFAULT_LIMIT), 1), MAX_LIMIT)
    if "after" in query:
        start, stop = min(max(_int(query["after"], end), 0), end), end
    else:
        stop = min(max(_int(query.get("before"), end), 0), end)
        start = max(0, stop - limit)
    return {
        "session_id": session_id,
        "messages": thread.messages[start:stop],
        "before": start if start > 0 else None,
        "after": thread.first_mutable(),
        "has_more": start > 0,
    }


_threads: OrderedDict[str, Thread] = OrderedDict()
_threads_lock = threading.Lock()


def thread_for(app: App, session_id: str) -> tuple[Thread, Path] | None:
    """The cached reader of the session's transcript, not synced yet; None without a transcript on disk or
    when its provider cannot show one as a chat."""
    rec = app.index.record(session_id)
    path = Path(rec["path"]) if rec and rec.get("path") else None
    if path is None or not path.is_file():
        return None
    with _threads_lock:
        thread = _threads.get(str(path))
        if thread is None:
            if (thread := providers.by_id(rec["provider"]).thread()) is None:
                return None
            _threads[str(path)] = thread
        _threads.move_to_end(str(path))
        while len(_threads) > CACHED_THREADS:
            _threads.popitem(last=False)
    return thread, path


def _image(thread: Thread, path: Path, key: str, n: int) -> Reply | None:
    found = thread.image(path, key, n)
    return (200, found[0], found[1]) if found and found[0] in IMAGE_TYPES else None


def handle_get(app: App, path: str, query: dict[str, str]) -> Reply | None:
    if not (m := ROUTE.fullmatch(path)):
        return None
    session_id = m[1]
    found = thread_for(app, session_id)
    if found is None:
        return json_reply({"error": "no transcript"}, 404)
    thread, transcript = found
    with thread.lock:
        try:
            thread.sync(transcript)
        except OSError:
            return json_reply({"error": "transcript unreadable"}, 404)
        if m[2]:
            return _image(thread, transcript, m[3], int(m[4])) or text_reply("no image", 404)
        return json_reply(page(thread, session_id, query))
