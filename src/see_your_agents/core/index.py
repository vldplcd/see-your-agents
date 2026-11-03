"""DuckDB index of transcript digests from every available provider.

A transcript is re-parsed only when its size or mtime changed, or its provider's parser did. Rows stay after
the program's own cleanup deletes the transcript (Claude Code's cleanupPeriodDays), so old sessions remain
searchable.

The key is `session_id`, and the provider is a field of the stored record (no migration: an older record
without one is Claude's): both programs name sessions by UUID (v4 for Claude Code, v7 for Codex), and every
address in the dashboard names a session by its id alone (/api/session/<id>, /api/chat/<id>, done marks,
groups, the page's own state). An id two providers report is indexed once, from the first in the registry.

A name the program keeps outside the transcript (Codex's thread names, `Provider.names`) is not stored: it
is read each time records are handed out or searched, so a rename shows without a re-parse.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb

from .. import cache_dir
from .providers import PROVIDERS
from .providers.base import Digest, Provider

# A record written before providers existed names none: they were all Claude Code's.
LEGACY_PROVIDER = "claude"
PROVIDER_OF_ROW = f"coalesce(record->>'provider', '{LEGACY_PROVIDER}')"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id VARCHAR PRIMARY KEY,
    path VARCHAR,
    file_size BIGINT,
    file_mtime DOUBLE,
    last_activity VARCHAR,
    transcript_missing BOOLEAN DEFAULT FALSE,
    record JSON
);
CREATE TABLE IF NOT EXISTS events (session_id VARCHAR, ts VARCHAR, kind VARCHAR, text VARCHAR);
CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR);
"""


def default_db_path() -> Path:
    return cache_dir() / "index.duckdb"


@dataclass(frozen=True)
class _Row:
    """One parsed transcript, ready to write: everything that can fail is done before the transaction."""

    session_id: str
    path: str
    size: int
    mtime: float
    last_activity: str
    record: str  # JSON, with the provider
    events: list[tuple[str, str, str, str]]

    @classmethod
    def of(cls, provider_id: str, d: Digest, size: int, mtime: float) -> _Row:
        record = json.dumps({**d.to_record(), "provider": provider_id}, ensure_ascii=False)
        events = [(d.session_id, e["ts"], e["kind"], e["text"]) for e in d.events]
        return cls(d.session_id, d.path, size, mtime, d.last_activity, record, events)


class Index:
    def __init__(self, db_path: Path | None, providers: Sequence[Provider] = PROVIDERS):
        self.providers = tuple(providers)
        self._ids = {p.id for p in self.providers}
        self.lock = threading.Lock()
        self._unparsable: dict[str, tuple[int, float]] = {}  # session id -> file stats the parser failed on
        if db_path is None:
            self.con = duckdb.connect(":memory:")
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.con = duckdb.connect(str(db_path))
            except duckdb.IOException:
                print(
                    f"{db_path} is held by another process (sya serve?); using an in-memory index",
                    file=sys.stderr,
                )
                self.con = duckdb.connect(":memory:")
        self.con.execute(SCHEMA)
        self._invalidate_if_parser_changed()

    def _invalidate_if_parser_changed(self) -> None:
        # The key from before providers: each provider keeps its own version now.
        self.con.execute("DELETE FROM meta WHERE key = 'parser_version'")
        for p in self.providers:
            key = f"parser_version.{p.id}"
            row = self.con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
            if row and row[0] == str(p.parser_version):
                continue
            # Forget file stats, keep records: transcripts the program already deleted keep their history.
            self.con.execute(f"UPDATE sessions SET file_size = -1 WHERE {PROVIDER_OF_ROW} = ?", [p.id])
            self.con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, str(p.parser_version)])

    def _transcripts(self) -> tuple[dict[str, tuple[Provider, Path]], set[str]]:
        """Transcripts by session id, and the providers that failed to list theirs (their rows stay)."""
        found: dict[str, tuple[Provider, Path]] = {}
        failed: set[str] = set()
        for p in self.providers:
            try:
                listed = p.transcripts() if p.available() else {}
            except Exception as exc:  # one broken provider must not stop the index for the others
                print(f"provider {p.id}: cannot list transcripts: {exc!r}", file=sys.stderr)
                failed.add(p.id)
                continue
            for sid, path in listed.items():
                found.setdefault(sid, (p, path))
        return found, failed

    def refresh(self) -> int:
        """Re-parse changed transcripts; returns how many were re-parsed."""
        files, failed = self._transcripts()
        with self.lock:
            known = {
                sid: (size, mtime, provider)
                for sid, size, mtime, provider in self.con.execute(
                    f"SELECT session_id, file_size, file_mtime, {PROVIDER_OF_ROW} FROM sessions "
                    "WHERE NOT transcript_missing"
                ).fetchall()
            }
        changed: list[_Row] = []
        for sid, (provider, path) in files.items():
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            stats = (st.st_size, st.st_mtime)
            if stats in (known.get(sid, ())[:2], self._unparsable.get(sid)):
                continue
            try:  # a digest that cannot be stored is as bad as one that cannot be read
                changed.append(_Row.of(provider.id, provider.parse(path), st.st_size, st.st_mtime))
            except Exception as exc:  # one odd transcript must not stall the index for every session
                print(f"skipping {path} until it changes: {exc!r}", file=sys.stderr)
                self._unparsable[sid] = stats
        gone = [sid for sid, (_, _, provider) in known.items() if sid not in files and provider not in failed]
        with self.lock:
            self.con.execute("BEGIN")
            try:
                for row in changed:
                    self._upsert(row)
                for sid in gone:
                    self.con.execute(
                        "UPDATE sessions SET transcript_missing = TRUE WHERE session_id = ?", [sid]
                    )
                self.con.execute("COMMIT")
            except BaseException:
                self.con.execute("ROLLBACK")
                raise
        return len(changed)

    def _upsert(self, row: _Row) -> None:
        self.con.execute("DELETE FROM sessions WHERE session_id = ?", [row.session_id])
        self.con.execute("DELETE FROM events WHERE session_id = ?", [row.session_id])
        self.con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, FALSE, ?)",
            [row.session_id, row.path, row.size, row.mtime, row.last_activity, row.record],
        )
        if row.events:
            self.con.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", row.events)

    def _names(self) -> dict[str, dict[str, str]]:
        """Each provider's session names kept outside its transcripts (`Provider.names`), read now: a rename
        shows without a re-parse. A provider that fails to tell keeps the names its transcripts have."""
        found = {}
        for p in self.providers:
            try:
                found[p.id] = p.names()
            except Exception as exc:
                print(f"provider {p.id}: cannot read session names: {exc!r}", file=sys.stderr)
        return found

    def _registered(
        self, record: str, transcript_missing: bool, names: dict[str, dict[str, str]]
    ) -> dict | None:
        """The stored record with its provider and current name, or None when that provider is not
        registered here: its rows stay in the file but no card, chat or detail is built for them."""
        rec = {"provider": LEGACY_PROVIDER, **json.loads(record), "transcript_missing": transcript_missing}
        if rec["provider"] not in self._ids:
            return None
        if name := names.get(rec["provider"], {}).get(rec.get("session_id", "")):
            rec["custom_name"] = rec["title"] = name
        return rec

    def records(self) -> list[dict]:
        with self.lock:
            rows = self.con.execute("SELECT record, transcript_missing FROM sessions").fetchall()
        names = self._names()
        return [rec for row in rows if (rec := self._registered(*row, names))]

    def record(self, session_id: str) -> dict | None:
        with self.lock:
            row = self.con.execute(
                "SELECT record, transcript_missing FROM sessions WHERE session_id = ?", [session_id]
            ).fetchone()
        return self._registered(*row, self._names()) if row else None

    def events(self, session_id: str) -> list[dict]:
        with self.lock:
            rows = self.con.execute(
                "SELECT ts, kind, text FROM events WHERE session_id = ? ORDER BY ts", [session_id]
            ).fetchall()
        return [{"ts": ts, "kind": kind, "text": text} for ts, kind, text in rows]

    def search(self, query: str, limit: int = 200) -> dict[str, list[dict]]:
        """Event hits grouped by session, newest first."""
        pattern = f"%{query}%"
        with self.lock:
            rows = self.con.execute(
                """
                SELECT session_id, ts, kind, text FROM events WHERE text ILIKE ?
                UNION ALL
                SELECT session_id, last_activity, 'title', record->>'title' FROM sessions
                WHERE record->>'title' ILIKE ? OR CAST(record->'jira_keys' AS VARCHAR) ILIKE ?
                ORDER BY ts DESC LIMIT ?
                """,
                [pattern, pattern, pattern, limit],
            ).fetchall()
        rows = sorted(rows + self._named(query), key=lambda row: row[1] or "", reverse=True)
        hits: dict[str, list[dict]] = {}
        for sid, ts, kind, text in rows:
            hits.setdefault(sid, []).append({"ts": ts, "kind": kind, "text": text})
        return hits

    def _named(self, query: str) -> list[tuple[str, str, str, str]]:
        """Title hits on the names programs keep outside transcripts (`Provider.names`): stored records do
        not hold them."""
        needle = query.casefold()
        named = {
            sid: n for names in self._names().values() for sid, n in names.items() if needle in n.casefold()
        }
        if not named:
            return []
        with self.lock:
            rows = self.con.execute(
                "SELECT session_id, last_activity FROM sessions WHERE list_contains(?, session_id)",
                [list(named)],
            ).fetchall()
        return [(sid, ts, "title", named[sid]) for sid, ts in rows]
