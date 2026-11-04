"""The `peer` route: a live session in a plain terminal or in the background gets the text by courier."""

import json
import subprocess
import threading
from email.message import Message

import pytest

from see_your_agents import i18n
from see_your_agents.core.providers.base import LiveSession
from see_your_agents.core.providers.claude import courier
from see_your_agents.core.providers.claude import routes as claude_routes
from see_your_agents.core.providers.claude.transcript import OLD_REPLY_NOTE, REPLY_NOTE, peer_text
from see_your_agents.features.chat import delivery, wire
from see_your_agents.i18n import Problem

SID = "11111111-2222-3333-4444-555555555555"
NAME = "acme-ledger-c0"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SYA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SYA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(claude_routes.shutil, "which", lambda n: "/usr/bin/claude")
    claude_routes.reset_caches()
    claude_routes._couriers.clear()
    yield
    claude_routes.reset_caches()


def named(status="idle", **extra):
    return LiveSession(
        provider="claude", session_id=SID, kind="interactive", status=status, pid=500, name=NAME, **extra
    )


def snap(registry=None):
    return claude_routes.Snapshot({}, registry or {}, set(), False)


class FakeIndex:
    def record(self, sid):
        return {"session_id": sid, "cwd": "/", "provider": "claude"} if sid == SID else None


class FakeApp:
    index = FakeIndex()

    def __init__(self, live, *others: LiveSession):
        self._live = {SID: live, **{o.session_id: o for o in others}}
        wire(self)

    def live(self):
        return self._live

    def resume_args(self, session_id):
        return []


def post(app, payload, lang="en"):
    """The handler as the host runs it: a Problem it raises is {"error": ...} in the request's language."""
    try:
        status, _, text = delivery.handle_post(
            app, f"/api/chat/{SID}/send", json.dumps(payload).encode(), Message()
        )
    except Problem as exc:
        return exc.status, {"error": exc.text(lang)}
    return status, json.loads(text)


@pytest.fixture
def courier_calls(monkeypatch):
    """Replaces courier.deliver: records every call, outcome[0] is what it returns."""
    calls, outcome = [], [courier.Delivery(True, msg_id="m1")]

    def fake(to, text, summary, sender):
        calls.append((to, text, summary, sender, claude_routes.busy(SID)))
        return outcome[0]

    monkeypatch.setattr(courier, "deliver", fake)
    monkeypatch.setattr(claude_routes, "snapshot", lambda: snap())
    monkeypatch.setattr(claude_routes, "verify_live", lambda lv: None)
    return calls, outcome


# ---- choice of route --------------------------------------------------------------------------


def test_named_live_session_without_a_terminal_route_gets_the_courier():
    idle = claude_routes.route_for(named(), "/", False, False, snap())
    busy = claude_routes.route_for(named("busy"), "/", False, False, snap())

    assert (idle.route, idle.can_send, idle.target, idle.alt) == ("peer", True, NAME, "takeover")
    assert (busy.route, busy.can_send, busy.alt) == (
        "peer",
        True,
        None,
    )  # a busy agent still gets the message


def test_terminal_routes_beat_the_courier():
    assert claude_routes.route_for(named(), "/", False, False, snap({500: {"tmux": "%3"}})).route == "tmux"


def test_courier_is_refused_while_the_session_waits_on_a_prompt():
    r = claude_routes.route_for(named("waiting", waiting_for="permission prompt"), "/", False, False, snap())

    assert (r.route, r.can_send, r.reason) == (None, False, claude_routes.WAITING_PROMPT)
    assert r.why("en") == "Waiting for a permission answer: answer in the terminal"
    assert r.why("ru") == "ждёт ответа на разрешение — ответь в терминале"


def test_unnamed_session_keeps_the_takeover():
    unnamed = LiveSession(provider="claude", session_id=SID, kind="interactive", status="idle", pid=500)

    r = claude_routes.route_for(unnamed, "/", False, False, snap())

    assert (r.route, r.alt) == ("takeover", None)


def background(status="idle", **extra):
    return LiveSession(
        provider="claude", session_id=SID, kind="background", status=status, job_id="ab12cd34", **extra
    )


def test_named_background_session_gets_the_courier_and_no_takeover():
    r = claude_routes.route_for(background(name="w"), "/", False, False, snap())

    assert (r.route, r.can_send, r.target, r.alt) == ("peer", True, "w", None)


def test_background_session_on_a_permission_prompt_or_unnamed_points_to_claude_attach():
    # what `claude agents --json` gave for a bg job on a Write prompt (Claude Code 2.1.317)
    asking = background("waiting", name="w", waiting_for="permission prompt", needs="approve Write: /x")
    made_up = background(name="bg-1f2e", name_source="auto")

    for session in (asking, background(), made_up):
        r = claude_routes.route_for(session, "/", False, False, snap())
        assert (r.route, r.can_send) == (None, False)
        assert "claude attach ab12cd34" in r.why()


def test_background_session_that_ended_its_turn_with_a_question_gets_the_courier():
    # `claude agents` reports it as state "blocked" with the question in `needs` and no waitingFor
    question = background("blocked", name="w", needs="confirm: merge?")
    r = claude_routes.route_for(question, "/", False, False, snap())

    assert (r.route, r.can_send, r.target) == ("peer", True, "w")


# ---- sending ----------------------------------------------------------------------------------


def test_peer_send_hands_the_text_to_the_courier_as_the_dashboard(courier_calls):
    calls, _ = courier_calls

    status, reply = post(FakeApp(named()), {"text": "  что с CI?  "})

    assert (status, reply) == (200, {"ok": True, "route": "peer"})
    assert calls == [(NAME, REPLY_NOTE + "что с CI?", "что с CI?", "Dashboard", False)]
    assert not claude_routes.busy(SID)


def test_a_courier_on_its_way_leaves_the_input_open_and_refuses_a_second_one(courier_calls, monkeypatch):
    """The card is not "busy" meanwhile, so the page keeps its input; a second message at once is refused."""
    app, seen = FakeApp(named()), []

    def fake(to, text, summary, sender):
        card = delivery.card_fields(app.index.record(SID), named())
        seen.append((card["running_reply"], card["delivery"]["can_send"], post(app, {"text": "again"})))
        return courier.Delivery(True, msg_id="m1")

    monkeypatch.setattr(courier, "deliver", fake)

    assert post(app, {"text": "hi"})[0] == 200
    [(running_reply, can_send, (status, reply))] = seen
    assert (running_reply, can_send) == (False, True)
    assert status == 409 and "on its way" in reply["error"]


def test_peer_failure_reaches_the_page_and_frees_the_session(courier_calls):
    _, outcome = courier_calls
    outcome[0] = courier.Delivery(False, error="app.provider.claude.courier.distorted")

    assert post(FakeApp(named()), {"text": "hi"}) == (
        502,
        {"error": "Not delivered: The courier changed the text"},
    )
    assert not claude_routes.busy(SID)
    with i18n.answering("ru"):  # the courier's reason is worded in the request's language
        assert post(FakeApp(named()), {"text": "hi"}, lang="ru") == (
            502,
            {"error": "не доставлено: курьер исказил текст"},
        )


def test_peer_send_refuses_files_instead_of_dropping_them(courier_calls, tmp_path):
    uploads = delivery.uploads_root() / SID
    uploads.mkdir(parents=True)
    (uploads / "a.txt").write_text("x")

    status, reply = post(FakeApp(named()), {"text": "see", "files": [str(uploads / "a.txt")]})

    assert status == 400 and "only text" in reply["error"]
    assert courier_calls[0] == []


def test_peer_send_is_refused_while_the_session_waits(courier_calls):
    status, _ = post(FakeApp(named("waiting", waiting_for="dialog")), {"text": "hi"})

    assert status == 409 and courier_calls[0] == []


def test_takeover_stays_available_next_to_the_courier(courier_calls, monkeypatch):
    sleeper = subprocess.Popen(["sleep", "60"])
    threading.Thread(target=sleeper.wait, daemon=True).start()
    resumed = []
    monkeypatch.setattr(claude_routes, "resume", lambda session, msg: resumed.append(msg.text) or "x.log")
    app = FakeApp(
        LiveSession(
            provider="claude", session_id=SID, kind="interactive", status="idle", pid=sleeper.pid, name=NAME
        )
    )
    monkeypatch.setattr(claude_routes, "_resume_blocker", lambda cwd, missing: "")
    try:
        status, reply = post(app, {"text": "wrap up", "takeover": True})
    finally:
        sleeper.kill()

    assert (status, reply["route"]) == (200, "takeover")
    assert resumed == ["wrap up"] and courier_calls[0] == []


def test_takeover_of_a_busy_session_is_refused(courier_calls):
    status, _ = post(FakeApp(named("busy")), {"text": "x", "takeover": True})

    assert status == 409 and courier_calls[0] == []


def test_background_send_skips_the_pid_check_it_has_no_pid_for(courier_calls, monkeypatch):
    calls, _ = courier_calls
    monkeypatch.setattr(claude_routes, "verify_live", lambda lv: pytest.fail("no pid to check"))

    status, reply = post(FakeApp(background(name="w")), {"text": "готов?"})

    assert (status, reply) == (200, {"ok": True, "route": "peer"}) and calls[0][0] == "w"


def test_peer_send_is_refused_while_two_live_sessions_carry_the_name(courier_calls):
    calls, _ = courier_calls
    twin = LiveSession(
        provider="claude", session_id="other", kind="interactive", status="idle", pid=501, name=NAME.upper()
    )

    status, reply = post(FakeApp(named(), twin), {"text": "hi"})

    assert status == 409 and "Several live sessions are named" in reply["error"]
    assert calls == []


def test_the_reply_note_is_not_shown_as_part_of_the_users_message():
    assert peer_text({"body": REPLY_NOTE + "что с CI?"}) == "что с CI?"
    joined = {"members": [{"body": REPLY_NOTE + "a"}, {"body": "b"}]}
    assert peer_text(joined) == "a\n\nb"


def test_the_note_of_an_older_transcript_is_stripped_too():
    """Transcripts from before the note was English carry the Russian one: history still reads cleanly."""
    assert REPLY_NOTE.isascii() and not OLD_REPLY_NOTE.isascii()
    assert peer_text({"body": OLD_REPLY_NOTE + "что с CI?"}) == "что с CI?"
    joined = {"members": [{"body": OLD_REPLY_NOTE + "a"}, {"body": REPLY_NOTE + "b"}]}
    assert peer_text(joined) == "a\n\nb"
