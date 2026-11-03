"""Tools: skills, subagents, MCP servers, plugins, commands and hooks, with usage counts."""

from pathlib import Path

from ...app.feature import Feature
from . import toolbox

FEATURE = Feature(
    id="tools",
    folder=Path(__file__).parent,
    handle_get=toolbox.handle_get,
    scripts=("tools.js",),
    styles=("tools.css",),
)
