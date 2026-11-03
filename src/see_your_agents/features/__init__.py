"""The features the dashboard runs, in this order: page assets load in it, tabs and MCP tools follow it.

A new feature is a folder next to this file and one line here; see docs/ARCHITECTURE.md.
"""

from ..app.feature import Feature
from . import (
    approvals,
    awake,
    board,
    builder,
    chat,
    groups,
    keys,
    launch,
    looks,
    mr,
    office,
    supervisor,
    todo,
    tools,
)

FEATURES: tuple[Feature, ...] = (
    board.FEATURE,
    office.FEATURE,
    chat.FEATURE,
    looks.FEATURE,
    tools.FEATURE,
    supervisor.FEATURE,
    todo.FEATURE,
    groups.FEATURE,
    builder.FEATURE,
    launch.FEATURE,
    awake.FEATURE,
    mr.FEATURE,
    keys.FEATURE,
    approvals.FEATURE,
)
