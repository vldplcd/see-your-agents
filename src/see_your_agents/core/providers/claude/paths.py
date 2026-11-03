"""Where Claude Code keeps its data. CLAUDE_CONFIG_DIR moves both, as it does for Claude Code itself."""

import os
from pathlib import Path


def claude_home() -> Path:
    """~/.claude: transcripts (projects/), the process registry (sessions/), jobs, tasks, settings, agents."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def claude_json() -> Path:
    """Claude Code's own config: user-level MCP servers, per-project settings. It holds tokens."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / ".claude.json" if base else Path.home() / ".claude.json"
