"""Codex next to Claude: what the board tells the page about it, and its failures staying its own."""

import os
from datetime import UTC, datetime
from pathlib import Path

from codex_rollouts import meta, uid, write
from fakes import FakeApp, no_processes

from see_your_agents.core import providers
from see_your_agents.core.providers.base import LiveSession, Provider
from see_your_agents.core.providers.codex import live as codex_live
from see_your_agents.core.providers.codex import queue as codex_queue
from see_your_agents.core.providers.codex.provider import Codex

NOW = datetime.now(UTC).isoformat()
CANNOT = {"cost", "tasks", "peer", "permission_wait", "background", "name", "stop", "files"}


class Other(Provider):
    """A provider whose session runs, to see it stay on the board while Codex fails."""

    id, title, caps, parser_version = "other", "Другая", frozenset(), 1

    def available(self):
        return True

    def transcripts(self):
        return {}

    def parse(self, path):
        raise AssertionError(path)

    def live(self):
        return {"o1": LiveSession("o1", "interactive", "idle", provider=self.id)}


def broken(home):
    raise PermissionError(f"{home} is unreadable")


def test_the_board_says_what_each_program_can_do_so_the_page_hides_the_rest(monkeypatch):
    no_processes(monkeypatch)
    monkeypatch.setattr(codex_queue.shutil, "which", lambda name: f"/bin/{name}")
    records = [
        {
            "session_id": "c1",
            "title": "Claude work",
            "provider": "claude",
            "cost_usd": 1.5,
            "last_activity": NOW,
        },
        {
            "session_id": "x1",
            "title": "Codex work",
            "provider": "codex",
            "tokens": 120_000,
            "cwd": "/w/app",
            "last_activity": NOW,
        },
    ]
    live = {"x1": LiveSession("x1", "interactive", "busy", provider="codex", host="daemon")}

    board = FakeApp(records, live).board()

    caps = {pid: set(p["caps"]) for pid, p in board["providers"].items()}
    assert next(iter(board["providers"])) == providers.default().id  # the page labels every other program
    assert {"cost", "tasks", "stop"} <= caps["claude"]
    assert not CANNOT & caps["codex"] and {"attach", "launch"} <= caps["codex"]
    codex = next(c for c in board["cards"] if c["session_id"] == "x1")
    assert (codex["provider"], codex["column"], codex["tokens"], codex["tasks"]) == (
        "codex",
        "working",
        120_000,
        [],
    )
    busy = "Codex is working on a turn: you can write once it finishes"  # English: outside a request
    assert codex["delivery"] == {"route": None, "can_send": False, "reason": busy, "alt": None}


def test_the_terminal_command_resumes_a_thread_in_its_folder_once_it_has_a_rollout(monkeypatch):
    no_processes(monkeypatch)
    thread, fresh = uid(0), uid(1)
    write(Path(os.environ["CODEX_HOME"]), thread, meta(thread))
    record = {
        "session_id": thread,
        "title": "t",
        "provider": "codex",
        "cwd": "/w/my app",
        "last_activity": NOW,
    }
    opened = LiveSession(fresh, "interactive", "idle", provider="codex", host="daemon")  # no prompt yet
    app = FakeApp([record], {fresh: opened})

    assert app.detail(thread)["resume_command"] == f"cd '/w/my app' && codex resume {thread}"
    assert app.detail(fresh)["resume_command"] == ""


def test_codex_failing_to_tell_who_runs_leaves_the_others_on_the_board(monkeypatch, capsys):
    monkeypatch.setattr(codex_live, "lock_files", broken)
    monkeypatch.setattr(providers, "PROVIDERS", (Codex(), Other()))

    assert list(providers.live()) == ["o1"]
    assert "provider codex" in capsys.readouterr().err
