import json
from datetime import UTC, datetime, timedelta

import pytest

from see_your_agents.core.providers.claude import live

SID = "84669404-1b11-4451-be13-20f9da2dc863"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(
        live, "_agents_json", lambda: [{"id": "84669404", "kind": "background", "sessionId": SID}]
    )
    return tmp_path


def job(home, transcript_exists: bool, quiet_for: timedelta, **extra) -> None:
    transcript = home / "projects" / "-w" / f"{SID}.jsonl"
    if transcript_exists:
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n")
    state = {
        "state": "blocked",
        "needs": "confirm",
        "linkScanPath": str(transcript),
        "updatedAt": (datetime.now(UTC) - quiet_for).isoformat().replace("+00:00", "Z"),
        **extra,
    }
    (home / "jobs" / "84669404").mkdir(parents=True)
    (home / "jobs" / "84669404" / "state.json").write_text(json.dumps(state))


def test_a_background_job_whose_transcript_was_deleted_is_not_live(home):
    job(home, transcript_exists=False, quiet_for=timedelta(days=95))
    assert live.read_live(home) == {}


def test_a_background_job_with_its_transcript_is_live(home):
    job(home, transcript_exists=True, quiet_for=timedelta(days=95))
    assert live.read_live(home)[SID].needs == "confirm"


def test_a_background_job_is_addressed_by_its_name_from_the_job_state(home):
    job(home, transcript_exists=True, quiet_for=timedelta(minutes=1), name="w", nameSource="user")
    session = live.read_live(home)[SID]
    assert (session.name, session.address, session.job_id) == ("w", "w", "84669404")


def test_a_name_claude_code_made_up_is_no_address(home):
    job(home, transcript_exists=True, quiet_for=timedelta(minutes=1), name="bg-1f2e", nameSource="auto")
    session = live.read_live(home)[SID]
    assert (session.name, session.address) == ("bg-1f2e", "")


def test_a_just_started_job_without_a_transcript_yet_is_live(home):
    job(home, transcript_exists=False, quiet_for=timedelta(minutes=1))
    assert SID in live.read_live(home)


def test_a_courier_is_not_shown(tmp_path, monkeypatch):
    monkeypatch.setenv("SYA_CACHE_DIR", str(tmp_path / "cache"))
    agents = [
        {"kind": "interactive", "sessionId": "courier", "cwd": str(tmp_path / "cache" / "courier")},
        {"kind": "interactive", "sessionId": "worker", "cwd": str(tmp_path / "w")},
    ]
    monkeypatch.setattr(live, "_agents_json", lambda: agents)
    assert list(live.read_live(tmp_path)) == ["worker"]
