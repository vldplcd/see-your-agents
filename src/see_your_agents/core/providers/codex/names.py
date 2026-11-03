"""Thread names, which Codex keeps outside the rollout: `threads.name` in state_5.sqlite, else
`thread_name` in session_index.jsonl (named threads only).

The database is Codex's own cache, opened read-only and never written (`_from_db`); a locked or odd one is
skipped for the index file. Both are read again only when one of the files changed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

STATE_DB = "state_5.sqlite"
SESSION_INDEX = "session_index.jsonl"
BUSY_TIMEOUT_S = 0.3  # how long sqlite retries a locked database before the name falls back

_lock = threading.Lock()
_cache: tuple[tuple, dict[str, str]] = ((), {})


def _stamp(home: Path) -> tuple:
    stamp = []
    for name in (STATE_DB, STATE_DB + "-wal", SESSION_INDEX):
        try:
            st = (home / name).stat()
            stamp.append((name, st.st_size, st.st_mtime_ns))
        except OSError:
            stamp.append((name, None, None))
    return (str(home), *stamp)


def _from_db(path: Path) -> dict[str, str]:
    """Even `mode=ro` makes sqlite create -wal and -shm next to a WAL database that has none: without a
    -wal (Codex wrote nothing since its last checkpoint) the file is read as `immutable`, which touches
    nothing; with one, `mode=ro` reads what the -wal holds too."""
    if not path.is_file():
        return {}
    flags = "mode=ro" if path.with_name(path.name + "-wal").exists() else "mode=ro&immutable=1"
    try:
        con = sqlite3.connect(f"{path.as_uri()}?{flags}", uri=True, timeout=BUSY_TIMEOUT_S)
        try:
            rows = con.execute("SELECT id, name FROM threads WHERE name != ''").fetchall()
        finally:
            con.close()
    except sqlite3.Error:  # locked past the timeout, another schema, not a database: the index file answers
        return {}
    return {str(tid): str(name) for tid, name in rows}


def _from_index(path: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return names
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("id") and isinstance(entry.get("thread_name"), str):
            names[str(entry["id"])] = entry["thread_name"]  # a rename appends: the last line wins
    return names


def thread_names(home: Path) -> dict[str, str]:
    """{thread id: name} of every named thread."""
    global _cache
    stamp = _stamp(home)
    with _lock:
        if _cache[0] == stamp:
            return _cache[1]
        names = {**_from_index(home / SESSION_INDEX), **_from_db(home / STATE_DB)}
        _cache = (stamp, names)
        return names
