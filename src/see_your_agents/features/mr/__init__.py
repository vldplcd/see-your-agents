"""MR: live GitLab state of a session's MRs on its chips and in its drawer (read-only, via glab)."""

from pathlib import Path

from ...app.feature import Feature
from . import watch

FEATURE = Feature(
    id="mr",
    folder=Path(__file__).parent,
    scripts=("mr.js",),
    styles=("mr.css",),
    card_extra=watch.card_fields,
    setup=watch.setup,
)
