# Configuration

Requirements, the `sya` commands, environment variables, ports, the one-time setup of MCP, the supervisor, the
permission hook and the VS Code bridge, and where the dashboard reads and writes data. What the page shows is in
[user-guide.md](user-guide.md).

## Requirements

- [`uv`](https://docs.astral.sh/uv/) and Python 3.11 or newer. Run every command from a clone of this repository:
  `uv run sya …`.
- The Claude Code CLI (`claude`) in `PATH`: live statuses, writing to sessions, launching them, the supervisor and
  the builder.
- The Codex CLI (`codex`), if you want to see its sessions too: they come from `~/.codex`, live statuses via `lsof`;
  writing and launching go through `codex queue` and `codex exec`.
- macOS for everything that changes something from the page:
  - browser pairing opens the tab through `osascript`, and every action needs a paired tab
    ([user-guide.md](user-guide.md#pairing-the-browser));
  - the builder's sandboxes need `sandbox-exec`;
  - keeping the Mac awake needs `caffeinate`.

  Elsewhere the dashboard refuses to pair, so the page is read-only; the builder's checks and preview refuse to run
  unconfined.
- Optional:
  - `glab` for MR states ([user-guide.md](user-guide.md#mr-state));
  - VS Code or tmux for typing into a live session's terminal
    ([user-guide.md](user-guide.md#writing-to-sessions));
  - `vplan` to render `.mdx` visual plans right in the session panel.

## Commands

From a checkout the commands run through `uv run`; installed with `uv tool install` or pipx, drop it.
`see-your-agents` is the same command as `sya`, so `uvx see-your-agents serve` runs the dashboard without installing
it. `sya hooks install` writes the path of the Python that runs it into Claude Code's settings, so run it from a
checkout or an installed copy, not through `uvx`, whose environments are temporary.

| Command | What it does |
|---|---|
| `uv run sya serve` | the dashboard at `http://127.0.0.1:8765`; opens a browser tab with a pairing code. `--port N` picks another port, `--no-open` opens no tab. It runs as a watchdog that restarts the server after a build is accepted or rolled back |
| `uv run sya ls` | the same board in the terminal; `--all` adds the archive |
| `uv run sya index` | refresh the index once |
| `uv run sya mcp install` | once: register the dashboard's MCP server in Claude Code at user level; `--dry-run` prints the commands with the token masked |
| `uv run sya mcp status` | check the connection and print the tool list |
| `uv run sya supervisor install` | once: the `fleet-supervisor` subagent in `~/.claude/agents`; `--force` overwrites a foreign file of that name |
| `uv run sya supervisor` | start or resume the supervisor in this terminal; `--bg` in the background, `--new` from scratch, `--print` prints the command instead |
| `uv run sya hooks install` | add the permission hook to Claude Code's settings; `--print` only shows the change |
| `uv run sya hooks status` | is the hook in place and will it run: Python, hook file, token |
| `uv run sya hooks uninstall` | remove the hook |
| `uv run sya bridge install` | build the VS Code bridge `.vsix` and run `code --install-extension` |
| `uv run sya bridge status` | live bridges and their terminals |

Every command takes `--db <path>` before the subcommand for another index file (`uv run sya --db /tmp/x.duckdb serve`).
The commands that talk to the dashboard (`mcp`, `supervisor`, `hooks`) take `--port` (default 8765): use the port
`sya serve` listens on.

## Ports

- `8765`: the dashboard, `127.0.0.1` only. `sya serve --port` changes it.
- `/mcp` on the same port: the MCP endpoint (`http://127.0.0.1:8765/mcp`).
- `8766` or the next free one: a build's "Preview".

A second dashboard on another port (for development, `uv run sya serve --no-open --port 8771`) works next to the main
one: the first server holds the DuckDB file, and the second takes an in-memory index.

## Environment variables

| Variable | Why |
|---|---|
| `SYA_JIRA_URL` | Jira address for ticket links. By default it is found automatically from `…/browse/KEY-N` links in transcripts |
| `SYA_JIRA_PROJECTS` | A list of project keys (`PROJ,OPS`), if junk like `UTF-8` gets into tickets |
| `CLAUDE_CONFIG_DIR` | If Claude Code keeps its data somewhere other than `~/.claude` |
| `CODEX_HOME` | If Codex CLI keeps its data somewhere other than `~/.codex` |
| `SYA_CONFIG_DIR`, `SYA_CACHE_DIR` | Where the dashboard's own settings (`~/.config/see-your-agents`) and its cache: the index, uploads, logs, bridge sockets (`~/.cache/see-your-agents`) live |
| `SYA_PAIR_OPENER` | For live checks: a command that gets the pairing URL on stdin instead of the browser being opened. It also enables pairing off macOS |

## MCP and the supervisor

```bash
uv run sya serve                # MCP runs inside the dashboard: http://127.0.0.1:8765/mcp
uv run sya mcp install          # once: the sya server in Claude Code at user level (~/.claude.json)
uv run sya supervisor install   # once: the fleet-supervisor subagent in ~/.claude/agents
uv run sya supervisor           # start or resume the supervisor in this terminal (--bg in the background, --new from scratch)
uv run sya mcp status           # check the connection and the tool list
```

`sya mcp install` runs `claude mcp add --transport http -s user sya http://127.0.0.1:<port>/mcp` with the `read`
token in an `Authorization` header (it replaces an existing `sya` entry). The supervisor gets the `control` token
from its own config, `~/.config/see-your-agents/supervisor/mcp.json` (0600). After a new tool is added, restart the
supervisor (`sya supervisor`) and, if needed, run `sya supervisor install` again.

| MCP tool | Token | What it does |
|---|---|---|
| `fleet_overview`, `session_detail`, `session_thread`, `search_sessions`, `list_delegations` | read | session overview, details, conversation, search, who delegated what to whom |
| `todo_list` | read | the to-do list: open items and those done within the last day |
| `list_groups`, `group_feed` | read | group chats and their feed |
| `send_to_session` | control | a message to any session through its program (Codex via `codex queue`), with confirmation |
| `resume_session`, `start_agent`, `post_digest` | control | a turn in a closed session, a new background agent, a digest on the board |
| `group_post` | control | a supervisor entry in a group feed (not delivered to members, no confirmation) |
| `todo_add`, `todo_update` | control | a to-do item into the list, the "done" mark, urgency, text; no confirmation |
| `request_change` | control | a build: change the dashboard itself ([user-guide.md](user-guide.md#flexible-dashboard-the-builder)) |

Tokens and what they do and do not separate: [security.md](security.md#mcp-tokens).

## Permission hook

```bash
uv run sya hooks install --print   # show what would change in ~/.claude/settings.json
uv run sya hooks install           # add the hook (dashboard port: --port, default 8765)
uv run sya hooks status            # is the hook in place and will it run: Python, hook file, token
uv run sya hooks uninstall         # remove it
```

- `install` adds one entry to `hooks.PermissionRequest` in `~/.claude/settings.json` (or
  `$CLAUDE_CONFIG_DIR/settings.json`) and touches nothing else; running it again changes nothing.
- Before writing, the old file is copied alongside (`settings.json.sya-backup-<time>`) and the change is printed as
  a diff; a `settings.json` symlink (dotfiles) stays a symlink, and its target is written.
- The hook runs with the Python of the dashboard's environment from this checkout: install it from the directory you
  run `sya serve` from.
- From an agent worktree (`.claude/worktrees/`) `install` refuses: the hook would vanish together with the worktree
  (`--from-worktree` if you really need it).
- `install` also creates `~/.config/see-your-agents/approvals-token` (0600), which signs the hook's requests and the
  dashboard's answers.

How to use it: [user-guide.md](user-guide.md#permissions-from-the-dashboard).

## VS Code bridge

The bridge is a small VS Code extension that lets the dashboard type into a live session's terminal (the `vscode`
route). It is installed once and is picked up after VS Code windows reload:

```bash
uv run sya bridge install   # builds the .vsix and runs `code --install-extension`
uv run sya bridge status    # live bridges and their terminals
```

The bridge socket lives in `~/.cache/see-your-agents/vscode/` (directory 0700, socket 0600); the extension does not
go to the network.

## What the dashboard writes

The dashboard writes nothing to `~/.claude` and `~/.codex` itself. The exceptions are the commands
`sya mcp install` (through `claude mcp add`, file `~/.claude.json`), `sya supervisor install`
(`~/.claude/agents/fleet-supervisor.md`), `sya hooks install` (its own hook in `~/.claude/settings.json`, with a
copy of the old file next to it), and the `codex queue` and `codex exec` commands the dashboard runs when you write
to a Codex session or launch a new one.

Its own files, in `SYA_CONFIG_DIR` (`~/.config/see-your-agents`), what you want to keep:

| File | What |
|---|---|
| `done.json` | "Done" marks; a mark clears itself when new activity appears in the session |
| `looks.json` | agents' looks |
| `groups.json`, `groups/<id>.jsonl` | groups and their feeds (0600) |
| `todo.json` | the to-do list |
| `awake.json` | the "Keep the Mac awake while agents work" switch |
| `digest.json` | the supervisor's latest digest |
| `builds.json`, `builder/build-<name>.json` | builds and each build's Claude Code settings |
| `mcp-tokens.json` | MCP tokens (0600) |
| `supervisor/` | the supervisor's MCP config with the `control` token (0600), settings and prompt |
| `approvals-token` | the permission hook's signing token (0600) |

In `SYA_CACHE_DIR` (`~/.cache/see-your-agents`), what can be rebuilt:

| Path | What |
|---|---|
| `index.duckdb` | the index; `--db` changes the path |
| `uploads/` | files attached to chat messages |
| `runs/` | logs of `resume` turns and `codex exec` launches |
| `courier/` | the courier's working folder and its last failed runs (0600) |
| `vscode/` | VS Code bridge sockets |
| `mr.json` | MR states |

You can delete the index at any time. Only the history of sessions whose transcripts Claude Code has already deleted
itself is lost.

## What the dashboard reads

| Source | What we take |
|---|---|
| `claude agents --json` | Live sessions (interactive and background): `status`, `waitingFor`, `pid`, `name` |
| `~/.claude/sessions/<pid>.json` | Since when the session has been in its current status |
| `~/.claude/jobs/<id>/state.json` | What a background session needs, the suggested answer |
| `~/.claude/projects/*/<id>.jsonl` | Title (`ai-title`, `/rename`), recaps, prompts, questions, MRs, plans, subagents, cost |
| `~/.claude/tasks/<id>/` | The session's task checklist |
| `~/.codex/sessions/**/rollout-*.jsonl`, `archived_sessions/` | Codex sessions: prompts, replies, actions, tokens, subagents |
| `~/.codex/state_5.sqlite` (read-only), `session_index.jsonl` | Codex session names |
| `~/.codex/thread-writer-locks/`, `lsof` | Which Codex sessions are running and by whom (daemon, VS Code, `exec`, TUI) |
| `~/.codex/models_cache.json`, `config.toml` (only `model`) | Models and their effort levels for "New session", the default model |

Transcript formats are internal and change between Claude Code and Codex versions. Parsers read fields defensively,
and when a parser changes (the provider's `PARSER_VERSION`), the index rereads that provider's transcripts.

### Context on the card

Verified on Claude Code 2.1.317 transcripts:

- filled: the `usage` of the last reply in the main thread (not subagents): `input_tokens` +
  `cache_creation_input_tokens` + `cache_read_input_tokens`, i.e. everything that went into the last request.
  Placeholder replies (`<synthetic>`, all zeros) are skipped;
- after compaction (`system` / `compact_boundary`) and until the next reply: `compactMetadata.postTokens`, what
  remained after compaction (without the system prompt and tools, so slightly less than the real figure);
- window: 1M if the model has the `[1m]` suffix (`message.model` or the `modelUsage` keys in `cost-state`), if it is
  Claude 5 or newer (they have only a 1M window), or if the session's context has already exceeded 200k; otherwise
  200k. For comparison: for a session on Haiku 5.5 with 59 263 tokens, the dashboard shows 6 %, as does Claude Code's
  own status line.

Codex does not report its context window yet, so its cards show no figure.
