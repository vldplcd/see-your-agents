import json

import duckdb

from see_your_agents.core.index import Index
from see_your_agents.core.providers.base import Digest, LiveSession, Provider

# An index file as the code before providers wrote it.
MAIN_SCHEMA = """
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
MAIN_INSERT = "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, FALSE, ?)"


class Folder(Provider):
    """A provider whose transcripts are the .jsonl files of a folder. A file named bad* breaks its parser,
    one named odd* parses into a digest the index cannot store."""

    title = "Папка"
    caps = frozenset()
    parser_version = 1

    def __init__(self, provider_id, folder, available=True):
        self.id, self.folder, self.on, self.broken = provider_id, folder, available, False
        folder.mkdir(parents=True, exist_ok=True)

    def add(self, sid):
        (self.folder / f"{sid}.jsonl").write_text(json.dumps({"title": f"{self.id}:{sid}"}) + "\n")

    def available(self):
        return self.on

    def transcripts(self):
        if self.broken:
            raise OSError("the program's folder is unreadable")
        return {p.stem: p for p in self.folder.glob("*.jsonl")}

    def parse(self, path):
        if path.stem.startswith("bad"):
            raise ValueError("unreadable")
        digest = Digest(
            session_id=path.stem, path=str(path), custom_name=json.loads(path.read_text())["title"]
        )
        if path.stem.startswith("odd"):
            digest.events.append({"ts": "2026-11-03T00:00:00Z", "kind": "prompt"})  # no text
        return digest

    def live(self) -> dict[str, LiveSession]:
        return {}


def titles(index):
    return {r["session_id"]: (r["provider"], r["title"]) for r in index.records()}


def test_a_transcript_the_parser_chokes_on_is_skipped_and_the_rest_is_indexed(tmp_path, capsys):
    folder = Folder("one", tmp_path / "one")
    folder.add("good")
    folder.add("bad")
    index = Index(None, [folder])

    assert index.refresh() == 1
    assert [r["session_id"] for r in index.records()] == ["good"]
    assert index.refresh() == 0  # not retried until the file changes
    assert capsys.readouterr().err.count("skipping") == 1


def test_a_digest_that_cannot_be_stored_is_skipped_and_the_index_stays_usable(tmp_path):
    folder = Folder("one", tmp_path / "one")
    folder.add("odd")
    folder.add("good")
    index = Index(None, [folder])

    assert index.refresh() == 1
    folder.add("later")
    assert index.refresh() == 1  # the connection is not left inside an aborted transaction
    assert sorted(titles(index)) == ["good", "later"]


def test_every_available_provider_is_walked_and_each_session_keeps_its_provider(tmp_path):
    one, two, off = (
        Folder("one", tmp_path / "1"),
        Folder("two", tmp_path / "2"),
        Folder("off", tmp_path / "3", False),
    )
    one.add("a")
    two.add("b")
    two.add("a")  # an id both report: the first provider in the registry has it
    off.add("c")
    index = Index(None, [one, two, off])

    index.refresh()

    assert titles(index) == {"a": ("one", "one:a"), "b": ("two", "two:b")}
    assert index.record("b")["provider"] == "two"


def test_a_provider_that_fails_to_list_keeps_its_sessions_and_the_others_still_index(tmp_path):
    one, two = Folder("one", tmp_path / "1"), Folder("two", tmp_path / "2")
    one.add("a")
    two.add("b")
    index = Index(None, [one, two])
    index.refresh()
    one.broken = True
    two.add("c")

    assert index.refresh() == 1
    assert sorted(titles(index)) == ["a", "b", "c"] and index.record("a")["transcript_missing"] is False


def test_rows_of_a_provider_that_is_not_registered_any_more_are_left_out(tmp_path):
    db = tmp_path / "index.duckdb"
    gone, kept = Folder("gone", tmp_path / "1"), Folder("kept", tmp_path / "2")
    gone.add("a")
    kept.add("b")
    Index(db, [gone, kept]).refresh()

    index = Index(db, [kept])

    assert titles(index) == {"b": ("kept", "kept:b")} and index.record("a") is None


def test_an_index_from_before_providers_keeps_its_rows_as_claudes_and_parses_them_again(tmp_path):
    db = tmp_path / "index.duckdb"
    con = duckdb.connect(str(db))
    con.execute(MAIN_SCHEMA)
    con.execute("INSERT INTO meta VALUES ('parser_version', '8')")
    claude = Folder("claude", tmp_path / "projects")
    claude.add("kept")
    st = (claude.folder / "kept.jsonl").stat()
    for sid, size, mtime in (("kept", st.st_size, st.st_mtime), ("deleted", 1, 1.0)):
        con.execute(MAIN_INSERT, [sid, "p", size, mtime, "", json.dumps({"session_id": sid, "title": "old"})])
    con.close()

    index = Index(db, [claude])

    assert titles(index) == {"kept": ("claude", "old"), "deleted": ("claude", "old")}
    assert index.refresh() == 1  # the same file, parsed again by the provider's parser
    assert titles(index) == {"kept": ("claude", "claude:kept"), "deleted": ("claude", "old")}
    assert index.record("deleted")["transcript_missing"] is True
    assert index.con.execute("SELECT key FROM meta").fetchall() == [("parser_version.claude",)]
