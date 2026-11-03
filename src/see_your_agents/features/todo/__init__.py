"""To-do: a todo list the user and the supervisor keep together, in a right dock."""

from pathlib import Path

from ...app.feature import Feature
from . import todo

FEATURE = Feature(
    id="todo",
    folder=Path(__file__).parent,
    handle_get=todo.handle_get,
    handle_post=todo.handle_post,
    tools=todo.TOOLS,
    scripts=("todo.js",),
    styles=("todo.css",),
    board_extra=todo.board_fields,
    supervisor_prompt="supervisor.md",
)
