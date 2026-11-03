"""Approvals: the HTTP side of permission prompts answered from the page.

    POST /api/approvals/hello           the hook checks it talks to the dashboard   signed
    POST /api/approvals/ask             the hook registers a prompt                 signed
    POST /api/approvals/<id>/wait       the hook's long poll for the decision       signed
    POST /api/approvals/<id>/withdraw   the hook leaves (terminal answered…)        signed
    POST /api/approvals/<id>/decide     the page answers: allow / deny              page_may_decide

The host lets a POST in only with the page's Origin and X-SYA. The hook's routes are signed both ways with the
token from a 0600 file (hook.py: nonce, time, HMAC; the token itself never travels), so neither a web page nor
a process that cannot read the file can register a prompt or pose as the dashboard to a hook. Deciding also
wants the secret of a paired page (app/pairing.py): forged headers alone, from curl or an agent, are not
enough. The page's open requests ride on the board: `approvals` on the session's card.

What the page reads is worded in its language: `decide`'s refusals are Problems (approvals.*), and a request's
view carries codes, not words (`warn`, an empty `summary`), that approvals.js words. The hook's routes answer
the hook only: their refusals are English.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

from ... import config_dir
from ...app.replies import Reply, json_reply
from ...core import providers
from ...core.providers.base import Cap
from ...i18n import Problem, request_lang, t
from .hook import ASK, PLAN, mac, same
from .store import Request, Store, waiting

if TYPE_CHECKING:
    from ...app.server import App

ROUTE = "/api/approvals"
ACTION = re.compile(r"/api/approvals/([A-Za-z0-9_-]{16,64})/(wait|withdraw|decide)")
# The hook's routes carry the hook token's HMAC, not the paired browser; the host lets them past pairing.
# `decide` is NOT here: only the paired browser answers a prompt (page_may_decide).
OPEN_POSTS = (
    f"{ROUTE}/hello",
    f"{ROUTE}/ask",
    r"/api/approvals/[A-Za-z0-9_-]{16,64}/wait",
    r"/api/approvals/[A-Za-z0-9_-]{16,64}/withdraw",
)
SESSION_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
NONCE = re.compile(r"[0-9a-f]{32,64}")
SKEW_S = 60  # a signed request older or newer than this is refused; its nonce is remembered twice as long
WAIT_S = 2.0  # one long poll of the hook: short, so it checks the transcript between polls
SETTLE_S = 4.0  # a live hook takes the page's decision within one poll; past this it is gone
MAX_BODY = 60000  # the command, the file content or the plan, in full; longer is not shown, so not allowed
MAX_FIELD = 4000
MAX_FIELDS = 40
MAX_MESSAGE = 2000
MAX_QUESTIONS = 4
MAX_OPTIONS = 8
NO_SANDBOX = "no_sandbox"  # a `warn` code: the call asks to run outside the sandbox

STORE = Store()
_seen: dict[str, float] = {}  # nonces of signed requests, against replays
_seen_lock = threading.Lock()


# ---- the token and the signed envelope of the hook's routes ------------------------------------


def token_path() -> Path:
    return config_dir() / "approvals-token"


def ensure_token() -> Path:
    """The token file, created at mode 0600 if missing: `sya hooks install` makes it, the hook reads it."""
    path = token_path()
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".approvals-token-")  # created as 0600
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
        os.replace(tmp, path)
    return path


def _token() -> str:
    try:
        return token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def opened(path: str, body: bytes) -> tuple[dict, str, str] | None:
    """(payload, nonce, token) of a request the hook signed with the token, fresh and not seen before."""
    token = _token()
    try:
        env = json.loads(body)
        nonce, at, text = env["nonce"], env["time"], env["body"]
        fresh = abs(time.time() - int(at)) <= SKEW_S
    except (ValueError, TypeError, KeyError):
        return None
    if not (token and fresh and isinstance(nonce, str) and NONCE.fullmatch(nonce) and isinstance(text, str)):
        return None
    if not same(env.get("mac"), mac(token, "req", path, nonce, str(at), text)):
        return None
    now = time.monotonic()
    with _seen_lock:
        for old, seen_at in list(_seen.items()):
            if now - seen_at > 2 * SKEW_S:
                del _seen[old]
        if nonce in _seen:
            return None
        _seen[nonce] = now
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return (payload, nonce, token) if isinstance(payload, dict) else None


def sealed(token: str, path: str, nonce: str, payload: dict) -> Reply:
    """A reply the hook can check: the exact body and its HMAC, bound to the request's nonce and path."""
    text = json.dumps(payload, ensure_ascii=False)
    return json_reply({"body": text, "mac": mac(token, "resp", path, nonce, text)})


def page_may_decide(app: App, headers) -> bool:
    """Who may answer a prompt: on top of the host's POST guard (Host, Origin, X-SYA), a browser the user
    paired, by the secret its page sends as X-SYA-Pair, on 127.0.0.1 only."""
    return app.pairing.paired(headers)


# ---- what the page shows ----------------------------------------------------------------------


def _text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)


def _line(text: str, limit: int = 120) -> str:
    first = " ".join(text.strip().splitlines()[:1]) if text.strip() else ""
    return first if len(first) <= limit else first[: limit - 1] + "…"


def questions_of(tool_input: dict) -> tuple[list[dict], bool]:
    """AskUserQuestion's questions as the page draws them, and whether that is all of them: a question or an
    option with a field the form does not draw, or too long to draw, makes it partial."""
    raw = tool_input.get("questions")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_QUESTIONS:
        return [], False
    out, whole = [], True
    for q in raw:
        if not isinstance(q, dict) or not isinstance(q.get("question"), str):
            return [], False
        options = q.get("options") if isinstance(q.get("options"), list) else []
        whole &= (
            set(q) <= {"question", "header", "options", "multiSelect"} and 0 < len(options) <= MAX_OPTIONS
        )
        whole &= isinstance(q.get("header", ""), str) and len(q.get("header", "")) <= 200
        drawn = []
        for o in options[:MAX_OPTIONS]:
            if not isinstance(o, dict) or not isinstance(o.get("label"), str):
                whole = False
                continue
            description = o.get("description", "")
            whole &= set(o) <= {"label", "description"} and isinstance(description, str)
            whole &= len(o["label"]) <= 500 and len(_text(description)) <= MAX_FIELD
            drawn.append({"label": o["label"], "description": _text(description)[:MAX_FIELD]})
        out.append({"question": q["question"], "header": _text(q.get("header", ""))[:200], "options": drawn,
                    "multi": q.get("multiSelect") is True})  # fmt: skip
    return out, whole


def short_path(path: object, cwd: object) -> str:
    """A path as the user reads it: relative to the session's folder inside it, ~ for the home folder."""
    if not isinstance(path, str) or not path:
        return ""
    if isinstance(cwd, str) and cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1 :]
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home + "/") else path


def _summary(tool: str, inp: dict, cwd: str) -> str:
    """One line for the card, cut freely: the chat panel shows the call in full before it can be allowed.
    "" for a plan without a title or a question without text: the page words that."""
    if tool == "Bash":
        return f"Bash: {_line(_text(inp.get('command', '')))}"
    if tool == PLAN:
        plan = _text(inp.get("plan", ""))
        return _line(next((ln.lstrip("# ") for ln in plan.splitlines() if ln.strip()), ""))
    if tool == ASK:
        qs = inp.get("questions")
        first = qs[0].get("question") if isinstance(qs, list) and qs and isinstance(qs[0], dict) else ""
        return _line(_text(first or ""))
    if path := short_path(inp.get("file_path") or inp.get("notebook_path") or inp.get("path"), cwd):
        return f"{tool}: {_line(path)}"
    first = inp.get("url") or next((v for v in inp.values() if isinstance(v, str) and v.strip()), "")
    return f"{tool}: {_line(_text(first))}" if first else tool


BODY = {"Bash": "command", "Write": "content", PLAN: "plan"}  # the input field drawn as the call's body


def view(tool: str, tool_input: dict, cwd: str = "") -> dict:
    """What the card and the chat panel show: {summary, text, plan, questions, fields, warn, complete}.

    Every input field is drawn: the body (`text`, `plan` or `questions`) and each other field as a name and
    a value. `complete` is False when anything is cut or not drawn; then the page offers no Allow, since an
    allow approves the whole input, seen or not. `warn` holds codes (NO_SANDBOX) the page words."""
    inp = tool_input
    out: dict = {"summary": _summary(tool, inp, cwd), "fields": [], "warn": [], "complete": True}
    body = BODY.get(tool)
    if body and body in inp:
        text = _text(inp[body])
        out["plan" if tool == PLAN else "text"] = text[:MAX_BODY]
        out["complete"] &= len(text) <= MAX_BODY
    if tool == ASK:
        out["questions"], whole = questions_of(inp)
        out["complete"] &= whole
        body = "questions"
    rest = [(k, _text(v)) for k, v in inp.items() if k != body]
    out["fields"] = [[k, v[:MAX_FIELD]] for k, v in rest[:MAX_FIELDS]]
    out["complete"] &= len(rest) <= MAX_FIELDS and all(len(v) <= MAX_FIELD for _, v in rest)
    if inp.get("dangerouslyDisableSandbox") is True:
        out["warn"].append(NO_SANDBOX)
    return out


# ---- requests ---------------------------------------------------------------------------------


def answers_prompts(provider_id: object) -> bool:
    """The session's program shows its permission prompts to the dashboard (Cap.PERMISSION_WAIT): Claude Code,
    whose hook this feature installs. Codex has no such cap, so its sessions never get a request."""
    try:
        return Cap.PERMISSION_WAIT in providers.by_id(str(provider_id)).caps
    except KeyError:
        return False


def _json(body: bytes) -> dict | None:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _ask(app: App, data: dict) -> dict | str:
    """The new request's id, or what is wrong with it (for the hook: English)."""
    sid, tool, tool_input = data.get("session_id"), data.get("tool_name"), data.get("tool_input")
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        return "session_id"
    if (live := app.live().get(sid)) is not None and not answers_prompts(live.provider):
        return "this program has no permission prompts"
    if not isinstance(tool, str) or not 0 < len(tool) <= 200:
        return "tool_name"
    if not isinstance(tool_input, dict):
        tool_input = {}
    req = STORE.add(sid, tool, view(tool, tool_input, data.get("cwd") or ""))
    return {"id": req.id} if req else "too many requests are waiting for an answer"


def _answers(req: Request, raw: object) -> dict[str, str] | None:
    """Every question answered with a non-empty string of a sane length, and nothing else."""
    questions = [q["question"] for q in req.view.get("questions", [])]
    if not isinstance(raw, dict) or not questions or set(raw) != set(questions):
        return None
    if not all(isinstance(v, str) and 0 < len(v.strip()) <= MAX_MESSAGE for v in raw.values()):
        return None
    return {q: raw[q].strip() for q in questions}


def _decide(app: App, rid: str, body: bytes) -> Reply:
    """The page's answer; a refusal is a Problem the host words in the page's language."""
    data = _json(body)
    req = STORE.get(rid)
    # answered in the terminal: the hook may not have noticed yet, the live state already has
    live = app.live().get(req.session_id) if req else None
    if req is None or req.decision is not None or not waiting(live) or not answers_prompts(live.provider):
        raise Problem("approvals.not_open", HTTPStatus.CONFLICT)
    if data is None or data.get("behavior") not in ("allow", "deny"):
        raise Problem("approvals.bad_behavior")
    decision: dict = {"behavior": data["behavior"]}
    if data["behavior"] == "deny":
        message = data.get("message") or ""
        if not isinstance(message, str) or len(message) > MAX_MESSAGE:
            raise Problem("approvals.bad_message", max=MAX_MESSAGE)
        decision["message"] = message.strip()
    elif not req.view.get("complete"):
        raise Problem("approvals.not_all_shown", HTTPStatus.CONFLICT)
    elif req.tool == ASK:
        if (answers := _answers(req, data.get("answers"))) is None:
            raise Problem("approvals.answer_all")
        decision["answers"] = answers
    if not STORE.decide(rid, decision):
        raise Problem("approvals.not_open", HTTPStatus.CONFLICT)
    if not STORE.settle(rid, SETTLE_S):
        raise Problem("approvals.not_taken", HTTPStatus.CONFLICT)
    return json_reply({"ok": True})


def handle_post(app: App, path: str, body: bytes, headers) -> Reply | None:
    m = ACTION.fullmatch(path)
    if m and m[2] == "decide":  # the page; the host has checked Host, Origin and X-SYA
        if not page_may_decide(app, headers):
            error = t(request_lang(), "app.pair.not_paired")
            return json_reply({"error": error, "unpaired": True}, HTTPStatus.FORBIDDEN)
        return _decide(app, m[1], body)
    if not m and path not in (f"{ROUTE}/hello", f"{ROUTE}/ask"):
        return None
    if (found := opened(path, body)) is None:
        return json_reply({"error": "a signature with the hook's token is required"}, HTTPStatus.FORBIDDEN)
    payload, nonce, token = found
    if path == f"{ROUTE}/hello":
        return sealed(token, path, nonce, {"ok": True})
    if path == f"{ROUTE}/ask":
        made = _ask(app, payload)
        if isinstance(made, str):
            return json_reply({"error": made}, HTTPStatus.BAD_REQUEST)
        return sealed(token, path, nonce, made)
    if m[2] == "withdraw":
        STORE.withdraw(m[1])
        return sealed(token, path, nonce, {"ok": True})
    STORE.sweep(app.live())
    state, decision = STORE.wait(m[1], WAIT_S)
    return sealed(token, path, nonce, {"state": state, **({"decision": decision} if decision else {})})


def card_fields(rec: dict, live) -> dict:
    """`approvals` on the card of a session with prompts the page can answer: only while the session shows a
    prompt, so one answered in the terminal is not offered for the seconds its hook needs to notice."""
    if not (waiting(live) and answers_prompts(live.provider)):
        return {}
    found = STORE.open_for(rec["session_id"])
    return {"approvals": [r.public() for r in found]} if found else {}


def board_fields(app: App) -> dict:
    """Nothing for the board itself: each build drops requests nobody waits for, so a prompt whose hook was
    killed without a word leaves the cards even when no other hook polls."""
    STORE.sweep(app.live())
    return {}
