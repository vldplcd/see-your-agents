"""A Codex rollout as chat messages (Provider.thread): the chat panel's view of a thread, read-only.

The same lines as the digest (rollout.py): the human's prompts (`you`), the agent's messages (`agent`), its
tool calls grouped like Claude's ("N actions"), a subagent's report to the thread (`peer_in`)
and compactions. A fork's replayed parent history is skipped (`Replay`). Images are not served.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..base import COMPACTED, Thread
from .rollout import Replay, answer_of, event_type, payload, prompt_of, request_text

OPEN_WINDOW = 20  # a tool call still without output this far back is treated as abandoned
TEXT_LIMIT = 30000
SUMMARY_LIMIT = 200
CALLS = ("function_call", "custom_tool_call", "local_shell_call", "web_search_call")
OUTPUTS = ("function_call_output", "custom_tool_call_output")
EXIT = re.compile(r"(?:Exit code:|Process exited with code) (-?\d+)")
PATCHED = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
FAILED = ("exec_command failed", "apply_patch verification failed")


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tail(path: str, parts: int = 3) -> str:
    return "/".join(Path(path).parts[-parts:])


def _args(call: dict) -> dict:
    try:
        args = json.loads(call.get("arguments") or "{}")
    except (TypeError, ValueError):
        return {}
    return args if isinstance(args, dict) else {}


def summarize_call(call: dict) -> tuple[str, str]:
    """(name, one line saying what the call did)."""
    kind, args = call.get("type"), _args(call)
    action = call.get("action") if isinstance(call.get("action"), dict) else {}
    name = "web_search" if kind == "web_search_call" else str(call.get("name") or "shell")
    if kind == "web_search_call":
        queries = action.get("queries") if isinstance(action.get("queries"), list) else []
        text = action.get("query") or (queries[0] if queries else "") or action.get("url") or ""
    elif name in ("exec_command", "shell_command", "shell") or kind == "local_shell_call":
        cmd = args.get("cmd") or args.get("command") or action.get("command") or ""
        text = " ".join(cmd) if isinstance(cmd, list) else cmd
    elif name == "apply_patch":
        patched = PATCHED.findall(str(call.get("input") or ""))
        text = ", ".join(_tail(p.strip()) for p in patched)
    elif name == "exec":  # code mode: a script that calls the tools
        text = call.get("input") or ""
    elif name == "spawn_agent":
        text = args.get("task_name") or ""  # its `message` is stored encrypted
    elif name == "update_plan":
        text = args.get("explanation") or ""
    else:
        text = args.get("target") or ""
    line = str(text).strip().split("\n", 1)[0]
    return name, _clip(f"{name}: {line}" if line else name, SUMMARY_LIMIT)


def failed(output: object) -> bool:
    """Whether a tool's output says it failed: a non-zero exit code, or Codex's own refusal."""
    if isinstance(output, list):
        output = " ".join(str(o.get("text") or "") for o in output if isinstance(o, dict))
    if not isinstance(output, str):
        return False
    if output.startswith("{"):
        try:
            meta = json.loads(output).get("metadata")
        except (ValueError, AttributeError):
            meta = None
        if isinstance(meta, dict) and isinstance(meta.get("exit_code"), int):
            return meta["exit_code"] != 0
    if m := EXIT.search(output[:500]):
        return m[1] != "0"
    return output.startswith(FAILED)


class CodexThread(Thread):
    """Messages parsed from one rollout, extended as the file grows."""

    def __init__(self) -> None:
        super().__init__()
        self.clear()

    def clear(self) -> None:
        self.stat: tuple[int, float] | None = None
        self.offset = 0  # bytes consumed: up to and including the last complete line
        self.messages: list[dict] = []
        self.replay = Replay()
        self.open_calls: dict[str, tuple[int, int]] = {}  # call_id -> (message, call)
        self.turn_prompts: set[str] = set()

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
        for raw in complete.splitlines():
            self.feed(raw)
        self.offset += len(complete)
        self.stat = (st.st_size, st.st_mtime)

    def feed(self, raw: bytes) -> None:
        try:
            entry = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return
        if not isinstance(entry, dict) or not self.replay.own(entry):
            return
        kind, p = entry.get("type"), payload(entry)
        if event_type(entry) == "task_started":
            self.turn_prompts.clear()
        elif (text := prompt_of(entry)) is not None:
            text = request_text(text)
            if text and text not in self.turn_prompts:
                self.turn_prompts.add(text)
                self.add("you", entry, text=_clip(text, TEXT_LIMIT), images=[])
        elif (answer := answer_of(entry)) is not None and answer[0].strip():
            self.add("agent", entry, text=_clip(answer[0], TEXT_LIMIT))
        elif kind == "response_item" and p.get("type") in CALLS:
            self.on_call(entry, p)
        elif kind == "response_item" and p.get("type") in OUTPUTS:
            if (at := self.open_calls.pop(str(p.get("call_id")), None)) is not None:
                self.messages[at[0]]["calls"][at[1]]["error"] = failed(p.get("output"))
        elif kind == "response_item" and p.get("type") == "agent_message" and p.get("author"):
            self.on_handover(entry, p)
        elif kind == "compacted":
            self.add("compact", entry, text=COMPACTED)

    def add(self, kind: str, entry: dict, **fields: object) -> dict:
        msg = {"id": len(self.messages), "at": str(entry.get("timestamp") or ""), "kind": kind, **fields}
        self.messages.append(msg)
        return msg

    def on_call(self, entry: dict, call: dict) -> None:
        name, summary = summarize_call(call)
        last = self.messages[-1] if self.messages else None
        if last is None or last["kind"] != "tools":
            last = self.add("tools", entry, calls=[])
        last["calls"].append({"name": name, "summary": summary, "error": False})
        if call.get("call_id") and call.get("type") != "web_search_call":
            self.open_calls[str(call["call_id"])] = (last["id"], len(last["calls"]) - 1)

    def on_handover(self, entry: dict, p: dict) -> None:
        """A message another agent of the thread's tree sent it: a subagent's report to its parent."""
        parts = p.get("content") if isinstance(p.get("content"), list) else []
        text = "\n".join(str(c.get("text") or "") for c in parts if isinstance(c, dict) and c.get("text"))
        if text.strip():
            self.add("peer_in", entry, **{"from": str(p["author"])}, text=_clip(text, TEXT_LIMIT), msg_id="")

    def first_mutable(self) -> int:
        """The oldest recent message a later line may still change: a call without output, a growing group."""
        end = len(self.messages)
        mutable = [i for i, _ in self.open_calls.values() if i >= end - OPEN_WINDOW]
        if end and self.messages[-1]["kind"] == "tools":
            mutable.append(end - 1)
        return min(mutable, default=end)
