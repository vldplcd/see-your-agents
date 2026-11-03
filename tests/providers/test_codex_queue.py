"""Writing to a Codex thread: `codex queue` is stubbed, and delivery is read from a synthetic rollout."""

import os
import subprocess
from pathlib import Path

import pytest
from codex_rollouts import (
    agent_item,
    dump,
    meta,
    queue_started,
    turn_complete,
    turn_started,
    uid,
    user_item,
    write,
)

from see_your_agents.core.providers.base import (
    LiveSession,
    Message,
    SendError,
    SendInvalid,
    SendRefused,
    SendTimeout,
    Sent,
    Session,
)
from see_your_agents.core.providers.codex import queue as codex_queue

TID = uid(0)
QUEUED = "Queued message 01a1a150-21aa-70d1-8350-a490592af076 for thread {tid}.\n"
HELD = (
    "Warning: message was queued, but could not resume thread {tid}: thread/resume failed: thread {tid} "
    "already has an active writer (code -32600)\n"
)  # what 0.166.0 printed for a thread a `codex exec` run held


def queue_turn(text: str, minute: int = 5) -> list[dict]:
    """A turn the daemon started from the queue, as it lands in the rollout."""
    turn = uid(minute, "b")
    answer = agent_item("ok", minute=minute)
    return [queue_started(turn, minute), user_item(text, minute), answer, turn_complete(turn, "ok", minute)]


class Codex:
    """`codex queue`: records argv and keyword arguments, answers with `said`, and may append to the rollout
    what the daemon would write."""

    def __init__(self, said: str = QUEUED, appends: list[dict] | None = None, code: int = 0):
        self.said, self.appends, self.code = said.format(tid=TID), appends or [], code
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.appends:
            with rollout_path().open("a") as fh:
                fh.write(dump(*self.appends))
        return subprocess.CompletedProcess(argv, self.code, "", self.said)


def rollout_path() -> Path:
    return next(Path(os.environ["CODEX_HOME"]).rglob(f"rollout-*-{TID}.jsonl"))


@pytest.fixture
def thread():
    """A thread whose first turn ended: what `codex exec` from the New session dialog leaves."""
    turn = uid(1, "a")
    home = Path(os.environ["CODEX_HOME"])
    write(home, TID, meta(TID), turn_started(turn, 1), user_item("start", 1), turn_complete(turn, "done", 1))
    return Session(TID, "/work/app")


@pytest.fixture
def codex(monkeypatch):
    """`codex` on PATH, a short confirmation window, and `codex queue` answering as set per test."""
    monkeypatch.setattr(codex_queue.shutil, "which", lambda name: "/bin/codex")
    monkeypatch.setattr(codex_queue, "CONFIRM_S", 0.3)
    monkeypatch.setattr(codex_queue, "POLL_S", 0.02)

    def answer(**kw):
        fake = Codex(**kw)
        monkeypatch.setattr(codex_queue.subprocess, "run", fake)
        return fake

    return answer


def held_by(host: str, status: str = "idle") -> LiveSession:
    return LiveSession(TID, "interactive", status, host=host, provider="codex")


# ---- the route ----------------------------------------------------------------------------------


def test_a_stopped_or_idle_daemon_thread_takes_the_queue_and_says_where_the_text_goes(codex):
    for live in (None, held_by("daemon")):
        found = codex_queue.route(Session(TID, "/w", live=live))
        assert (found.route, found.can_send, found.reason) == ("queue", True, codex_queue.READY)


@pytest.mark.parametrize("host", ["vscode", "tui", "exec"])
def test_a_thread_another_process_holds_is_refused_before_anything_is_queued(codex, host):
    found = codex_queue.route(Session(TID, "/w", live=held_by(host)))

    assert not found.can_send and found.reason == codex_queue.HELD[host]


def test_a_busy_thread_is_refused_its_message_would_wait_past_any_confirmation(codex):
    found = codex_queue.route(Session(TID, "/w", live=held_by("daemon", "busy")))

    assert not found.can_send and found.reason == codex_queue.BUSY


def test_a_thread_whose_last_turn_was_aborted_is_sent_to_the_terminal(codex):
    """Codex resumes such a thread from the queue but starts no turn (docs/notes/codex-cli-notes.md,
    «Send»)."""
    record = {"session_id": TID, "cwd": "/w", "last_turn_aborted": True}

    found = codex_queue.route(Session.of(record, None))

    assert not found.can_send and found.why("en").endswith(f"open codex resume {TID}")
    assert found.why("ru").endswith(f"— открой codex resume {TID}")


def test_no_route_without_codex_or_without_the_transcript(codex, monkeypatch):
    assert not codex_queue.route(Session(TID, "/w", transcript_missing=True)).can_send
    monkeypatch.setattr(codex_queue.shutil, "which", lambda name: None)
    assert codex_queue.route(Session(TID, "/w")).reason == codex_queue.NO_CODEX


# ---- sending ------------------------------------------------------------------------------------


def test_a_message_counts_as_delivered_once_a_queue_turn_carries_it_and_names_that_turn(codex, thread):
    fake = codex(appends=queue_turn("--проверь CI"))

    sent = codex_queue.send(thread, Message("--проверь CI"))

    assert sent == Sent("queue", msg_id=uid(5, "b"))  # the turn's id finds its answer in the digest
    (argv, kw), *_ = fake.calls
    assert argv == ["/bin/codex", "queue", f"--thread={TID}", "--message=--проверь CI", "--resume"]
    assert kw["stdin"] == subprocess.DEVNULL and "VIRTUAL_ENV" not in kw["env"]


def test_exit_0_without_a_turn_is_not_a_delivery(codex, thread):
    codex()  # "Queued message …", and the rollout stays as it was

    with pytest.raises(SendTimeout, match="didn’t start a turn"):
        codex_queue.send(thread, Message("привет"))
    assert codex_queue.route(thread).can_send  # the session is free for the next try


def test_a_queue_turn_with_someone_elses_text_does_not_confirm_this_one(codex, thread):
    codex(appends=queue_turn("an older queued message"))

    with pytest.raises(SendTimeout):
        codex_queue.send(thread, Message("привет"))


def test_a_thread_held_elsewhere_is_a_refusal_even_though_codex_exits_0(codex, thread):
    codex(said=HELD)

    with pytest.raises(SendRefused, match="Another Codex process holds the session"):
        codex_queue.send(thread, Message("привет"))


def test_a_resume_that_failed_for_another_reason_is_an_error(codex, thread):
    codex(said=QUEUED + "Warning: message was queued, but could not resume thread {tid}: daemon is gone\n")

    with pytest.raises(SendError, match="daemon is gone") as caught:
        codex_queue.send(thread, Message("привет"))
    assert not isinstance(caught.value, SendRefused)


def test_files_and_refused_routes_never_reach_codex(codex, thread):
    fake = codex()

    with pytest.raises(SendInvalid):
        codex_queue.send(thread, Message("см. файл", files=("/u/a.png",)))
    with pytest.raises(SendRefused):
        codex_queue.send(Session(TID, "/w", live=held_by("vscode")), Message("привет"))
    assert fake.calls == []


def test_a_thread_without_a_rollout_yet_is_confirmed_once_its_first_turn_writes_one(codex, monkeypatch):
    """A TUI's thread before its first prompt: the queue turn creates the rollout."""
    calls = []

    def first_turn(argv, **kw):
        calls.append(argv)
        write(Path(os.environ["CODEX_HOME"]), TID, meta(TID), *queue_turn("начни"))
        return subprocess.CompletedProcess(argv, 0, "", QUEUED.format(tid=TID))

    monkeypatch.setattr(codex_queue.subprocess, "run", first_turn)

    assert codex_queue.send(Session(TID, "", live=held_by("daemon")), Message("начни")).route == "queue"
    assert len(calls) == 1


def test_the_card_says_a_message_is_on_its_way_while_the_send_waits(codex, thread, monkeypatch):
    seen = []

    def watching(argv, **kw):
        seen.append(codex_queue.route(thread))
        with rollout_path().open("a") as fh:
            fh.write(dump(*queue_turn("привет")))
        return subprocess.CompletedProcess(argv, 0, "", QUEUED.format(tid=TID))

    monkeypatch.setattr(codex_queue.subprocess, "run", watching)
    codex_queue.send(thread, Message("привет"))

    assert seen[0].pending and not seen[0].can_send
    assert not codex_queue.route(thread).pending
