import json
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeApp, no_processes

from see_your_agents.core.providers.base import LiveSession
from see_your_agents.features.supervisor import fleet

NOW = datetime.now(UTC)


def rec(sid, title, minutes_ago, **extra):
    ts = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "session_id": sid,
        "title": title,
        "cwd": "/w/proj",
        "project": "/w/proj",
        "last_activity": ts,
        **extra,
    }


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SYA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SYA_CACHE_DIR", str(tmp_path / "cache"))
    no_processes(monkeypatch)


def live(sid, status, **extra):
    return LiveSession(
        provider="claude", session_id=sid, kind="interactive", status=status, cwd="/w/proj", **extra
    )


@pytest.fixture
def app(tmp_path):
    records = [
        rec("old", "Old work", 60 * 24 * 10),
        rec("closed", "Closed lately", 60 * 24),
        rec("idle", "Idle one", 5, last_prompt="p" * 400, recap="r" * 500),
        rec("busy", "Busy one", 2, jira_keys=["PROJ-1"]),
        rec("ask", "Needs me", 1),
    ]
    sessions = {
        "idle": live("idle", "idle", name="idler"),
        "busy": live("busy", "busy", name="worker"),
        "ask": live("ask", "waiting", name="asker", waiting_for="permission"),
    }
    return FakeApp(records, sessions)


def test_overview_orders_by_column_and_compacts(app):
    out = fleet.overview(app)

    assert out["note"] == fleet.DATA_NOTE
    assert [s["id"] for s in out["sessions"]] == ["ask", "busy", "idle", "closed"]
    ask, busy, idle, closed = out["sessions"]
    assert ask["name"] == "asker" and ask["status"] == "waiting (permission)"
    assert busy["jira"] == ["PROJ-1"]
    assert len(idle["recap"]) == 200 and len(idle["last_prompt"]) == 160
    assert "task" not in idle and "peer" not in idle  # empty fields are dropped
    assert "name" not in closed  # not live: no address


def test_overview_archive_and_project_filter(app):
    assert [s["id"] for s in fleet.overview(app, include_archive=True)["sessions"]][-1] == "old"
    assert fleet.overview(app, project="other")["sessions"] == []


def test_delegations_join_sender_and_receiver_by_msg_id(app):
    app.index._records["idle"]["messages_out"] = [
        {
            "ts": "2026-11-01T10:00:00Z",
            "to": "worker",
            "summary": "do",
            "text": "full text",
            "msg_id": "m1",
            "ok": True,
        },
        {
            "ts": "2026-11-01T11:00:00Z",
            "to": "ghost",
            "summary": "lost",
            "text": "t",
            "msg_id": "m2",
            "ok": True,
        },
        {
            "ts": "2026-11-01T12:00:00Z",
            "to": "worker",
            "summary": "",
            "text": "refused",
            "msg_id": "",
            "ok": False,
        },
        {
            "ts": "2026-11-01T12:30:00Z",
            "to": "worker",
            "summary": "",
            "text": "awaits approval",
            "msg_id": "",
            "ok": None,
        },
    ]
    app.index._records["busy"]["messages_in"] = [
        {"ts": "2026-11-01T10:00:05Z", "from": "idler", "text": "full text", "msg_id": "m1"}
    ]

    items = fleet.delegations(app)["delegations"]

    assert [i["text"] for i in items] == ["t", "full text"]  # newest first
    assert items[0]["delivered"] is False and "to_session" not in items[0]
    assert items[1]["to_session"] == "busy" and items[1]["delivered"] is True
    assert items[1]["from_session"] == "idle" and items[1]["from_title"] == "Idle one"
    assert len(fleet.delegations(app, session_id="busy")["delegations"]) == 1


def test_delegations_since_compares_times_not_strings(app):
    app.index._records["idle"]["messages_out"] = [
        {"ts": "2026-11-01T10:01:00Z", "to": "w", "summary": "", "text": "t", "msg_id": "m", "ok": True}
    ]

    assert len(fleet.delegations(app, since="2026-11-01T12:30:00+03:00")["delegations"]) == 1  # 09:30Z
    assert fleet.delegations(app, since="2026-11-01T10:30:00Z")["delegations"] == []
    with pytest.raises(ValueError):
        fleet.delegations(app, since="yesterday")


def test_detail_includes_messages_and_unknown_is_none(app):
    app.index._records["busy"]["messages_in"] = [{"ts": "t", "from": "x", "text": "hi", "msg_id": "m"}]

    out = fleet.detail(app, "busy")

    assert out["name"] == "worker" and out["messages_in"][0]["msg_id"] == "m"
    assert fleet.detail(app, "nope") is None


def test_codex_sessions_come_through_with_their_provider_and_no_route(app):
    app.index._records["cx"] = rec("cx", "Codex work", 3, provider="codex")
    app.fake_live["cx"] = LiveSession(provider="codex", session_id="cx", kind="interactive", status="busy")

    seen = {s["id"]: (s["provider"], s.get("status")) for s in fleet.overview(app)["sessions"]}
    detail = fleet.detail(app, "cx")

    assert (seen["cx"], seen["busy"]) == (("codex", "busy"), ("claude", "busy"))
    assert detail["provider"] == "codex" and "delivery" not in detail  # nothing reaches it yet


def test_digest_round_trip(tmp_path):
    assert fleet.read_digest() == {}

    fleet.write_digest("x" * 3000, "sess")
    got = fleet.read_digest()

    assert len(got["text"]) == fleet.DIGEST_LIMIT and got["session_id"] == "sess"
    assert json.loads((tmp_path / "config" / "digest.json").read_text())["at"] == got["at"]
