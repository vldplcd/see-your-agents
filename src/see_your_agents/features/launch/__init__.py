"""New session: the «+» in the header starts a background session of an agent program and opens its chat;
Fork in a chat's header and the drawer starts one from a copy of a session's history."""

from pathlib import Path

from ...app.feature import Feature
from . import launch

FEATURE = Feature(
    id="launch",
    folder=Path(__file__).parent,
    handle_get=launch.handle_get,
    handle_post=launch.handle_post,
    scripts=("launch.js",),
    styles=("launch.css",),
    card_extra=launch.can_fork,
)
