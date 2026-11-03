import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from fakes import FakeApp, no_processes

from see_your_agents.app import cli, mcp, server
from see_your_agents.core.providers.base import LiveSession, Sent
from see_your_agents.core.providers.claude import launch as claude_launch
from see_your_agents.features.chat import delivery
from see_your_agents.features.supervisor import mcp_tools

PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SYA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SYA_CACHE_DIR", str(tmp_path / "cache"))
    no_processes(monkeypatch)


@pytest.fixture
def fake_app(tmp_path):
    records = [
        {
            "session_id": "s1",
            "title": "One",
            "cwd": "/w/p",
            "project": "/w/p",
            "last_activity": "2026-11-01T00:00:00Z",
        }
    ]
    live = {
        "s1": LiveSession(
            provider="claude", session_id="s1", kind="interactive", status="idle", name="one", cwd="/w/p"
        )
    }
    return FakeApp(records, live)


@pytest.fixture
def base(fake_app):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(fake_app, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(fake_app, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", port
    httpd.shutdown()


def rpc(base, payload, token="read", headers=None, method="POST"):
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    if token:
        hdrs["Authorization"] = f"Bearer {mcp.tokens().get(token, token)}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{base}/mcp", data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if body else None, resp.headers
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body), exc.headers
        except ValueError:
            return exc.code, body, exc.headers


def call(base, name, arguments, token="read"):
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return rpc(base, request, token)[1]


def test_initialize_negotiates_protocol(base):
    url, _ = base
    for asked, got in [("2025-03-26", "2025-03-26"), ("2099-01-01", "2025-06-18")]:
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": asked}}
        status, body, _ = rpc(url, request)
        assert status == 200 and body["result"]["protocolVersion"] == got
    assert body["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert body["result"]["serverInfo"]["name"] == "see-your-agents"


def test_notification_is_accepted_without_body(base):
    status, body, _ = rpc(base[0], {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202 and body is None


def test_tools_list_depends_on_scope(base):
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    read = {t["name"] for t in rpc(base[0], listing, "read")[1]["result"]["tools"]}
    control = {t["name"] for t in rpc(base[0], listing, "control")[1]["result"]["tools"]}

    # other features add their own tools: the supervisor's must be there, the scopes must not mix
    assert {
        "fleet_overview",
        "session_detail",
        "session_thread",
        "search_sessions",
        "list_delegations",
    } <= read
    assert {"resume_session", "start_agent", "post_digest", "request_change"} <= control
    assert not read & control


def test_tool_call_happy_path_and_errors(base):
    url, _ = base
    ok = call(url, "fleet_overview", {})["result"]
    assert ok["isError"] is False
    assert json.loads(ok["content"][0]["text"])["sessions"][0]["name"] == "one"

    assert call(url, "session_detail", {"session_id": "nope"})["result"]["isError"] is True
    # a control tool is invisible to the read scope
    assert call(url, "post_digest", {"text": "x"}, "read")["error"]["code"] == -32602
    assert call(url, "post_digest", {"text": "x"}, "control")["result"]["isError"] is False
    assert call(url, "post_digest", {"text": ""}, "control")["result"]["isError"] is True


def test_json_rpc_errors(base):
    url, _ = base
    assert rpc(url, {"jsonrpc": "2.0", "id": 1, "method": "nope"})[1]["error"]["code"] == -32601
    assert rpc(url, [PING])[1]["error"]["code"] == -32600
    status, body, _ = rpc(url, None)
    assert status == 400 and body["error"]["code"] == -32700


def test_resume_refuses_live_session_and_resumes_stopped_one(base, fake_app, tmp_path, monkeypatch):
    url, _ = base
    refused = call(url, "resume_session", {"session_id": "s1", "text": "hi"}, "control")["result"]
    assert refused["isError"]
    assert "SendMessage" in refused["content"][0]["text"] and "“one”" in refused["content"][0]["text"]

    started = []
    monkeypatch.setattr(
        delivery, "start_resume", lambda app, sid, msg: started.append((sid, msg)) or Sent("resume", "x.log")
    )
    monkeypatch.setattr(
        delivery, "delivery_for", lambda app, sid: {"route": "resume", "can_send": True, "reason": ""}
    )
    fake_app.fake_live = {}
    ok = call(url, "resume_session", {"session_id": "s1", "text": "hi"}, "control")["result"]
    assert not ok["isError"] and started == [("s1", "hi")]


def test_start_agent_runs_claude_bg_in_cwd(base, tmp_path, monkeypatch):
    url, _ = base
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDECODE", "1")  # the dashboard was started from a Claude session
    work = tmp_path / "proj"
    work.mkdir()
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["cwd"], kw["env"]))
        return subprocess.CompletedProcess(cmd, 0, stdout="backgrounded · abc12345 · w\n", stderr="")

    monkeypatch.setattr(claude_launch.subprocess, "run", fake_run)
    monkeypatch.setattr(claude_launch.shutil, "which", lambda n: "/bin/claude")
    monkeypatch.setattr(
        claude_launch.live, "_agents_json", lambda: [{"id": "abc12345", "sessionId": "s-abc"}]
    )
    out = call(url, "start_agent", {"cwd": str(work), "prompt": "do x", "name": "w"}, "control")["result"]

    result = json.loads(out["content"][0]["text"])
    assert not out["isError"] and result["attach"] == "claude attach abc12345"
    assert (result["job_id"], result["session_id"], result["name"]) == ("abc12345", "s-abc", "w")
    (cmd, cwd, env), *_ = calls
    assert (cmd, cwd) == (["/bin/claude", "--bg", "--name=w", "--", "do x"], work.resolve())
    assert "CLAUDECODE" not in env
    haiku = call(url, "start_agent", {"cwd": str(work), "prompt": "x", "model": "haiku"}, "control")["result"]
    assert not haiku["isError"] and "--model=haiku" in calls[-1][0]
    taken = call(url, "start_agent", {"cwd": str(work), "prompt": "x", "name": "One"}, "control")["result"]
    assert json.loads(taken["content"][0]["text"])["name"] == "One-2"  # the live session «one» has it
    for bad in ({"cwd": "/", "prompt": "x"}, {"cwd": str(work), "prompt": "x", "model": "gpt-4"}):
        assert call(url, "start_agent", bad, "control")["result"]["isError"]
    assert len(calls) == 3


def test_auth_is_required(base):
    status, _, headers = rpc(base[0], PING, token=None)
    assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
    assert rpc(base[0], PING, token="wrong")[0] == 401


def test_foreign_origin_and_host_are_forbidden(base):
    url, _ = base
    assert rpc(url, PING, headers={"Origin": "http://evil.example"})[0] == 403
    assert rpc(url, PING, headers={"Origin": url})[0] == 200
    assert rpc(url, PING, headers={"Host": "evil.example"})[0] == 403


def test_get_and_delete_are_405(base):
    assert rpc(base[0], None, method="GET")[0] == 405
    assert rpc(base[0], None, method="DELETE")[0] == 405


def test_tokens_are_stable_and_private(tmp_path):
    first = mcp.tokens()
    assert mcp.tokens() == first and first["read"] != first["control"]
    assert (tmp_path / "config" / "mcp-tokens.json").stat().st_mode & 0o777 == 0o600


def test_cli_install_replaces_existing_registration(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(
        cli.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0)
    )
    cli.main(["mcp", "install", "--port", "9999"])
    token = mcp.tokens()["read"]

    assert ran[0] == ["claude", "mcp", "get", "sya"]
    assert ran[1] == ["claude", "mcp", "remove", "-s", "user", "sya"]
    assert ran[2] == [
        "claude", "mcp", "add", "--transport", "http", "-s", "user", "sya",
        "http://127.0.0.1:9999/mcp", "-H", f"Authorization: Bearer {token}",
    ]  # fmt: skip
    assert token not in capsys.readouterr().out


def test_cli_install_dry_run_masks_token(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(
        cli.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 1)
    )
    cli.main(["mcp", "install", "--dry-run"])
    out = capsys.readouterr().out

    assert ran == [["claude", "mcp", "get", "sya"]]  # nothing was added
    assert "claude mcp add --transport http -s user sya http://127.0.0.1:8765/mcp" in out
    assert mcp.tokens()["read"] not in out


def test_cli_status_lists_tools(base, capsys):
    cli.main(["mcp", "status", "--port", str(base[1])])
    assert "fleet_overview" in capsys.readouterr().out


def test_start_agent_refuses_the_home_folder_and_hidden_folders(base, tmp_path, monkeypatch):
    url, _ = base
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ssh").mkdir()
    monkeypatch.setattr(claude_launch.subprocess, "run", lambda *a, **k: pytest.fail("claude must not start"))
    monkeypatch.setattr(claude_launch.shutil, "which", lambda n: "/bin/claude")

    for cwd in (str(tmp_path), str(tmp_path / ".ssh"), "~nosuchuser/x", "bad\x00path"):
        assert call(url, "start_agent", {"cwd": cwd, "prompt": "x"}, "control")["result"]["isError"]


def test_a_tool_that_crashes_still_answers(base, monkeypatch):
    url, _ = base
    monkeypatch.setattr(mcp_tools.fleet, "search", lambda app, q: 1 / 0)

    out = call(url, "search_sessions", {"query": "x"})["result"]

    assert out["isError"] and "ZeroDivisionError" in out["content"][0]["text"]


def test_a_tool_name_that_is_not_a_string_is_an_unknown_tool(base):
    url, _ = base
    assert call(url, ["fleet_overview"], {})["error"]["code"] == -32602


def test_a_negative_content_length_is_refused(base):
    import http.client

    url, port = base
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", "/mcp")
    conn.putheader("Content-Length", "-1")
    conn.endheaders()

    assert conn.getresponse().status == 400
