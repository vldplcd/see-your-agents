"""The environment of a process the dashboard starts: an agent, a courier, git, uv, a preview server."""

from __future__ import annotations

import os

# What belongs to this server, not to a process started from it: its venv and watchdog, `uv run`'s recursion
# guard, and the Claude Code session that ran `sya serve` (with its variables a child would think it runs
# inside that session). A list, not a prefix: CLAUDE_CODE_USE_BEDROCK and the like are the user's choices.
OWN_ENV = (
    "VIRTUAL_ENV",
    "SYA_WATCHDOG",
    "UV_RUN_RECURSION_DEPTH",
    "CLAUDECODE",
    "CLAUDE_EFFORT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_INVOKED_SKILLS",
    "CLAUDE_PID",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_ENTRYPOINT",
)


def clean_env(**extra: str) -> dict[str, str]:
    """This server's environment without OWN_ENV and without its venv's bin/ on PATH."""
    env = {k: v for k, v in os.environ.items() if k not in OWN_ENV}
    if (venv := os.environ.get("VIRTUAL_ENV")) and "PATH" in env:
        own_bin = os.path.normpath(os.path.join(venv, "bin"))
        kept = [p for p in env["PATH"].split(os.pathsep) if os.path.normpath(p) != own_bin]
        env["PATH"] = os.pathsep.join(kept)
    return {**env, **extra}
