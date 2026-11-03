import json
import os
import sqlite3
from pathlib import Path

import pytest
from codex_rollouts import (
    agent_item,
    call,
    event,
    fork_rollout,
    harness_text,
    line,
    meta,
    queue_started,
    tokens,
    ts,
    turn_complete,
    turn_context,
    turn_started,
    uid,
    user_item,
    write,
)

from see_your_agents.core.index import Index
from see_your_agents.core.providers.codex.provider import Codex

THREAD = uid(0)


@pytest.fixture
def home():
    return Path(os.environ["CODEX_HOME"])


def parse(path):
    return Codex().parse(path)


def test_a_thread_reads_into_the_digest(home):
    vscode_prompt = (
        "# Context from my IDE setup:\n\n## Active file: app.py\n\n## My request for Codex:\nfix PROJ-12 now"
    )
    path = write(
        home,
        THREAD,
        meta(THREAD),
        turn_started(uid(1, "a"), 1),
        turn_context("gpt-6-luna", 1, cwd="/work/app/sub"),
        harness_text("<environment_context><cwd>/work/app</cwd></environment_context>", 1),
        user_item(vscode_prompt, 1),
        call("c1", "exec_command", 1, cmd="ls"),
        call("c2", "spawn_agent", 1, task_name="ci_runner", message="gAAAAABqqcY7ty-encrypted"),
        agent_item("thinking aloud", "commentary", 2),
        agent_item("done, see app.py", minute=2),
        tokens(1200, 2),
        turn_complete(uid(1, "a"), "done, see app.py", 2),
        event("a_future_event", 3, whatever=1),
        line("a_future_line", {"x": 1}, 3),
    )
    path.write_text(
        path.read_text() + '{"timestamp": "2026-11-03T00:04:00Z", "type": "event_ms'
    )  # half-written

    d = parse(path)

    assert (d.session_id, d.title, d.first_prompt) == (THREAD, "fix PROJ-12 now", "fix PROJ-12 now")
    assert (d.n_prompts, d.n_replies, d.n_tool_calls) == (1, 2, 2)
    assert (d.project, d.cwd, d.git_branch, d.models) == (
        "/work/app",
        "/work/app/sub",
        "PROJ-42-fix",
        ["gpt-6-luna"],
    )
    assert d.last_assistant_text == "done, see app.py"
    assert (d.tokens, d.cost_usd) == (1200, 0.0)
    assert d.jira_keys[:2] == ["PROJ-12", "PROJ-42"]
    assert [(e["kind"], e["text"]) for e in d.events] == [
        ("prompt", "fix PROJ-12 now"),
        ("agent", "ci_runner"),
    ]
    assert [a["description"] for a in d.agents] == ["ci_runner"]  # the task text is stored encrypted


def test_a_compaction_and_an_unnamed_subagent_are_indexed_without_words(home):
    """Markers, not UI text: the session panel words them in the page's language."""
    path = write(
        home,
        THREAD,
        meta(THREAD),
        turn_started(uid(1)),
        user_item("go", 1),
        call("c1", "spawn_agent", 1, message="gAAAAABqqcY7ty-encrypted"),
        line("compacted", {"message": ""}, 2),
    )

    d = parse(path)

    assert [(e["kind"], e["text"]) for e in d.events][1:] == [("agent", ""), ("compact", "")]
    assert [a["description"] for a in d.agents] == [""]


@pytest.mark.parametrize("heading", ["## My request for Codex:", "## My request:"])
def test_the_vs_code_editor_context_is_not_the_prompt(home, heading):
    """The extension's 0.154 alpha builds shortened the heading; both are in real rollouts."""
    prompt = f"# Context from my IDE setup:\n\n## Open tabs:\n- app.py\n\n{heading}\nrun the tests"
    path = write(home, THREAD, meta(THREAD), turn_started(uid(1)), user_item(prompt, 1))

    assert parse(path).first_prompt == "run the tests"


def test_the_digest_says_when_the_last_turn_was_aborted_and_a_finished_turn_clears_it(home):
    first, second = uid(1, "a"), uid(2, "b")
    interrupted = event("turn_aborted", 1, turn_id=first, reason="interrupted")
    aborted = [meta(THREAD), turn_started(first, 1), user_item("go", 1), interrupted]

    assert parse(write(home, THREAD, *aborted)).last_turn_aborted
    finished = [*aborted, turn_started(second, 2), user_item("again", 2), turn_complete(second, "ok", 2)]
    assert not parse(write(home, THREAD, *finished)).last_turn_aborted


def test_turns_the_queue_started_keep_their_latest_answer_under_the_turn_id(home):
    """What a group finds a Codex member's answer by: the turn its message started (Sent.msg_id)."""
    typed, queued, running = uid(1, "a"), uid(2, "b"), uid(3, "c")
    lines = [
        meta(THREAD),
        turn_started(typed, 1), user_item("typed in the terminal", 1), turn_complete(typed, "done", 1),
        queue_started(queued, 2), user_item("[Группа «Релиз»…]\n\nпривет", 2),
        agent_item("смотрю", "commentary", 2), turn_complete(queued, "Привет!", 3),
        queue_started(running, 4), user_item("ещё", 4), agent_item("думаю…", "commentary", 5),
    ]  # fmt: skip

    d = parse(write(home, THREAD, *lines))

    assert d.queued_turns == [
        {"ts": ts(2), "msg_id": queued, "reply": "Привет!", "reply_at": ts(3)},
        {"ts": ts(4), "msg_id": running, "reply": "думаю…", "reply_at": ts(5)},
    ]
    assert d.last_assistant_text == "Привет!"  # a commentary is not the last answer


def test_a_fork_skips_the_parent_history_it_replays(home):
    child = uid(5)
    d = parse(write(home, child, *fork_rollout(THREAD, child, at=5)))

    assert d.session_id == child
    assert (d.n_prompts, d.title) == (1, "the fork's ask")
    assert (d.n_replies, d.last_assistant_text) == (1, "CI is green")
    assert d.tokens == 300  # its own, past the parent's 1500 it starts from


def test_an_old_cli_writes_prompts_and_answers_as_events_and_numbers_its_turns(home):
    """≤0.100 and some IDE builds: event_msg/user_message and agent_message, turn ids "rollout-N"."""
    path = write(
        home,
        THREAD,
        meta(THREAD, cli_version="0.89.0"),
        turn_started("rollout-1", started_at=1_793_664_060),
        event("user_message", 1, message="old style ask"),
        event("agent_message", 1, message="old style answer"),  # no phase: a final answer
        turn_complete("rollout-1", None, 1),
    )

    d = parse(path)

    assert (d.n_prompts, d.last_prompt, d.n_replies, d.last_assistant_text) == (
        1,
        "old style ask",
        1,
        "old style answer",
    )


def test_an_old_fork_tells_its_own_turns_by_their_start_in_seconds(home):
    child = uid(5)
    path = write(
        home,
        child,
        meta(child, 5, forked_from_id=THREAD),
        meta(THREAD, 5),
        turn_started("rollout-1", 5, started_at=1_793_664_060),  # the parent's, at minute 1
        user_item("parent's ask", 5),
        turn_started("rollout-2", 5, started_at=1_793_664_300),  # its own, at minute 5
        user_item("the fork's ask", 5),
    )

    assert parse(path).first_prompt == "the fork's ask"


def named(home, **names):
    (home / "session_index.jsonl").write_text(
        "".join(json.dumps({"id": tid, "thread_name": name}) + "\n" for tid, name in names.items())
    )


def database(home, rows, wal=False):
    """state_5.sqlite as Codex leaves it; with `wal` in WAL mode, closed, so no -wal or -shm is left."""
    db = sqlite3.connect(home / "state_5.sqlite")
    if wal:
        db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT)")
    db.executemany("INSERT INTO threads VALUES (?, ?, ?)", rows)
    db.commit()
    db.close()


def test_the_title_is_codexs_name_for_the_thread_read_when_records_are_built(home):
    write(home, THREAD, meta(THREAD), turn_started(uid(1)), user_item("the first ask", 1))
    index = Index(None, [Codex()])
    index.refresh()
    title = lambda: index.record(THREAD)["title"]  # noqa: E731

    assert title() == "the first ask"
    named(home, **{THREAD: "Index name"})
    assert title() == "Index name"  # the rollout did not change: no re-parse needed
    database(home, [(THREAD, "Database name", "ignored title")])
    assert title() == "Database name"
    assert index.refresh() == 0
    assert [h["text"] for h in index.search("database NAME")[THREAD]] == ["Database name"]


def test_a_database_that_cannot_be_read_falls_back_to_the_index(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "state_5.sqlite").write_text("not a database")
    named(home, **{THREAD: "Index name"})

    assert Codex().names() == {THREAD: "Index name"}


def test_reading_names_leaves_no_file_behind_in_codexs_folder(home):
    home.mkdir(parents=True, exist_ok=True)
    database(home, [(THREAD, "Database name", "")], wal=True)
    before = sorted(p.name for p in home.iterdir())

    assert Codex().names() == {THREAD: "Database name"}
    assert sorted(p.name for p in home.iterdir()) == before


def test_subagents_and_threads_codex_runs_for_itself_are_not_sessions(home):
    """A spawned subagent shows under its parent («Субагенты», "Subagents"), as Claude's do."""
    spawned, guardian, titler = uid(1), uid(2), uid(3)
    write(home, THREAD, meta(THREAD))
    write(
        home,
        spawned,
        meta(
            spawned,
            forked_from_id=THREAD,
            source={"subagent": {"thread_spawn": {"parent_thread_id": THREAD}}},
        ),
    )
    write(
        home,
        guardian,
        meta(guardian, thread_source="guardian_review", source={"subagent": {"other": "guardian"}}),
    )
    write(home, titler, meta(titler, source="exec", originator="codex_vscode"))

    assert list(Codex().transcripts()) == [THREAD]


def test_a_live_rollout_wins_over_its_archived_copy(home):
    live = write(home, THREAD, meta(THREAD))
    write(home, THREAD, meta(THREAD), archived=True)

    assert Codex().transcripts() == {THREAD: live}


def test_a_compressed_rollout_is_unparsable_and_the_rest_is_indexed(home, capsys):
    write(home, THREAD, meta(THREAD), turn_started(uid(1)), user_item("readable", 1))
    packed = home / "sessions" / "2026" / "11" / "03" / f"rollout-2026-11-03T11-00-00-{uid(9)}.jsonl.zst"
    packed.write_bytes(b"\x28\xb5\x2f\xfd not json")
    index = Index(None, [Codex()])

    assert index.refresh() == 1
    assert [r["session_id"] for r in index.records()] == [THREAD]
    assert index.refresh() == 0  # not retried until the file changes
    assert "jsonl.zst" in capsys.readouterr().err
