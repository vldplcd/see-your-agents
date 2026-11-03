"""Joins indexed transcripts with live state into board cards and session details.

Fields that need more than the index and the live state (how to reach a session, say) come from the features
as `extras`: callables (record, live session) -> dict merged into every card. What only the program knows (its
task list, how to open the session in a terminal) comes from the session's provider.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..i18n import request_lang, t
from . import done, providers
from .providers.base import LiveSession, Session, Status

COLUMNS = [  # (id, the catalog key of its title): the board's titles are worded in the request's language
    ("needs_you", "app.column.needs_you"),
    ("working", "app.column.working"),
    ("your_move", "app.column.your_move"),
    ("recent", "app.column.recent"),
    ("archive", "app.column.archive"),
]
RECENT = timedelta(days=3)
WORKING = {Status.BUSY, Status.SHELL, "working"}
NEEDS_YOU = {Status.WAITING, Status.BLOCKED}

CardExtra = Callable[[dict, LiveSession | None], dict]


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def column_for(live: LiveSession | None, last_activity: str, now: datetime, is_done: bool = False) -> str:
    """Live sessions sort by what they need, closed ones age into the archive, «done» goes there at once."""
    if is_done:
        return "archive"
    if live is not None:
        if live.status in NEEDS_YOU or live.needs:
            return "needs_you"
        return "working" if live.status in WORKING else "your_move"
    ts = _parse_ts(last_activity)
    return "recent" if ts and now - ts <= RECENT else "archive"


def jira_base_url(records: list[dict]) -> str:
    if env := os.environ.get("SYA_JIRA_URL"):
        return env.rstrip("/")
    seen = Counter(r["jira_base"] for r in records if r.get("jira_base"))
    return seen.most_common(1)[0][0] if seen else ""


def _task_summary(tasks: list[dict]) -> dict:
    done = sum(t["status"] == "completed" for t in tasks)
    current = next((t for t in tasks if t["status"] == "in_progress"), None)
    return {
        "tasks_done": done,
        "tasks_total": len(tasks),
        "current_task": (current.get("active_form") or current["subject"]) if current else "",
    }


def _latest_peer(rec: dict) -> dict | None:
    """The newest cross-session message of the session, sent or received; a send that has not left
    (waiting for approval, refused, unknown target) is not an exchange."""
    sent = [("out", m["to"], m) for m in rec.get("messages_out", []) if m.get("ok")]
    received = [("in", m["from"], m) for m in rec.get("messages_in", [])]
    if not (both := sent + received):
        return None
    direction, who, m = max(both, key=lambda t: t[2]["ts"])
    return {"dir": direction, "who": who, "text": m["text"][:160], "at": m["ts"]}


def build_card(
    rec: dict,
    live: LiveSession | None,
    base: str,
    now: datetime,
    done_marks: dict[str, str] | None = None,
    extras: Sequence[CardExtra] = (),
) -> dict:
    marks = done.load() if done_marks is None else done_marks
    is_done = done.applies(marks.get(rec["session_id"], ""), rec.get("last_activity", ""))
    provider = providers.by_id(rec["provider"])
    tasks = provider.tasks(Session.of(rec, live)) or rec.get("tasks") or []
    cwd = rec.get("cwd") or (live.cwd if live else "")
    card = {
        "session_id": rec["session_id"],
        "provider": provider.id,
        "title": rec.get("title") or (live.name if live else rec["session_id"][:8]),
        "column": column_for(live, rec.get("last_activity", ""), now, is_done),
        "done": is_done,
        "live": asdict(live) if live else None,
        "project": Path(rec.get("project") or cwd).name,
        "cwd": cwd,
        "branch": rec.get("git_branch", ""),
        "worktree": rec.get("worktree", ""),
        "jira": [{"key": k, "url": f"{base}/browse/{k}" if base else ""} for k in rec.get("jira_keys", [])],
        "prs": rec.get("prs", []),
        "recap": rec.get("recap") or (live.detail if live else ""),
        "recap_at": rec.get("recap_at", ""),
        "last_prompt": rec.get("last_prompt", ""),
        "last_prompt_at": rec.get("last_prompt_at", ""),
        "last_assistant_text": rec.get("last_assistant_text", ""),
        "last_activity": rec.get("last_activity", ""),
        "started_at": rec.get("started_at", ""),
        "pending_tool": rec.get("pending_tool", ""),
        "open_questions": sum(q.get("answer") is None for q in rec.get("questions", [])),
        "peer": _latest_peer(rec),
        "n_plans": len(rec.get("plans", [])),
        "n_agents": len(rec.get("agents", [])),
        "n_prompts": rec.get("n_prompts", 0),
        "n_replies": rec.get("n_replies", 0),
        "cost_usd": rec.get("cost_usd", 0.0),
        "tokens": rec.get("tokens", 0),
        "context_tokens": rec.get("context_tokens", 0),
        "context_window": rec.get("context_window", 0),  # 0: the provider cannot tell
        "transcript_missing": rec.get("transcript_missing", False),
        "continued_in": rec.get("continued_in", ""),
        "tasks": tasks,
        **_task_summary(tasks),
    }
    for extra in extras:
        card.update(extra(rec, live))
    return card


def provider_caps() -> dict[str, dict]:
    """What each registered program can do, for the page to hide the rest; the first is the default one."""
    return {p.id: {"title": p.title, "caps": sorted(p.caps)} for p in providers.PROVIDERS}


def stub_record(live: LiveSession) -> dict:
    """A live session whose transcript is not indexed yet (just started)."""
    return {
        "session_id": live.session_id,
        "provider": live.provider,
        "title": live.name or live.detail[:90] or t(request_lang(), "app.new_session"),
        "cwd": live.cwd,
        "project": live.cwd,
        "last_activity": live.updated_at,
    }


def build_board(records: list[dict], live: dict[str, LiveSession], extras: Sequence[CardExtra] = ()) -> dict:
    now = datetime.now(UTC)
    base = jira_base_url(records)
    by_id = {r["session_id"]: r for r in records}
    for sid, item in live.items():
        by_id.setdefault(sid, stub_record(item))
    marks = done.load()
    cards = [build_card(rec, live.get(sid), base, now, marks, extras) for sid, rec in by_id.items()]
    cards.sort(key=lambda c: c["last_activity"], reverse=True)
    return {
        "generated_at": now.isoformat(),
        "columns": [{"id": cid, "title": t(request_lang(), key)} for cid, key in COLUMNS],
        "recent_days": RECENT.days,
        "jira_base": base,
        "cards": cards,
        "providers": provider_caps(),
    }


def build_detail(
    rec: dict,
    events: list[dict],
    live: LiveSession | None,
    base: str,
    extras: Sequence[CardExtra] = (),
) -> dict:
    plans = [
        {**p, "index": i, "exists": bool(p.get("path")) and Path(p["path"]).is_file()}
        for i, p in enumerate(rec.get("plans", []))
    ]
    return {
        **build_card(rec, live, base, datetime.now(UTC), extras=extras),
        "plans": plans,
        "questions": rec.get("questions", []),
        "agents": rec.get("agents", []),
        "events": events,
        "top_tools": rec.get("top_tools", []),
        "models": rec.get("models", []),
        "lines_added": rec.get("lines_added", 0),
        "lines_removed": rec.get("lines_removed", 0),
        "first_prompt": rec.get("first_prompt", ""),
        "resume_command": providers.by_id(rec["provider"]).attach_command(Session.of(rec, live)),
    }
