"""Board: sessions in columns by what they need from you. Page only: the data is the host's /api/board."""

from pathlib import Path

from ...app.feature import Feature

FEATURE = Feature(id="board", folder=Path(__file__).parent, scripts=("board.js",), styles=("board.css",))
