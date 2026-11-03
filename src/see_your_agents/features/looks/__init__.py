"""Looks: each agent's figure, colour, desk and things on it, plus palette tweaks for the skins."""

from pathlib import Path

from ...app.feature import Feature
from . import looks

FEATURE = Feature(
    id="looks",
    folder=Path(__file__).parent,
    handle_get=looks.handle_get,
    handle_post=looks.handle_post,
    scripts=("looks.js",),
    styles=("looks.css",),
)
