"""Claude Code's routes: which way a message goes, the process checks before touching one."""

import shutil

import pytest
from fakes import needs_ps, utc_start_of

from see_your_agents.core.providers.base import LiveSession, Message, SendRefused, Session
from see_your_agents.core.providers.claude import routes as claude_routes

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def fresh():
    claude_routes.reset_caches()
    claude_routes._runs.clear()
    yield
    claude_routes.reset_caches()
    claude_routes._runs.clear()  # a fake turn left here would hold the session busy in other files


def live(status="idle", kind="interactive", pid=500, **extra):
    return LiveSession(provider="claude", session_id=SID, kind=kind, status=status, pid=pid, **extra)


def snap(parents=None, registry=None, shells=(), bridge_alive=False):
    return claude_routes.Snapshot(parents or {}, registry or {}, set(shells), bridge_alive)


@pytest.fixture
def claude_on_path(monkeypatch):
    real = shutil.which
    monkeypatch.setattr(
        claude_routes.shutil, "which", lambda n: "/usr/bin/claude" if n == "claude" else real(n)
    )


# ---- route rules ------------------------------------------------------------------------------


def test_waiting_session_refuses_text_even_when_a_route_exists(tmp_path):
    s = snap(parents={500: 400}, shells={400}, bridge_alive=True)

    r = claude_routes.route_for(
        live("waiting", waiting_for="permission prompt"), str(tmp_path), False, False, s
    )

    assert (r.route, r.can_send, r.reason) == ("vscode", False, claude_routes.WAITING_PROMPT)


def test_live_session_under_a_bridge_terminal_goes_through_vscode(tmp_path):
    s = snap(parents={500: 450, 450: 400, 400: 1}, shells={400}, bridge_alive=True)

    r = claude_routes.route_for(live(), str(tmp_path), False, False, s)

    assert (r.route, r.can_send, r.target) == ("vscode", True, 400)


def test_registry_tmux_field_wins_and_names_the_pane(tmp_path):
    s = snap(registry={500: {"tmux": {"paneId": "%7"}}}, parents={500: 400}, shells={400})

    r = claude_routes.route_for(live(), str(tmp_path), False, False, s)

    assert (r.route, r.target) == ("tmux", "%7")


def test_idle_session_nothing_can_type_into_is_offered_a_takeover(tmp_path, claude_on_path):
    with_bridge = claude_routes.route_for(live(), str(tmp_path), False, False, snap(bridge_alive=True))
    no_bridge = claude_routes.route_for(live(), str(tmp_path), False, False, snap())
    busy = claude_routes.route_for(live("busy"), str(tmp_path), False, False, snap())

    assert (with_bridge.route, with_bridge.can_send) == ("takeover", True)
    assert "outside VS Code and tmux" in with_bridge.why("en")
    assert "вне VS Code и tmux" in with_bridge.why("ru")
    assert "without the VS Code bridge" in no_bridge.why("en")
    assert (busy.route, busy.can_send) == (None, False)


def test_stopped_session_resumes_when_transcript_and_cwd_exist(tmp_path, claude_on_path):
    ok = claude_routes.route_for(None, str(tmp_path), False, False, snap())
    no_transcript = claude_routes.route_for(None, str(tmp_path), True, False, snap())
    no_cwd = claude_routes.route_for(None, str(tmp_path / "gone"), False, False, snap())

    assert (ok.route, ok.can_send) == ("resume", True)
    assert not no_transcript.can_send
    assert not no_cwd.can_send


def test_a_folder_the_disk_cannot_stat_is_no_folder_not_an_error(tmp_path, claude_on_path):
    too_long = str(tmp_path / ("x" * 300))  # ENAMETOOLONG: Path.is_dir() raises instead of saying False

    r = claude_routes.route_for(None, too_long, False, False, snap())

    assert (r.route, r.can_send, r.reason) == (None, False, "app.provider.claude.no_transcript")


def test_running_resume_blocks_another_send(tmp_path, claude_on_path):
    r = claude_routes.route_for(None, str(tmp_path), False, True, snap())

    assert (r.route, r.can_send) == ("resume", False)


@needs_ps
def test_verify_pid_accepts_only_the_recorded_process(sleeper):
    claude_routes.verify_pid(sleeper.pid, utc_start_of(sleeper.pid), comm="sleep")

    with pytest.raises(SendRefused) as start_mismatch:
        claude_routes.verify_pid(sleeper.pid, utc_start_of(sleeper.pid, shift_s=3600), comm="sleep")
    with pytest.raises(SendRefused) as wrong_program:
        claude_routes.verify_pid(sleeper.pid, utc_start_of(sleeper.pid))  # default comm is claude
    with pytest.raises(SendRefused):
        claude_routes.verify_pid(sleeper.pid, None)

    assert start_mismatch.value.key == wrong_program.value.key == claude_routes.NOT_THE_PROCESS


# ---- the dashboard's side, and signed messages ----------------------------------------------------


@pytest.fixture
def couriered(monkeypatch):
    """Courier deliveries recorded instead of run; the pid check passes."""
    sent = []
    ok = claude_routes.courier.Delivery(True, msg_id="m1")
    monkeypatch.setattr(claude_routes.courier, "deliver", lambda **kw: sent.append(kw) or ok)
    monkeypatch.setattr(claude_routes, "verify_live", lambda lv: None)
    monkeypatch.setattr(claude_routes, "snapshot", lambda: snap())
    return sent


def plug(*sessions, resume_args=lambda sid: []):
    claude_routes.plug(None, lambda: {s.session_id: s for s in sessions}, resume_args)


def test_a_namesake_of_another_program_does_not_block_a_peer_message(couriered):
    named = live(name="proj")
    plug(named, LiveSession("other", "interactive", "idle", name="proj", provider="codex"))

    sent = claude_routes.send(Session(SID, "/", live=named), Message("hi"))

    assert (sent.route, sent.msg_id) == ("peer", "m1") and couriered[0]["to"] == "proj"


def test_two_claude_sessions_with_one_name_refuse_a_peer_message(couriered):
    named = live(name="proj")
    plug(named, LiveSession("other", "interactive", "idle", name="PROJ", provider="claude"))

    with pytest.raises(SendRefused, match="Several live sessions are named"):
        claude_routes.send(Session(SID, "/", live=named), Message("hi"))
    assert couriered == []


def test_a_signed_message_goes_by_courier_where_the_chat_would_type_into_tmux(couriered, monkeypatch):
    in_tmux = live(name="proj")
    monkeypatch.setattr(claude_routes, "snapshot", lambda: snap(registry={500: {"tmux": "%3"}}))
    plug(in_tmux)
    session = Session(SID, "/", live=in_tmux)

    assert claude_routes.route(session).route == "tmux"
    sent = claude_routes.send(session, Message("hi", sender="Группа «Релиз»", summary="hi"))

    assert sent.route == "peer" and couriered[0]["sender"] == "Группа «Релиз»"


class Running:
    def poll(self):
        return None


def test_a_resume_asks_for_its_flags_outside_the_lock_and_refuses_unplugged(tmp_path, claude_on_path):
    session = Session(SID, str(tmp_path))
    with pytest.raises(SendRefused, match="aren’t plugged into the dashboard"):
        claude_routes.resume(session, Message("hi"))

    asked = []
    plug(resume_args=lambda sid: asked.append(claude_routes._runs_lock.locked()) or ["--x"])
    claude_routes._runs[SID] = Running()  # a turn under way: refused under the lock, after the flags
    with pytest.raises(SendRefused, match="already running"):
        claude_routes.resume(session, Message("hi"))

    assert asked == [False]


@pytest.fixture
def tmux_calls(monkeypatch):
    """tmux on PATH, its subcommands recorded instead of run; the session is in pane %3."""
    calls = []
    monkeypatch.setattr(claude_routes.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(claude_routes.subprocess, "run", lambda argv, **kw: calls.append(argv[1]))
    monkeypatch.setattr(claude_routes.time, "sleep", lambda s: None)
    monkeypatch.setattr(claude_routes, "snapshot", lambda: snap(registry={500: {"tmux": "%3"}}))
    return calls


def test_tmux_presses_enter_only_while_no_prompt_is_open(couriered, tmux_calls, monkeypatch):
    in_tmux = live(name="proj")
    plug(in_tmux)
    session = Session(SID, "/", live=in_tmux)

    monkeypatch.setattr(claude_routes, "on_prompt", lambda sid: False)
    assert claude_routes.send(session, Message("hi")).route == "tmux"
    assert tmux_calls == ["load-buffer", "paste-buffer", "send-keys"]

    tmux_calls.clear()
    monkeypatch.setattr(claude_routes, "on_prompt", lambda sid: True)  # opened while the text was pasted
    with pytest.raises(SendRefused, match="opened before Enter"):
        claude_routes.send(session, Message("hi"))
    assert tmux_calls == ["load-buffer", "paste-buffer"]


def test_a_command_like_text_is_not_typed_into_a_terminal(couriered, tmux_calls):
    named = live(name="proj")
    plug(named)

    sent = claude_routes.send(Session(SID, "/", live=named), Message("/clear"))

    assert sent.route == "peer" and couriered[0]["to"] == "proj" and tmux_calls == []
    with pytest.raises(SendRefused, match="as a command"):
        claude_routes.send(Session(SID, "/", live=live()), Message("# note"))
