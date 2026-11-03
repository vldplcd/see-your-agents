"""Sessions the user marked done: {session_id: done_at_iso} in config_dir()/done.json.

A mark lapses by itself when the session shows activity after it (see `applies`), so a session
that is picked up again returns to the board without anyone clearing the flag.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import UTC, datetime, timedelta

from .. import config_dir

_lock = threading.Lock()
# Stopping a session can still append a few entries to its transcript; that is not new work.
GRACE = timedelta(minutes=2)


def _path():
    return config_dir() / "done.json"


def load() -> dict[str, str]:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def mark(session_id: str, done: bool, now: datetime | None = None) -> None:
    with _lock:
        marks = load()
        if done:
            marks[session_id] = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
        else:
            marks.pop(session_id, None)
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".done-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(marks, f, indent=1)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def applies(done_at: str, last_activity: str) -> bool:
    """True while the mark is current: the session has had no activity after it."""
    marked = _parse(done_at)
    if marked is None:
        return False
    last = _parse(last_activity)
    return last is None or last <= marked + GRACE
