"""Keep the Mac awake: while agents work, `caffeinate` holds off idle sleep; a switch in Appearance."""

from pathlib import Path

from ...app.feature import Feature
from . import awake

FEATURE = Feature(
    id="awake",
    folder=Path(__file__).parent,
    handle_get=awake.handle_get,
    handle_post=awake.handle_post,
    scripts=("awake.js",),
    background=awake.run,
)
