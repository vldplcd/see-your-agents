"""Writing to sessions from the dashboard: POST /api/chat/<id>/<action>, action one of send, upload, stop,
done, focus.

How a message reaches a session, how it stops and which window shows it is its provider's business
(`Provider.route`, `send`, `resume`, `stop`, `focus`; Claude's are core/providers/claude/routes.py). Here: the
request's checks, the uploads, the reply, `start_resume`, which the supervisor's resume_session and the
morning review use for a stopped session, and `send_signed`, the supervisor's send_to_session. The VS Code
bridge (bridge.py) reaches Claude's routes through the chat's `Feature.setup` (chat/__init__.py).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote

from ...app.replies import Reply, json_reply
from ...core import done, providers
from ...core.providers.base import (
    MAX_TEXT,
    LiveSession,
    Message,
    Provider,
    Route,
    SendError,
    Sent,
    Session,
)
from ...core.uploads import session_uploads, uploads_root
from ...i18n import Problem

if TYPE_CHECKING:
    from email.message import Message as Headers

    from ...app.server import App

MAX_NAME = 80
ACTION = re.compile(r"/api/chat/([A-Za-z0-9_-]{1,64})/(send|upload|stop|done|focus)")


class DeliveryError(Problem):
    """A write did not happen: a catalog key (chat.*, or the provider's app.provider.*) and its params with
    the HTTP status. The host words it for the page; str() is English, for an agent."""

    @classmethod
    def of(cls, exc: SendError) -> DeliveryError:
        return cls(exc.key, exc.status, **exc.params)


def _session(app: App, session_id: str) -> tuple[Provider, Session]:
    live = app.live().get(session_id)
    rec = app.index.record(session_id) or (
        {"session_id": session_id, "cwd": live.cwd, "provider": live.provider} if live else None
    )
    if rec is None:
        raise DeliveryError("chat.no_session", HTTPStatus.NOT_FOUND)
    return providers.by_id(rec["provider"]), Session.of({**rec, "session_id": session_id}, live)


def card_fields(rec: dict, live: LiveSession | None) -> dict:
    """What the chat adds to a board card: how a message would reach the session, and a reply in progress."""
    route = providers.by_id(rec["provider"]).route(Session.of(rec, live))
    return {"delivery": route.public(), "running_reply": route.pending}


def _route(app: App, session_id: str) -> Route:
    provider, session = _session(app, session_id)
    return provider.route(session)


def delivery_for(app: App, ref: str | dict) -> dict:
    """{route, can_send, reason, alt} for a session id or a board card."""
    try:
        return _route(app, ref["session_id"] if isinstance(ref, dict) else ref).public()
    except DeliveryError as exc:
        return Route(None, False, exc.key, params=exc.params).public()


def start_resume(app: App, session_id: str, text: str) -> Sent:
    """One turn in a stopped session, in the background, by its provider; the flags come from the feature that
    launched the session (`Feature.resume_args`)."""
    provider, session = _session(app, session_id)
    try:
        return provider.resume(session, Message(text))
    except SendError as exc:
        raise DeliveryError.of(exc) from exc


def send_signed(app: App, session_id: str, message: Message) -> Sent:
    """A message from someone other than the user (`Message.sender`: the supervisor) by the session's
    provider, on its signed route: never typed into a terminal."""
    if not message.sender:
        raise ValueError("a signed message needs its sender")
    provider, session = _session(app, session_id)
    try:
        return provider.send(session, message)
    except SendError as exc:
        raise DeliveryError.of(exc) from exc


# ---- the request ------------------------------------------------------------------------------


def sanitize_name(raw: str) -> str:
    """A basename of [A-Za-z0-9._-] only, at most MAX_NAME long, never empty or hidden."""
    base = re.split(r"[\\/]", unquote(raw))[-1]
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    if len(base) > MAX_NAME:
        suffix = Path(base).suffix[:12]
        base = base[: MAX_NAME - len(suffix)] + suffix
    return base if base.strip("_") else "file"


def _json_body(body: bytes) -> dict:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise DeliveryError("app.bad_body")
    return data


def _upload_paths(files: object) -> list[str]:
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise DeliveryError("chat.files_not_paths")
    root = uploads_root().resolve()
    paths = []
    for f in files:
        p = Path(f).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            raise DeliveryError("chat.not_an_upload", name=Path(f).name)
        paths.append(str(p))
    return paths


def clean_text(text: str) -> str:
    """An ESC in the text could close a bracketed paste early and turn the rest into keystrokes."""
    return text.replace("\x1b", "").replace("\r", "").strip()


# ---- actions ----------------------------------------------------------------------------------


def send(app: App, session_id: str, body: bytes, headers: Headers) -> dict:
    data = _json_body(body)
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise DeliveryError("chat.text_empty")
    if len(text) > MAX_TEXT:
        raise DeliveryError("chat.text_too_long", max=MAX_TEXT)
    files = tuple(_upload_paths(data.get("files") or []))
    provider, session = _session(app, session_id)
    sent = provider.send(session, Message(clean_text(text), files, data.get("takeover") is True))
    return {"route": sent.route, "log": sent.log} if sent.log else {"route": sent.route}


def upload(app: App, session_id: str, body: bytes, headers: Headers) -> dict:
    _session(app, session_id)
    if not body:
        raise DeliveryError("chat.file_empty")
    name = sanitize_name(headers.get("X-Filename") or "")
    folder = session_uploads(session_id)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    for n in range(100):
        final = f"{stamp}-{name}" if n == 0 else f"{stamp}-{n}-{name}"
        try:
            fd = os.open(folder / final, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise DeliveryError("chat.no_file_name", HTTPStatus.CONFLICT)
    with os.fdopen(fd, "wb") as f:
        f.write(body)
    return {"path": str((folder / final).resolve()), "name": final}


def stop(app: App, session_id: str, body: bytes, headers: Headers) -> dict:
    provider, session = _session(app, session_id)
    if session.live is None:
        raise DeliveryError("chat.not_running", HTTPStatus.CONFLICT)
    provider.stop(session)
    done.mark(session_id, True)
    return {"stopped": True}


def set_done(app: App, session_id: str, body: bytes, headers: Headers) -> dict:
    _session(app, session_id)
    flag = _json_body(body).get("done")
    if not isinstance(flag, bool):
        raise DeliveryError("chat.bad_done")
    done.mark(session_id, flag)
    return {"done": flag}


def focus(app: App, session_id: str, body: bytes, headers: Headers) -> dict:
    provider, session = _session(app, session_id)
    provider.focus(session)
    return {}


ACTIONS = {"send": send, "upload": upload, "stop": stop, "done": set_done, "focus": focus}


def handle_post(app: App, path: str, body: bytes, headers: Headers) -> Reply | None:
    match = ACTION.fullmatch(path)
    if not match:
        return None
    session_id, action = match.groups()
    try:
        result = ACTIONS[action](app, session_id, body, headers)
    except Problem:  # SendError, DeliveryError: the host words it, {"error": ...} with its status
        raise
    except Exception as exc:  # the page needs a JSON error, not a dropped connection
        raise Problem("chat.internal_error", HTTPStatus.INTERNAL_SERVER_ERROR, error=repr(exc)) from exc
    return json_reply({"ok": True, **result})
