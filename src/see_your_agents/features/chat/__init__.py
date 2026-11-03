"""Chat: a session's transcript as a chat, and writing into sessions (text, files, stop, done, focus)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...app.feature import Feature
from ...core.providers.claude import routes as claude_routes
from . import bridge, delivery, thread

if TYPE_CHECKING:
    from ...app.server import App


def wire(app: App) -> None:
    """Claude's routes type into VS Code terminals through the bridge, see every live session (a peer name
    must be unique) and ask the host for the flags of a resume (`Feature.resume_args`)."""
    claude_routes.plug(bridge, app.live, app.resume_args)


FEATURE = Feature(
    id="chat",
    folder=Path(__file__).parent,
    handle_get=thread.handle_get,
    handle_post=delivery.handle_post,
    add_cli=bridge.add_cli,
    scripts=("chat.js", "quote.js"),
    styles=("chat.css", "quote.css"),
    card_extra=delivery.card_fields,
    setup=wire,
)
