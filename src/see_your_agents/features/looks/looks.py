"""Per-agent looks in the office (shape, hue, desk, things on the desk), kept across browsers.

Routes: GET and POST /api/looks. The document lives in
config_dir()/looks.json as {"sessions": {session_id: look}, "projects": {project: look}}. A refusal is a
Problem: the host words it in the page's language.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
from typing import TYPE_CHECKING

from ... import config_dir
from ...app.replies import Reply, json_reply
from ...i18n import Problem

if TYPE_CHECKING:
    from email.message import Message
    from pathlib import Path

    from ...app.server import App

ROUTE = "/api/looks"
LOOK_KEYS = {"shape", "hue", "desk", "items", "label"}
POST_KEYS = {"session_id", "project", "look"}
ID = re.compile(r"[a-z0-9-]{1,24}")
SESSION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
MAX_ITEMS = 4
MAX_LABEL = 40
MAX_PROJECT = 200

_lock = threading.Lock()


def clean_look(raw: object) -> dict:
    """The look with every field checked; a Problem names the first thing wrong."""
    if not isinstance(raw, dict):
        raise Problem("looks.bad.look")
    if extra := set(raw) - LOOK_KEYS:
        raise Problem("looks.bad.look_keys", keys=", ".join(sorted(extra)))
    look: dict = {}
    for key in ("shape", "desk"):
        value = raw.get(key)
        if value is not None and not (isinstance(value, str) and ID.fullmatch(value)):
            raise Problem("looks.bad.id", field=key)
        look[key] = value
    hue = raw.get("hue")
    if hue is not None and not (isinstance(hue, int) and not isinstance(hue, bool) and 0 <= hue <= 359):
        raise Problem("looks.bad.hue")
    look["hue"] = hue
    items = raw.get("items", [])
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise Problem("looks.bad.items", max=MAX_ITEMS)
    if not all(isinstance(i, str) and ID.fullmatch(i) for i in items):
        raise Problem("looks.bad.item_id")
    look["items"] = list(dict.fromkeys(items))
    label = raw.get("label", "")
    if not isinstance(label, str):
        raise Problem("looks.bad.label")
    look["label"] = label.strip()[:MAX_LABEL].strip()
    return look


def _file() -> Path:
    return config_dir() / "looks.json"


def _empty() -> dict:
    return {"sessions": {}, "projects": {}}


def _tidy(table: object) -> dict:
    """Entries that still pass validation; hand-edited or older junk is dropped, not served."""
    out = {}
    for key, raw in table.items() if isinstance(table, dict) else ():
        try:
            out[key] = clean_look(raw)
        except Problem:
            continue
    return out


def load() -> dict:
    path = _file()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError):
        # keep the unreadable file for the user instead of overwriting it with the next save
        with contextlib.suppress(OSError):
            path.replace(path.with_name("looks.json.bad"))
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    return {"sessions": _tidy(data.get("sessions")), "projects": _tidy(data.get("projects"))}


def _save(doc: dict) -> None:
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    tmp.replace(path)


def handle_get(app: App, path: str, query: dict[str, str]) -> Reply | None:
    if path != ROUTE:
        return None
    with _lock:
        return json_reply(load())


def handle_post(app: App, path: str, body: bytes, headers: Message) -> Reply | None:
    if path != ROUTE:
        return None
    try:
        req = json.loads(body)
    except ValueError:
        raise Problem("app.bad_body") from None
    if not isinstance(req, dict) or "look" not in req or set(req) - POST_KEYS:
        raise Problem("looks.bad.request")
    if ("session_id" in req) == ("project" in req):
        raise Problem("looks.bad.one_target")
    kind, key = ("sessions", req["session_id"]) if "session_id" in req else ("projects", req["project"])
    valid_key = isinstance(key, str) and 0 < len(key) <= MAX_PROJECT
    if valid_key and kind == "sessions":
        valid_key = bool(SESSION_ID.fullmatch(key))
    if not valid_key:
        raise Problem("looks.bad.session" if kind == "sessions" else "looks.bad.project")
    look = None if req["look"] is None else clean_look(req["look"])
    with _lock:
        doc = load()
        if look is None:
            doc[kind].pop(key, None)
        else:
            doc[kind][key] = look
        _save(doc)
    return json_reply(doc)
