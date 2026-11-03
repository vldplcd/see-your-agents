"""Digest of one Codex rollout: ~/.codex/sessions/YYYY/MM/DD/rollout-<local time>-<thread id>.jsonl, or the
flat archived_sessions/ that `codex archive` moves it to.

Each line is {timestamp, type, payload}. The format is internal to Codex and changes between releases, so an
unknown line is skipped and a missing field stays empty. What counts (docs/codex-cli-notes.md):
- identity, cwd, branch: the FIRST session_meta; a resumed file repeats it, a fork or a subagent replays its
  parent's after it (`Replay` below);
- prompts: event_msg/item_completed with a UserMessage, or event_msg/user_message (CLIs that wrote no items),
  never response_item/message role=user, which also carries harness text (environment_context, AGENTS.md);
- answers: AgentMessage items or event_msg/agent_message; the last answer is task_complete.last_agent_message;
- tokens: the last event_msg/token_count total. Codex records no cost.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from ...jira import BROWSE_URL, jira_keys_in
from ..base import Digest

THREAD_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ROLLOUT = re.compile(rf"rollout-.+-({THREAD_ID})\.jsonl(\.zst)?")
UUID7 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}")
# The VS Code extension wraps a prompt in the editor's context; the request is the part after this heading.
IDE_CONTEXT = "# Context from my IDE setup:"
IDE_REQUEST = re.compile(r"^## My request(?: for Codex)?:", re.M)  # the 0.154 alpha builds drop "for Codex"
TEXT_LIMIT = 1500
# Bump when the digest changes: the index then re-parses every Codex rollout still on disk.
PARSER_VERSION = 5


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def payload(entry: dict) -> dict:
    p = entry.get("payload")
    return p if isinstance(p, dict) else {}


def event_type(entry: dict) -> str:
    """The event_msg's own type ("task_started"…), "" for any other line."""
    return str(payload(entry).get("type") or "") if entry.get("type") == "event_msg" else ""


def uuid7_ms(value: object) -> int | None:
    """The creation time a UUIDv7 carries (Codex thread and turn ids); None for anything else."""
    if not isinstance(value, str) or not UUID7.fullmatch(value):
        return None
    return int(value.replace("-", "")[:12], 16)


def iso_ms(value: object) -> int | None:
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def request_text(text: str) -> str:
    """What the human asked: the VS Code extension's editor context dropped."""
    if text.lstrip().startswith(IDE_CONTEXT) and (m := IDE_REQUEST.search(text)):
        return text[m.end() :].strip()
    return text.strip()


def _item(entry: dict, kind: str) -> tuple[dict, str] | None:
    """(item, its text) of an item_completed line whose item is of this kind."""
    item = payload(entry).get("item")
    if event_type(entry) != "item_completed" or not isinstance(item, dict) or item.get("type") != kind:
        return None
    parts = [c for c in item.get("content") or [] if isinstance(c, dict) and c.get("text")]
    return item, "\n".join(str(c["text"]) for c in parts)


def prompt_of(entry: dict) -> str | None:
    """The human's prompt, as typed, if the line is one."""
    if event_type(entry) == "user_message":
        return str(payload(entry).get("message") or "")
    found = _item(entry, "UserMessage")
    return found[1] if found else None


def queue_turn(entry: dict) -> str | None:
    """The turn id of a task_started line that `codex queue` started (`turn_trigger` "queue"; "" when it names
    no id); None for any other line."""
    p = payload(entry)
    attribution = p.get("turn_attribution")
    if event_type(entry) != "task_started" or not isinstance(attribution, dict):
        return None
    if attribution.get("turn_trigger") != "queue":
        return None
    return str(p.get("turn_id") or attribution.get("turn_id") or "")


def answer_of(entry: dict) -> tuple[str, bool] | None:
    """(text, final) of the agent's message, if the line is one; old CLIs mark no phase: theirs are final."""
    p = payload(entry)
    if event_type(entry) == "agent_message":
        return str(p.get("message") or ""), p.get("phase") != "commentary"
    found = _item(entry, "AgentMessage")
    return (found[1], found[0].get("phase") != "commentary") if found else None


class Replay:
    """Tells the thread's own lines from the parent history that a fork or a subagent replays first.

    Such a file starts with its own session_meta, then the parent's meta and the parent's lines up to the
    fork, stamped with the fork time, then the thread's own first turn. A turn is the thread's own when it
    began after the thread was created: its turn id is a UUIDv7 at or after the thread id's time (older CLIs,
    whose turn ids are "rollout-N", give `started_at` in seconds). A turn whose start cannot be told counts as
    the thread's own.
    """

    def __init__(self) -> None:
        self.meta: dict = {}
        self.created_ms: int | None = None
        self.replaying = False
        self.turns = 0  # own turns begun

    def own(self, entry: dict) -> bool:
        """Whether the line is the thread's own; session_meta lines are never content."""
        p = payload(entry)
        if entry.get("type") == "session_meta":
            if not self.meta:
                self.meta = p
                self.created_ms = uuid7_ms(p.get("id")) or iso_ms(p.get("timestamp"))
                self.replaying = bool(p.get("forked_from_id"))
            elif p.get("id") != self.meta.get("id") and not self.turns:
                self.replaying = True  # the parent's meta: its history follows
            return False
        if event_type(entry) == "task_started":
            if self.replaying and not self._after_creation(p):
                return False
            self.replaying = False
            self.turns += 1
            return True
        return not self.replaying

    def _after_creation(self, turn: dict) -> bool:
        began, slack = uuid7_ms(turn.get("turn_id")), 0
        if began is None and isinstance(turn.get("started_at"), int | float):
            began, slack = int(turn["started_at"] * 1000), 999  # whole seconds
        return began is None or self.created_ms is None or began + slack >= self.created_ms


def _not_a_session(meta: dict) -> bool:
    """A thread that is not a session of its own: any subagent (`source.subagent`: one the agent spawned,
    which its parent lists, or Codex's approval reviewer), or the title an old VS Code extension asked
    `codex exec` for."""
    source = meta.get("source")
    if (isinstance(source, dict) and "subagent" in source) or meta.get("thread_source") == "guardian_review":
        return True
    return source == "exec" and meta.get("originator") == "codex_vscode"


def first_meta(path: Path) -> dict:
    """The rollout's first line, if it is a session_meta: the thread's identity. {} for anything else."""
    if path.suffix == ".zst":
        return {}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            entry = json.loads(fh.readline())
    except (OSError, ValueError):
        return {}
    return payload(entry) if isinstance(entry, dict) and entry.get("type") == "session_meta" else {}


_hidden: dict[Path, bool] = {}  # rollout -> not a session; the first line never changes


def is_hidden(path: Path) -> bool:
    """Whether the rollout's thread is not a session of its own (`_not_a_session`): the board shows it
    neither indexed nor live. Subagents appear under their parent, as Claude's do."""
    if path not in _hidden:
        meta = first_meta(path)
        if not meta:
            return False  # half-written or unreadable: ask again next time
        _hidden[path] = _not_a_session(meta)
    return _hidden[path]


def _rollouts(home: Path, pattern: str = "rollout-*") -> Iterator[tuple[str, Path]]:
    """(thread id, path): sessions/ before archived_sessions/, and x.jsonl before x.jsonl.zst (sorted)."""
    for folder in (home / "sessions", home / "archived_sessions"):
        for path in sorted(folder.rglob(pattern)):
            if m := ROLLOUT.fullmatch(path.name):
                yield m[1], path


def transcripts(home: Path) -> dict[str, Path]:
    """Rollouts by thread id, hidden threads left out; a thread found twice keeps the first (live, plain)."""
    found: dict[str, Path] = {}
    for tid, path in _rollouts(home):
        found.setdefault(tid, path)
    return {tid: path for tid, path in found.items() if not is_hidden(path)}


def find_rollout(home: Path, tid: str) -> Path | None:
    """One thread's rollout, by its id: what the live threads need between two index refreshes."""
    return next((path for _, path in _rollouts(home, f"rollout-*-{tid}.jsonl*")), None)


def thread_id(path: Path) -> str:
    m = ROLLOUT.fullmatch(path.name)
    return m[1] if m else path.stem


class _Parser:
    def __init__(self, digest: Digest):
        self.d = digest
        self.replay = Replay()
        self.tools: Counter[str] = Counter()
        self.prompt_keys: Counter[str] = Counter()
        self.open_calls: dict[str, str] = {}  # call_id -> tool name, until its output arrives
        self.turn_prompts: set[str] = set()  # a prompt written twice in a turn (item, event) is one
        self.inherited_tokens = 0  # the parent's total a fork's own token counts start from
        self.total_tokens = 0
        self.queued: dict | None = None  # the open turn's queued_turns entry, when the queue started it

    def event(self, ts: str, kind: str, text: str) -> None:
        self.d.events.append({"ts": ts, "kind": kind, "text": _clip(text)})

    def feed(self, entry: dict, raw: str) -> None:
        d = self.d
        if "/browse/" in raw and not d.jira_base and (m := BROWSE_URL.search(raw)):
            d.jira_base = m.group(1)
        if not self.replay.own(entry):
            if event_type(entry) == "token_count":
                self.inherited_tokens = self.tokens_in(entry) or self.inherited_tokens
            return
        ts = str(entry.get("timestamp") or "")
        if ts:
            d.last_activity = max(d.last_activity, ts)
        kind, p = entry.get("type"), payload(entry)
        if kind == "turn_context":
            self.on_turn_context(p)
        elif kind == "event_msg":
            self.on_event(entry, p, ts)
        elif kind == "response_item":
            self.on_item(p, ts)
        elif kind == "compacted":
            self.event(ts, "compact", "")  # the kind is the marker: the page words it (app.event.compacted)

    def on_turn_context(self, p: dict) -> None:
        if isinstance(p.get("model"), str) and p["model"] not in self.d.models:
            self.d.models.append(p["model"])
        if isinstance(p.get("cwd"), str) and p["cwd"]:
            self.d.cwd = p["cwd"]

    def on_event(self, entry: dict, p: dict, ts: str) -> None:
        d, kind = self.d, p.get("type")
        if kind in ("task_started", "task_complete", "turn_aborted"):  # a queue will not start after an abort
            d.last_turn_aborted = kind == "turn_aborted"
        if kind == "task_started":
            self.turn_prompts.clear()
            self.on_turn(queue_turn(entry), ts)
        elif kind == "task_complete" and isinstance(last := p.get("last_agent_message"), str) and last:
            d.last_assistant_text = _clip(last, 800)
            self.on_reply(last, ts)
        elif kind == "token_count":
            self.total_tokens = self.tokens_in(entry) or self.total_tokens
        elif (text := prompt_of(entry)) is not None:
            self.on_prompt(request_text(text), ts)
        elif (answer := answer_of(entry)) is not None and answer[0].strip():
            d.n_replies += 1
            if answer[1]:
                d.last_assistant_text = _clip(answer[0], 800)
            self.on_reply(answer[0], ts)

    def on_turn(self, queued_id: str | None, ts: str) -> None:
        """A turn begins: a queue-started one is a message from outside, its answer found by the turn id."""
        self.queued = None
        if queued_id:
            self.queued = {"ts": ts, "msg_id": queued_id, "reply": "", "reply_at": ""}
            self.d.queued_turns.append(self.queued)

    def on_reply(self, text: str, ts: str) -> None:
        if self.queued is not None:
            self.queued.update(reply=_clip(text, 800), reply_at=ts)

    def on_prompt(self, text: str, ts: str) -> None:
        if not text or text in self.turn_prompts:
            return
        self.turn_prompts.add(text)
        d = self.d
        d.n_prompts += 1
        d.first_prompt = d.first_prompt or _clip(text, 400)
        d.last_prompt, d.last_prompt_at = _clip(text, 400), ts
        self.prompt_keys.update(jira_keys_in(text))
        self.event(ts, "prompt", text)

    def on_item(self, p: dict, ts: str) -> None:
        kind = p.get("type")
        if kind in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
            name = str(p.get("name") or ("web_search" if kind == "web_search_call" else "shell"))
            self.d.n_tool_calls += 1
            self.tools[name] += 1
            if p.get("call_id") and kind != "web_search_call":
                self.open_calls[str(p["call_id"])] = name
            if name == "spawn_agent":
                self.on_spawn(p, ts)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            self.open_calls.pop(str(p.get("call_id")), None)

    def on_spawn(self, p: dict, ts: str) -> None:
        """A subagent, named by its task: the task's text (`message`) is stored encrypted. No task name is
        "", which the page words (app.event.subagent)."""
        try:
            args = json.loads(p.get("arguments") or "{}")
        except ValueError:
            args = {}
        task = _clip(str(args.get("task_name") or "") if isinstance(args, dict) else "", 200)
        self.d.agents.append({"ts": ts, "description": task, "type": "", "background": True})
        self.event(ts, "agent", task)

    @staticmethod
    def tokens_in(entry: dict) -> int:
        info = payload(entry).get("info")
        usage = info.get("total_token_usage") if isinstance(info, dict) else None
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        return total if isinstance(total, int) else 0

    def finish(self) -> Digest:
        d, meta = self.d, self.replay.meta
        git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
        d.project = str(meta.get("cwd") or d.cwd)
        d.cwd = d.cwd or d.project
        d.git_branch = str(git.get("branch") or "")
        d.started_at = str(meta.get("timestamp") or "") or d.last_activity
        d.last_activity = d.last_activity or d.started_at
        d.tokens = max(0, self.total_tokens - self.inherited_tokens)
        ranked = [k for k, _ in self.prompt_keys.most_common()]
        ranked += jira_keys_in(f"{d.git_branch} {Path(d.cwd).name}", any_case=True)
        d.jira_keys = list(dict.fromkeys(ranked))[:6]
        d.top_tools = [[n, c] for n, c in self.tools.most_common(8)]
        d.pending_tool = next(reversed(self.open_calls.values()), "") if self.open_calls else ""
        return d


def parse_rollout(path: Path) -> Digest:
    """The rollout as a Digest. Its name lives outside it (names.py), applied when records are built. A
    compressed rollout is not read: ValueError, and the index counts it as unparsable until it changes."""
    if path.suffix == ".zst":
        raise ValueError("a compressed rollout (.jsonl.zst) cannot be read yet")
    parser = _Parser(Digest(session_id=thread_id(path), path=str(path)))
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue  # the last line may be half-written while the thread runs
            if isinstance(entry, dict):
                parser.feed(entry, raw)
    return parser.finish()
