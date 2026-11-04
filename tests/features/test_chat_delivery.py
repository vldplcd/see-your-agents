import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import zipfile
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from xml.etree import ElementTree

import pytest
from fakes import needs_ps, utc_start_of

from see_your_agents import i18n
from see_your_agents.app.feature import Feature
from see_your_agents.app.server import App
from see_your_agents.core import done
from see_your_agents.core.providers.base import LiveSession
from see_your_agents.core.providers.claude import routes as claude_routes
from see_your_agents.core.sessions import build_board
from see_your_agents.features import chat
from see_your_agents.features.chat import bridge, delivery, wire
from see_your_agents.i18n import Problem

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def isolated():
    claude_routes._runs.clear()


@pytest.fixture
def short_cache(monkeypatch):
    """Unix socket paths are limited to ~100 bytes, which pytest's tmp_path can exceed. $TMPDIR, not /tmp:
    in the builder's sandbox only its own short TMPDIR is writable."""
    d = Path(tempfile.mkdtemp(prefix="sya"))
    monkeypatch.setenv("SYA_CACHE_DIR", str(d))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def live(status="idle", kind="interactive", pid=500, **extra):
    return LiveSession(provider="claude", session_id=SID, kind=kind, status=status, pid=pid, **extra)


def snap(parents=None, registry=None, shells=(), bridge_alive=False):
    return claude_routes.Snapshot(parents or {}, registry or {}, set(shells), bridge_alive)


# ---- done -------------------------------------------------------------------------------------


def record(last_activity):
    return {
        "session_id": SID,
        "provider": "claude",
        "title": "t",
        "cwd": "/nonexistent",
        "last_activity": last_activity,
    }


def card_of(board):
    return board["cards"][0]


def test_done_sends_a_live_session_to_the_archive_until_new_activity(tmp_path):
    active = datetime.now(UTC) - timedelta(hours=1)
    rec = record(active.isoformat())
    lv = {SID: live("idle")}
    assert card_of(build_board([rec], lv))["column"] == "your_move"

    done.mark(SID, True)
    marked = card_of(build_board([rec], lv))
    assert (marked["done"], marked["column"]) == (True, "archive")

    later = record((datetime.now(UTC) + timedelta(minutes=5)).isoformat())
    revived = card_of(build_board([later], lv))
    assert (revived["done"], revived["column"]) == (False, "your_move")


def test_done_file_roundtrip_and_unmark(tmp_path):
    done.mark("a", True, now=NOW)
    done.mark("b", True, now=NOW)
    done.mark("a", False)

    assert done.load() == {"b": "2026-10-30T12:00:00Z"}
    assert [p.name for p in (tmp_path / "config").iterdir()] == ["done.json"]  # no temp file left behind


def test_board_cards_carry_delivery_and_running_reply(tmp_path):
    card = card_of(build_board([record(NOW.isoformat())], {}, [delivery.card_fields]))

    assert set(card["delivery"]) == {"route", "can_send", "reason", "alt"}
    assert card["running_reply"] is False


def test_the_cards_reason_is_worded_in_the_requests_language(tmp_path):
    rec = record(NOW.isoformat())  # its folder is gone: nothing can continue it

    english = delivery.card_fields(rec, None)["delivery"]
    with i18n.answering("ru"):
        russian = delivery.card_fields(rec, None)["delivery"]

    assert (english["can_send"], english["reason"]) == (
        False,
        "No transcript or working folder: it can’t be continued",
    )
    assert russian["reason"] == "нет транскрипта или рабочей папки: продолжить нельзя"


# ---- stop guard -------------------------------------------------------------------------------


class FakeIndex:
    def __init__(self, records):
        self.records = records

    def record(self, sid):
        return self.records.get(sid)


class FakeApp:
    """What the chat asks of the host; wired into Claude's routes as an App would be (the chat's setup)."""

    def __init__(self, home, live_sessions=None, records=None):
        self._live = live_sessions or {}
        records = records if records is not None else {SID: {"cwd": str(home)}}
        self.index = FakeIndex({sid: {"provider": "claude", **r} for sid, r in records.items()})
        wire(self)

    def live(self):
        return self._live

    def resume_args(self, session_id):
        return []


def post(app, action, payload=None, headers=None, body=None, lang="en"):
    """The handler as the host runs it: a Problem it raises is {"error": ...} in the request's language."""
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    raw = body if body is not None else json.dumps(payload or {}).encode()
    try:
        status, _, text = delivery.handle_post(app, f"/api/chat/{SID}/{action}", raw, msg)
    except Problem as exc:
        return exc.status, {"error": exc.text(lang)}
    return status, json.loads(text)


@needs_ps
def test_stop_refuses_a_process_that_is_not_the_registered_claude(tmp_path, claude_home, sleeper):
    sessions = claude_home / "sessions"
    sessions.mkdir(parents=True)
    reg = {"pid": sleeper.pid, "sessionId": SID, "procStart": utc_start_of(sleeper.pid, shift_s=3600)}
    (sessions / f"{sleeper.pid}.json").write_text(json.dumps(reg))
    app = FakeApp(tmp_path, {SID: live(pid=sleeper.pid)})

    status, reply = post(app, "stop")

    assert status == 409 and reply["error"]
    assert sleeper.poll() is None  # still running
    assert done.load() == {}  # and not marked done


def test_stop_of_a_session_that_is_not_live_is_refused(tmp_path):
    assert post(FakeApp(tmp_path), "stop") == (409, {"error": "The session isn’t running"})
    assert post(FakeApp(tmp_path), "stop", lang="ru") == (409, {"error": "сессия не запущена"})


# ---- upload -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("..%2F..%2Fsecret.txt", "secret.txt"),
        ("C:\\Users\\me\\scan 1.png", "scan_1.png"),
        ("отчёт.txt", "_____.txt"),
        ("...", "file"),
        ("", "file"),
        (".bashrc", "bashrc"),
    ],
)
def test_sanitize_name(raw, expected):
    assert delivery.sanitize_name(raw) == expected


def test_sanitize_name_caps_length_and_keeps_the_extension():
    name = delivery.sanitize_name("a" * 300 + ".png")

    assert len(name) == 80
    assert name.endswith(".png")


def test_upload_saves_under_the_session_folder_and_never_escapes(tmp_path):
    app = FakeApp(tmp_path)

    status, reply = post(app, "upload", headers={"X-Filename": "..%2F..%2Fevil%20name.txt"}, body=b"data")

    saved = Path(reply["path"])
    assert status == 200 and reply["ok"]
    assert saved.read_bytes() == b"data"
    assert saved.parent == (tmp_path / "cache" / "uploads" / SID).resolve()
    assert reply["name"] == saved.name
    assert saved.name.endswith("-evil_name.txt")
    assert len(saved.name.split("-")[0]) == 8  # yyyymmdd
    assert oct(saved.stat().st_mode & 0o777) == "0o600"


def test_upload_with_the_same_name_in_the_same_second_does_not_overwrite(tmp_path):
    app = FakeApp(tmp_path)

    first = post(app, "upload", headers={"X-Filename": "a.txt"}, body=b"1")[1]
    second = post(app, "upload", headers={"X-Filename": "a.txt"}, body=b"2")[1]

    assert first["path"] != second["path"]
    assert Path(first["path"]).read_bytes() == b"1"


def test_upload_rejects_an_empty_body(tmp_path):
    assert post(FakeApp(tmp_path), "upload", headers={"X-Filename": "a.txt"}, body=b"")[0] == 400


# ---- send validation --------------------------------------------------------------------------


def test_send_rejects_bad_input(tmp_path):
    app = FakeApp(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("x")

    assert post(app, "send", {"text": "   "})[0] == 400
    assert post(app, "send", {"text": "x" * 20_001})[0] == 400
    assert post(app, "send", body=b"not json")[0] == 400
    status, reply = post(app, "send", {"text": "hi", "files": [str(outside)]})
    assert status == 400 and "secret.txt" in reply["error"]
    assert (
        post(app, "send", {"text": "hi", "files": [str(tmp_path / "cache" / "uploads" / ".." / "x")]})[0]
        == 400
    )


def test_send_to_an_unknown_session_is_404(tmp_path):
    app = FakeApp(tmp_path, records={})

    assert post(app, "send", {"text": "hi"})[0] == 404
    assert post(app, "done", {"done": True})[0] == 404


def test_send_to_a_waiting_session_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_routes, "snapshot", lambda: snap(parents={500: 400}, shells={400}))
    app = FakeApp(tmp_path, {SID: live("waiting", waiting_for="dialog")})

    status, reply = post(app, "send", {"text": "yes"})
    assert (status, reply["error"]) == (409, "Waiting for a permission answer: answer in the terminal")

    status, reply = post(app, "send", {"text": "yes"}, lang="ru")
    assert (status, reply["error"]) == (409, "ждёт ответа на разрешение — ответь в терминале")


def test_done_endpoint_sets_and_clears(tmp_path):
    app = FakeApp(tmp_path)

    assert post(app, "done", {"done": True}) == (200, {"ok": True, "done": True})
    assert SID in done.load()
    assert post(app, "done", {"done": False})[1]["done"] is False
    assert done.load() == {}
    assert post(app, "done", {"done": "yes"})[0] == 400


# ---- bridge client against a fake server ------------------------------------------------------


class FakeBridge:
    """Speaks the extension's JSON-lines protocol on a unix socket and records what it was asked."""

    def __init__(self, path: Path, terminals):
        self.requests: list[dict] = []
        self.terminals = terminals
        path.parent.mkdir(parents=True, exist_ok=True)
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(path))
        self.server.listen(5)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                req = json.loads(conn.makefile().readline())
                self.requests.append(req)
                if req["op"] == "list":
                    reply = {"ok": True, "terminals": self.terminals}
                elif req.get("shellPid") in {t["shellPid"] for t in self.terminals}:
                    reply = {"ok": True}
                else:
                    reply = {"ok": False, "error": "no such terminal"}
                conn.sendall(json.dumps(reply).encode() + b"\n")

    def close(self):
        self.server.close()


@pytest.fixture
def fake_bridge(short_cache):
    made = []

    def make(pid, terminals):
        b = FakeBridge(short_cache / "vscode" / f"{pid}.sock", terminals)
        made.append(b)
        return b

    yield make
    for b in made:
        b.close()


def test_client_skips_dead_sockets_and_finds_the_window_that_owns_the_terminal(fake_bridge, short_cache):
    dead = short_cache / "vscode" / "1.sock"
    dead.parent.mkdir(parents=True, exist_ok=True)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(dead))
    s.close()  # a socket file nobody listens on
    other = fake_bridge(2, [{"name": "zsh", "shellPid": 111}])
    mine = fake_bridge(3, [{"name": "claude", "shellPid": 400}])

    assert [b["terminals"][0]["shellPid"] for b in bridge.list_bridges()] == [111, 400]
    bridge.send(400, "hello")

    assert mine.requests[-1] == {"op": "send", "shellPid": 400, "text": "hello"}
    assert other.requests[-1]["op"] == "send"  # asked first, declined: not its terminal
    with pytest.raises(bridge.BridgeError, match="wasn’t found"):
        bridge.send(999, "x")


def test_the_chats_setup_gives_claudes_routes_the_vscode_terminals(tmp_path, fake_bridge, monkeypatch):
    monkeypatch.setattr(claude_routes, "_ps_parents", lambda: {500: 450, 450: 400})
    fake_bridge(3, [{"name": "claude", "shellPid": 400}])
    rec = {"session_id": SID, "provider": "claude", "cwd": str(tmp_path)}
    assert delivery.card_fields(rec, live())["delivery"]["route"] != "vscode"  # nothing plugged in yet

    App(FakeIndex({}), [Feature(id="other"), chat.FEATURE])
    claude_routes.reset_caches()

    assert delivery.card_fields(rec, live())["delivery"]["route"] == "vscode"


def test_send_endpoint_pastes_text_with_file_mentions_through_the_bridge(tmp_path, fake_bridge, monkeypatch):
    fb = fake_bridge(3, [{"name": "claude", "shellPid": 400}])
    monkeypatch.setattr(
        claude_routes, "snapshot", lambda: snap(parents={500: 400}, shells={400}, bridge_alive=True)
    )
    monkeypatch.setattr(claude_routes, "verify_live", lambda lv: None)
    app = FakeApp(tmp_path, {SID: live()})
    uploaded = post(app, "upload", headers={"X-Filename": "log.txt"}, body=b"x")[1]["path"]

    status, reply = post(app, "send", {"text": "look\x1b[201~ at this", "files": [uploaded]})

    assert (status, reply["route"]) == (200, "vscode")
    sent = fb.requests[-1]
    assert sent["shellPid"] == 400
    assert sent["text"] == f"look[201~ at this\n@{uploaded}"  # no ESC survives to end the paste early


def test_focus_goes_to_the_bridge_then_raises_vscode(tmp_path, fake_bridge, monkeypatch):
    fb = fake_bridge(3, [{"name": "claude", "shellPid": 400}])
    monkeypatch.setattr(
        claude_routes, "snapshot", lambda: snap(parents={500: 400}, shells={400}, bridge_alive=True)
    )
    opened = []
    monkeypatch.setattr(bridge.subprocess, "run", lambda cmd, **kw: opened.append(cmd))
    app = FakeApp(tmp_path, {SID: live("waiting", waiting_for="permission prompt")})

    assert post(app, "focus")[0] == 200
    assert fb.requests[-1] == {"op": "focus", "shellPid": 400}
    assert opened == [["open", "-a", "Visual Studio Code"]]


# ---- tmux and resume --------------------------------------------------------------------------


def fake_executable(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_tmux_route_pastes_in_bracketed_mode_then_presses_enter(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "tmux.log"
    fake_executable(bin_dir, "tmux", f'echo "$@" >> {calls}\ncat > /dev/null\n')
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(claude_routes, "snapshot", lambda: snap(registry={500: {"tmux": "%3"}}))
    monkeypatch.setattr(claude_routes, "verify_live", lambda lv: None)
    monkeypatch.setattr(claude_routes, "on_prompt", lambda sid: False)  # `claude agents`: no prompt open

    status, reply = post(FakeApp(tmp_path, {SID: live()}), "send", {"text": "hello"})

    assert (status, reply["route"]) == (200, "tmux")
    lines = calls.read_text().splitlines()
    assert lines[0].startswith("load-buffer -b sya-")
    assert lines[1].startswith("paste-buffer -p -d -b sya-") and lines[1].endswith("-t %3")
    assert lines[2] == "send-keys -t %3 Enter"


def test_takeover_needs_confirmation_then_stops_the_process_before_resuming(tmp_path, monkeypatch, sleeper):
    monkeypatch.setattr(claude_routes, "snapshot", lambda: snap())
    monkeypatch.setattr(claude_routes.shutil, "which", lambda n: "/usr/bin/claude")
    monkeypatch.setattr(claude_routes, "verify_live", lambda lv: None)
    resumed = []

    def fake_resume(session, message):
        resumed.append(sleeper.poll())
        return "x.log"

    monkeypatch.setattr(claude_routes, "resume", fake_resume)
    threading.Thread(target=sleeper.wait, daemon=True).start()  # reap it, as the terminal's shell would
    app = FakeApp(tmp_path, {SID: live(pid=sleeper.pid)})

    status, reply = post(app, "send", {"text": "wrap up"})
    assert status == 409 and "confirmation" in reply["error"]
    assert sleeper.poll() is None

    status, reply = post(app, "send", {"text": "wrap up", "takeover": True})
    assert (status, reply["route"]) == (200, "takeover")
    assert resumed == [-15]  # resumed only after the terminal's process had exited on SIGTERM


def test_resume_spawns_one_detached_turn_and_refuses_a_second(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    out = tmp_path / "args.txt"
    fake_executable(bin_dir, "claude", f'pwd > {out}\nprintf "%s\\n" "$@" >> {out}\nsleep 2\n')
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    work = tmp_path / "work"
    work.mkdir()
    app = FakeApp(tmp_path, records={SID: {"cwd": str(work)}})
    uploaded = post(app, "upload", headers={"X-Filename": "a.txt"}, body=b"x")[1]["path"]

    status, reply = post(app, "send", {"text": "continue", "files": [uploaded]})

    assert (status, reply["route"]) == (200, "resume")
    assert claude_routes.busy(SID)
    assert Path(reply["log"]).parent == tmp_path / "cache" / "runs"
    status, again = post(app, "send", {"text": "again"})
    assert status == 409 and again["error"]
    card = delivery.card_fields({"session_id": SID, "provider": "claude", "cwd": str(work)}, None)
    assert card["delivery"]["can_send"] is False and card["running_reply"] is True

    deadline = time.monotonic() + 3
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    cwd, *args = out.read_text().splitlines()
    assert Path(cwd).resolve() == work.resolve()
    uploads = str(tmp_path / "cache" / "uploads" / SID)
    assert args == ["-p", f"--resume={SID}", "--add-dir", uploads, "--", "continue", f"@{uploaded}"]


# ---- VS Code extension ------------------------------------------------------------------------


def test_vsix_layout(tmp_path):
    vsix = bridge.build_vsix(tmp_path / "out")

    with zipfile.ZipFile(vsix) as z:
        names = set(z.namelist())
        manifest = ElementTree.fromstring(z.read("extension.vsixmanifest"))
        ElementTree.fromstring(z.read("[Content_Types].xml"))
        packaged = json.loads(z.read("extension/package.json"))
        assert z.read("extension/extension.js") == (bridge.source_dir() / "extension.js").read_bytes()
    assert names == {
        "[Content_Types].xml",
        "extension.vsixmanifest",
        "extension/package.json",
        "extension/extension.js",
    }
    identity = manifest.find(".//{*}Identity")
    assert identity.attrib["Id"] == packaged["name"] and identity.attrib["Publisher"] == packaged["publisher"]
    assert vsix.name == f"{packaged['name']}-{packaged['version']}.vsix"
    assert packaged["engines"]["vscode"] == "^1.75.0"
    assert packaged["activationEvents"] == ["onStartupFinished"]


NODE_HARNESS = r"""
const Module = require("module");
const net = require("net");
const fs = require("fs");
const sent = [];
const mk = (name, pid) => ({
  name,
  processId: Promise.resolve(pid),
  sendText: (t, nl) => sent.push([name, t, nl]),
  show: (preserve) => sent.push([name, "show", preserve]),
});
const noPid = { name: "dead", processId: Promise.resolve(undefined) };
const vscode = { window: { terminals: [mk("zsh", 101), mk("claude", 202), noPid] } };
const load = Module._load;
Module._load = (request, ...rest) => (request === "vscode" ? vscode : load(request, ...rest));
const ext = require(process.argv[2]);
const ask = (sock, req) => new Promise((resolve) => {
  const c = net.createConnection(sock); let buf = "";
  c.on("data", (d) => { buf += d; if (buf.endsWith("\n")) { c.end(); resolve(JSON.parse(buf)); } });
  c.write(typeof req === "string" ? req : JSON.stringify(req) + "\n");
});
ext.activate();
const sock = process.env.SYA_CACHE_DIR + "/vscode/" + process.pid + ".sock";
(async () => {
  while (!fs.existsSync(sock)) await new Promise((r) => setTimeout(r, 20));
  await new Promise((r) => setTimeout(r, 50));
  const dir = process.env.SYA_CACHE_DIR + "/vscode";
  const out = { modes: [fs.statSync(sock).mode & 0o777, fs.statSync(dir).mode & 0o777] };
  out.list = await ask(sock, { op: "list" });
  out.send = await ask(sock, { op: "send", shellPid: 202, text: "hi\nthere" });
  out.sendMissing = await ask(sock, { op: "send", shellPid: 999, text: "x" });
  out.sendEmpty = await ask(sock, { op: "send", shellPid: 202, text: "" });
  out.focus = await ask(sock, { op: "focus", shellPid: 101 });
  out.badJson = await ask(sock, "{nope\n");
  out.badOp = await ask(sock, { op: "rm" });
  out.sent = sent;
  ext.deactivate();
  await new Promise((r) => setTimeout(r, 50));
  out.socketGone = !fs.existsSync(sock);
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_extension_socket_logic_with_a_stub_vscode(short_cache, tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(NODE_HARNESS)
    stale = short_cache / "vscode"
    stale.mkdir(parents=True)

    run = subprocess.run(
        ["node", str(harness), str(bridge.source_dir() / "extension.js")],
        env={**os.environ, "SYA_CACHE_DIR": str(short_cache)},
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.splitlines()[-1])
    assert out["modes"] == [0o600, 0o700]
    assert out["list"] == {
        "ok": True,
        "terminals": [{"name": "zsh", "shellPid": 101}, {"name": "claude", "shellPid": 202}],
    }
    assert out["send"] == {"ok": True}
    assert out["sent"][0] == ["claude", "\x1b[200~hi\nthere\x1b[201~", False]
    assert out["sent"][1] == ["claude", "\r", False]
    assert out["sendMissing"]["ok"] is False and out["sendEmpty"]["ok"] is False
    assert out["focus"] == {"ok": True} and out["sent"][2] == ["zsh", "show", False]
    assert out["badJson"]["ok"] is False and out["badOp"]["ok"] is False
    assert out["socketGone"] is True
