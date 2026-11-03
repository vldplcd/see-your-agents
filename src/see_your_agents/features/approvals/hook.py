"""Claude Code's PermissionRequest hook: hand the prompt to the dashboard and print its answer.

Claude Code shows its dialog and runs this hook at the same time, and the first answer wins
(docs/notes/claude-hooks-notes.md). So the hook only adds a second place to answer: on any failure (no
dashboard, a bad reply, the wait is over) it exits 0 with no output, and the dialog stays the only one. It
never takes a tool input from the dashboard: an allow is plain, and only AskUserQuestion gets answers,
checked against its own questions.

Whoever listens on the port is not trusted for being there: while the dashboard is down or restarting, any
local process may bind it. Both sides prove they hold the token file without ever sending it. Each request
carries a fresh nonce, the time and HMAC-SHA256(token, "req", path, nonce, time, body); each reply carries
HMAC(token, "resp", path, nonce, body) over the exact body. A reply that does not verify is a failure like
any other, so an impostor gets no answer printed, and a first `hello` exchange proves the dashboard before
the hook tells it what the prompt is.

Stdlib only, run with `python -I` by the command `sya hooks install` writes into Claude Code's settings:
    python -I hook.py --port 8765 --token-file ~/.config/see-your-agents/approvals-token
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import signal
import sys
import time
import urllib.error
import urllib.request

ASK = "AskUserQuestion"
PLAN = "ExitPlanMode"
DENIED = "The user denied this action in the dashboard."  # Claude Code hands it to the agent: English
WAIT_S = 3540.0  # under the 3600 s `timeout` of the settings entry: the hook leaves before it is killed
OUTAGE_S = 300.0  # the dashboard went away mid-wait (a restart) for longer than this: give up
RETRY_S = 2.0
TAIL_BYTES = 512 * 1024


class Gone(Exception):
    """The dashboard refused the request or answered nonsense: leave the dialog to the terminal."""


ROUTE = "/api/approvals"


def mac(token: str, *parts: str) -> str:
    """HMAC-SHA256 of the parts, one per line: a request's or a reply's signature (the server uses it too)."""
    return hmac.new(token.encode(), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def same(a: object, b: str) -> bool:
    return isinstance(a, str) and hmac.compare_digest(a.encode(), b.encode())


class Dashboard:
    """The approvals API of `sya serve` on 127.0.0.1: signed requests, and only replies signed back count."""

    def __init__(self, port: int, token: str):
        self.url = f"http://127.0.0.1:{port}"
        self.token = token
        self.headers = {
            "Content-Type": "application/json",
            "Origin": self.url,
            "X-SYA": "1",
        }  # the host's guard

    def post(self, path: str, payload: dict, timeout: float) -> dict:
        """The verified JSON reply; OSError when nobody answers, Gone for a refusal or an unsigned reply."""
        path = ROUTE + path
        nonce, at, body = secrets.token_hex(16), str(int(time.time())), json.dumps(payload)
        envelope = {
            "nonce": nonce,
            "time": at,
            "body": body,
            "mac": mac(self.token, "req", path, nonce, at, body),
        }
        req = urllib.request.Request(self.url + path, json.dumps(envelope).encode(), self.headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise Gone(f"HTTP {exc.code}") from None
        try:
            outer = json.loads(raw)
            text = outer["body"]
            if not isinstance(text, str) or not same(
                outer.get("mac"), mac(self.token, "resp", path, nonce, text)
            ):
                raise Gone("reply not signed with the token")
            reply = json.loads(text)
        except (ValueError, TypeError, KeyError):
            raise Gone("not the dashboard's reply") from None
        if not isinstance(reply, dict):
            raise Gone("not an object")
        return reply


def _read(path: str, start: int, whole_lines: bool) -> tuple[list[dict], int]:
    """The transcript's complete JSON lines from byte `start`, and the offset after the last one. Unless
    `start` is known to begin a line, the partial line there is skipped."""
    with open(path, "rb") as f:
        f.seek(max(0, start))
        chunk = f.read()
    begin = max(0, start)
    if begin and not whole_lines:
        skip = chunk.find(b"\n") + 1
        chunk, begin = chunk[skip:], begin + skip
    end = chunk.rfind(b"\n") + 1
    items = []
    for line in chunk[:end].splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items, begin + end


def _blocks(item: dict, kind: str) -> list[dict]:
    message = item.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return (
        [b for b in content if isinstance(b, dict) and b.get("type") == kind]
        if isinstance(content, list)
        else []
    )


def find_tool_use(path: str, tool: str, tool_input: object) -> tuple[str, int]:
    """The id of the call this prompt is about, and where to read on: the newest unanswered tool_use of
    `tool` in the transcript, the one with this very input if there is one. Claude Code writes it first."""
    items, offset = _read(path, os.path.getsize(path) - TAIL_BYTES, whole_lines=False)
    answered = {b.get("tool_use_id") for i in items for b in _blocks(i, "tool_result")}
    calls = [b for i in items for b in _blocks(i, "tool_use") if b.get("name") == tool]
    open_calls = [b for b in calls if b.get("id") not in answered]
    exact = [b for b in open_calls if b.get("input") == tool_input]
    pick = (exact or open_calls)[-1:]
    return (str(pick[0].get("id") or ""), offset) if pick else ("", offset)


def answered_since(path: str, tool_use_id: str, offset: int) -> tuple[bool, int]:
    """Whether the call got its tool_result after `offset`: then the terminal answered first."""
    items, offset = _read(path, offset, whole_lines=True)
    return any(b.get("tool_use_id") == tool_use_id for i in items for b in _blocks(i, "tool_result")), offset


def answers_for(raw: object, tool_input: dict) -> dict[str, str] | None:
    """The page's answers when they answer exactly this call's questions, each with a non-empty string."""
    questions = [q.get("question") for q in tool_input.get("questions") or [] if isinstance(q, dict)]
    if not isinstance(raw, dict) or not questions or set(raw) != set(questions):
        return None
    if not all(isinstance(v, str) and v.strip() for v in raw.values()):
        return None
    return {q: raw[q].strip() for q in questions}


def output(decision: object, data: dict) -> dict | None:
    """Claude Code's answer for the dashboard's decision; None leaves the dialog alone."""
    if not isinstance(decision, dict):
        return None
    tool, tool_input = data.get("tool_name"), data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    if decision.get("behavior") == "deny":
        message = decision.get("message")
        if isinstance(message, str) and message.strip():
            verdict = {"behavior": "deny", "message": message.strip()}  # the agent goes on with the comment
        else:
            verdict = {"behavior": "deny", "message": DENIED, "interrupt": True}  # «No» in the terminal
    elif decision.get("behavior") == "allow":
        verdict = {"behavior": "allow"}
        if tool == ASK:  # a plain allow does not answer a question
            if (answers := answers_for(decision.get("answers"), tool_input)) is None:
                return None
            verdict["updatedInput"] = {**tool_input, "answers": answers}
        elif tool == PLAN:  # nor approve a plan; the session leaves plan mode for «ask each time»
            verdict["updatedInput"] = tool_input
            verdict["updatedPermissions"] = [{"type": "setMode", "mode": "default", "destination": "session"}]
    else:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": verdict}}


def wanted(data: object) -> bool:
    """A prompt the dashboard can race the dialog for: a main-thread call of a terminal or background session.
    `claude -p` has no dialog (it would wait for the dashboard); a subagent's dialog waits for the hook."""
    return (
        isinstance(data, dict)
        and data.get("hook_event_name") == "PermissionRequest"
        and os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "cli"
        and not data.get("agent_id")
        and isinstance(data.get("session_id"), str)
        and isinstance(data.get("tool_name"), str)
    )


def run(data: dict, dash: Dashboard, wait_s: float = WAIT_S) -> dict | None:
    """Register the prompt and wait for the page's decision: the answer to print, or None."""
    request = {k: data.get(k) for k in ("session_id", "tool_name", "tool_input", "cwd", "permission_mode")}
    transcript = data.get("transcript_path") if isinstance(data.get("transcript_path"), str) else ""
    deadline = time.monotonic() + wait_s
    tool_use, offset, down_since = "", 0, None
    if dash.post("/hello", {}, timeout=5).get("ok") is not True:  # the dashboard itself, before the prompt
        return None
    rid = str(dash.post("/ask", request, timeout=5)["id"])
    try:
        while time.monotonic() < deadline:
            try:  # the transcript tells when the terminal answered first
                if not tool_use:
                    tool_use, offset = find_tool_use(transcript, data["tool_name"], request["tool_input"])
                else:
                    done, offset = answered_since(transcript, tool_use, offset)
                    if done:
                        return None
            except OSError:
                pass
            try:
                reply = dash.post(f"/{rid}/wait", {}, timeout=30)
                down_since = None
            except OSError:  # a restarting dashboard: try again for a while
                down_since = down_since or time.monotonic()
                if time.monotonic() - down_since > OUTAGE_S:
                    return None
                time.sleep(RETRY_S)
                continue
            state = reply.get("state")
            if state == "decided":
                rid = ""
                return output(reply.get("decision"), data)
            if state == "unknown":  # the dashboard restarted and forgot it: ask again
                rid = ""
                rid = str(dash.post("/ask", request, timeout=5)["id"])
            elif state != "pending":
                rid = ""
                return None
        return None
    finally:
        if rid:  # leaving anyway: if this fails, the dashboard drops a silent request itself
            with contextlib.suppress(Exception):
                dash.post(f"/{rid}/withdraw", {}, timeout=2)


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # Claude Code's timeout or Esc: withdraw, then go
    try:
        parser = argparse.ArgumentParser(prog="hook.py")
        parser.add_argument("--port", type=int, required=True)
        parser.add_argument("--token-file", required=True)
        parser.add_argument("--wait", type=float, default=WAIT_S)
        args = parser.parse_args(argv)
        data = json.loads(sys.stdin.buffer.read())
        if not wanted(data):
            return 0
        with open(args.token_file, encoding="utf-8") as f:
            token = f.read().strip()
        answer = run(data, Dashboard(args.port, token), args.wait)
        if answer is not None:
            sys.stdout.write(json.dumps(answer))
            sys.stdout.flush()
    except BaseException:  # noqa: BLE001 - never a traceback or a failing exit: the dialog just stays
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
