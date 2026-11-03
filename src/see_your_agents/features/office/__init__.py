"""Office: the same sessions on a floor plan, a room per project and a desk per session. Page only."""

from pathlib import Path

from ...app.feature import Feature

FEATURE = Feature(id="office", folder=Path(__file__).parent, scripts=("office.js",), styles=("office.css",))
