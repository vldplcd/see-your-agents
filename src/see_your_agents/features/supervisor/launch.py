"""The supervisor: a Claude Code session that watches the others, and the read-only subagent for any session.

`launch_files` writes what `claude` needs (MCP config with the control token, settings that make every send
ask, the prompt); `command` builds the `claude` invocation, and `resume_args` gives a one-turn resume from the
dashboard the same config. Nothing here starts a session.

What the supervisor may call and what it is told comes from the feature registry: every feature's MCP tools
(a `confirm` tool asks first) and every feature's `supervisor_prompt` fragment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ... import config_dir
from ...app import mcp
from ...core.providers.claude import ID as CLAUDE
from ...core.providers.claude.launch import OPTIONS
from ...core.providers.claude.paths import claude_home, claude_json

if TYPE_CHECKING:
    from ...app.feature import Feature
    from ...app.server import App

# The session's name (`claude -n`): its title on the board, its SendMessage address and the sender of
# send_to_session, so a session that answers by SendMessage reaches it. An identity, not a sentence: the page
# labels the supervisor by its role. A supervisor started before the name was English is titled «Супервизор»
# until its next start, and older transcripts carry that name: NAMES is every name it answers to (on the board
# as `supervisor_names`, for the features that recognize it by name).
NAME = "Supervisor"
NAMES = (NAME, "Супервизор")
# The supervisor is a Claude Code session, and its start_agent starts one: `claude --bg`, Claude's models.
PROVIDER = CLAUDE
MODELS = OPTIONS["model"]
AGENT_FILE = "fleet-supervisor.md"
AGENT_MARKER = "managed-by: see-your-agents"
SERVERS = {"read": "sya", "control": "sya_control"}  # MCP server name in the supervisor's config, per scope
# Built-in tools the supervisor gets: reading and messaging, no shell, edits or web. Transcript text may carry
# injected instructions, and the user's own allow rules (Bash, WebFetch) would otherwise apply here too.
BUILTIN_TOOLS = "Read,Grep,Glob,ListAgents,SendMessage,ToolSearch"
SESSION_ID = re.compile(r"[0-9a-f-]{36}")
JIRA = "jira"  # the supervisor's copy of the user's mcp-atlassian, for the morning review of open tickets
# READ_ONLY_MODE makes mcp-atlassian drop its write tools (checked in 0.23.1); ENABLED_TOOLS narrows it to
# these, and only these run without asking.
CONTAINER_RUNNERS = {"docker", "podman", "nerdctl"}
JIRA_TOOLS = (
    "jira_search",
    "jira_get_issue",
    "jira_get_user_profile",
    "jira_get_project_issues",
    "jira_get_agile_boards",
    "jira_get_board_issues",
    "jira_get_sprints_from_board",
    "jira_get_sprint_issues",
)
PROMPTS = Path(__file__).parent / "prompts"


def _asset(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _dir() -> Path:
    return config_dir() / "supervisor"


def _tools(features: Sequence[Feature]) -> list[mcp.Tool]:
    return [t for f in features for t in f.tools]


def _qualified(tool: mcp.Tool) -> str:
    return f"mcp__{SERVERS[tool.scope]}__{tool.name}"


def read_tools(features: Sequence[Feature]) -> list[str]:
    """What the read-only subagent may call: every read tool, and ListAgents to see who is live."""
    return [_qualified(t) for t in _tools(features) if t.scope == "read"] + ["ListAgents"]


def permissions(features: Sequence[Feature]) -> dict[str, list[str]]:
    """The supervisor's allow and ask rules. Every SendMessage asks: the recipient acts on it."""
    tools = _tools(features)
    allow = [_qualified(t) for t in tools if t.scope == "read" and not t.confirm] + ["ListAgents"]
    allow += [_qualified(t) for t in tools if t.scope != "read" and not t.confirm]
    allow += [f"mcp__{JIRA}__{name}" for name in JIRA_TOOLS]
    return {"allow": allow, "ask": ["SendMessage"] + [_qualified(t) for t in tools if t.confirm]}


def _is_atlassian(name: str, cfg: object) -> bool:
    """A stdio entry that runs mcp-atlassian directly (uvx, pipx, a venv), under any name and pinned version.
    Not in a container: READ_ONLY_MODE would land on docker's own environment, not the server's."""
    if not isinstance(cfg, dict) or not cfg.get("command"):
        return False
    if Path(str(cfg["command"])).name in CONTAINER_RUNNERS:
        return False
    words = [name, str(cfg["command"]), *map(str, cfg.get("args") or [])]
    return any(re.split(r"[@=<>]", w)[0] == "mcp-atlassian" for w in words)


def jira_server() -> dict | None:
    """The user's own mcp-atlassian entry made read-only; None when it is not configured at user level."""
    try:
        servers = json.loads(claude_json().read_text(encoding="utf-8")).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        return None
    cfg = next((c for n, c in servers.items() if _is_atlassian(n, c)), None)
    if cfg is None:
        return None
    env = {**(cfg.get("env") or {}), "READ_ONLY_MODE": "true", "ENABLED_TOOLS": ",".join(JIRA_TOOLS)}
    return {**cfg, "env": env}


def prompt(features: Sequence[Feature]) -> str:
    """The base prompt, then each feature's fragment in registry order."""
    text = _asset("supervisor.md")
    for fragment in (f.prompt() for f in features):
        if fragment.strip():
            text = text.rstrip("\n") + "\n\n" + fragment
    return text


def install_agent(features: Sequence[Feature], force: bool = False) -> Path:
    path = claude_home() / "agents" / AGENT_FILE
    if path.exists() and not force and AGENT_MARKER not in path.read_text(encoding="utf-8"):
        raise FileExistsError(f"{path} already exists and sya did not write it; --force overwrites it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_asset(AGENT_FILE).replace("{tools}", ", ".join(read_tools(features))), encoding="utf-8")
    return path


def launch_files(port: int, features: Sequence[Feature]) -> dict[str, Path]:
    toks = mcp.tokens()
    url = f"http://127.0.0.1:{port}/mcp"

    def server(token: str) -> dict:
        return {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}

    # Both servers here, and --strict-mcp-config drops the user's others (Slack, AWS...): their write tools
    # are not for a session that reads other sessions' text. Jira comes back read-only.
    servers = {SERVERS[scope]: server(toks[scope]) for scope in mcp.SCOPES}
    if jira := jira_server():
        servers[JIRA] = jira
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    paths = {"mcp": d / "mcp.json", "settings": d / "settings.json", "prompt": d / "prompt.md"}
    # The file holds the control token: create it 0600 before the secret is written
    fd = os.open(paths["mcp"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": servers}, f, indent=2)
    paths["mcp"].chmod(0o600)
    paths["settings"].write_text(
        json.dumps({"permissions": permissions(features)}, indent=2), encoding="utf-8"
    )
    paths["prompt"].write_text(prompt(features), encoding="utf-8")
    return paths


def session_id() -> str | None:
    """The supervisor's session, once Claude Code has written its transcript: a recorded id that never
    started (`--print`, a failed launch) must not be resumed or pinned on the dashboard."""
    try:
        sid = json.loads((_dir() / "session.json").read_text(encoding="utf-8")).get("session_id")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        return None
    return sid if next((claude_home() / "projects").glob(f"*/{sid}.jsonl"), None) else None


def _record_session(sid: str) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    data = {"session_id": sid, "created_at": datetime.now(UTC).isoformat()}
    (_dir() / "session.json").write_text(json.dumps(data), encoding="utf-8")


def config_args(port: int, features: Sequence[Feature]) -> list[str]:
    """The supervisor's own tools, permissions, MCP servers and prompt; writes the launch files."""
    files = launch_files(port, features)
    return [
        # default, not the user's usual auto mode: the supervisor must ask before each send
        "--permission-mode",
        "default",
        "--tools",
        BUILTIN_TOOLS,
        "--settings",
        str(files["settings"]),
        "--mcp-config",
        str(files["mcp"]),
        "--strict-mcp-config",
        "--append-system-prompt-file",
        str(files["prompt"]),
    ]


def resume_args(app: App, sid: str) -> list[str] | None:
    """A one-turn resume of the supervisor (chat, groups, resume_session, the morning review) runs with its
    own config: a plain `claude -p --resume` would give it the user's tools and MCP servers."""
    return config_args(app.port, app.features) if sid == session_id() else None


def command(port: int, features: Sequence[Feature], new: bool = False, background: bool = False) -> list[str]:
    """The `claude` command line; writes the launch files and records the id of a new supervisor."""
    cmd = ["claude", "-n", NAME, *config_args(port, features)]
    sid = None if new else session_id()
    if sid:
        cmd.append(f"--resume={sid}")
    else:
        sid = str(uuid.uuid4())
        _record_session(sid)
        cmd += ["--session-id", sid]
    if background:
        cmd.append("--bg")
    return cmd


def add_cli(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("supervisor", help="run the supervisor session, or `install` its subagent")
    p.add_argument("action", nargs="?", choices=["install"])
    p.add_argument("--new", action="store_true", help="start a fresh supervisor instead of resuming")
    p.add_argument("--bg", action="store_true", help="run in the background")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--print", action="store_true", help="print the command instead of running it")
    p.add_argument("--force", action="store_true", help="install: overwrite a foreign agent file")
    p.set_defaults(run=run_cli)


def run_cli(args: argparse.Namespace, features: Sequence[Feature]) -> None:
    if args.action == "install":
        try:
            print(install_agent(features, args.force))
        except FileExistsError as exc:
            raise SystemExit(f"supervisor install: {exc}") from exc
        return
    cmd = command(args.port, features, new=args.new, background=args.bg)
    if args.print:
        print(shlex.join(cmd))
    elif args.bg:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
        print((out.stdout + out.stderr).strip())
        if out.returncode:
            raise SystemExit(out.returncode)
    else:
        print("The supervisor starts in this terminal; the dashboard must be running: sya serve")
        os.execvp(cmd[0], cmd)
