"""Where Codex keeps its data; CODEX_HOME moves it, as for Codex itself. The dashboard only reads it: what it
writes there, `codex queue` and `codex exec` write."""

import os
from pathlib import Path


def codex_home() -> Path:
    """~/.codex: rollouts (sessions/, archived_sessions/), thread names, writer locks, the daemon's pid."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
