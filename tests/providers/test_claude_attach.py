"""The `attach` route: a chat message typed into a background session through `claude attach` on a pty."""

import json
import os
import sys
import textwrap

import pytest

from see_your_agents.core.providers.base import (
    LiveSession,
    Message,
    SendError,
    SendRefused,
    SendTimeout,
    Session,
)
from see_your_agents.core.providers.claude import attach
from see_your_agents.core.providers.claude import routes as claude_routes

SID = "11111111-2222-3333-4444-555555555555"
JOB = "ab12cd34"

# A stand-in for `claude attach`: raw mode, a prompt mark, the input redrawn as it arrives (spaces as cursor
# moves, like Claude Code), every byte it gets logged; Enter records the input as a typed prompt.
FAKE = textwrap.dedent(
    """\
    import json, os, sys, tty
    mode, out = os.environ["FAKE_MODE"], sys.stdout.buffer
    tty.setraw(0)
    if mode == "exit":
        out.write(b"No background session ab12cd34\\r\\n"); out.flush(); sys.exit(1)
    out.write("history \\x1b[1m\\u276f\\x1b[0m ".encode()); out.flush()
    log, buf = open(os.environ["FAKE_INPUT"], "ab"), b""

    def text_of(b):
        b = b.replace(b"\\x1b[200~", b"").replace(b"\\x1b[201~", b"")
        return b.split(b"\\x1b")[0].decode("utf-8", "ignore")  # a paste mark still on its way

    while chunk := os.read(0, 4096):
        log.write(chunk); log.flush()
        if b"\\r" not in chunk:
            buf += chunk
            if mode != "dialog":  # a dialog takes no paste
                frame = "\\r\\x1b[2K\\u276f " + text_of(buf).replace(" ", "\\x1b[C")
                out.write(frame.encode()); out.flush()
            continue
        if mode == "ok":
            said = {"content": text_of(buf).strip()}
            entry = {"type": "user", "origin": {"kind": "human"}, "message": said}
            with open(os.environ["FAKE_TRANSCRIPT"], "a") as fh:
                fh.write(json.dumps(entry) + "\\n")
        buf = b""
    """
)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """The fake client as `claude`, its input log, the session's transcript; `claude agents` says free."""
    exe = tmp_path / "claude"
    exe.write_text(f"#!{sys.executable}\n{FAKE}")
    exe.chmod(0o755)
    transcript = tmp_path / "projects" / "p" / f"{SID}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("FAKE_INPUT", str(tmp_path / "input"))
    monkeypatch.setenv("FAKE_TRANSCRIPT", str(transcript))
    monkeypatch.setenv("FAKE_MODE", "ok")
    monkeypatch.setattr(attach, "SETTLE_S", 0)
    monkeypatch.setattr(attach, "on_prompt", lambda sid: False)
    (tmp_path / "input").write_bytes(b"")
    return str(exe), tmp_path / "input", transcript


def typed(transcript):
    return [json.loads(line)["message"]["content"] for line in transcript.read_text().splitlines()]


def test_the_text_is_typed_as_pastes_and_sent_once_it_is_drawn(fake):
    exe, keys, transcript = fake
    text = "first line, кириллица 🚀\n" + "x" * 450

    attach.type_into(exe, JOB, SID, text)

    got = keys.read_bytes()
    assert got.endswith(b" \r") and got.count(b"\r") == 1
    assert got.count(b"\x1b[200~") == got.count(b"\x1b[201~") == 4  # one line, then 450 chars in 3 pieces
    assert typed(transcript) == [text]


def test_a_permission_prompt_stops_it_before_typing_and_before_enter(fake, monkeypatch):
    exe, keys, transcript = fake
    monkeypatch.setattr(attach, "on_prompt", lambda sid: True)
    with pytest.raises(SendRefused, match=f"claude attach {JOB}") as before:
        attach.type_into(exe, JOB, SID, "hi")
    assert before.value.key == "app.provider.claude.bg_waiting" and keys.read_bytes() == b""

    answers = iter([False, True])  # the prompt opens while the text is typed
    monkeypatch.setattr(attach, "on_prompt", lambda sid: next(answers))
    with pytest.raises(SendRefused) as after:
        attach.type_into(exe, JOB, SID, "hi")

    assert after.value.key == "app.provider.claude.attach_prompt"
    assert b"hi" in keys.read_bytes() and b"\r" not in keys.read_bytes() and typed(transcript) == []


def test_a_session_claude_agents_no_longer_lists_is_not_typed_into(fake, monkeypatch):
    exe, keys, _ = fake
    monkeypatch.setattr(attach, "on_prompt", lambda sid: None)

    with pytest.raises(SendRefused, match="isn’t running"):
        attach.type_into(exe, JOB, SID, "hi")
    assert keys.read_bytes() == b""


def test_text_that_never_shows_in_the_input_gets_no_enter(fake, monkeypatch):
    exe, keys, _ = fake
    monkeypatch.setenv("FAKE_MODE", "dialog")
    monkeypatch.setattr(attach, "DRAWN_S", 0.5)

    with pytest.raises(SendError, match=f"look at it with claude attach {JOB}"):
        attach.type_into(exe, JOB, SID, "hi")
    assert b"\r" not in keys.read_bytes()


def test_enter_the_transcript_does_not_confirm_is_reported(fake, monkeypatch):
    exe, keys, _ = fake
    monkeypatch.setenv("FAKE_MODE", "lost")
    monkeypatch.setattr(attach, "RECORDED_S", 0.5)

    with pytest.raises(SendTimeout, match="check the chat before sending again"):
        attach.type_into(exe, JOB, SID, "hi")
    assert keys.read_bytes().endswith(b"\r")


def test_a_client_that_exits_says_why(fake, monkeypatch):
    exe, _, _ = fake
    monkeypatch.setenv("FAKE_MODE", "exit")

    with pytest.raises(SendError, match="No background session ab12cd34"):
        attach.type_into(exe, JOB, SID, "hi")


def test_keystrokes_carry_no_control_character_but_tab_and_newline():
    assert attach.keystrokes("a\x03b\tc\x7f\nd") == b"\x1b[200~ab\tc\x1b[201~\n\x1b[200~d\x1b[201~ "
    assert attach.keystrokes("a\n\nb") == b"\x1b[200~a\x1b[201~\n\n\x1b[200~b\x1b[201~ "


def test_only_the_text_as_typed_counts_as_recorded(tmp_path):
    path = tmp_path / "t.jsonl"

    def user(kind, content):
        return {"type": "user", "origin": {"kind": kind}, "message": {"content": content}}

    lines = [user("peer", "hi there"), user("human", "<pasted_content>hi there\n</pasted_content>")]
    path.write_text("".join(json.dumps(e) + "\n" for e in lines))
    assert not attach._recorded(path, 0, "hi there")

    for entry in (
        {"type": "queue-operation", "operation": "enqueue", "content": "hi there"},  # mid-turn
        user("human", "a draft hi  there"),
    ):
        path.write_text(json.dumps(entry) + "\n")
        assert attach._recorded(path, 0, "hi there")
    assert not attach._recorded(path, os.path.getsize(path), "hi there")  # only past the offset


# ---- the route ------------------------------------------------------------------------------------


def background(status="idle", **extra):
    return LiveSession(
        provider="claude", session_id=SID, kind="background", status=status, job_id=JOB, **extra
    )


def snap():
    return claude_routes.Snapshot({}, {}, set(), False)


@pytest.fixture
def routed(monkeypatch):
    """claude on PATH; typing and the courier recorded instead of run."""
    typed_in, couriered = [], []
    monkeypatch.setattr(claude_routes.shutil, "which", lambda n: "/usr/bin/claude")
    monkeypatch.setattr(claude_routes, "snapshot", snap)
    monkeypatch.setattr(attach, "type_into", lambda exe, job, sid, text: typed_in.append((job, text)))
    ok = claude_routes.courier.Delivery(True, msg_id="m1")
    monkeypatch.setattr(claude_routes.courier, "deliver", lambda **kw: couriered.append(kw) or ok)
    claude_routes.plug(None, lambda: {}, lambda sid: [])
    yield typed_in, couriered
    claude_routes.unplug()


def test_a_background_session_free_or_with_a_question_is_typed_into_named_or_not(routed):
    # a job that ended its turn with a question is only "blocked" (`claude agents`, Claude Code 2.1.317)
    for live in (background(), background(name="w"), background("blocked", needs="confirm: merge?")):
        r = claude_routes.route_for(live, "/", False, False, snap())
        assert (r.route, r.can_send, r.target) == ("attach", True, JOB)


def test_files_go_as_mentions_and_a_command_like_text_by_courier(routed):
    typed_in, couriered = routed
    named = Session(SID, "/", live=background(name="w"))

    assert claude_routes.send(named, Message("look", ("/u/a.png",))).route == "attach"
    assert claude_routes.send(named, Message("/clear")).route == "peer"

    assert typed_in == [(JOB, "look\n@/u/a.png")]
    assert couriered[0]["to"] == "w" and couriered[0]["text"].endswith("/clear")
    with pytest.raises(SendRefused, match="would run in the session as a command"):
        claude_routes.send(Session(SID, "/", live=background()), Message("!ls"))


def test_a_signed_message_still_goes_by_courier(routed):
    typed_in, couriered = routed

    sent = claude_routes.send(Session(SID, "/", live=background(name="w")), Message("hi", sender="Group"))

    assert sent.route == "peer" and couriered[0]["sender"] == "Group" and typed_in == []
