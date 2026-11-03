"""Waiting on an approval, from the Codex daemon: the WebSocket framing, the read-only JSON-RPC poll against a
fake daemon that answers with recorded frames, and the poll turning itself off on every failure."""

import functools
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from codex_daemon import (
    ACTIVE,
    APPROVAL,
    APPROVAL_REQUEST,
    IDLE,
    NOT_LOADED,
    NOTIFICATIONS,
    USER_INPUT,
    FakeDaemon,
    read_client_frame,
)
from codex_rollouts import meta, turn_started, uid, user_item, write
from fakes import FakeApp

from see_your_agents.core.providers.base import Cap, LiveSession
from see_your_agents.core.providers.codex import daemon as codex_daemon
from see_your_agents.core.providers.codex import live as codex_live
from see_your_agents.core.providers.codex import queue as codex_queue
from see_your_agents.core.providers.codex.daemon import Mismatch, Waits, accept, frame, poll, waits_for
from see_your_agents.core.providers.codex.provider import Codex
from see_your_agents.i18n import t

A, B, C, D, GONE = uid(0, "a"), uid(1, "b"), uid(2, "c"), uid(3, "d"), uid(4, "e")
FAST = functools.partial(poll, timeout=0.2)


@pytest.fixture
def home() -> Path:
    return Path(os.environ["CODEX_HOME"])


@pytest.fixture
def daemons(home):
    """Start a fake daemon in CODEX_HOME; each is closed after the test."""
    started = []

    def start(statuses, mode="ok", chatter=()):
        started.append(FakeDaemon(home, statuses, mode, chatter))
        return started[-1]

    yield start
    for fake in started:
        fake.close()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Spawn:
    """Runs the poll at once, in the caller's thread, and counts the polls."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, fn) -> None:
        self.count += 1
        fn()


def busy(tid: str, host: str = "daemon") -> LiveSession:
    return LiveSession(tid, "interactive", "busy", provider="codex", host=host)


def waits(poll=FAST) -> tuple[Waits, Spawn, Clock]:
    spawn, clock = Spawn(), Clock()
    return Waits(poll=poll, spawn=spawn, clock=clock), spawn, clock


# ---- framing and parsing ------------------------------------------------------------------------


def test_the_handshake_key_is_answered_as_rfc_6455_says():
    # the RFC's own example; the 0.166.0 daemon gave the same answer to this key
    assert accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize("size", [0, 125, 126, 70_000])
def test_a_client_frame_is_masked_and_carries_its_payload_whole(size):
    payload = bytes(range(256)) * (size // 256) + bytes(size % 256)
    left, right = socket.socketpair()
    with left, right:
        sender = threading.Thread(target=left.sendall, args=(frame(payload),))  # more than the socket buffers
        sender.start()
        assert read_client_frame(right) == (0x1, payload, True)
        sender.join()


@pytest.mark.parametrize(
    ("status", "said"),
    [
        (IDLE, ""),
        (NOT_LOADED, ""),
        (ACTIVE, ""),
        (APPROVAL, "permission prompt"),
        (USER_INPUT, "input needed"),
        ({"type": "active", "activeFlags": ["waitingOnUserInput", "waitingOnApproval"]}, "permission prompt"),
        ({"type": "active", "activeFlags": ["somethingNew"]}, ""),  # a new flag is not a wait
        ({"type": "somethingNew"}, ""),
    ],
)
def test_a_thread_status_says_what_the_thread_waits_for(status, said):
    assert waits_for(status) == said


@pytest.mark.parametrize("status", [None, "active", {"activeFlags": []}, {"type": "active"}, {"type": 1}])
def test_a_status_of_an_unknown_shape_is_a_mismatch(status):
    with pytest.raises(Mismatch):
        waits_for(status)


# ---- the poll -----------------------------------------------------------------------------------


def test_the_poll_only_reads_and_finds_the_threads_that_wait(home, daemons):
    chatter = [*NOTIFICATIONS, APPROVAL_REQUEST]
    fake = daemons({A: APPROVAL, B: USER_INPUT, C: ACTIVE, D: IDLE}, chatter=chatter)

    found = poll(str(codex_daemon.socket_path(home)), {A, B, C, GONE})

    assert found == {A: "permission prompt", B: "input needed"}
    assert fake.closed.wait(2)  # closed politely
    assert fake.methods() == ["initialize", "initialized", "thread/loaded/list", *["thread/read"] * 3]
    sent = fake.sent()
    assert all("method" in m for m in sent)  # the daemon's approval request got no answer
    assert all(masked for _, _, masked in fake.received)
    assert sent[0]["params"]["clientInfo"]["name"] == "see_your_agents"
    reads = [m["params"] for m in fake.messages("thread/read")]
    assert reads == [{"threadId": t, "includeTurns": False} for t in sorted((A, B, C))]  # not D: not asked


def test_a_ping_gets_its_pong_and_the_answer_still_comes(home, daemons):
    fake = daemons({A: APPROVAL}, "ping")

    assert poll(str(codex_daemon.socket_path(home)), {A}) == {A: "permission prompt"}
    assert fake.closed.wait(2)
    assert (0xA, b"hi", True) in fake.received


def test_a_thread_the_daemon_cannot_read_is_skipped_not_a_failure(home, daemons):
    daemons({A: APPROVAL, B: None})  # B unloaded between the list and the read: the daemon answers an error

    assert poll(str(codex_daemon.socket_path(home)), {A, B}) == {A: "permission prompt"}


# ---- turning itself off -------------------------------------------------------------------------


def test_without_a_daemon_nothing_is_polled_and_the_sessions_stay(home):
    sessions = {A: busy(A)}
    w, spawn, _ = waits()

    assert w.apply(home, sessions) == sessions
    (home / "app-server-daemon").mkdir(parents=True)
    (home / "app-server-daemon" / "daemon.pid").write_text(f'{{"pid": {os.getpid()}}}')
    assert w.apply(home, sessions) == sessions  # a live pid, but no socket
    assert spawn.count == 0


def test_a_dead_daemon_is_not_asked(home, daemons):
    daemons({A: APPROVAL})
    gone = subprocess.Popen(["true"])
    gone.wait()
    (home / "app-server-daemon" / "daemon.pid").write_text(f'{{"pid": {gone.pid}}}')
    w, spawn, _ = waits()

    assert w.apply(home, {A: busy(A)})[A].status == "busy"
    assert spawn.count == 0


@pytest.mark.parametrize("mode", ["refuse", "silent", "mute"])
def test_an_unreachable_daemon_turns_the_poll_off_for_a_while(home, daemons, mode):
    daemons({A: APPROVAL}, mode)
    sessions = {A: busy(A)}
    w, spawn, clock = waits()

    assert w.apply(home, sessions) == sessions
    clock.now += codex_daemon.POLL_S
    assert w.apply(home, sessions) == sessions
    assert spawn.count == 1  # quiet now
    clock.now += codex_daemon.RETRY_S
    w.apply(home, sessions)
    assert spawn.count == 2  # and tried again later: the daemon may be back


@pytest.mark.parametrize("mode", ["bad_handshake", "bad_accept", "bad_json", "binary", "fragmented"])
def test_a_daemon_speaking_another_protocol_turns_the_poll_off_while_it_runs(home, daemons, mode):
    fake = daemons({A: APPROVAL}, mode)
    sessions = {A: busy(A)}
    w, spawn, clock = waits()

    assert w.apply(home, sessions) == sessions
    clock.now += 10 * codex_daemon.RETRY_S
    assert w.apply(home, sessions) == sessions
    assert spawn.count == 1
    pid_file = home / "app-server-daemon" / "daemon.pid"
    pid_file.write_text(pid_file.read_text().replace("10:42:24", "11:00:00"))  # another daemon started
    fake.mode = "ok"
    assert w.apply(home, sessions)[A].status == "waiting"
    assert spawn.count == 2


def test_a_status_of_an_unknown_shape_turns_the_poll_off(home, daemons):
    daemons({A: "waiting"})
    sessions = {A: busy(A)}
    w, spawn, clock = waits()

    assert w.apply(home, sessions) == sessions
    clock.now += 10 * codex_daemon.RETRY_S
    w.apply(home, sessions)
    assert spawn.count == 1


def test_an_old_answer_is_not_used(home, daemons):
    fake = daemons({A: APPROVAL})
    w, spawn, clock = waits()
    assert w.apply(home, {A: busy(A)})[A].status == "waiting"

    fake.close()  # the next poll fails, and the last answer ages out
    clock.now += codex_daemon.STALE_S + 1
    assert w.apply(home, {A: busy(A)})[A].status == "busy"


# ---- on the board -------------------------------------------------------------------------------


def test_only_a_busy_daemon_thread_is_asked_about(home, daemons):
    fake = daemons({A: APPROVAL, B: APPROVAL, C: APPROVAL})
    idle = LiveSession(C, "interactive", "idle", provider="codex", host="daemon")
    sessions = {A: busy(A), B: busy(B, host="vscode"), C: idle}
    w, _, _ = waits()

    found = w.apply(home, sessions)

    assert (found[A].status, found[A].waiting_for) == ("waiting", "permission prompt")
    assert found[B] == sessions[B] and found[C] == sessions[C]  # VS Code's threads are not the daemon's
    assert [m["params"]["threadId"] for m in fake.messages("thread/read")] == [A]


def daemon_thread(home, monkeypatch, tid: str) -> None:
    """A thread with an open turn whose lock the daemon (this process, as the fake's pid file says) holds."""
    write(home, tid, meta(tid), turn_started(uid(3, "t"), 3), user_item("touch x", 3))
    (home / "thread-writer-locks").mkdir(parents=True, exist_ok=True)
    (home / "thread-writer-locks" / f"{tid}.lock").touch()
    monkeypatch.setattr(codex_live, "holders", lambda locks: dict.fromkeys(locks, os.getpid()))
    command = "codex app-server --listen unix:// --managed-daemon"
    monkeypatch.setattr(codex_live, "command_lines", lambda pids: dict.fromkeys(pids, command))


def test_without_the_daemon_the_board_is_as_before(home, monkeypatch):
    daemon_thread(home, monkeypatch, A)

    found = Codex().live()[A]

    assert (found.host, found.status, found.waiting_for) == ("daemon", "busy", "")


def test_a_thread_waiting_on_an_approval_needs_you_and_takes_no_text(home, daemons, monkeypatch):
    daemons({A: APPROVAL})
    daemon_thread(home, monkeypatch, A)
    codex = Codex()
    deadline = time.monotonic() + 5
    while (live := codex.live()[A]).status != "waiting" and time.monotonic() < deadline:
        time.sleep(0.05)  # the first ask starts the poll in its own thread and does not wait for it

    assert (live.status, live.waiting_for) == ("waiting", "permission prompt")
    record = {"session_id": A, "title": "t", "provider": "codex", "cwd": "/work/app", "last_activity": ""}
    monkeypatch.setattr(codex_queue.shutil, "which", lambda name: f"/bin/{name}")
    card = FakeApp([record], {A: live}).board()["cards"][0]
    assert card["column"] == "needs_you"
    assert card["delivery"]["can_send"] is False
    assert card["delivery"]["reason"] == t("en", codex_queue.WAITING)
    assert Cap.PERMISSION_WAIT not in codex.caps  # the dashboard cannot answer Codex's prompts
