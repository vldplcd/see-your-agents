"""Groups: group chats of the user, the supervisor and several sessions, with one shared feed."""

from pathlib import Path

from ...app.feature import Feature
from . import api, mcp_tools

FEATURE = Feature(
    id="groups",
    folder=Path(__file__).parent,
    handle_get=api.handle_get,
    handle_post=api.handle_post,
    tools=mcp_tools.TOOLS,
    scripts=("groups.js",),
    styles=("groups.css",),
    supervisor_prompt="supervisor.md",
)
