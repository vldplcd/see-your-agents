"""What is running right now.

Primary source is `claude agents --json`: the CLI's scripting interface, which lists interactive
and background sessions with their state (busy / idle / waiting + waitingFor / blocked).
The per-process registry ~/.claude/sessions/<pid>.json adds the time the status last changed;
~/.claude/jobs/<id>/state.json adds what a background agent needs from you.

Couriers (courier.py) are sessions too; they live a few seconds in their own folder and are not shown.

A background job outlives its transcript: Claude Code's cleanup (cleanupPeriodDays) deletes the transcript,
while `claude agents` keeps listing the job as blocked. Such a job can be neither attached nor resumed, so
it is not live.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..base import Kind, LiveSession, Status, TaskItem
from . import ID, courier

ORPHAN_AFTER = timedelta(hours=1)  # a just-started job may point at a transcript not written yet


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _iso_ms(ms: object) -> str:
    if not isinstance(ms, int | float):
        return ""
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat().replace("+00:00", "Z")


def _agents_json() -> list[dict] | None:
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "agents", "--json"], capture_output=True, text=True, timeout=10, check=True
        )
        data = json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _orphaned(job: dict) -> bool:
    """The job's transcript is gone and the job has been quiet long enough to rule out a fresh start."""
    transcript = job.get("linkScanPath")
    try:
        updated = datetime.fromisoformat(str(job.get("updatedAt")).replace("Z", "+00:00"))
    except ValueError:
        return False
    stale = updated.tzinfo is not None and datetime.now(UTC) - updated > ORPHAN_AFTER
    return bool(transcript) and stale and not Path(transcript).exists()


def registry_by_pid(home: Path) -> dict[int, dict]:
    return {d["pid"]: d for f in (home / "sessions").glob("*.json") if (d := _read_json(f)).get("pid")}


def read_live(home: Path) -> dict[str, LiveSession]:
    """LiveSession state keyed by session id; empty when the CLI is unavailable."""
    agents = _agents_json()
    if agents is None:
        return {}
    registry = registry_by_pid(home)
    couriers = str(courier.workdir())
    live: dict[str, LiveSession] = {}
    for a in agents:
        sid = a.get("sessionId")
        if not sid or a.get("cwd") == couriers:
            continue
        reg = registry.get(a.get("pid"), {})
        item = LiveSession(
            provider=ID,
            session_id=sid,
            kind=a.get("kind", ""),
            status=a.get("status") or a.get("state") or "",
            waiting_for=a.get("waitingFor") or reg.get("waitingFor") or "",
            name=a.get("name") or reg.get("name") or "",
            name_source=a.get("nameSource") or reg.get("nameSource") or "",
            pid=a.get("pid"),
            cwd=a.get("cwd", ""),
            status_since_ms=reg.get("statusUpdatedAt"),
            updated_at=_iso_ms(reg.get("updatedAt")),
        )
        if item.kind == Kind.BACKGROUND and (
            job := _read_json(home / "jobs" / a.get("id", "") / "state.json")
        ):
            if _orphaned(job):
                continue
            item.job_id = a.get("id", "")
            item.name = item.name or job.get("name") or ""  # `-n` of `claude --bg`: the courier's address
            item.name_source = item.name_source or job.get("nameSource") or ""
            item.needs = job.get("needs") or ""
            item.detail = job.get("detail") or ""
            item.suggested_reply = job.get("suggestedReply") or ""
            item.updated_at = job.get("updatedAt") or item.updated_at
        live[sid] = item
    return live


def on_prompt(session_id: str) -> bool | None:
    """`claude agents`, asked now, has the session on a permission prompt or a dialog; None: it does not list
    the session. A background job that ended its turn with a question is only "blocked": text answers it."""
    found = next((a for a in _agents_json() or () if a.get("sessionId") == session_id), None)
    if found is None:
        return None
    status = found.get("status") or found.get("state") or ""
    background = found.get("kind") in ("bg", Kind.BACKGROUND)
    waiting = {Status.WAITING} if background else {Status.WAITING, Status.BLOCKED}
    return bool(found.get("waitingFor")) or status in waiting


def read_tasks(home: Path, session_id: str) -> list[TaskItem] | None:
    """The session's task list as Claude Code keeps it on disk, if it has one."""
    folder = home / "tasks" / session_id
    if not folder.is_dir():
        return None
    tasks = []
    for f in folder.glob("*.json"):
        t = _read_json(f)
        if t.get("id"):
            tasks.append(
                {
                    "id": str(t["id"]),
                    "subject": t.get("subject", ""),
                    "active_form": t.get("activeForm", ""),
                    "status": t.get("status", "pending"),
                }
            )
    return sorted(tasks, key=lambda t: int(t["id"]) if t["id"].isdigit() else 0)
