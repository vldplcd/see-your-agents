"""The watchdog with real processes: tiny stdlib children, a real git repo, nothing beyond 127.0.0.1."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest
from gitrepo import commit, git, isolate_git, make_repo

from see_your_agents.app import watchdog

# Each run appends its arguments to the state file. Run 1 asks for a restart; later runs follow `mode`.
CHILD = r"""
import http.server, json, os, sys, threading
from pathlib import Path
state, mode, port = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
runs = json.loads(state.read_text()) if state.exists() else []
runs.append({"argv": sys.argv[4:], "watchdog": os.environ.get("SYA_WATCHDOG")})
state.write_text(json.dumps(runs))
n = len(runs)
if n == 1:
    sys.exit(75)
if mode == "broken" and n == 2:
    sys.exit(1)
if mode == "silent" and n == 2:
    import time
    time.sleep(30)  # alive, never answers; the watchdog stops it
if mode == "plain" or n > 2:
    sys.exit(0)

class Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        threading.Thread(target=server.shutdown).start()  # one answer, then exit 0

    def log_message(self, *args):
        pass

server = http.server.HTTPServer(("127.0.0.1", port), Health)
server.serve_forever()
"""

# Waits for a signal and records which one came.
PATIENT = r"""
import signal, sys, time
from pathlib import Path
out = Path(sys.argv[1])
def got(signum, frame):
    out.write_text(str(signum))
    sys.exit(0)
signal.signal(signal.SIGINT, got)
signal.signal(signal.SIGTERM, got)
out.write_text("ready")
time.sleep(30)
"""

# Ignores SIGTERM and SIGINT, writing down each one it got.
STUBBORN = r"""
import signal, sys, time
from pathlib import Path
out = Path(sys.argv[1])
def got(signum, frame):
    with out.open("a") as f:
        f.write(f" {signum}")
signal.signal(signal.SIGINT, got)
signal.signal(signal.SIGTERM, got)
out.write_text("ready")
time.sleep(60)
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SYA_CONFIG_DIR", str(tmp_path / "config"))
    isolate_git(monkeypatch)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def child(tmp_path, mode: str, port: int) -> tuple[list[str], object]:
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    state = tmp_path / "runs.json"
    return [sys.executable, str(script), str(state), mode, str(port)], state


def accepted_merge(tmp_path, accepted_at: datetime) -> tuple[object, str]:
    """A checkout whose HEAD merges build/x, and builds.json saying it was just accepted."""
    repo = make_repo(tmp_path / "repo")
    git(repo, "switch", "-q", "-c", "build/x")
    commit(repo, "feature.txt", "new\n")
    git(repo, "switch", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "accept build/x", "build/x")
    merge = git(repo, "rev-parse", "HEAD")
    build = {
        "id": "x-1a2b",
        "status": "accepted",
        "merge_sha": merge,
        "checkout": str(repo),
        "base_branch": "main",
        "accepted_at": accepted_at.isoformat(),
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "builds.json").write_text(json.dumps({"builds": [build]}))
    return repo, merge


def test_a_restart_request_starts_the_child_again_without_a_browser(tmp_path):
    cmd, state = child(tmp_path, "plain", free_port())

    assert watchdog.supervise(cmd, 1) == 0
    runs = json.loads(state.read_text())
    assert [r["argv"] for r in runs] == [[], ["--no-open"]]
    assert {r["watchdog"] for r in runs} == {"1"}


def test_any_other_exit_code_is_the_parents(tmp_path):
    script = tmp_path / "fail.py"
    script.write_text("import sys; sys.exit(3)")
    assert watchdog.supervise([sys.executable, str(script)], 1) == 3


def test_an_accepted_build_that_answers_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "HEALTH_WAIT_S", 60.0)  # a cold CI runner may need more than 15 s
    port = free_port()
    repo, merge = accepted_merge(tmp_path, datetime.now(UTC))
    cmd, state = child(tmp_path, "healthy", port)

    assert watchdog.supervise(cmd, port) == 0
    assert len(json.loads(state.read_text())) == 2
    assert git(repo, "rev-parse", "HEAD") == merge


@pytest.mark.parametrize(
    ("mode", "why", "reason"),
    [
        ("broken", "the new version crashed on start (code 1)", ("builder.error.crashed", {"code": 1})),
        ("silent", "the new version did not answer in 1 s", ("builder.error.no_answer", {"n": 1})),
    ],
)
def test_an_accepted_build_that_does_not_come_up_is_reverted(tmp_path, monkeypatch, capfd, mode, why, reason):
    """The terminal reads why in English; builds.json keeps a key of the builder's catalog, for the card."""
    monkeypatch.setattr(watchdog, "HEALTH_WAIT_S", 1.0)
    port = free_port()
    repo, merge = accepted_merge(tmp_path, datetime.now(UTC))
    cmd, state = child(tmp_path, mode, port)

    assert watchdog.supervise(cmd, port) == 0
    assert len(json.loads(state.read_text())) == 3  # restart, the bad one, the reverted one
    assert git(repo, "rev-parse", "HEAD^") == merge
    assert not (repo / "feature.txt").exists()
    build = json.loads((tmp_path / "config" / "builds.json").read_text())["builds"][0]
    key, params = reason
    assert build["status"] == "rolled_back" and build["error"] == {"key": key, "params": params}
    assert build["revert_sha"] == git(repo, "rev-parse", "HEAD")
    assert f"sya: {why}; rolled back {merge[:8]}" in capfd.readouterr().err


def test_an_accepted_build_that_finds_its_port_taken_is_not_reverted(tmp_path, capfd):
    port = free_port()
    repo, merge = accepted_merge(tmp_path, datetime.now(UTC))
    cmd, state = child(tmp_path, "healthy", port)  # run 2 would answer, but cannot bind
    with socket.socket() as other:
        other.bind(("127.0.0.1", port))

        assert watchdog.supervise(cmd, port) == 1

    assert len(json.loads(state.read_text())) == 2
    assert git(repo, "rev-parse", "HEAD") == merge
    assert json.loads((tmp_path / "config" / "builds.json").read_text())["builds"][0]["status"] == "accepted"
    assert f"port {port} is taken by another process" in capfd.readouterr().err


def test_an_old_accept_a_moved_head_or_another_branch_is_left_alone(tmp_path):
    repo, merge = accepted_merge(tmp_path, datetime.now(UTC) - timedelta(hours=1))
    assert watchdog.last_accept() is None

    accepted_merge_build = json.loads((tmp_path / "config" / "builds.json").read_text())["builds"][0]
    git(repo, "switch", "-q", "-c", "elsewhere")  # the merge is HEAD, but of another branch
    crashed = watchdog.why_reverted(1)[1]
    assert "the checkout is on 'elsewhere'" in watchdog.rollback(accepted_merge_build, crashed)
    git(repo, "switch", "-q", "main")
    head = commit(repo, "more.txt", "x\n")
    assert "is no longer the merge" in watchdog.rollback(accepted_merge_build, crashed)
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "rev-parse", "elsewhere") == merge


def run_watchdog(child_argv: list[str], port: int = 1, setup: str = "") -> subprocess.Popen:
    """supervise() in its own process, so the test can signal it."""
    runner = (
        f"import sys; from see_your_agents.app import watchdog; {setup}"
        f"sys.exit(watchdog.supervise({[sys.executable, *child_argv]!r}, {port}))"
    )
    return subprocess.Popen([sys.executable, "-c", runner])


def wait_for(check, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not check() and time.monotonic() < deadline:
        time.sleep(0.05)


def test_ctrl_c_reaches_the_child_and_the_parent_exits_with_it(tmp_path):
    script = tmp_path / "patient.py"
    script.write_text(PATIENT)
    out = tmp_path / "signal.txt"
    parent = run_watchdog([str(script), str(out)])
    wait_for(lambda: out.exists() and out.read_text() == "ready")
    parent.send_signal(signal.SIGINT)
    parent.send_signal(signal.SIGINT)  # uv and the terminal both send one

    assert parent.wait(10) == 0
    assert out.read_text() == str(int(signal.SIGINT))


def test_a_stop_signal_during_the_health_check_keeps_the_build_and_starts_nothing(tmp_path):
    port = free_port()
    repo, merge = accepted_merge(tmp_path, datetime.now(UTC))
    cmd, state = child(tmp_path, "silent", port)  # run 2 is alive and not answering yet
    parent = run_watchdog(cmd[1:], port)
    wait_for(lambda: state.exists() and len(json.loads(state.read_text())) == 2)
    time.sleep(0.3)  # the watchdog is inside healthy()
    parent.send_signal(signal.SIGTERM)
    try:
        code = parent.wait(10)
    finally:
        parent.kill()

    assert code == 128 + signal.SIGTERM  # run 2 died of the forwarded signal
    assert len(json.loads(state.read_text())) == 2  # nothing started after it
    assert git(repo, "rev-parse", "HEAD") == merge
    assert json.loads((tmp_path / "config" / "builds.json").read_text())["builds"][0]["status"] == "accepted"


def test_every_signal_reaches_the_child_and_one_that_ignores_them_is_killed(tmp_path):
    script = tmp_path / "stubborn.py"
    script.write_text(STUBBORN)
    out = tmp_path / "signals.txt"
    parent = run_watchdog([str(script), str(out)], setup="watchdog.STOP_WAIT_S = 2; ")
    wait_for(lambda: out.exists() and out.read_text().startswith("ready"))
    parent.send_signal(signal.SIGTERM)
    wait_for(lambda: out.read_text().count(" ") == 1)
    parent.send_signal(signal.SIGINT)
    try:
        code = parent.wait(10)
    finally:
        parent.kill()

    assert out.read_text().split()[1:] == [str(int(signal.SIGTERM)), str(int(signal.SIGINT))]
    assert code == 128 + signal.SIGKILL


def test_the_parent_loads_no_feature_and_no_index():
    code = (
        "import json, sys, see_your_agents.app.watchdog; "
        "print(json.dumps([m for m in sys.modules if m.startswith(('see_your_agents', 'duckdb'))]))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    loaded = set(json.loads(out.stdout))
    assert loaded == {"see_your_agents", "see_your_agents.app", "see_your_agents.app.watchdog"}


def test_the_child_command_runs_the_cli_with_child_flag():
    cmd = watchdog.child_command(["--db", "x.duckdb", "serve", "--port", "9"])
    assert cmd[1:] == ["-m", "see_your_agents.app.cli", "--db", "x.duckdb", "serve", "--port", "9", "--child"]
    assert os.path.basename(cmd[0]).startswith("python")
