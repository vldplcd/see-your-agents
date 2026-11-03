"""Digest of one Claude Code transcript: ~/.claude/projects/<project>/<session-id>.jsonl.

The transcript format is internal to Claude Code and changes between releases, so every
field is read defensively: an unknown entry type is skipped, a missing field stays empty.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from ...jira import BROWSE_URL, jira_keys_in
from ..base import Digest

# Bulky entries that carry nothing for the overview; skipped before json.loads.
SKIP_PREFIXES = (
    '{"type":"api-request',
    '{"type":"file-history',
    '{"type":"atis-latch"',
    '{"type":"mode"',
    '{"type":"permission-mode"',
    '{"type":"queue-operation"',
    '{"type":"last-prompt"',
)
# Wrappers Claude Code puts into user-role messages that the human did not type.
SYSTEM_WRAPPERS = (
    "<local-command",
    "<system-reminder",
    "<task-notification",
    "<bash-",
    "<user-prompt-submit-hook",
    "<command-message",
)
COMMAND = re.compile(r"<command-name>(.*?)</command-name>(?:.*?<command-args>(.*?)</command-args>)?", re.S)
TAG = re.compile(r"</?[a-z_]+(?:\s[^>]*)?>")
RECAP_SUFFIX = re.compile(r"\s*\(disable recaps in /config\)\s*$")
MDX_TOKEN = re.compile(r"[\w./~\-]+\.mdx")
TASK_ID = re.compile(r"#(\d+)")
# SendMessage also continues subagents, addressed by agent id; anything else is a session name.
AGENT_ID = re.compile(r"a(?:fork-)?[0-9a-f]{16}")
CROSS_SESSION = "another Claude session"
FROM_NAME = re.compile(r'<cross-session-message\b[^>]*\bfrom-name="([^"]*)"')
TEXT_LIMIT = 1500
# Bump when the digest changes: the index then re-parses every Claude transcript still on disk.
PARSER_VERSION = 10
# What a request carried into the context window: its input, cache writes and cache reads (`usage` of the
# reply it got). Claude 5 and later have a 1M window only; an older model has 200k unless Claude Code ran it
# with the `[1m]` suffix. A context past 200k proves the 1M window whatever the model id says.
CONTEXT_USAGE = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
WINDOW, WINDOW_1M = 200_000, 1_000_000
MODEL_MAJOR = re.compile(r"claude-[a-z]+-(\d+)(?:-|\[|$)")  # claude-opus-5-5, claude-haiku-4-5-20251001
WEB_URL = re.compile(r"https?://", re.IGNORECASE)  # a PR/MR link must be one of these


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def human_prompt(entry: dict) -> str | None:
    """Text the human typed (or the slash command they ran), or None for machine-made user entries."""
    if entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("toolUseResult") is not None:
        return None
    origin = entry.get("origin") or {}
    if origin and origin.get("kind") != "human":
        return None
    if not origin and entry.get("promptSource") == "system":
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    if not isinstance(content, str) or not content.strip():
        return None
    if m := COMMAND.search(content):
        return f"{m.group(1)} {m.group(2) or ''}".strip()
    if content.lstrip().startswith(SYSTEM_WRAPPERS):
        return None
    return TAG.sub("", content).strip() or None


def is_cross_session_send(to: object) -> bool:
    """SendMessage target looks like a session name, not a subagent id (before the result confirms it)."""
    return isinstance(to, str) and bool(to) and not AGENT_ID.fullmatch(to)


def send_outcome(block: dict, result: object) -> tuple[bool, bool | None]:
    """(ok, cross_session) of a SendMessage result; cross_session is None when the result does not say."""
    res = result if isinstance(result, dict) else {}
    ok = res.get("success") is not False and not block.get("is_error")
    said = f"{res.get('message', '')} {res.get('display', '')}"
    return ok, (True if CROSS_SESSION in said else (False if ok and said.strip() else None))


def is_session_message(e: dict) -> bool:
    """A message from another live session; subagent hand-backs share the origin kind but carry a task id."""
    origin = e.get("origin") or {}
    return bool(e.get("isMeta")) and origin.get("kind") == "peer" and not origin.get("senderTaskId")


# Put before a chat message the courier delivers: without it an agent tends to answer "Dashboard" with
# SendMessage, but the courier is gone by then and the answer lands in some other session. A bracketed first
# line, like a group's header: the haiku courier copies that verbatim, while a trailing note in parentheses
# read to it as an aside and was dropped, so its guard refused every send.
REPLY_NOTE = (
    "[From the see-your-agents dashboard. Answer in plain text in this session: the dashboard shows the "
    'answer in its chat. No need to SendMessage "Dashboard".]\n'
)
# The note as it was before it was English: older transcripts carry it, and the chat still strips it.
OLD_REPLY_NOTE = (
    "[Из дашборда see-your-agents. Ответь обычным текстом в этой сессии: дашборд покажет ответ в чате. "
    "SendMessage «Дашборд» не нужен.]\n"
)


def _without_note(body: str) -> str:
    return body.removeprefix(REPLY_NOTE).removeprefix(OLD_REPLY_NOTE)


def peer_text(origin: dict) -> str:
    """The message body without the reply note (REPLY_NOTE, or OLD_REPLY_NOTE in an older transcript);
    several queued messages from one sender arrive joined, each body under `members`."""
    if isinstance(origin.get("body"), str):
        return _without_note(origin["body"])
    members = origin.get("members") if isinstance(origin.get("members"), list) else []
    bodies = (m["body"] for m in members if isinstance(m, dict) and isinstance(m.get("body"), str))
    return "\n\n".join(_without_note(b) for b in bodies)


def peer_sender(e: dict) -> str:
    """The sender's session name; a joined message has no `name`, but its envelope still carries from-name."""
    origin = e.get("origin") or {}
    if name := origin.get("name"):
        return str(name)
    content = (e.get("message") or {}).get("content")
    if isinstance(content, str) and (m := FROM_NAME.search(content)):
        return m.group(1)
    return str(origin.get("from") or "")


def is_plan_path(path: str) -> bool:
    p = Path(path)
    if p.suffix == ".mdx":
        return True
    return p.suffix == ".md" and ("plan" in p.name.lower() or "/plans/" in path)


class _Parser:
    def __init__(self, digest: Digest):
        self.d = digest
        self.prompt_keys: Counter[str] = Counter()  # prompts and recaps
        self.tools: Counter[str] = Counter()
        self.pending: dict[str, str] = {}  # tool_use_id -> tool name, until its result arrives
        self.questions_by_id: dict[str, dict] = {}
        self.task_by_tool_id: dict[str, dict] = {}
        self.seen_prs: set[str] = set()
        self.sends: dict[
            str, tuple[dict, dict] | None
        ] = {}  # SendMessage tool_use id -> (message, event); None once resolved
        self.replied: set[str] = set()  # assistant message ids already counted in n_replies
        self.awaiting_reply: dict | None = None  # the peer message the session's next texts answer
        self.context_model = ""  # the model of the latest reply that reported usage
        self.context_peak = 0

    def bump(self, key: str, ts: str) -> None:
        count, last = self.d.tool_usage.get(key, (0, ""))
        self.d.tool_usage[key] = [count + 1, max(last, ts)]

    def event(self, ts: str, kind: str, text: str) -> None:
        self.d.events.append({"ts": ts, "kind": kind, "text": _clip(text)})

    def feed(self, e: dict, raw: str) -> None:
        d = self.d
        if "/browse/" in raw and not d.jira_base and (m := BROWSE_URL.search(raw)):
            d.jira_base = m.group(1)
        if e.get("isSidechain"):
            return
        ts = e.get("timestamp") or ""
        if ts:
            d.started_at = d.started_at or ts
            d.last_activity = max(d.last_activity, ts)
        if cwd := e.get("cwd"):
            d.project = d.project or cwd
            d.cwd = cwd
        if (branch := e.get("gitBranch")) and branch != "HEAD":
            d.git_branch = branch

        kind = e.get("type")
        handler = getattr(self, "on_" + str(kind).replace("-", "_"), None)
        if handler:
            handler(e, ts)

    # --- metadata entries -------------------------------------------------------------
    def on_ai_title(self, e: dict, ts: str) -> None:
        self.d.ai_title = e.get("aiTitle") or self.d.ai_title

    def on_agent_name(self, e: dict, ts: str) -> None:
        self.d.custom_name = e.get("agentName") or self.d.custom_name

    def on_pr_link(self, e: dict, ts: str) -> None:
        url = e.get("prUrl")
        # it becomes a link on the page: a web address only, never javascript: or data:
        if isinstance(url, str) and WEB_URL.match(url) and url not in self.seen_prs:
            self.seen_prs.add(url)
            self.d.prs.append({"url": url, "number": e.get("prNumber"), "repo": e.get("prRepository")})
            self.event(ts, "pr", url)

    def on_cost_state(self, e: dict, ts: str) -> None:
        d = self.d
        d.cost_usd = float(e.get("totalCostUSD") or 0)
        d.lines_added = int(e.get("totalLinesAdded") or 0)
        d.lines_removed = int(e.get("totalLinesRemoved") or 0)
        d.models = sorted(e.get("modelUsage") or {})

    def on_worktree_state(self, e: dict, ts: str) -> None:
        ws = e.get("worktreeSession") or {}
        self.d.worktree = ws.get("worktreeName") or self.d.worktree

    def on_relocated(self, e: dict, ts: str) -> None:
        self.d.cwd = e.get("relocatedCwd") or self.d.cwd

    def on_continued_in(self, e: dict, ts: str) -> None:
        self.d.continued_in = e.get("continuedInSessionId") or ""

    def on_system(self, e: dict, ts: str) -> None:
        subtype = e.get("subtype")
        if subtype == "away_summary" and isinstance(e.get("content"), str):
            text = RECAP_SUFFIX.sub("", e["content"]).strip()
            self.d.recap, self.d.recap_at = _clip(text), ts
            self.prompt_keys.update(jira_keys_in(text))
            self.event(ts, "recap", text)
        elif subtype == "compact_boundary":
            self.event(ts, "compact", "")  # the kind is the marker: the page words it (app.event.compacted)
            # Until the next reply the context is what the compaction left: its summary and kept messages.
            post = (e.get("compactMetadata") or {}).get("postTokens")
            self.d.context_tokens = post if isinstance(post, int) and post > 0 else 0

    # --- conversation ------------------------------------------------------------------
    def on_user(self, e: dict, ts: str) -> None:
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.on_tool_result(block, e.get("toolUseResult"))
        if is_session_message(e):
            self.on_peer_message(e, ts)
        text = human_prompt(e)
        if text is None:
            return
        if isinstance(content, str) and (m := COMMAND.search(content)):
            self.bump("command:" + m.group(1).strip().lstrip("/"), ts)
        d = self.d
        self.awaiting_reply = None  # what follows answers the human now
        d.n_prompts += 1
        d.first_prompt = d.first_prompt or _clip(text, 400)
        d.last_prompt, d.last_prompt_at = _clip(text, 400), ts
        self.prompt_keys.update(jira_keys_in(text))
        self.event(ts, "prompt", text)

    def on_peer_message(self, e: dict, ts: str) -> None:
        sender, text = peer_sender(e), _clip(peer_text(e.get("origin") or {}))
        msg = {"ts": ts, "from": sender, "text": text, "msg_id": (e.get("origin") or {}).get("msg_id") or ""}
        self.d.messages_in.append(msg)
        self.awaiting_reply = msg
        self.event(ts, "msg_in", f"← {sender}: {text}")

    def on_send_message(self, block: dict, inp: dict, ts: str) -> None:
        tool_id, to = block.get("id", ""), inp.get("to")
        if tool_id in self.sends or not is_cross_session_send(to):
            return  # Claude Code writes the same assistant entry twice, the copy may follow the result
        text, summary = _clip(str(inp.get("message") or "")), _clip(str(inp.get("summary") or ""), 200)
        # ok stays None until the result: a send may still wait for the user's approval
        msg = {"ts": ts, "to": to, "summary": summary, "text": text, "msg_id": "", "ok": None}
        self.d.messages_out.append(msg)
        self.event(ts, "msg_out", f"→ {to}: {summary or text}")
        self.sends[tool_id] = (msg, self.d.events[-1])

    def on_send_result(self, tool_id: str, block: dict, result: object) -> None:
        msg, event = self.sends[tool_id]
        self.sends[tool_id] = None  # resolved; the key stays so a later copy of the tool_use is still a copy
        msg["ok"], cross_session = send_outcome(block, result)
        if cross_session is False:  # the result says it was a subagent after all
            self.d.messages_out.remove(msg)
            self.d.events.remove(event)
        elif isinstance(result, dict):
            msg["msg_id"] = result.get("msg_id") or ""

    def on_tool_result(self, block: dict, result: object) -> None:
        tool_id = block.get("tool_use_id", "")
        self.pending.pop(tool_id, None)
        if self.sends.get(tool_id):
            self.on_send_result(tool_id, block, result)
        if (q := self.questions_by_id.get(tool_id)) is not None:
            answers = result.get("answers") if isinstance(result, dict) else None
            q["answer"] = (
                "; ".join(map(str, answers.values())) if answers else _clip(str(block.get("content")), 300)
            )
        if (task := self.task_by_tool_id.pop(tool_id, None)) is not None:
            text = block.get("content")
            text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
            if m := TASK_ID.search(text):
                task["id"] = m.group(1)

    def on_assistant(self, e: dict, ts: str) -> None:
        d = self.d
        self.on_usage(e.get("message") or {})
        for block in (e.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                d.last_assistant_text = _clip(block["text"], 800)
                self.on_reply(e, block["text"], ts)
            elif block.get("type") == "tool_use":
                self.on_tool_use(block, e, ts)

    def on_usage(self, message: dict) -> None:
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        tokens = sum(n for key in CONTEXT_USAGE if isinstance(n := usage.get(key), int))
        if tokens > 0:  # a synthetic reply (an API error shown as one) reports zeros
            self.d.context_tokens = tokens
            self.context_peak = max(self.context_peak, tokens)
            self.context_model = str(message.get("model") or self.context_model)

    def on_reply(self, e: dict, text: str, ts: str) -> None:
        mid = (e.get("message") or {}).get("id") or e.get("uuid") or ""
        if mid not in self.replied:  # Claude Code may write the same assistant entry twice
            self.replied.add(mid)
            self.d.n_replies += 1
        if self.awaiting_reply is not None:  # a group chat shows this as the member's answer
            self.awaiting_reply["reply"], self.awaiting_reply["reply_at"] = _clip(text, 4000), ts

    def on_tool_use(self, block: dict, e: dict, ts: str) -> None:
        d, name = self.d, block.get("name", "")
        inp = block.get("input") or {}
        d.n_tool_calls += 1
        self.tools[name] += 1
        self.pending[block.get("id", "")] = name
        self.count_usage(name, inp, ts)

        if name in ("Write", "Edit", "MultiEdit") and is_plan_path(path := inp.get("file_path") or ""):
            self.add_plan(ts, path=path)
        elif name == "Bash" and "vplan" in (cmd := inp.get("command") or ""):
            for token in MDX_TOKEN.findall(cmd):
                self.add_plan(ts, path=str(Path(e.get("cwd") or "/", Path(token).expanduser())))
        elif name == "ExitPlanMode" and inp.get("plan"):
            self.add_plan(ts, text=inp["plan"])
        elif name in ("Agent", "Task"):
            agent = {
                "ts": ts,
                "description": inp.get("description") or "",
                "type": inp.get("subagent_type") or "",
                "background": bool(inp.get("run_in_background")),
            }
            d.agents.append(agent)
            self.event(ts, "agent", f"{agent['description']} ({agent['type'] or 'agent'})")
        elif name == "SendMessage":
            self.on_send_message(block, inp, ts)
        elif name == "AskUserQuestion":
            items = [
                {
                    "header": q.get("header", ""),
                    "question": q.get("question", ""),
                    "options": [o.get("label", "") for o in q.get("options") or []],
                }
                for q in inp.get("questions") or []
            ]
            q = {"ts": ts, "items": items, "answer": None}
            d.questions.append(q)
            self.questions_by_id[block.get("id", "")] = q
            self.event(ts, "question", " / ".join(i["question"] for i in items))
        elif name == "TaskCreate":
            task = {
                "id": str(len(d.tasks) + 1),
                "subject": inp.get("subject", ""),
                "active_form": inp.get("activeForm", ""),
                "status": "pending",
            }
            d.tasks.append(task)
            self.task_by_tool_id[block.get("id", "")] = task
        elif name == "TaskUpdate":
            self.update_task(inp)
        elif name == "TodoWrite":
            d.tasks = [
                {
                    "id": str(i + 1),
                    "subject": t.get("content", ""),
                    "active_form": t.get("activeForm", ""),
                    "status": t.get("status", "pending"),
                }
                for i, t in enumerate(inp.get("todos") or [])
            ]

    def count_usage(self, name: str, inp: dict, ts: str) -> None:
        if name == "Skill" and inp.get("skill"):
            self.bump(f"skill:{inp['skill']}", ts)
        elif name in ("Agent", "Task"):
            self.bump(f"agent:{inp.get('subagent_type') or 'general-purpose'}", ts)
        elif name.startswith("mcp__") and "__" in name[5:]:
            server, tool = name[5:].split("__", 1)
            self.bump(f"mcp:{server}", ts)
            self.bump(f"mcp_tool:{server}/{tool}", ts)

    def update_task(self, inp: dict) -> None:
        tasks = self.d.tasks
        task = next((t for t in tasks if t["id"] == str(inp.get("taskId"))), None)
        if task is None:
            return
        if inp.get("status") == "deleted":
            tasks.remove(task)
            return
        for src, dst in (("status", "status"), ("subject", "subject"), ("activeForm", "active_form")):
            if inp.get(src):
                task[dst] = inp[src]

    def add_plan(self, ts: str, path: str = "", text: str = "") -> None:
        plans = self.d.plans
        if path:
            plans[:] = [p for p in plans if p.get("path") != path]
        plans.append({"ts": ts, "path": path, "text": _clip(text, 20000) if text else ""})
        self.event(ts, "plan", path or text.strip().split("\n", 1)[0])

    def finish(self) -> Digest:
        d = self.d
        # The title says what the session is about; a branch may belong to a shared checkout
        # that many unrelated sessions run in, so it ranks last.
        ranked = jira_keys_in(f"{d.custom_name} {d.ai_title}")
        ranked += jira_keys_in(d.worktree, any_case=True)
        ranked += [k for k, _ in self.prompt_keys.most_common()]
        ranked += jira_keys_in(f"{d.git_branch} {Path(d.cwd).name}", any_case=True)
        d.jira_keys = list(dict.fromkeys(ranked))[:6]
        d.top_tools = [[n, c] for n, c in self.tools.most_common(8)]
        d.pending_tool = next(reversed(self.pending.values()), "") if self.pending else ""
        if self.context_model:
            d.context_window = context_window(self.context_model, d.models, self.context_peak)
        return d


def context_window(model: str, models: list[str], peak: int) -> int:
    """The window of the session's latest model; `models` are all it used (cost-state names carry `[1m]`),
    `peak` the largest context it reached."""
    major = MODEL_MAJOR.match(model)
    one_m = any("[1m]" in m for m in (model, *models))
    return WINDOW_1M if one_m or peak > WINDOW or (major and int(major[1]) >= 5) else WINDOW


def transcripts(home: Path) -> dict[str, Path]:
    """Transcripts by session id; a session copied into two project folders counts once, the newer copy."""
    found: dict[str, Path] = {}
    for p in (home / "projects").glob("*/*.jsonl"):
        prev = found.get(p.stem)
        if prev is None or p.stat().st_mtime > prev.stat().st_mtime:
            found[p.stem] = p
    return found


def parse_transcript(path: Path) -> Digest:
    parser = _Parser(Digest(session_id=path.stem, path=str(path)))
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if raw.startswith(SKIP_PREFIXES):
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue  # the last line may be half-written while the session runs
            if isinstance(entry, dict):
                parser.feed(entry, raw)
    return parser.finish()
