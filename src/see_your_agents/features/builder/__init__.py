"""Builder: the dashboard changes itself, in a branch, and the user accepts or rolls back with a click."""

from pathlib import Path

from ...app.feature import Feature
from . import routes

FEATURE = Feature(
    id="builder",
    folder=Path(__file__).parent,
    handle_get=routes.handle_get,
    handle_post=routes.handle_post,
    tools=(routes.TOOL,),
    scripts=("builder.js",),
    styles=("builder.css",),
    board_extra=routes.board_fields,
    supervisor_prompt="supervisor.md",
)
