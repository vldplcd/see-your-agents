"""See your agents: a local overview of coding-agent sessions. Here: where the dashboard keeps its data."""

import os
from pathlib import Path


def config_dir() -> Path:
    """What the user chose and wants kept (agent looks, sessions marked done). SYA_CONFIG_DIR overrides."""
    return Path(os.environ.get("SYA_CONFIG_DIR", Path.home() / ".config" / "see-your-agents"))


def cache_dir() -> Path:
    """What can be rebuilt or thrown away (index, uploads, bridge sockets). SYA_CACHE_DIR overrides."""
    return Path(os.environ.get("SYA_CACHE_DIR", Path.home() / ".cache" / "see-your-agents"))
