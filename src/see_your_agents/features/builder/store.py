"""Builds: what the builder was asked, where it works, how far it got. Kept in config_dir()/builds.json.

The watchdog (app/watchdog.py) reads the same file after a restart to find the merge it may have to revert,
so it stays plain JSON: {"builds": [record, ...]}, oldest first.

Statuses: building → checking → ready | failed → accepted | rejected | rolled_back. Rework sends ready and
failed back to building; Accept sends ready back to checking when the branch moved after the checks. Every
change goes through `move`, a compare-and-set: a check that finishes after the user rejected the build cannot
bring it back.

A build's `error` is {"key", "params"}: a catalog key (builder.*) worded in the reader's language when a page
reads it (`worded`). A string is shown as it is: the builder's own words, or a record from before errors were
keys. The watchdog writes the same shape.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import UTC, datetime

from ... import config_dir
from ...i18n import Problem, t

BUILDING, CHECKING, READY, FAILED = "building", "checking", "ready", "failed"
ACCEPTED, REJECTED, ROLLED_BACK = "accepted", "rejected", "rolled_back"
ACTIVE = (BUILDING, CHECKING, READY, FAILED)
NEXT = {
    BUILDING: {CHECKING, FAILED, REJECTED},
    CHECKING: {READY, FAILED, REJECTED},
    READY: {BUILDING, CHECKING, ACCEPTED, REJECTED},
    FAILED: {BUILDING, REJECTED},
    ACCEPTED: {ROLLED_BACK},
    REJECTED: set(),
    ROLLED_BACK: set(),
}
_lock = threading.Lock()


def problem(key: str, **params: object) -> dict:
    """An error as builds.json keeps it: a key and its params as plain JSON values, worded when read."""
    return {
        "key": key,
        "params": {k: v if isinstance(v, int | float | str) else str(v) for k, v in params.items()},
    }


def problem_of(exc: Problem) -> dict:
    return problem(exc.key, **exc.params)


def worded(error: object, lang: str) -> str:
    """A stored error in `lang`; a string as it is."""
    if isinstance(error, dict) and isinstance(error.get("key"), str):
        params = error.get("params")
        return t(lang, error["key"], **(params if isinstance(params, dict) else {}))
    return error if isinstance(error, str) else ""


def now() -> str:
    return datetime.now(UTC).isoformat()


def _path():
    return config_dir() / "builds.json"


def load() -> list[dict]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    builds = data.get("builds") if isinstance(data, dict) else None
    if not isinstance(builds, list):
        return []
    return [b for b in builds if isinstance(b, dict) and b.get("id") and b.get("status") in NEXT]


def _save(builds: list[dict]) -> None:
    folder = _path().parent
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".builds-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"builds": builds}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path())


def get(build_id: str) -> dict | None:
    return next((b for b in load() if b["id"] == build_id), None)


def add(build: dict) -> dict:
    with _lock:
        _save([*load(), build])
    return dict(build)


def update(build_id: str, **fields) -> dict | None:
    """Change fields other than the status."""
    with _lock:
        builds = load()
        found = next((b for b in builds if b["id"] == build_id), None)
        if found is None:
            return None
        found.update(fields, updated_at=now())
        _save(builds)
        return dict(found)


def move(build_id: str, to: str, *, only_from: tuple[str, ...] = (), **fields) -> dict | None:
    """Set the status if the current one allows it (and is one of `only_from`, when given); None otherwise."""
    with _lock:
        builds = load()
        found = next((b for b in builds if b["id"] == build_id), None)
        if found is None or to not in NEXT[found["status"]]:
            return None
        if only_from and found["status"] not in only_from:
            return None
        found.update(fields, status=to, updated_at=now())
        _save(builds)
        return dict(found)
