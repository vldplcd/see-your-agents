"""Groups on disk: config_dir()/groups.json and one JSONL log per group in config_dir()/groups/.

Every write goes through a temporary file and os.replace under one lock, mode 0600. A log line is a post
({id, ts, kind: "post", text, targets, deliveries}) or a supervisor entry
({id, ts, kind: "supervisor", text, session_id}); a delivery that finishes rewrites its post's line in
place, so the log always shows the latest state.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ... import config_dir

GROUP_ID = re.compile(r"g[0-9a-f]{8}")
LOCK = threading.RLock()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return "g" + secrets.token_hex(4)


def _groups_path() -> Path:
    return config_dir() / "groups.json"


def log_path(group_id: str) -> Path:
    return config_dir() / "groups" / f"{group_id}.jsonl"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".groups-")  # mkstemp creates it 0600
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load() -> list[dict]:
    try:
        data = json.loads(_groups_path().read_text(encoding="utf-8")).get("groups")
    except (OSError, ValueError, AttributeError):
        return []
    return [g for g in data if isinstance(g, dict)] if isinstance(data, list) else []


def save(groups: list[dict]) -> None:
    with LOCK:
        _write(_groups_path(), json.dumps({"groups": groups}, ensure_ascii=False, indent=1))


def get(group_id: str) -> dict | None:
    return next((g for g in load() if g.get("id") == group_id), None)


def add(group: dict) -> None:
    with LOCK:
        save([*load(), group])


def update(group_id: str, change: Callable[[dict], None]) -> dict | None:
    """Apply `change(group)` to the stored group under the lock; None when there is no such group."""
    with LOCK:
        groups = load()
        group = next((g for g in groups if g.get("id") == group_id), None)
        if group is None:
            return None
        change(group)
        save(groups)
        return group


def remove(group_id: str) -> bool:
    with LOCK:
        groups = load()
        kept = [g for g in groups if g.get("id") != group_id]
        if len(kept) == len(groups):
            return False
        save(kept)
        log_path(group_id).unlink(missing_ok=True)
        return True


# ---- log ---------------------------------------------------------------------------------------


def read_log(group_id: str) -> list[dict]:
    try:
        lines = log_path(group_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _write_log(group_id: str, entries: list[dict]) -> None:
    _write(log_path(group_id), "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))


def append(group_id: str, entry: dict) -> None:
    with LOCK:
        _write_log(group_id, [*read_log(group_id), entry])


def finish_delivery(group_id: str, post_id: str, delivery: dict) -> None:
    """Replace the pending delivery to the same session in a post; a deleted group or post is ignored."""
    with LOCK:
        if get(group_id) is None:
            return
        entries = read_log(group_id)
        for entry in entries:
            if entry.get("id") == post_id:
                entry["deliveries"] = [
                    delivery if d.get("session_id") == delivery["session_id"] else d
                    for d in entry.get("deliveries", [])
                ]
        _write_log(group_id, entries)
