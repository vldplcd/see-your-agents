"""Keys: ⌘K — quick switch to a session, a group or the supervisor; ? — every shortcut. Page only."""

from pathlib import Path

from ...app.feature import Feature

FEATURE = Feature(id="keys", folder=Path(__file__).parent, scripts=("keys.js",), styles=("keys.css",))
