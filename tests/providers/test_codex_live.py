import json
import os
import subprocess
from pathlib import Path

import pytest
from codex_rollouts import dump, fork_rollout, meta, turn_complete, turn_started, uid, user_item, write

from see_your_agents.core.providers.codex import live as codex_live
from see_your_agents.core.providers.codex.provider import Codex

HELD, FREE, NEW = uid(0), uid(1), uid(2)
DAEMON, VSCODE = 4100, 4200


class Processes:
    """lsof and ps as this machine would answer them: who holds which lock, and their command lines."""

    def __init__(self, held: dict[str, int], commands: dict[int, str], lsof_works: bool = True):
        self.held, self.commands, self.lsof_works = held, commands, lsof_works

    def __call__(self, argv, **kw):
        if Path(argv[0]).name == "lsof":
            if not self.lsof_works:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
            # lsof prints the resolved path, which need not be the one asked for (/tmp vs /private/tmp)
            out = "".join(
                f"p{pid}\nf9\nn/private{f}\n" for f in argv[5:] if (pid := self.held.get(Path(f).stem))
            )
            return subprocess.CompletedProcess(argv, 0 if out else 1, out, "")
        if argv[0] == "ps":
            pids = [int(p) for p in argv[-1].split(",")]
            out = "".join(f"{p} {self.commands[p]}\n" for p in pids if p in self.commands)
            return subprocess.CompletedProcess(argv, 0, out, "")
        raise AssertionError(argv)


@pytest.fixture
def home():
    home = Path(os.environ["CODEX_HOME"])
    (home / "thread-writer-locks").mkdir(parents=True)
    for tid in (HELD, FREE, NEW):
        (home / "thread-writer-locks" / f"{tid}.lock").touch()
    (home / "thread-writer-locks" / ".coordination.lock").touch()
    (home / "app-server-daemon").mkdir()
    (home / "app-server-daemon" / "daemon.pid").write_text(
        json.dumps({"pid": DAEMON, "processStartTime": "x"})
    )
    return home


def stub(monkeypatch, processes):
    monkeypatch.setattr(codex_live.subprocess, "run", processes)


DAEMON_CMD = "/x/codex app-server --listen unix:// --managed-daemon"
VSCODE_CMD = (
    "/x/.vscode/extensions/openai.chatgpt/bin/codex -c features.code_mode_host=true app-server --analytics"
)


def test_only_a_held_lock_is_a_running_thread_and_its_holder_says_how_it_runs(home, monkeypatch):
    write(home, HELD, meta(HELD), turn_started(uid(3)), user_item("go", 3))
    stub(monkeypatch, Processes({HELD: DAEMON, NEW: VSCODE}, {DAEMON: DAEMON_CMD, VSCODE: VSCODE_CMD}))

    found = Codex().live()

    assert set(found) == {HELD, NEW}  # FREE's lock file is left behind by nobody
    held, new = found[HELD], found[NEW]
    assert (held.provider, held.host, held.kind, held.pid) == ("codex", "daemon", "interactive", DAEMON)
    assert (new.host, new.status) == ("vscode", "idle")  # no rollout yet: nothing asked so far


def test_when_lsof_fails_a_lock_that_exists_counts_as_held(home, monkeypatch):
    stub(monkeypatch, Processes({}, {}, lsof_works=False))

    found = Codex().live()

    assert set(found) == {HELD, FREE, NEW}
    assert {s.host for s in found.values()} == {""}


def test_a_thread_is_busy_until_its_turn_completes_or_aborts(home, monkeypatch):
    path = write(home, HELD, meta(HELD), turn_started(uid(3, "a"), 3), user_item("go", 3))
    stub(monkeypatch, Processes({HELD: DAEMON}, {DAEMON: DAEMON_CMD}))
    codex = Codex()
    status = lambda: codex.live()[HELD].status  # noqa: E731

    assert status() == "busy"
    with path.open("a") as fh:
        fh.write(dump(turn_complete(uid(3, "a"), "done", 4)))
    assert status() == "idle"
    with path.open("a") as fh:
        fh.write(dump(turn_started(uid(5, "b"), 5)))
    assert status() == "busy"
    with path.open("a") as fh:
        fh.write(
            dump(
                {
                    "timestamp": "2026-11-03T00:06:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "turn_aborted", "reason": "interrupted"},
                }
            )
        )
    assert status() == "idle"
    assert codex.live()[HELD].updated_at == "2026-11-03T00:06:00.000Z"


def test_a_fork_is_not_busy_with_the_open_turn_of_the_parent_it_replays(home, monkeypatch):
    fork = uid(5, "e")  # created at minute 5, after the parent's turns
    (home / "thread-writer-locks" / f"{fork}.lock").touch()
    lines = fork_rollout(HELD, fork, at=5)
    own = next(i for i, e in enumerate(lines) if e["payload"].get("turn_id") == uid(5, "c"))
    path = write(home, fork, *lines[:own])  # the replay ends inside the parent's open turn
    stub(monkeypatch, Processes({fork: DAEMON}, {DAEMON: DAEMON_CMD}))
    codex = Codex()

    assert codex.live()[fork].status == "idle"
    with path.open("a") as fh:
        fh.write(dump(*lines[own : own + 2]))
    assert codex.live()[fork].status == "busy"


@pytest.mark.parametrize(
    ("command", "host", "kind"),
    [
        ("codex exec --json -C /work -- write tests", "exec", "background"),
        ("codex -m gpt-6 exec --json -- go", "exec", "background"),
        ("codex --no-daemon", "tui", "interactive"),
        ("codex fix the exec path", "tui", "interactive"),  # a prompt is not a subcommand, the first word is
    ],
)
def test_a_lock_held_by_codex_itself_is_exec_or_a_tui(home, monkeypatch, command, host, kind):
    stub(monkeypatch, Processes({HELD: 4300}, {4300: command}))

    found = Codex().live()[HELD]

    assert (found.host, found.kind) == (host, kind)


def test_a_running_subagent_is_not_a_card_of_its_own(home, monkeypatch):
    write(home, HELD, meta(HELD, source={"subagent": {"thread_spawn": {"parent_thread_id": NEW}}}))
    stub(monkeypatch, Processes({HELD: DAEMON}, {DAEMON: DAEMON_CMD}))

    assert HELD not in Codex().live()
