"""The builder's doors: the MCP tool that starts a build, the board field, and the page's routes.

Accept, rollback, reject, preview and Rework are page POSTs only (the host checked Host, Origin, X-SYA and the
paired browser). No MCP tool can merge or revert: the supervisor reads other sessions' text, and planted text
must not be able to land code. A refusal is {ok: false, error} in the request's language; the tool's is
English, for the agent.
"""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

from ...app.mcp import Tool, ToolError, string_arg
from ...app.replies import Reply, json_reply
from ...i18n import Problem, request_lang
from . import builder, preview, repo
from .builder import BuildError

if TYPE_CHECKING:
    from email.message import Message

    from ...app.server import App

ROUTE = "/api/builder"
ACTION = re.compile(r"/api/builder/([a-z0-9-]{1,64})/(preview|comment|accept|rollback|reject)")
DIFF = re.compile(r"/api/builder/([a-z0-9-]{1,64})/diff")
MAX_TITLE, MAX_DESCRIPTION, MAX_COMMENT = 120, 8000, 8000


def board_fields(app: App) -> dict:
    """`builder` on /api/board: the banner's builds and the running preview. Moves the builds along too."""
    builder.tick(app)
    live = app.live()
    builds = [builder.public(b, builder.live_for(b, live)) for b in builder.mine() if builder.shown(b)]
    return {"builder": {"builds": builds, "preview": preview.current()}}


def _request_change(app: App, args: dict) -> dict:
    title = " ".join(string_arg(args, "title", limit=MAX_TITLE).split())
    description = string_arg(args, "description", limit=MAX_DESCRIPTION).strip()
    try:
        b = builder.create(title, description)
    except (BuildError, repo.GitError) as exc:
        raise ToolError(str(exc)) from exc
    return {
        "id": b["id"],
        "branch": b["branch"],
        "status": b["status"],
        "session_id": b["session_id"],
        "note": "A build card is on the dashboard now. The build is done when the card says it is ready; "
        "only the user can accept, reject or roll it back, with the card's buttons.",
    }


TOOL = Tool(
    "request_change",
    "control",
    "Ask the builder to change the dashboard itself: a separate agent does it in its own branch and worktree "
    "following the add-feature skill, the dashboard checks its work and shows the user a card with a "
    "preview, the diff and Accept and Reject buttons. The description must stand on its own: what to do, "
    "where it is on the screen, how to tell it is done.",
    {
        "title": {"type": "string", "description": "Short, up to 120 characters: “hello-clock module”"},
        "description": {"type": "string", "description": "What, where on the screen, what “done” means"},
    },
    ("title", "description"),
    _request_change,
    read_only=False,
    open_world=True,
    confirm=True,
)


def _body(body: bytes) -> dict:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise Problem("app.bad_body")
    return data


def _comment_text(data: dict) -> str:
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise BuildError("builder.no_comment", HTTPStatus.BAD_REQUEST)
    if len(text) > MAX_COMMENT:
        raise BuildError("builder.comment_too_long", HTTPStatus.BAD_REQUEST, max=MAX_COMMENT)
    return text.replace("\x1b", "").replace("\r", "").strip()


def _act(app: App, build_id: str, action: str, body: bytes) -> dict:
    match action:
        case "accept":
            return builder.accept(app, build_id)
        case "rollback":
            return builder.rollback(app, build_id)
        case "reject":
            return builder.reject(app, build_id)
        case "comment":
            return builder.comment(app, build_id, _comment_text(_body(body)))
        case _:  # preview: a PreviewError or SandboxError is a 502
            return {"preview": preview.start(builder.find(build_id), app.port)}


def _guarded(fn) -> Reply:
    try:
        return json_reply({"ok": True, **fn()})
    except Problem as exc:
        return json_reply({"ok": False, "error": exc.text(request_lang())}, exc.status)
    except repo.GitError as exc:
        return json_reply({"ok": False, "error": f"git: {exc}"}, HTTPStatus.CONFLICT)


def handle_get(app: App, path: str, query: dict[str, str]) -> Reply | None:
    if path == ROUTE:
        return json_reply(board_fields(app)["builder"])
    if found := DIFF.fullmatch(path):
        return _guarded(lambda: _diff(found[1]))
    return None


def _diff(build_id: str) -> dict:
    b = builder.find(build_id)
    return repo.diff(Path(b["checkout"]), b["base_sha"], b["branch"])


def handle_post(app: App, path: str, body: bytes, headers: Message) -> Reply | None:
    if path == f"{ROUTE}/stop-preview":
        return _guarded(lambda: {"stopped": preview.stop()})
    if found := ACTION.fullmatch(path):
        return _guarded(lambda: _act(app, found[1], found[2], body))
    return None
