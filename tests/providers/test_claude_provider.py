import os

from see_your_agents.core import providers
from see_your_agents.core.providers.base import LiveSession, Session

CLAUDE = providers.by_id("claude")


def test_a_session_copied_into_two_project_folders_is_indexed_from_the_newer_copy(claude_home):
    old, new = claude_home / "projects" / "-a" / "s1.jsonl", claude_home / "projects" / "-b" / "s1.jsonl"
    for path in (old, new):
        path.parent.mkdir(parents=True)
        path.write_text("{}\n")
    os.utime(old, (1, 1))

    assert CLAUDE.transcripts() == {"s1": new}


def test_attach_command_opens_a_background_job_by_its_id_and_any_other_session_by_resume():
    job = LiveSession("s1", "background", "idle", job_id="ab12cd34", provider="claude")
    closed = Session("s1", "/w/my proj")

    assert CLAUDE.attach_command(Session("s1", "/w", live=job)) == "claude attach ab12cd34"
    assert CLAUDE.attach_command(closed) == "cd '/w/my proj' && claude --resume s1"
    assert CLAUDE.attach_command(Session("s1")) == ""
