"""Approvals: answer Claude Code's permission prompts, questions and plans from the page.

A PermissionRequest hook (hook.py, installed by `sya hooks install`) hands each prompt to the dashboard while
the terminal shows its own dialog; the first answer wins. See docs/claude-hooks-notes.md.
"""

from pathlib import Path

from ...app.feature import Feature
from . import approvals, install

FEATURE = Feature(
    id="approvals",
    folder=Path(__file__).parent,
    handle_post=approvals.handle_post,
    open_posts=approvals.OPEN_POSTS,  # the hook's signed routes; `decide` still needs the paired browser
    add_cli=install.add_cli,
    scripts=("approvals.js",),
    styles=("approvals.css",),
    card_extra=approvals.card_fields,
    board_extra=approvals.board_fields,
)
