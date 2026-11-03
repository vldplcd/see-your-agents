"""Supervisor: a Claude Code session over the fleet, its MCP tools, `sya supervisor` and its digest."""

from pathlib import Path

from ...app.feature import Feature
from . import fleet, launch, mcp_tools

FEATURE = Feature(
    id="supervisor",
    folder=Path(__file__).parent,
    tools=mcp_tools.TOOLS,
    add_cli=launch.add_cli,
    scripts=("supervisor.js",),
    styles=("supervisor.css",),
    board_extra=fleet.board_fields,
    resume_args=launch.resume_args,
)
