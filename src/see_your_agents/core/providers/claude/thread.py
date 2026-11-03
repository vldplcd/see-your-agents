"""A Claude Code transcript as chat messages (Provider.thread): what the chat panel shows of a session.

The transcript is parsed into a flat list of messages; a parsed thread is extended from the last complete
line when the file only grew. Images stay in the transcript: a message lists them by URL and `image` reads
one back from its line.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path

from ..base import COMPACTED, Thread
from .transcript import (
    RECAP_SUFFIX,
    SKIP_PREFIXES,
    human_prompt,
    is_cross_session_send,
    is_plan_path,
    is_session_message,
    peer_sender,
    peer_text,
    send_outcome,
)

OPEN_WINDOW = 20  # a tool call still unanswered this far back is treated as abandoned
TEXT_LIMIT = 30000
SUMMARY_LIMIT = 200


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tail(path: str, parts: int = 3) -> str:
    return "/".join(Path(path).parts[-parts:])


def summarize_tool(name: str, inp: dict) -> str:
    """One line saying what a tool call did; the model's own wording wins where it gave one."""

    def first(*keys: str) -> str:
        return next((str(inp[k]) for k in keys if inp.get(k)), "")

    if name == "Bash":
        lines = first("description", "command").strip().splitlines()
        text = lines[0] if lines else ""
    elif name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        text = _tail(first("file_path", "notebook_path"))
    elif name == "Grep":
        text = f"{first('pattern')} {_tail(first('path'), 2) if inp.get('path') else ''}".strip()
    elif name in ("Agent", "Task"):
        text = first("description", "subagent_type")
    elif name == "Skill":
        text = first("skill")
    elif name == "WebFetch":
        text = first("url")
    elif name == "WebSearch":
        text = first("query")
    elif name in ("Glob", "Monitor"):
        text = first("pattern", "description", "command")
    elif name == "TaskCreate":
        text = first("subject")
    else:
        text = ""
    if name.startswith("mcp__"):
        _, server, *tool = name.split("__")
        name = f"{server}.{'__'.join(tool)}"
    return _clip(f"{name}: {text}" if text else name, SUMMARY_LIMIT)


def _is_image(block: dict) -> bool:
    source = block.get("source")
    return block.get("type") == "image" and isinstance(source, dict) and source.get("type") == "base64"


def _typed_by_human(e: dict) -> bool:
    """The entry is a human's own message (it may hold only a pasted image, which has no text)."""
    if e.get("isMeta") or e.get("isCompactSummary") or e.get("toolUseResult") is not None:
        return False
    origin = e.get("origin") or {}
    return origin.get("kind") == "human" if origin else e.get("promptSource") != "system"


class ClaudeThread(Thread):
    """Messages parsed from one Claude Code transcript, extended as the file grows."""

    def __init__(self) -> None:
        super().__init__()
        self.clear()

    def clear(self) -> None:
        self.stat: tuple[int, float] | None = None
        self.offset = 0  # bytes consumed: up to and including the last complete line
        self.messages: list[dict] = []
        self.open_calls: dict[str, tuple[int, int]] = {}  # tool_use id -> (message, call)
        self.open_questions: dict[str, int] = {}  # tool_use id -> message
        self.sent: dict[
            str, int
        ] = {}  # SendMessage tool_use id -> peer_out message (the entry is written twice)
        self.open_sends: set[str] = set()  # sent ids whose result has not arrived
        self.image_lines: dict[str, tuple[int, int]] = {}  # entry uuid -> (offset, length) of its line

    def sync(self, path: Path) -> None:
        st = path.stat()
        if self.stat == (st.st_size, st.st_mtime):
            return
        if st.st_size < self.offset:  # rewritten, not appended
            self.clear()
        with path.open("rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read(st.st_size - self.offset)
        complete = chunk[: chunk.rfind(b"\n") + 1]  # a half-written last line waits for the next sync
        pos = self.offset
        for raw in complete.splitlines(keepends=True):
            self.feed(raw, pos)
            pos += len(raw)
        self.offset = pos
        self.stat = (st.st_size, st.st_mtime)

    def feed(self, raw: bytes, pos: int) -> None:
        line = raw.decode("utf-8", errors="replace")
        if line.startswith(SKIP_PREFIXES):
            return
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(e, dict) or e.get("isSidechain"):
            return
        match e.get("type"):
            case "user":
                self.on_user(e, pos, len(raw))
            case "assistant":
                self.on_assistant(e)
            case "system":
                self.on_system(e)

    def add(self, kind: str, e: dict, **fields: object) -> dict:
        msg = {"id": len(self.messages), "at": e.get("timestamp") or "", "kind": kind, **fields}
        self.messages.append(msg)
        return msg

    # --- entries ------------------------------------------------------------------------------
    def on_system(self, e: dict) -> None:
        if e.get("subtype") == "away_summary" and isinstance(e.get("content"), str):
            self.add("recap", e, text=RECAP_SUFFIX.sub("", e["content"]).strip())
        elif e.get("subtype") == "compact_boundary":
            self.add("compact", e, text=COMPACTED)

    def on_user(self, e: dict, pos: int, length: int) -> None:
        content = (e.get("message") or {}).get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        for block in blocks:
            if block.get("type") == "tool_result":
                self.on_tool_result(block, e.get("toolUseResult"))
        if is_session_message(e):
            origin = e["origin"]
            self.add(
                "peer_in",
                e,
                **{"from": peer_sender(e)},
                text=_clip(peer_text(origin), TEXT_LIMIT),
                msg_id=origin.get("msg_id") or "",
            )
        text = human_prompt(e)
        images = [b for b in blocks if _is_image(b)] if text is not None or _typed_by_human(e) else []
        if text is None and not images:
            return
        uuid = e.get("uuid") or f"m{len(self.messages)}"
        if images:
            self.image_lines[uuid] = (pos, length)
        refs = [
            {"url": f"image/{uuid}/{n}", "media_type": b["source"].get("media_type", "")}
            for n, b in enumerate(images)
        ]
        self.add("you", e, text=_clip(text or "", TEXT_LIMIT), images=refs)

    def on_assistant(self, e: dict) -> None:
        for block in (e.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                self.add("agent", e, text=_clip(block["text"], TEXT_LIMIT))
            elif block.get("type") == "tool_use":
                self.on_tool_use(block, e)

    def on_tool_use(self, block: dict, e: dict) -> None:
        name, inp, tool_id = block.get("name", ""), block.get("input") or {}, block.get("id", "")
        if name == "AskUserQuestion":
            items = [
                {
                    "header": q.get("header", ""),
                    "question": q.get("question", ""),
                    "options": [
                        {"label": o.get("label", ""), "description": o.get("description", "")}
                        for o in q.get("options") or []
                    ],
                }
                for q in inp.get("questions") or []
            ]
            msg = self.add("question", e, items=items, answer=None)
            self.open_questions[tool_id] = msg["id"]
        elif name == "SendMessage" and is_cross_session_send(inp.get("to")):
            if tool_id not in self.sent:
                msg = self.add(
                    "peer_out",
                    e,
                    to=inp["to"],
                    text=_clip(str(inp.get("message") or ""), TEXT_LIMIT),
                    summary=_clip(str(inp.get("summary") or ""), SUMMARY_LIMIT),
                    ok=None,  # until the result: the send may wait for the user's approval
                )
                self.sent[tool_id] = msg["id"]
                self.open_sends.add(tool_id)
        elif name == "ExitPlanMode" and inp.get("plan"):
            self.add("plan", e, text=_clip(inp["plan"], TEXT_LIMIT), path="")
        elif name == "Write" and is_plan_path(path := inp.get("file_path") or ""):
            self.add("plan", e, text=_clip(inp.get("content") or "", TEXT_LIMIT), path=path)
        else:
            last = self.messages[-1] if self.messages else None
            if last is None or last["kind"] != "tools":
                last = self.add("tools", e, calls=[])
            last["calls"].append({"name": name, "summary": summarize_tool(name, inp), "error": False})
            self.open_calls[tool_id] = (last["id"], len(last["calls"]) - 1)

    def on_tool_result(self, block: dict, result: object) -> None:
        tool_id = block.get("tool_use_id", "")
        if (at := self.open_calls.pop(tool_id, None)) is not None:
            self.messages[at[0]]["calls"][at[1]]["error"] = bool(block.get("is_error"))
        if tool_id in self.open_sends:
            self.open_sends.discard(tool_id)
            msg = self.messages[self.sent[tool_id]]
            ok, cross_session = send_outcome(block, result)
            if cross_session is False:  # a named subagent or teammate after all: an ordinary tool line
                call = {"name": "SendMessage", "summary": f"SendMessage: {msg['to']}", "error": not ok}
                self.messages[msg["id"]] = {
                    "id": msg["id"],
                    "at": msg["at"],
                    "kind": "tools",
                    "calls": [call],
                }
            else:
                msg["ok"] = ok
        if (qi := self.open_questions.pop(tool_id, None)) is not None:
            answers = result.get("answers") if isinstance(result, dict) else None
            self.messages[qi]["answer"] = (
                "; ".join(map(str, answers.values())) if answers else _clip(str(block.get("content")), 300)
            )

    # --- reading ------------------------------------------------------------------------------
    def first_mutable(self) -> int:
        """Index of the oldest recent message that a later transcript line may still change."""
        end = len(self.messages)
        pending = [i for i, _ in self.open_calls.values()] + list(self.open_questions.values())
        pending += [self.sent[i] for i in self.open_sends]
        mutable = [i for i in pending if i >= end - OPEN_WINDOW]
        if end and self.messages[-1]["kind"] == "tools":
            mutable.append(end - 1)  # more calls may join the group
        return min(mutable, default=end)

    def image(self, path: Path, key: str, n: int) -> tuple[str, bytes] | None:
        where = self.image_lines.get(key)
        if where is None:
            return None
        with path.open("rb") as fh:
            fh.seek(where[0])
            entry = json.loads(fh.read(where[1]))
        blocks = (entry.get("message") or {}).get("content") or []
        images = [b for b in blocks if isinstance(b, dict) and _is_image(b)]
        if n >= len(images):
            return None
        source = images[n]["source"]
        try:
            return source.get("media_type", ""), base64.b64decode(source.get("data", ""), validate=True)
        except binascii.Error:
            return None
