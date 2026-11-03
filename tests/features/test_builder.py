"""The builder against a real temp git repo; `claude` and `uv` are tiny fake scripts on PATH."""

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fakes import FakeApp, needs_seatbelt, no_processes, pair
from gitrepo import commit, git, isolate_git, make_repo

from see_your_agents import config_dir, i18n
from see_your_agents.app import mcp, server, watchdog
from see_your_agents.core.providers.base import LaunchError, LiveSession
from see_your_agents.core.providers.claude.courier import Delivery
from see_your_agents.features.builder import FEATURE, agent, builder, preview, repo, routes, store

JOB, SID = "abcd1234", "abcd1234-0000-4000-8000-000000000000"
JOB2, SID2 = "efef5678", "efef5678-0000-4000-8000-000000000000"  # `--bg --resume` makes a new session
FAKE_CLAUDE = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
if sys.argv[1:2] == ["--bg"]:
    resumed = any(a.startswith("--resume=") for a in sys.argv)
    print("backgrounded · " + ("{JOB2}" if resumed else "{JOB}") + " · builder")
elif sys.argv[1:3] == ["agents", "--json"]:
    print(json.dumps([{{"id": "{JOB}", "sessionId": "{SID}"}}, {{"id": "{JOB2}", "sessionId": "{SID2}"}}]))
"""
# `uv run sya serve --no-open --port N`: answers /api/health and records what it was given.
# `uv sync ...`: records the call and exits with FAKE_UV_SYNC_EXIT.
FAKE_UV = f"""#!{sys.executable}
import http.server, json, os, sys
if sys.argv[1:2] == ["sync"]:
    with open(os.environ["FAKE_UV_LOG"] + ".sync", "a") as f:
        f.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
    print("Resolved 3 packages\\nerror: no wheel for tick==1.0")
    sys.exit(int(os.environ.get("FAKE_UV_SYNC_EXIT", "0")))
port = int(sys.argv[sys.argv.index("--port") + 1])
cfg = os.environ["SYA_CONFIG_DIR"]
seen = {{"argv": sys.argv[1:], "cwd": os.getcwd(), "config": cfg, "files": sorted(os.listdir(cfg))}}
seen["pid"] = os.getpid()
with open(os.environ["FAKE_UV_LOG"], "w") as f:
    json.dump(seen, f)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{{"ok": true}}')
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SYA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SYA_CACHE_DIR", str(tmp_path / "cache"))
    isolate_git(monkeypatch)
    no_processes(monkeypatch)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("claude", FAKE_CLAUDE), ("uv", FAKE_UV)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.log"))
    monkeypatch.setenv("FAKE_UV_LOG", str(tmp_path / "uv.log"))
    monkeypatch.setattr(builder, "_spawn", lambda fn, *args: fn(*args))  # checks run inline
    monkeypatch.setattr(builder, "CHECKS", "echo checks passed")
    monkeypatch.setattr(agent, "TMP_PARENT", tmp_path / "tmp")  # the builders' temp folders, not in /tmp
    (tmp_path / "tmp").mkdir()
    # pytest's tmp_path is far longer than /private/tmp, so Claude Code's 44-byte cap would refuse every
    # start here; the real layout's length has a test of its own.
    monkeypatch.setattr(agent, "CLAUDE_TMP_MAX", 10_000)
    # `confine` still runs for real (it builds the prefix, checks the binary, makes the temp root), but the
    # binary is a shim that drops `-p <profile>` and execs the rest: the fakes write outside the worktree,
    # which a real Seatbelt profile would deny. The profile's enforcement is tested on its own, under the
    # real /usr/bin/sandbox-exec.
    shim = bin_dir / "sandbox-exec"
    shim.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setattr(agent, "SANDBOX_EXEC", shim)
    yield
    preview.stop()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    path = make_repo(tmp_path / "repo")
    monkeypatch.setattr(builder, "PACKAGE", path)
    return path


@pytest.fixture
def app(tmp_path, checkout):
    return FakeApp([], {})


def claude_calls(tmp_path) -> list[dict]:
    log = tmp_path / "claude.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def uv_syncs(tmp_path, cwd: Path | None = None) -> list[dict]:
    log = tmp_path / "uv.log.sync"
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [c for c in calls if cwd is None or c["cwd"] == str(cwd)]


def live(status: str, **extra) -> LiveSession:
    return LiveSession(
        provider="claude", session_id=SID, kind="background", status=status, name="Builder: часы", **extra
    )


def checked(b: dict) -> dict:
    """The build after the dashboard's own checks (CHECKS is `echo` unless a test says otherwise), run by a
    dashboard on 8765: the port its checks' sandbox closes."""
    store.move(b["id"], store.CHECKING, checks_port=8765)
    builder.run_checks(b["id"])
    return store.get(b["id"])


def new_build(title="Модуль hello-clock: часы в шапке", commits=1, status=store.BUILDING, files=None) -> dict:
    b = builder.create(title, "В шапке текущее время, обновляется раз в минуту.")
    for n in range(commits):
        commit(Path(b["worktree"]), f"clock{n}.txt", "tick\n")
    for name, text in (files or {}).items():
        commit(Path(b["worktree"]), name, text)
    return checked(b) if status == store.READY else store.get(b["id"])


# ---- request_change through the real /mcp handler ---------------------------------------------


PAIRED: dict[str, dict[str, str]] = {}  # base url -> the paired browser's header; a mutating POST needs it


@pytest.fixture
def base(app):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(app, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    PAIRED[url] = pair(app, port)
    yield url
    httpd.shutdown()
    PAIRED.pop(url, None)


def call_tool(base, name, args, scope="control"):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    headers = {"Authorization": f"Bearer {mcp.tokens()[scope]}", "Content-Type": "application/json"}
    req = urllib.request.Request(f"{base}/mcp", data=json.dumps(msg).encode(), headers=headers)
    return json.loads(urllib.request.urlopen(req).read())


def page(base, path, payload=None, paired=True, lang="en"):
    """(status, json) of a page request; a payload makes it a POST with the page's headers, and by default
    the paired browser's secret, as the real page sends."""
    data = json.dumps(payload).encode() if payload is not None else None
    pair_header = PAIRED.get(base, {}) if paired else {}
    headers = (
        {"Origin": base, "X-SYA": "1", "Content-Type": "application/json", **pair_header} if data else {}
    )
    headers["X-SYA-Lang"] = lang
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers)) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_request_change_starts_a_builder_in_its_own_branch_and_worktree(base, checkout, tmp_path):
    out = call_tool(base, "request_change", {"title": "Модуль hello-clock", "description": "Часы в шапке."})
    result = json.loads(out["result"]["content"][0]["text"])

    b = store.get(result["id"])
    worktree = checkout / ".claude" / "worktrees" / f"build-{b['id']}"
    assert result["id"].startswith("modul-hello-clock-") and result["branch"] == f"build/{b['id']}"
    assert b["status"] == "building" and b["session_id"] == SID and b["job_id"] == JOB
    assert b["base_sha"] == git(checkout, "rev-parse", "HEAD") and b["base_branch"] == "main"
    assert git(worktree, "branch", "--show-current") == b["branch"]
    assert git(checkout, "status", "--porcelain") == ""  # the worktree is ignored in the checkout

    start = claude_calls(tmp_path)[0]
    assert start["cwd"] == str(worktree)
    settings = tmp_path / "config" / "builder" / f"build-{b['id']}.json"  # per build
    name = "Builder: Модуль hello-clock"
    assert start["argv"][:5] == [
        "--bg",
        "-n",
        name,
        "--permission-mode=acceptEdits",
        f"--settings={settings}",
    ]
    prompt = start["argv"][6]
    assert start["argv"][5] == "--" and "Часы в шапке." in prompt and "BUILD READY" in prompt
    assert prompt.startswith("A task for the see-your-agents dashboard: “Модуль hello-clock”.")
    written = json.loads(settings.read_text())
    rules = written["permissions"]
    assert "Bash(git push:*)" in rules["deny"] and "Bash(uv run pytest:*)" in rules["allow"]
    assert written["sandbox"]["enabled"] is True and written["sandbox"]["allowUnsandboxedCommands"] is False
    # its venv was made before it started: in its sandbox uv is offline
    assert uv_syncs(tmp_path) == [{"argv": ["sync", "--locked"], "cwd": str(worktree)}]


def test_the_builder_runs_sandboxed_writing_only_its_worktree_and_temp_folder(checkout, tmp_path):
    git(checkout, "branch", "feature-x")
    other = builder.create("Другая сборка", "что-то ещё")
    b = builder.create("Часы", "в шапке")
    worktree, root = Path(b["worktree"]), agent.tmp_root(Path(b["worktree"]))
    common = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"))

    written = json.loads((tmp_path / "config" / "builder" / f"{worktree.name}.json").read_text())
    box = written["sandbox"]
    assert box["enabled"] and box["failIfUnavailable"] and box["allowUnsandboxedCommands"] is False
    assert box["autoAllowBashIfSandboxed"] is False  # ALLOW and DENY still decide what it may run unasked
    assert box["filesystem"]["allowWrite"] == [str(root)] and root.is_dir()
    assert oct(root.stat().st_mode & 0o777) == "0o700"
    assert box["filesystem"]["denyRead"] == [str(tmp_path / "config")]  # the MCP control token lives there
    net = box["network"]
    assert net["allowedDomains"] == [] and net["deniedDomains"] == ["*"] and net["strictAllowlist"] is True
    assert net["allowLocalBinding"] is True and net["allowUnixSockets"] == [str(root)]

    denied = box["filesystem"]["denyWrite"]
    for path in (
        "HEAD",
        "packed-refs",
        "refs/heads/main",
        "refs/heads/feature-x",
        f"refs/heads/{other['branch']}",
    ):
        assert str(common / path) in denied and str(common / f"{path}.lock") in denied
    assert str(common / "worktrees" / f"build-{other['id']}") in denied
    assert str(common / f"refs/heads/{b['branch']}") not in denied  # its own branch takes its commits
    assert not any(p.startswith(str(common / "worktrees" / worktree.name)) for p in denied)

    env = written["env"]
    assert env["CLAUDE_CODE_TMPDIR"] == str(root) and env["UV_OFFLINE"] == "1" and env["UV_NO_CACHE"] == "1"
    assert env["SYA_CONFIG_DIR"].startswith(str(root)) and env["SYA_CACHE_DIR"].startswith(str(root))


def test_the_builders_temp_folder_is_short_enough_for_claude_code_to_keep_it(monkeypatch):
    monkeypatch.setattr(agent, "TMP_PARENT", agent.DEFAULT_TMP_PARENT)  # the real, resolved parent
    worktree = Path(
        "/Users/someone/a/rather/long/path/to/a/checkout/.claude/worktrees/build-modul-chasy-1a2b"
    )
    root = agent.tmp_root(worktree)
    assert str(root.parent) == os.path.realpath("/tmp")  # /private/tmp on macOS: what Seatbelt matches
    # longer than this, Claude Code gives sandboxed commands the /tmp/claude-<uid> all sessions share; the
    # cap holds for uids of up to five digits
    assert len(f"{root}/claude-99999".encode()) <= 44
    assert len(f"{root}/claude-{os.getuid()}".encode()) <= 44


def test_a_temp_root_too_long_for_claude_code_stops_the_builder_from_starting(checkout, monkeypatch):
    monkeypatch.setattr(agent, "CLAUDE_TMP_MAX", 44)  # the real cap; the fixture's tmp_path parent is long
    with pytest.raises(builder.BuildError, match="longer than 44 bytes"):
        builder.create("Часы", "в шапке")
    assert store.load() == [] and git(checkout, "branch", "--list", "build/*") == ""


def test_a_temp_folder_that_is_a_link_stops_the_builder_from_starting(checkout, tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "slugify", lambda title: "chasy-0001")
    worktree = checkout / ".claude" / "worktrees" / "build-chasy-0001"
    agent.tmp_root(worktree).symlink_to(tmp_path)

    with pytest.raises(builder.BuildError, match="someone else’s folder or a link"):
        builder.create("Часы", "в шапке")
    assert store.load() == [] and claude_calls(tmp_path) == [] and not worktree.exists()


def test_a_worktree_whose_venv_does_not_sync_gets_no_builder(checkout, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_UV_SYNC_EXIT", "1")
    with pytest.raises(builder.BuildError, match="The builder didn’t start: uv sync failed in the worktree"):
        builder.create("Часы", "в шапке")
    assert store.load() == [] and claude_calls(tmp_path) == []
    assert git(checkout, "branch", "--list", "build/*") == ""


def _listen(port: int) -> socket.socket:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(5)

    def accept_until_closed():
        with contextlib.suppress(OSError):
            while True:
                s.accept()[0].close()

    threading.Thread(target=accept_until_closed, daemon=True).start()
    return s


@needs_seatbelt
def test_the_checks_and_preview_run_under_a_sandbox_profile_confined_to_the_worktree(checkout, tmp_path):
    """The profile that wraps the dashboard's own checks and the preview, enforced by the real sandbox-exec:
    a write lands only in the worktree or the build's temp root, config_dir() cannot be read, there is no
    network but loopback, and the dashboard's own port is closed while other loopback ports stay open."""
    b = builder.create("Часы", "в шапке")  # a real worktree and its temp root
    worktree, root = Path(b["worktree"]), agent.tmp_root(Path(b["worktree"]))
    (config_dir()).mkdir(parents=True, exist_ok=True)
    (config_dir() / "mcp-tokens.json").write_text("{}")
    profile = agent.profile(worktree, 7351)  # 7351 stands for the dashboard; 7352 is another local service
    servers = [_listen(7351), _listen(7352)]

    def under(code: str) -> str:
        out = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c", code],
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        return (out.stdout + out.stderr).strip()

    try:
        assert "OK" in under(f"open({str(worktree / 'a.txt')!r},'w').write('x'); print('OK')")
        assert "OK" in under(f"open({str(root / 'b.txt')!r},'w').write('x'); print('OK')")
        assert "OK" not in under(f"open({str(tmp_path / 'esc.txt')!r},'w').write('x'); print('OK')")
        assert "OK" not in under(f"import os; os.listdir({str(config_dir())!r}); print('OK')")
        assert "OK" not in under(
            "import socket; socket.create_connection(('127.0.0.1',7351),timeout=3); print('OK')"
        )
        assert "OK" not in under(
            "import socket; socket.create_connection(('localhost',7351),timeout=3); print('OK')"
        )
        assert "OK" in under(
            "import socket; socket.create_connection(('127.0.0.1',7352),timeout=3); print('OK')"
        )
        assert "OK" not in under(
            "import urllib.request as u; u.urlopen('https://example.com',timeout=5); print('OK')"
        )
    finally:
        for s in servers:
            s.close()


def test_the_profile_names_the_worktree_the_temp_root_config_dir_and_the_dashboard_port(checkout):
    b = builder.create("Часы", "в шапке")
    worktree = Path(b["worktree"])
    text = agent.profile(worktree, 8765)
    assert f'(allow file-write* (subpath "{worktree}"))' in text
    assert f'(allow file-write* (subpath "{agent.tmp_root(worktree)}"))' in text
    assert "(deny file-write*)" in text
    assert f'(deny file-read* (subpath "{config_dir()}"))' in text
    assert "(deny network*)" in text and '(allow network-outbound (remote ip "localhost:*"))' in text
    assert '(deny network-outbound (remote ip "localhost:8765"))' in text


@needs_seatbelt
def test_the_profile_lets_the_real_tmp_root_be_written_through_tmp_and_nothing_next_to_it(
    tmp_path, monkeypatch
):
    """With the real default TMP_PARENT (no monkeypatch to a canonical pytest path): /tmp is a link to
    /private/tmp and Seatbelt matches canonical paths, so the rule must name /private/tmp. A write into the
    temp root succeeds whether the process spells it /tmp or /private/tmp; a write next to it fails."""
    monkeypatch.setattr(agent, "TMP_PARENT", agent.DEFAULT_TMP_PARENT)
    monkeypatch.setattr(agent, "SANDBOX_EXEC", Path("/usr/bin/sandbox-exec"))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    root = agent.tmp_root(worktree)
    try:
        argv = agent.confine([], worktree, 8765)  # makes the root; argv = sandbox-exec -p <profile>
        assert root.is_dir() and str(root).startswith(os.path.realpath("/tmp") + "/")
        spelled_tmp = f"/tmp/{root.name}"  # the spelling a process gets from TMPDIR=/tmp/... or a script

        def write(path: str) -> int:
            return subprocess.run(
                [*argv, "/bin/sh", "-c", f"echo x > {path}"], capture_output=True
            ).returncode

        assert write(f"{root}/a.txt") == 0
        assert write(f"{spelled_tmp}/b.txt") == 0
        assert (root / "a.txt").exists() and (root / "b.txt").exists()
        assert write(f"{root}-next.txt") != 0 and not Path(f"{root}-next.txt").exists()
        assert write(f"{agent.DEFAULT_TMP_PARENT}/sya-next-{root.name}.txt") != 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("port", [None, 0, "8765", 70000, True])
def test_confine_refuses_without_a_dashboard_port_to_close(tmp_path, port):
    with pytest.raises(agent.SandboxError, match="port for the sandbox is unknown"):
        agent.confine(["true"], tmp_path, port)


def test_the_checks_fail_closed_without_a_recorded_dashboard_port(app):
    b = new_build(commits=1)
    store.move(b["id"], store.CHECKING)  # an old record: no checks_port
    builder.run_checks(b["id"])
    done = store.get(b["id"])
    assert done["status"] == "failed" and done["error"] == store.problem(
        "builder.sandbox.no_port", port="None"
    )


def test_checks_orphaned_by_a_restart_get_this_servers_port(app, monkeypatch):
    b = new_build(commits=1)
    store.move(b["id"], store.CHECKING, checks_pid=999_999_999)  # dead, and no checks_port
    monkeypatch.setattr(builder, "run_checks", lambda build_id: None)  # just the bookkeeping
    builder.tick(app, force=True)
    assert store.get(b["id"])["checks_port"] == app.port


def test_the_checks_fail_closed_when_the_sandbox_is_missing(app, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "SANDBOX_EXEC", tmp_path / "no-sandbox-exec")  # not on disk
    b = new_build(commits=1)  # a ready build goes through run_checks
    store.move(b["id"], store.CHECKING, checks_port=app.port)
    builder.run_checks(b["id"])
    done = store.get(b["id"])
    assert done["status"] == "failed" and done["error"]["key"] == "builder.sandbox.missing"


def test_the_preview_fails_closed_when_the_sandbox_is_missing(base, app, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "SANDBOX_EXEC", tmp_path / "no-sandbox-exec")
    b = new_build(status=store.READY)
    status, body = page(base, f"/api/builder/{b['id']}/preview", {})
    assert status == 502 and "not running the build’s code without a sandbox" in body["error"]
    status, body = page(base, f"/api/builder/{b['id']}/preview", {}, lang="ru")
    assert status == 502 and "код сборки без песочницы не запускаю" in body["error"]


def test_request_change_is_a_confirmed_control_tool_and_the_only_one():
    # accept, rollback and reject must stay page buttons: no MCP tool may land code
    assert [t.name for t in FEATURE.tools] == ["request_change"]
    assert FEATURE.tools[0].scope == "control" and FEATURE.tools[0].confirm


def test_request_change_refuses_outside_a_git_checkout(base, tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(builder, "PACKAGE", plain)
    out = call_tool(base, "request_change", {"title": "x", "description": "y"})["result"]
    assert out["isError"] and "isn’t running from a git checkout" in out["content"][0]["text"]
    assert claude_calls(tmp_path) == []


def test_a_builder_that_does_not_start_leaves_nothing_behind(checkout, monkeypatch):
    def broken(*args):
        raise LaunchError("app.provider.said", text="Workspace not trusted")

    monkeypatch.setattr(agent, "start", broken)
    with pytest.raises(builder.BuildError, match="Workspace not trusted"):
        builder.create("часы", "в шапке")
    assert store.load() == [] and git(checkout, "branch", "--list", "build/*") == ""


# ---- the status machine ------------------------------------------------------------------------


def test_moves_are_compare_and_set():
    store.add({"id": "b", "status": store.CHECKING})
    assert store.move("b", store.REJECTED)["status"] == "rejected"
    assert store.move("b", store.READY) is None  # checks finishing late cannot revive it
    assert store.get("b")["status"] == "rejected"


def test_a_working_or_waiting_builder_is_left_alone(app):
    b = new_build(commits=1)
    app.fake_live = {SID: live("busy")}
    builder.tick(app, force=True)
    assert store.get(b["id"])["status"] == "building" and store.get(b["id"])["busy_seen"]

    app.fake_live = {SID: live("waiting", waiting_for="permission prompt")}
    builder.tick(app, force=True)
    shown = routes.board_fields(app)["builder"]["builds"]
    assert shown[0]["status"] == "building" and shown[0]["waiting"] is True


def test_a_fresh_idle_builder_gets_a_grace_period(app):
    b = new_build(commits=1)
    app.fake_live = {SID: live("idle")}
    builder.tick(app, force=True)
    assert store.get(b["id"])["status"] == "building"


def test_finished_with_commits_runs_the_checks_and_becomes_ready(app):
    b = new_build(commits=2)
    app.fake_live = {SID: live("busy")}
    builder.tick(app, force=True)
    app.fake_live = {SID: live("idle")}
    builder.tick(app, force=True)

    done = store.get(b["id"])
    assert done["status"] == "ready" and done["checks"] == {"ok": True, "tail": "checks passed\n"}


def test_failing_checks_keep_the_tail(app, monkeypatch):
    monkeypatch.setattr(builder, "CHECKS", "echo 'E501 too long'; exit 1")
    b = new_build(commits=1)
    store.update(b["id"], busy_seen=True)
    builder.tick(app, force=True)  # not live at all: finished

    done = store.get(b["id"])
    assert (
        done["status"] == "failed"
        and "E501" in done["checks"]["tail"]
        and done["error"] == store.problem("builder.error.checks_failed")
    )


def last_answer_is(tmp_path, text: str) -> None:
    answer = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    folder = tmp_path / "claude" / "projects" / "-w"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{SID}.jsonl").write_text(json.dumps({"type": "user"}) + "\n" + json.dumps(answer) + "\n")


def test_finished_without_a_commit_fails_and_says_what_it_asked(app, tmp_path):
    b = new_build(commits=0)
    store.update(b["id"], busy_seen=True)
    app.fake_live = {SID: live("idle")}
    builder.tick(app, force=True)
    assert store.get(b["id"])["error"] == store.problem("builder.error.no_commit")

    last_answer_is(tmp_path, "Где разместить часы:\nслева или справа?")
    store.move(b["id"], store.BUILDING, busy_seen=True)
    builder.tick(app, force=True)
    said = "Где разместить часы: слева или справа?"
    assert store.get(b["id"])["error"] == store.problem("builder.error.no_commit_said", said=said)
    (shown,) = routes.board_fields(app)["builder"]["builds"]
    assert shown["error"] == f"the builder ended its turn without a commit: “{said}”"


def test_the_builders_own_verdict_wins(app, tmp_path):
    b = new_build(commits=1)
    store.update(b["id"], busy_seen=True)
    last_answer_is(tmp_path, "Сделал.\n\n**BUILD FAILED: нет доступа к сети**")
    builder.tick(app, force=True)

    done = store.get(b["id"])
    assert done["status"] == "failed" and done["error"] == "нет доступа к сети"


def test_uncommitted_work_fails_the_checks(app):
    b = new_build(commits=1)
    (Path(b["worktree"]) / "forgotten.py").write_text("x = 1\n")
    store.update(b["id"], busy_seen=True)
    builder.tick(app, force=True)

    done = store.get(b["id"])
    assert (
        done["status"] == "failed"
        and done["error"] == store.problem("builder.error.uncommitted")
        and "forgotten.py" in done["checks"]["tail"]
    )


def test_checks_orphaned_by_a_restart_run_again(app):
    b = new_build(commits=1)
    store.move(b["id"], store.CHECKING, checks_pid=2**22 + 12345)  # no such process
    builder.tick(app, force=True)
    assert store.get(b["id"])["status"] == "ready"


# ---- the user's buttons -----------------------------------------------------------------------


def test_accept_needs_a_clean_checkout_on_the_base_branch(app, checkout):
    b = new_build(status=store.READY)
    (checkout / "app.txt").write_text("edited\n")
    with pytest.raises(builder.BuildError, match="uncommitted changes: app.txt"):
        builder.accept(app, b["id"])

    git(checkout, "checkout", "-q", "--", "app.txt")
    git(checkout, "switch", "-q", "-c", "elsewhere")
    with pytest.raises(builder.BuildError, match="on the branch “elsewhere”"):
        builder.accept(app, b["id"])
    assert store.get(b["id"])["status"] == "ready"


def test_accept_merges_records_and_asks_for_a_restart(app, checkout, tmp_path, monkeypatch):
    b = new_build(status=store.READY)
    (checkout / ".claude" / "skills").mkdir(parents=True)
    (checkout / ".claude" / "skills" / "local.md").write_text(
        "x"
    )  # untracked under .claude/: not the user's work
    prev = git(checkout, "rev-parse", "HEAD")
    monkeypatch.setattr(app, "request_restart", lambda: True)

    out = builder.accept(app, b["id"])

    done = out["build"]
    assert out["restart"] is True and done["status"] == "accepted"
    assert done["prev_sha"] == prev and done["merge_sha"] == git(checkout, "rev-parse", "HEAD")
    assert git(checkout, "rev-parse", "HEAD^2") == b["checked_sha"] == git(checkout, "rev-parse", b["branch"])
    assert (checkout / "clock0.txt").exists()
    assert not Path(b["worktree"]).exists() and git(checkout, "branch", "--list", b["branch"])
    assert uv_syncs(tmp_path, checkout) == []  # no dependency changed


def test_accept_refuses_a_branch_that_moved_after_the_checks_and_checks_it_again(app, checkout, monkeypatch):
    monkeypatch.setattr(builder, "CHECKS", "test ! -e broken.py")
    b = new_build(status=store.READY)
    base = git(checkout, "rev-parse", "HEAD")
    # after the card said ready the builder still has its chat, and commits something nobody checked
    commit(Path(b["worktree"]), "broken.py", "def (:\n", "after READY")

    with pytest.raises(builder.BuildError, match="new commits since the checks: checking it again"):
        builder.accept(app, b["id"])

    again = store.get(b["id"])
    assert again["status"] == "failed" and again["checked_sha"] == git(checkout, "rev-parse", b["branch"])
    assert git(checkout, "rev-parse", "HEAD") == base and not (checkout / "broken.py").exists()


def test_accept_refuses_paths_the_merge_would_overwrite_silently(app, checkout):
    commit(checkout, ".gitignore", ".claude/*\n!.claude/skills/\n*.local\n")
    mine = checkout / ".claude" / "settings.local.json"  # the user's, ignored
    mine.parent.mkdir()
    mine.write_text("USER\n")
    b = new_build(commits=0)
    worktree = Path(b["worktree"])
    (worktree / ".claude" / "skills").mkdir(parents=True)
    for name in (".claude/settings.local.json", "notes.local", ".claude/skills/clock.md", "clock.txt"):
        (worktree / name).write_text("from the build\n")
    git(worktree, "add", "-f", ".")  # the builder may run `git add` without asking
    git(worktree, "commit", "-qm", "with hidden files")
    b = checked(b)

    with pytest.raises(
        builder.BuildError, match=r"silently: \.claude/settings\.local\.json, notes\.local\. "
    ):
        builder.accept(app, b["id"])
    assert mine.read_text() == "USER\n" and store.get(b["id"])["status"] == "ready"


def test_accept_installs_changed_dependencies_before_the_restart(app, checkout, tmp_path, monkeypatch):
    b = new_build(files={"uv.lock": "tick = 1.0\n"}, status=store.READY)
    synced = []  # by the time the restart is asked for
    monkeypatch.setattr(app, "request_restart", lambda: synced.append(uv_syncs(tmp_path, checkout)) or True)

    out = builder.accept(app, b["id"])

    assert out["build"]["status"] == "accepted" and out["restart"] is True
    assert synced == [[{"argv": ["sync", "--locked"], "cwd": str(checkout)}]]


def test_dependencies_that_do_not_install_revert_the_merge(app, checkout, tmp_path, monkeypatch):
    b = new_build(files={"pyproject.toml": "[project]\nname = 'x'\n"}, status=store.READY)
    monkeypatch.setenv("FAKE_UV_SYNC_EXIT", "1")  # the worktree's own sync at the start did work
    prev = git(checkout, "rev-parse", "HEAD")
    monkeypatch.setattr(app, "request_restart", lambda: pytest.fail("restarted on a broken venv"))

    with pytest.raises(builder.BuildError, match=r"didn’t install \(uv sync\): the merge was rolled back"):
        builder.accept(app, b["id"])

    after = store.get(b["id"])
    assert after["status"] == "rolled_back" and "no wheel for tick" in after["checks"]["tail"]
    assert after["error"]["key"].startswith("builder.error.sync_rolled_back")
    assert git(checkout, "rev-parse", "HEAD^") == after["merge_sha"]
    assert git(checkout, "diff", prev, "HEAD") == ""
    assert len(uv_syncs(tmp_path, checkout)) == 2  # the build's lock, then the old one back


def test_rollback_reverts_the_merge_only_when_head_has_it(app, checkout):
    b = new_build(status=store.READY)
    merge = builder.accept(app, b["id"])["build"]["merge_sha"]

    (checkout / "app.txt").write_text("edited\n")
    with pytest.raises(builder.BuildError, match="uncommitted"):
        builder.rollback(app, b["id"])
    git(checkout, "checkout", "-q", "--", "app.txt")

    out = builder.rollback(app, b["id"])
    assert out["build"]["status"] == "rolled_back" and out["restart"] is False  # FakeApp: no watchdog
    assert git(checkout, "rev-parse", "HEAD^") == merge and not (checkout / "clock0.txt").exists()

    other = new_build(status=store.READY)
    store.move(other["id"], store.ACCEPTED, merge_sha="0" * 40)
    with pytest.raises(builder.BuildError, match="isn’t in the checkout’s current branch"):
        builder.rollback(app, other["id"])


def test_rollback_happens_only_on_the_base_branch(app, checkout):
    b = new_build(status=store.READY)
    merge = builder.accept(app, b["id"])["build"]["merge_sha"]

    git(checkout, "switch", "-q", "-c", "feature-x")  # other work, started from main after the accept
    with pytest.raises(
        builder.BuildError, match="on the branch “feature-x”, but the build started from “main”"
    ):
        builder.rollback(app, b["id"])
    git(checkout, "checkout", "-q", "--detach", "main")
    with pytest.raises(builder.BuildError, match="detached HEAD"):
        builder.rollback(app, b["id"])

    assert store.get(b["id"])["status"] == "accepted"  # the button stays
    assert git(checkout, "rev-parse", "main") == git(checkout, "rev-parse", "feature-x") == merge


def test_reject_removes_the_worktree_and_branch_it_made(app, checkout, tmp_path):
    b = new_build(commits=1)
    app.fake_live = {SID: live("idle", job_id=JOB, cwd=b["worktree"])}
    settings = tmp_path / "config" / "builder" / f"build-{b['id']}.json"
    root = agent.tmp_root(Path(b["worktree"]))
    assert settings.exists() and root.is_dir()
    out = builder.reject(app, b["id"])

    assert out["build"]["status"] == "rejected"
    assert not Path(b["worktree"]).exists() and git(checkout, "branch", "--list", b["branch"]) == ""
    assert ["stop", JOB] in [c["argv"] for c in claude_calls(tmp_path)]
    assert not settings.exists() and not root.exists()  # its sandbox settings and temp folder went too

    foreign = new_build(commits=0)
    store.update(foreign["id"], branch="main")
    with pytest.raises(builder.BuildError, match="wasn’t made by the builder"):
        builder.reject(app, foreign["id"])
    assert git(checkout, "branch", "--list", "main")


def test_comment_reaches_a_live_builder_or_resumes_a_stopped_one(app, tmp_path, monkeypatch):
    b = new_build(status=store.READY)
    told = []
    monkeypatch.setattr(agent, "tell", lambda to, text: told.append((to, text)) or Delivery(True, "m1"))
    app.fake_live = {SID: live("idle")}
    out = builder.comment(app, b["id"], "Сделай часы крупнее")
    assert told == [("Builder: часы", "Сделай часы крупнее")] and out["route"] == "message"
    assert store.get(b["id"])["status"] == "building" and store.get(b["id"])["busy_seen"] is False

    app.fake_live = {}
    assert builder.comment(app, b["id"], "И секунды")["route"] == "resume"
    resumed = [c for c in claude_calls(tmp_path) if c["argv"][0] == "--bg"][-1]
    assert resumed["argv"][:2] == ["--bg", f"--resume={SID}"] and resumed["argv"][-1] == "И секунды"
    assert resumed["cwd"] == b["worktree"]
    after = store.get(b["id"])
    assert (after["job_id"], after["session_id"]) == (JOB2, SID2)  # the copy is what runs now


def commented(app, monkeypatch, b: dict, text: str = "добавь секунды") -> None:
    """«Доработать» ("Rework") reached the live builder, which then took its turn."""
    monkeypatch.setattr(agent, "tell", lambda to, text: Delivery(True, "m1"))
    app.fake_live = {SID: live("idle")}
    builder.comment(app, b["id"], text)
    app.fake_live = {SID: live("busy")}
    builder.tick(app, force=True)
    app.fake_live = {SID: live("idle")}


def test_an_answer_without_a_commit_after_dorabotat_is_a_question_not_ready(app, tmp_path, monkeypatch):
    b = new_build(status=store.READY)
    commented(app, monkeypatch, b)
    last_answer_is(tmp_path, "Секунды   показывать\nв 24-часовом формате?")
    builder.tick(app, force=True)

    after = store.get(b["id"])
    assert after["status"] == "building" and after["question"] == "Секунды показывать в 24-часовом формате?"
    assert routes.board_fields(app)["builder"]["builds"][0]["question"] == after["question"]

    app.fake_live = {SID: live("busy")}  # answered in its chat: working again
    builder.tick(app, force=True)
    assert store.get(b["id"])["question"] == ""
    commit(Path(b["worktree"]), "seconds.txt", "ss\n")
    app.fake_live = {SID: live("idle")}
    builder.tick(app, force=True)
    assert store.get(b["id"])["status"] == "ready"


def test_ready_said_after_dorabotat_without_a_commit_is_checked_again(app, tmp_path, monkeypatch):
    b = new_build(status=store.READY)
    commented(app, monkeypatch, b, "часы уже 24-часовые?")
    last_answer_is(tmp_path, "Да, уже так.\n\nBUILD READY")
    builder.tick(app, force=True)
    assert store.get(b["id"])["status"] == "ready"


def test_a_builder_is_followed_by_its_job_when_its_session_id_changed(app):
    b = new_build(commits=1)
    store.update(b["id"], job_id=JOB2)
    app.fake_live = {
        SID2: LiveSession(provider="claude", session_id=SID2, kind="background", status="busy", job_id=JOB2)
    }
    builder.tick(app, force=True)

    after = store.get(b["id"])
    assert after["session_id"] == SID2 and after["busy_seen"] and after["status"] == "building"


def test_a_comment_that_was_not_delivered_changes_nothing(app, monkeypatch):
    b = new_build(status=store.READY)
    distorted = Delivery(False, error="app.provider.claude.courier.distorted")
    monkeypatch.setattr(agent, "tell", lambda to, text: distorted)
    app.fake_live = {SID: live("idle")}
    with pytest.raises(builder.BuildError, match="Not delivered: The courier changed the text"):
        builder.comment(app, b["id"], "крупнее")
    with i18n.answering("ru"), pytest.raises(builder.BuildError) as refused:
        builder.comment(app, b["id"], "крупнее")
    assert refused.value.text("ru") == "не доставлено: курьер исказил текст"
    assert store.get(b["id"])["status"] == "ready"


def test_the_live_builder_hears_the_dashboard_in_english(monkeypatch):
    sent = []
    monkeypatch.setattr(agent.courier, "deliver", lambda *a: sent.append(a) or Delivery(True, "m1"))
    agent.tell("Builder: часы", "крупнее")
    assert sent == [("Builder: часы", "крупнее", "Rework from the user", "Dashboard")]


# ---- page routes and the banner ---------------------------------------------------------------


def test_actions_are_page_posts_with_the_hosts_guard(base):
    b = new_build(status=store.READY)
    req = urllib.request.Request(f"{base}/api/builder/{b['id']}/reject", data=b"{}", headers={"Origin": base})
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(req)
    assert refused.value.code == 403 and store.get(b["id"])["status"] == "ready"

    # Origin and X-SYA but no paired-browser secret: the host refuses it, and the build is untouched.
    status, body = page(base, f"/api/builder/{b['id']}/reject", {}, paired=False)
    assert status == 403 and body.get("unpaired") and store.get(b["id"])["status"] == "ready"

    assert page(base, "/api/builder/nope-0000/accept", {}) == (404, {"ok": False, "error": "Build not found"})
    assert page(base, "/api/builder/nope-0000/diff", lang="ru") == (
        404,
        {"ok": False, "error": "сборка не найдена"},
    )
    status, body = page(base, f"/api/builder/{b['id']}/comment", {"text": "  "})
    assert status == 400 and not body["ok"]
    status, body = page(base, f"/api/builder/{b['id']}/reject", {})
    assert status == 200 and body["build"]["status"] == "rejected"


def test_the_diff_is_capped(base, checkout):
    b = new_build(commits=0)
    commit(Path(b["worktree"]), "big.txt", "".join(f"line {n}\n" for n in range(5000)))
    small = repo.diff(checkout, b["base_sha"], b["branch"], cap=1000)
    assert small["truncated"] and len(small["patch"]) == 1000 and "big.txt" in small["stat"]

    status, body = page(base, f"/api/builder/{b['id']}/diff")
    assert status == 200 and body["ok"] and not body["truncated"] and "+line 4999" in body["patch"]


def test_the_banner_lists_unresolved_and_recent_builds_of_this_checkout(app, checkout):
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    store.add({"id": "x-1", "status": "rejected", "checkout": str(checkout), "updated_at": store.now()})
    store.add({"id": "x-2", "status": "accepted", "checkout": str(checkout), "updated_at": old})
    store.add({"id": "x-3", "status": "accepted", "checkout": str(checkout), "updated_at": store.now()})
    store.add({"id": "x-4", "status": "ready", "checkout": "/elsewhere", "updated_at": store.now()})
    store.add({"id": "x-5", "status": "failed", "checkout": str(checkout), "updated_at": old})

    shown = routes.board_fields(app)["builder"]
    assert [b["id"] for b in shown["builds"]] == ["x-3", "x-5"] and shown["preview"] is None


def test_a_kept_error_is_worded_for_whoever_reads_the_board_and_an_old_one_as_it_is(app, checkout):
    """The checks and the watchdog keep a key; a record from before keys (or the builder's words) is text."""
    now, top = store.now(), str(checkout)
    store.add({"id": "x-1", "status": "failed", "checkout": top, "updated_at": now,
               "error": store.problem("builder.error.not_tip", branch="build/x-1")})  # fmt: skip
    store.add({"id": "x-2", "status": "rolled_back", "checkout": top, "updated_at": now,
               "error": watchdog.why_reverted(1)[1]})  # fmt: skip
    store.add(
        {"id": "x-3", "status": "failed", "checkout": top, "updated_at": now, "error": "проверки не прошли"}
    )

    def errors() -> list[str]:
        return [b["error"] for b in routes.board_fields(app)["builder"]["builds"]]

    assert errors() == [
        "the build’s worktree isn’t on the last commit of build/x-1: nothing to check",
        "the new version crashed on start (code 1)",
        "проверки не прошли",
    ]
    with i18n.answering("ru"):
        assert errors() == [
            "в worktree сборки не последний коммит ветки build/x-1: проверять нечего",
            "новая версия упала при запуске (код 1)",
            "проверки не прошли",
        ]


def test_preview_runs_from_the_worktree_with_a_copy_of_the_config(base, app, tmp_path):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "looks.json").write_text("{}")
    (tmp_path / "config" / "mcp-tokens.json").write_text("{}")
    (tmp_path / "config" / "supervisor").mkdir()
    b = new_build(status=store.READY)

    status, body = page(base, f"/api/builder/{b['id']}/preview", {})
    assert status == 200 and body["preview"]["url"].startswith("http://127.0.0.1:87")
    seen = json.loads((tmp_path / "uv.log").read_text())
    assert seen["cwd"] == b["worktree"] and seen["argv"][:4] == ["run", "sya", "serve", "--no-open"]
    want_config = agent.tmp_root(Path(b["worktree"])) / "preview" / "config"
    assert seen["config"] == str(want_config)  # the preview's config is under the build's temp root
    assert "looks.json" in seen["files"]
    assert not {"builds.json", "mcp-tokens.json", "supervisor"} & set(seen["files"])
    assert routes.board_fields(app)["builder"]["preview"]["id"] == b["id"]

    assert page(base, "/api/builder/stop-preview", {}) == (200, {"ok": True, "stopped": True})
    assert preview.current() is None


# ---- nothing outlives the dashboard ------------------------------------------------------------


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def eventually(check, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def answers(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=1) as r:
            return json.loads(r.read()).get("ok") is True
    except OSError:
        return False


@needs_seatbelt
def test_sigterm_to_the_dashboard_stops_its_preview(tmp_path):
    """A `kill` or a closed terminal (SIGHUP) must not leave the preview holding its port with no owner."""
    try:
        top = repo.toplevel(Path(builder.__file__).parent)  # the real checkout: the dashboard runs from it
    except repo.GitError:
        pytest.skip("the package is not in a git checkout")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store.add({"id": "clock-1a2b", "status": "ready", "checkout": str(top), "worktree": str(worktree)})
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cmd = [sys.executable, "-m", "see_your_agents.app.cli", "--db", str(tmp_path / "index.duckdb"), "serve"]
    log = (tmp_path / "dashboard.log").open("wb")
    # The real preview runs under sandbox-exec (no passthrough in the child dashboard), which allows writes
    # only in the worktree and the build's temp root; the fake uv logs inside the worktree. A preview is a
    # control action, so the child dashboard wants the paired browser: pair through its own bootstrap, with
    # an opener that writes the pairing URL to a file instead of opening a browser.
    uv_log = worktree / "uv.log"
    shim = tmp_path / "opener.py"
    shim.write_text("import sys; open(sys.argv[1], 'w').write(sys.stdin.read())")
    opened = tmp_path / "pair-url.txt"
    env = {
        **os.environ,
        "FAKE_UV_LOG": str(uv_log),
        "SYA_PAIR_OPENER": f"{sys.executable} {shim} {opened}",
    }
    dashboard = subprocess.Popen(
        [*cmd, "--child", "--no-open", "--port", str(port)], stdout=log, stderr=log, env=env
    )
    base, uv_pid = f"http://127.0.0.1:{port}", 0
    try:
        assert eventually(lambda: answers(base)), (tmp_path / "dashboard.log").read_text()
        assert page(base, "/api/pair/start", {})[0] == 200  # bootstrap: no pairing needed
        assert eventually(lambda: opened.exists())
        code = opened.read_text().split("#pair=")[1].strip()
        PAIRED[base] = {"X-SYA-Pair": page(base, "/api/pair", {"code": code})[1]["secret"]}
        assert page(base, "/api/builder/clock-1a2b/preview", {})[0] == 200
        uv_pid = json.loads(uv_log.read_text())["pid"]

        dashboard.send_signal(signal.SIGTERM)

        assert dashboard.wait(15) == 128 + signal.SIGTERM  # it exited, not killed by the signal
        assert eventually(lambda: not alive(uv_pid), 5)
    finally:
        PAIRED.pop(base, None)
        dashboard.kill()
        log.close()
        if uv_pid:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(uv_pid, signal.SIGKILL)
        # The child used the real TMP_PARENT (the monkeypatch is in-process), so its temp root is under
        # /private/tmp; remove it, since no reject/accept ran here.
        shutil.rmtree(agent.DEFAULT_TMP_PARENT / agent.tmp_root(worktree).name, ignore_errors=True)


# `uv run sya serve` for real is uv → the watchdog → the server, the server in a session of its own.
CHAIN_UV = """#!{python}
import json, os, subprocess, sys
if sys.argv[1:2] == ["sync"]:  # the build's venv, made before its builder starts
    sys.exit(0)
port, pids = sys.argv[sys.argv.index("--port") + 1], os.environ["CHAIN_PIDS"]
with open(pids + ".uv", "w") as f:
    f.write(str(os.getpid()))
server = [sys.executable, os.environ["CHAIN_SERVER"], pids, port]
runner = "import sys; from see_your_agents.app import watchdog; sys.exit(watchdog.supervise(%r, %s))"
sys.exit(subprocess.call([sys.executable, "-c", runner % (server, port)]))
"""
CHAIN_SERVER = """
import http.server, json, os, sys
with open(sys.argv[1], "w") as f:
    json.dump({"watchdog": os.getppid(), "server": os.getpid()}, f)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok": true}')
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[2])), H).serve_forever()
"""


def test_stopping_the_preview_ends_uv_its_watchdog_and_the_server(app, tmp_path, monkeypatch):
    chain = tmp_path / "chain"
    chain.mkdir()
    (chain / "uv").write_text(CHAIN_UV.format(python=sys.executable))
    (chain / "uv").chmod(0o755)
    (chain / "server.py").write_text(CHAIN_SERVER)
    monkeypatch.setenv("PATH", f"{chain}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CHAIN_SERVER", str(chain / "server.py"))
    monkeypatch.setenv("CHAIN_PIDS", str(chain / "pids.json"))
    b = new_build(status=store.READY)

    preview.start(b, app.port)
    uv = int((chain / "pids.json.uv").read_text())
    pids = [uv, *json.loads((chain / "pids.json").read_text()).values()]
    assert all(alive(p) for p in pids)
    preview.stop()

    assert eventually(lambda: not any(alive(p) for p in pids), 5)
