"""A fake App for the fleet and MCP tests: records and live sessions in memory, no ~/.claude."""

import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from codex_rollouts import agent_item, dump, queue_started, turn_complete, uid, user_item

from see_your_agents.app import pairing as _pairing
from see_your_agents.app.server import App
from see_your_agents.core.providers.claude import routes as claude_routes
from see_your_agents.core.providers.codex import queue as codex_queue
from see_your_agents.core.providers.codex.provider import Codex
from see_your_agents.core.providers.codex.rollout import find_rollout
from see_your_agents.features import FEATURES

DEFAULTS = {"transcript_missing": False, "provider": "claude"}  # what the real index adds to a record


class FakeIndex:
    def __init__(self, records):
        self._records = {r["session_id"]: r for r in records}

    def records(self):
        return [{**DEFAULTS, **r} for r in self._records.values()]

    def record(self, sid):
        return {**DEFAULTS, **self._records[sid]} if sid in self._records else None

    def events(self, sid):
        return []

    def search(self, query, limit=200):
        return {
            sid: [{"ts": r["last_activity"], "kind": "title", "text": r["title"]}]
            for sid, r in self._records.items()
            if query.lower() in r["title"].lower()
        }


class FakeApp(App):
    def __init__(self, records, live, features=FEATURES):
        super().__init__(FakeIndex(records), features)
        self.fake_live = live

    def live(self):
        return self.fake_live


def pair(app, port) -> dict[str, str]:
    """Pair a browser with `app`'s server on `port` and return the header its POSTs carry. The host now
    refuses a mutating POST from an unpaired browser, so a test that drives one through the real server
    sends this, as the dashboard's own tab does."""
    app.pairing = _pairing.Pairing(port)
    secret = app.pairing.redeem(app.pairing.new_code())
    return {"X-SYA-Pair": secret}


def no_processes(monkeypatch):
    monkeypatch.setattr(claude_routes, "snapshot", lambda: claude_routes.Snapshot({}, {}, set(), False))


class Spawned:
    """The Claude provider's subprocess.Popen: records each argv and starts nothing; the turn is finished."""

    def __init__(self):
        self.argv: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.argv.append(cmd)
        return self

    def poll(self):
        return 0


class _Subprocess:
    """The subprocess module with some names replaced, for one provider module only."""

    def __init__(self, **replaced):
        self.__dict__.update(replaced)

    def __getattr__(self, name):
        return getattr(subprocess, name)


def record_resume_argv(monkeypatch) -> Spawned:
    """`claude` on PATH, and a resume turn records its argv instead of running; only inside the Claude
    provider's send module, so subprocess.run elsewhere (git, ps) keeps working."""
    spawned, which = Spawned(), shutil.which
    monkeypatch.setattr(
        claude_routes.shutil, "which", lambda n: "/usr/bin/claude" if n == "claude" else which(n)
    )
    monkeypatch.setattr(claude_routes, "subprocess", _Subprocess(Popen=spawned))
    return spawned


def codex_record(thread: str) -> dict:
    """The index's record of a Codex thread, parsed from its rollout under CODEX_HOME as the index would."""
    path = find_rollout(Path(os.environ["CODEX_HOME"]), thread)
    return {**Codex().parse(path).to_record(), "provider": "codex"}


class CodexQueue:
    """`codex queue`, inside the Codex provider's send module only: records each argv and answers as 0.166.0
    did. With `answer`, the daemon starts a turn with the queued text and answers it in the rollout."""

    QUEUED = "Queued message 01a1a150-21aa-70d1-8350-a490592af076 for thread {tid}.\n"

    def __init__(self, answer: str | None = "ok", said: str = QUEUED):
        self.answer, self.said = answer, said
        self.argv: list[list[str]] = []
        self.turns: list[str] = []  # ids of the turns it started

    def run(self, argv, **kw):
        self.argv.append(argv)
        tid, text = argv[2].removeprefix("--thread="), argv[3].removeprefix("--message=")
        if self.answer is not None:
            turn = uid(30 + len(self.turns), "e")
            self.turns.append(turn)
            lines = [queue_started(turn, 30), user_item(text, 30), agent_item(self.answer, minute=30)]
            with find_rollout(Path(os.environ["CODEX_HOME"]), tid).open("a") as fh:
                fh.write(dump(*lines, turn_complete(turn, self.answer, 30)))
        return subprocess.CompletedProcess(argv, 0, "", self.said.format(tid=tid))

    def message(self, n: int = 0) -> str:
        return self.argv[n][3].removeprefix("--message=")


def fake_codex_queue(monkeypatch, **kw) -> CodexQueue:
    """`codex` on PATH for the Codex provider, a short wait for the turn, and `codex queue` faked."""
    fake = CodexQueue(**kw)
    which = shutil.which
    monkeypatch.setattr(codex_queue.shutil, "which", lambda n: "/bin/codex" if n == "codex" else which(n))
    monkeypatch.setattr(codex_queue, "subprocess", _Subprocess(run=fake.run))
    monkeypatch.setattr(codex_queue, "CONFIRM_S", 0.5)
    monkeypatch.setattr(codex_queue, "POLL_S", 0.02)
    return fake


def _ps_runs() -> bool:
    try:
        subprocess.run(["ps", "-o", "pid=", "-p", str(os.getpid())], capture_output=True)
    except OSError:
        return False
    return True


# /bin/ps is setuid root, and the builder's Bash sandbox (Seatbelt) refuses to exec it. The dashboard's own
# checks run outside the sandbox, so these tests still run before a build can be accepted.
needs_ps = pytest.mark.skipif(not _ps_runs(), reason="ps does not start in the builder's sandbox")


def _seatbelt_applies() -> bool:
    exe = "/usr/bin/sandbox-exec"
    if not os.path.exists(exe):
        return False
    try:
        probe = [exe, "-p", "(version 1)(allow default)", "/usr/bin/true"]
        return subprocess.run(probe, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# Seatbelt does not nest: inside a sandbox (the builder's, or the dashboard's own checks under sandbox-exec)
# sandbox-exec fails with `sandbox_apply: Operation not permitted`. The tests that run it skip there; run
# outside any sandbox (`uv run pytest` before a commit) they always run.
needs_seatbelt = pytest.mark.skipif(
    not _seatbelt_applies(), reason="sandbox-exec does not apply here (already sandboxed)"
)


def utc_start_of(pid, shift_s=0):
    """The registry's procStart for `pid`: ps prints local time, the registry stores UTC."""
    lstart = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout
    local = datetime.strptime(" ".join(lstart.split()), "%a %b %d %H:%M:%S %Y").astimezone()
    return (local.astimezone(UTC) + timedelta(seconds=shift_s)).strftime("%a %b %d %H:%M:%S %Y")
