"""Synthetic Codex rollouts in the shapes docs/codex-cli-notes.md records: no line comes from a real ~/.codex.

Thread and turn ids are UUIDv7, so their first 48 bits are a time: `uid(minute)` makes ids that sort by it.
"""

import json
from pathlib import Path

DAY = "2026-11-03"


def uid(minute: int, tail: str = "0") -> str:
    """A UUIDv7 created `minute` minutes into DAY (any day works: only the order matters)."""
    ms = 1_793_664_000_000 + minute * 60_000
    hexed = f"{ms:012x}"
    return f"{hexed[:8]}-{hexed[8:]}-7000-8000-{tail * 12}"


def ts(minute: int) -> str:
    return f"{DAY}T{minute // 60:02d}:{minute % 60:02d}:00.000Z"


def line(kind: str, payload: dict, minute: int = 0) -> dict:
    return {"timestamp": ts(minute), "type": kind, "payload": payload}


def meta(thread: str, minute: int = 0, **extra) -> dict:
    payload = {
        "id": thread,
        "session_id": thread,
        "timestamp": ts(minute),
        "cwd": "/work/app",
        "originator": "codex-tui",
        "cli_version": "0.166.0",
        "source": "cli",
        "git": {"branch": "PROJ-42-fix", "commit_hash": "abc"},
        **extra,
    }
    return line("session_meta", payload, minute)


def event(kind: str, minute: int = 0, **fields) -> dict:
    return line("event_msg", {"type": kind, **fields}, minute)


def turn_started(turn: str, minute: int = 0, **fields) -> dict:
    return event("task_started", minute, turn_id=turn, **fields)


def queue_started(turn: str, minute: int = 0) -> dict:
    """A turn the daemon started for a message `codex queue` handed it."""
    return turn_started(turn, minute, turn_attribution={"turn_id": turn, "turn_trigger": "queue"})


def turn_complete(turn: str, answer: str | None = None, minute: int = 0) -> dict:
    return event("task_complete", minute, turn_id=turn, last_agent_message=answer)


def turn_context(model: str = "gpt-6-luna", minute: int = 0, cwd: str = "/work/app") -> dict:
    return line("turn_context", {"model": model, "cwd": cwd, "approval_policy": "on-request"}, minute)


def user_item(text: str, minute: int = 0) -> dict:
    item = {"type": "UserMessage", "id": "u", "content": [{"type": "text", "text": text}]}
    return event("item_completed", minute, item=item)


def agent_item(text: str, phase: str | None = "final_answer", minute: int = 0) -> dict:
    item = {"type": "AgentMessage", "id": "a", "content": [{"type": "Text", "text": text}], "phase": phase}
    return event("item_completed", minute, item=item)


def harness_text(text: str, minute: int = 0) -> dict:
    """What Codex itself puts in the user role (environment_context, AGENTS.md): never a prompt."""
    return line(
        "response_item",
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
        minute,
    )


def tokens(total: int, minute: int = 0) -> dict:
    return event("token_count", minute, info={"total_token_usage": {"total_tokens": total}})


def call(call_id: str, name: str, minute: int = 0, **args) -> dict:
    return line(
        "response_item",
        {"type": "function_call", "name": name, "call_id": call_id, "arguments": json.dumps(args)},
        minute,
    )


def patch(call_id: str, *files: str, minute: int = 0) -> dict:
    body = (
        "*** Begin Patch\n" + "".join(f"*** Update File: {f}\n@@\n-a\n+b\n" for f in files) + "*** End Patch"
    )
    return line(
        "response_item",
        {"type": "custom_tool_call", "name": "apply_patch", "call_id": call_id, "input": body},
        minute,
    )


def output(call_id: str, text: str, minute: int = 0, custom: bool = False) -> dict:
    kind = "custom_tool_call_output" if custom else "function_call_output"
    return line("response_item", {"type": kind, "call_id": call_id, "output": text}, minute)


def dump(*entries) -> str:
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)


def write(home: Path, thread: str, *entries, archived: bool = False) -> Path:
    """The rollout where Codex keeps it under `home` (CODEX_HOME)."""
    folder = home / "archived_sessions" if archived else home / "sessions" / "2026" / "11" / "03"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{DAY}T10-00-00-{thread}.jsonl"
    path.write_text(dump(*entries))
    return path


def fork_rollout(parent: str, child: str, at: int) -> list[dict]:
    """A thread forked at minute `at`: its meta, then the parent's history replayed (meta, a finished turn,
    the turn still open at the fork; all stamped `at`), then its own turn. The shape of the subagent files
    in docs/codex-cli-notes.md, whose replay rule a fork shares."""
    return [
        meta(child, at, forked_from_id=parent),
        meta(parent, at),
        turn_started(uid(1, "a"), at),
        user_item("parent's first ask", at),
        agent_item("parent's answer", minute=at),
        tokens(1000, at),
        turn_complete(uid(1, "a"), "parent's answer", at),
        turn_started(uid(2, "b"), at),
        user_item("parent asks to review", at),
        tokens(1500, at),
        turn_started(uid(at, "c"), at),
        user_item("the fork's ask", at),
        call("c1", "exec_command", at, cmd="uv run pytest"),
        output("c1", "Chunk ID: 1\nWall time: 1 seconds\nProcess exited with code 0", at),
        agent_item("CI is green", minute=at),
        tokens(1800, at),
        turn_complete(uid(at, "c"), "CI is green", at),
    ]
