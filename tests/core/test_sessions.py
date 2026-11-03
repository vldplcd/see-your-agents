from datetime import UTC, datetime, timedelta

import pytest

from see_your_agents import i18n
from see_your_agents.core.providers.base import LiveSession
from see_your_agents.core.sessions import build_board, build_card, column_for

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def live(status, kind="interactive", **extra):
    return LiveSession(provider="claude", session_id="s", kind=kind, status=status, **extra)


@pytest.mark.parametrize(
    ("item", "column"),
    [
        (live("waiting", waiting_for="permission prompt"), "needs_you"),
        (live("blocked"), "needs_you"),
        (live("idle", kind="background", needs="confirm CI scope"), "needs_you"),
        (live("busy"), "working"),
        (live("shell"), "working"),
        (live("idle"), "your_move"),
    ],
)
def test_live_sessions_sort_by_what_they_need(item, column):
    assert column_for(item, "", NOW) == column


def test_closed_sessions_split_by_age():
    recent = (NOW - timedelta(days=2)).isoformat()
    old = (NOW - timedelta(days=4)).isoformat()

    assert column_for(None, recent, NOW) == "recent"
    assert column_for(None, old, NOW) == "archive"


def test_a_session_marked_done_goes_to_the_archive_even_while_its_process_runs():
    recent = (NOW - timedelta(hours=1)).isoformat()

    assert column_for(None, recent, NOW, is_done=True) == "archive"
    assert column_for(live("busy"), recent, NOW, is_done=True) == "archive"


def card(**rec):
    return build_card({"session_id": "s", "provider": "claude", **rec}, None, "", NOW, done_marks={})


def test_card_peer_is_the_latest_of_sent_and_received_messages():
    sent = {
        "ts": "2026-10-30T10:00:00Z",
        "to": "bob",
        "text": "task",
        "summary": "",
        "msg_id": "a",
        "ok": True,
    }
    got = {"ts": "2026-10-30T11:00:00Z", "from": "alice", "text": "x" * 300, "msg_id": "b"}

    assert card(messages_out=[sent], messages_in=[got])["peer"] == {
        "dir": "in",
        "who": "alice",
        "text": "x" * 160,
        "at": got["ts"],
    }
    assert (
        card(messages_out=[{**sent, "ts": "2026-10-30T12:00:00Z"}], messages_in=[got])["peer"]["dir"] == "out"
    )


def test_card_peer_is_none_without_messages():
    assert card()["peer"] is None


def test_card_peer_skips_a_send_that_never_left():
    refused = {
        "ts": "2026-10-30T12:00:00Z",
        "to": "bob",
        "text": "t",
        "summary": "",
        "msg_id": "",
        "ok": False,
    }
    got = {"ts": "2026-10-30T11:00:00Z", "from": "alice", "text": "hi", "msg_id": "b"}

    assert card(messages_out=[refused], messages_in=[got])["peer"]["who"] == "alice"


def test_the_board_names_its_columns_and_a_fresh_session_in_the_requests_language():
    fresh = {"s": live("busy")}  # live, not indexed yet

    english = build_board([], fresh)
    with i18n.answering("ru"):
        russian = build_board([], fresh)

    assert [c["title"] for c in english["columns"]][:2] == ["Needs you", "Working"]
    assert [c["title"] for c in russian["columns"]][:2] == ["Нужен ты", "Работают"]
    assert (english["cards"][0]["title"], russian["cards"][0]["title"]) == ("New session", "новая сессия")
