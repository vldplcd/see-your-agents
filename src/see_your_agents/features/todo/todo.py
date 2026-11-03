"""The todo list: items in config_dir()/todo.json, the page's routes and the supervisor's tools.

Jira keys are not stored: they are read from the text every time, so a link follows the current base URL.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING

from ... import config_dir
from ...app.mcp import Tool, string_arg
from ...app.replies import Reply, json_reply
from ...core import providers
from ...core.jira import jira_keys_in
from ...core.providers.base import Message, Provider, Session
from ...core.sessions import jira_base_url
from ...i18n import Problem, message, request_lang
from ..chat import delivery as chat_delivery

if TYPE_CHECKING:
    from email.message import Message as Headers

    from ...app.server import App

ROUTE = "/api/todo"
MORNING_ROUTE = "/api/todo/morning"
MAX_ITEMS = 300
MAX_TEXT = 500
DONE_SHOWN_FOR = timedelta(hours=24)
# What the supervisor reads (this message, its sender and summary, the tool descriptions below) is for an
# agent: English, never catalog strings (docs/i18n.md). Plain ASCII: the courier copies it verbatim.
MORNING_SUMMARY = "Morning review"
# The chat's courier sender (core/providers/claude/routes.SENDER): the courier puts its reply note, "answer
# in plain text here", only before a message from that sender. Change the two together.
MORNING_SENDER = "Dashboard"
MORNING = """Morning review. The user pressed the button on the dashboard and asks for:
1. What the agents have done since yesterday, what is left and who is waiting for the user
   (fleet_overview, list_delegations with since).
2. Which Jira tickets assigned to the user are open (jira_search), if the jira server is connected.
3. To-dos for today: urgent ones and the rest separately, with ticket keys.
   Add them with todo_add only after the user says yes.
4. A 2-5 line summary posted with post_digest.
The user's dashboard language is {lang}: answer in that language."""


def morning_text(lang: str) -> str:
    """The morning review's request, with the language (a code: en, ru) the person reads the dashboard in."""
    return MORNING.format(lang=lang)


_lock = threading.Lock()
_morning = {
    "state": "idle",
    "error": None,
    "at": "",
}  # idle | sending | sent | error; GET /api/todo shows it, the error worded in the reader's language


def not_running(reason: str = "", status: int = HTTPStatus.CONFLICT) -> Problem:
    """The morning button with no supervisor to ask: the page says how to start one, and why, if known."""
    if reason:
        return Problem("todo.not_running_because", status, reason=reason)
    return Problem("todo.not_running", status)


# ---- storage ----------------------------------------------------------------------------------


def _path():
    return config_dir() / "todo.json"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> list[dict]:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [i for i in data if isinstance(i, dict) and isinstance(i.get("id"), str)]


def _save(items: list[dict]) -> None:
    folder = _path().parent
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".todo-")  # mkstemp creates the file as 0600
    with os.fdopen(fd, "w") as f:
        json.dump(items, f, ensure_ascii=False)
    os.replace(tmp, _path())


# ---- operations -------------------------------------------------------------------------------


def _text(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not 0 < len(text) <= MAX_TEXT:
        raise Problem("todo.bad_text", max=MAX_TEXT)
    return text


def _flag(body: dict, key: str) -> bool:
    if not isinstance(body.get(key), bool):
        raise Problem("todo.bad_flag", field=key)
    return body[key]


def _session_id(value: object) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 100:
        raise Problem("todo.bad_session")
    return value


def _find(items: list[dict], body: dict) -> dict:
    item = next((i for i in items if i["id"] == body.get("id")), None)
    if item is None:
        raise Problem("todo.no_such_item", HTTPStatus.NOT_FOUND)
    return item


def _new_item(body: dict, source: str, count: int) -> dict:
    text, urgent = body.get("text"), body.get("urgent", False)
    if isinstance(text, str) and text.lstrip().startswith("!"):
        text, urgent = text.lstrip()[1:], True
    if not isinstance(urgent, bool):
        raise Problem("todo.bad_flag", field="urgent")
    if count >= MAX_ITEMS:
        raise Problem("todo.too_many", n=MAX_ITEMS)
    item = {
        "id": uuid.uuid4().hex[:8],
        "text": _text(text),
        "urgent": urgent,
        "done": False,
        "created_at": _now(),
        "done_at": None,
        "source": source,
    }
    if sid := _session_id(body.get("session_id")):
        item["session_id"] = sid
    return item


def _change(item: dict, body: dict) -> None:
    if not {"text", "urgent", "done", "session_id"} & body.keys():
        raise Problem("todo.update_needs_field")
    if "text" in body:
        item["text"] = _text(body["text"])
    if "urgent" in body:
        item["urgent"] = _flag(body, "urgent")
    if "done" in body and _flag(body, "done") != item["done"]:
        item["done"], item["done_at"] = body["done"], _now() if body["done"] else None
    if "session_id" in body:
        if sid := _session_id(body["session_id"]):
            item["session_id"] = sid
        else:
            item.pop("session_id", None)


def apply(body: dict, source: str = "you") -> tuple[list[dict], dict | None]:
    """Run one action on the stored list; returns the new list and the item that was added or changed.
    A bad action raises a Problem: the host words it for the page, and MCP in English for the supervisor."""
    with _lock:
        items, touched = _load(), None
        match body.get("action"):
            case "add":
                touched = _new_item(body, source, len(items))
                items.append(touched)
            case "update":
                touched = _find(items, body)
                _change(touched, body)
            case "delete":
                items.remove(_find(items, body))
            case "clear_done":
                items = [i for i in items if not i["done"]]
            case _:
                raise Problem("todo.bad_action")
        _save(items)
        return items, touched


def _keys(item: dict) -> list[str]:
    return list(dict.fromkeys(jira_keys_in(item["text"])))


def _view(app: App, items: list[dict]) -> list[dict]:
    """Items with the derived fields: Jira links from the text, the title of the attached session."""
    base = jira_base_url(app.index.records())
    out = []
    for item in items:
        view = {
            **item,
            "jira": [{"key": k, "url": f"{base}/browse/{k}" if base else ""} for k in _keys(item)],
        }
        if (sid := item.get("session_id")) and (rec := app.index.record(sid)):
            view["session_title"] = rec.get("title") or ""
        out.append(view)
    return out


def counts(items: list[dict]) -> dict:
    open_items = [i for i in items if not i["done"]]
    return {"urgent": sum(i["urgent"] for i in open_items), "open": len(open_items)}


def board_fields(app: App) -> dict:
    with _lock:
        return {"todo": counts(_load())}


# ---- morning review ---------------------------------------------------------------------------


def _send_morning(provider: Provider, session: Session, text: str) -> None:
    """A signed message: the live supervisor sees it from the dashboard (MORNING_SENDER), not typed into its
    terminal."""
    try:
        provider.send(session, Message(text, sender=MORNING_SENDER, summary=MORNING_SUMMARY))
        result = {"state": "sent", "error": None}
    except Exception as exc:  # SendError or worse: the thread must always leave a verdict for the page
        result = {"state": "error", "error": exc}  # worded when the page reads it, in the page's language
    with _lock:
        _morning.update(**result, at=_now())


def _resume_morning(app: App, session_id: str) -> Reply:
    """No live supervisor: continue its session one turn through the chat's resume path, which gives it its
    own config. Only the supervisor: another session would run the text with the user's tools."""
    if session_id != app.board().get("supervisor_id"):
        raise not_running()
    route = chat_delivery.delivery_for(app, session_id)
    if route["route"] != "resume" or not route["can_send"]:
        raise not_running(route["reason"])
    try:
        chat_delivery.start_resume(app, session_id, morning_text(request_lang()))
    except chat_delivery.DeliveryError as exc:
        raise not_running(message(exc, request_lang()), exc.status) from exc
    with _lock:
        _morning.update(state="sent", error=None, at=_now())
    return json_reply({"state": "sent"}, HTTPStatus.ACCEPTED)


def start_morning(app: App, session_id: object) -> Reply:
    if not isinstance(session_id, str):
        raise not_running()
    live = app.live().get(session_id)
    if live is None:
        return _resume_morning(app, session_id)
    if not live.name:
        raise not_running()
    with _lock:
        already = _morning["state"] == "sending"
        _morning.update(state="sending", error=None, at=_now())
    if not already:
        rec = app.index.record(session_id) or {"session_id": session_id, "provider": live.provider}
        target = (providers.by_id(rec["provider"]), Session.of(rec, live), morning_text(request_lang()))
        threading.Thread(target=_send_morning, args=target, daemon=True).start()
    return json_reply({"state": "sending"}, HTTPStatus.ACCEPTED)


# ---- routes -----------------------------------------------------------------------------------


def _payload(app: App, items: list[dict]) -> dict:
    with _lock:
        morning = dict(_morning)
    morning["error"] = message(morning["error"], request_lang()) if morning["error"] else ""
    return {"items": _view(app, items), **counts(items), "morning": morning}


def handle_get(app: App, path: str, query: dict[str, str]) -> Reply | None:
    if path != ROUTE:
        return None
    with _lock:
        items = _load()
    return json_reply(_payload(app, items))


def handle_post(app: App, path: str, body: bytes, headers: Headers) -> Reply | None:
    if path not in (ROUTE, MORNING_ROUTE):
        return None
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise Problem("app.bad_body")
    if path == MORNING_ROUTE:
        return start_morning(app, data.get("session_id"))
    items, _ = apply(data)  # a Problem becomes {"error": ...} in the page's language (the host)
    return json_reply(_payload(app, items))


# ---- MCP tools --------------------------------------------------------------------------------


def _tool_item(item: dict) -> dict:
    out = {**item, "jira_keys": _keys(item)}
    return {k: v for k, v in out.items() if v not in (None, [])}


def _list(app: App, args: dict) -> dict:
    with _lock:
        items = _load()
    cutoff = (datetime.now(UTC) - DONE_SHOWN_FOR).strftime("%Y-%m-%dT%H:%M:%SZ")
    open_items = sorted((i for i in items if not i["done"]), key=lambda i: not i["urgent"])
    recent = [i for i in items if i["done"] and (i.get("done_at") or "") >= cutoff]
    return {"open": [_tool_item(i) for i in open_items], "done_last_24h": [_tool_item(i) for i in recent]}


def _run(body: dict) -> dict:
    items, touched = apply(body, source="supervisor")  # a Problem reaches the supervisor in English (mcp.py)
    return {"id": touched["id"], **counts(items)}


def _add(app: App, args: dict) -> dict:
    body = {"action": "add", "text": string_arg(args, "text"), "urgent": args.get("urgent", False)}
    if "session_id" in args:
        body["session_id"] = string_arg(args, "session_id")
    return _run(body)


def _update(app: App, args: dict) -> dict:
    body = {"action": "update", "id": string_arg(args, "id")}
    body.update({k: args[k] for k in ("text", "urgent", "done") if k in args})
    return _run(body)


TOOLS = (
    Tool(
        "todo_list",
        "read",
        "The user's to-do list: open items first (urgent ones on top), then the ones done in the last 24 "
        "hours. Jira keys found in an item's text are listed in jira_keys.",
        {},
        (),
        _list,
    ),
    Tool(
        "todo_add",
        "control",
        "Add an item to the user's to-do list. Put a ticket key (PROJ-123) in the text: it becomes a link. "
        'urgent: true means "today, before everything else".',
        {
            "text": {"type": "string", "description": "The item's text, up to 500 characters"},
            "urgent": {"type": "boolean", "description": "Urgent"},
            "session_id": {"type": "string", "description": "The session the item belongs to"},
        },
        ("text",),
        _add,
        read_only=False,
    ),
    Tool(
        "todo_update",
        "control",
        "Change an item: mark it done (done), make it urgent or normal (urgent), correct its text.",
        {
            "id": {"type": "string", "description": "The item's id from todo_list"},
            "done": {"type": "boolean"},
            "urgent": {"type": "boolean"},
            "text": {"type": "string"},
        },
        ("id",),
        _update,
        read_only=False,
    ),
)
