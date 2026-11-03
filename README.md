# see-your-agents

A local dashboard for Claude Code and Codex CLI sessions: who is working, who is waiting for you, which ticket a
session belongs to, what was done and where the agent is in its plan. It reads what the programs already keep on
disk, so it works with any session, including plain `claude` or `codex` in any terminal. From the page you can also
write to sessions, answer Claude Code's permission prompts, start new sessions and talk to a supervisor session that
watches all the others. The dashboard runs on your machine: a Python server on `127.0.0.1` and a page in your
browser.

<!-- Screenshot: docs/images/board.png (the board in light and dark) -->

## Features

- **Board**: sessions in columns by what is needed from you ("Needs you", "Working", "Your move"), with tickets,
  tasks, cost or tokens, context fill and MR state; the same sessions in an **office** view.
- **Conversation**: read any session as a chat and write to it, whether it runs in VS Code, tmux or the background,
  or is already closed; group chats with several agents at once.
- **Permissions**: answer Claude Code's permission prompts, questions and plans from the page.
- **New session and Fork**: launch `claude --bg` or `codex exec` in a project folder, or fork a session.
- **Supervisor**: a Claude Code session that watches the others over the dashboard's MCP server and hands out tasks,
  with your confirmation.
- **To-do**, **search**, **tools** (skills, MCP servers, hooks and how often agents use them), keyboard shortcuts.
- **Builder**: ask the supervisor for a new module, and an agent builds it in a sandboxed branch for you to accept.
- Six UI languages: English, Russian, Spanish, German, French, Simplified Chinese. All UI text was written by
  Claude Opus 5.5; if a translation or a name could be better, suggestions are welcome.

## Quick start

```bash
uv tool install see-your-agents     # or: pipx install see-your-agents
sya serve                           # http://127.0.0.1:8765, opens a browser tab
sya ls                              # the same board in the terminal (--all adds the archive)
```

From a checkout, for development or for the builder (it works on the dashboard's own git repository):

```bash
git clone https://github.com/vldplcd/see-your-agents.git && cd see-your-agents
uv run sya serve
```

The page is in English by default; Appearance → Language switches it. Optional one-time setup (the supervisor, the
permission hook, the VS Code bridge) is in [docs/configuration.md](docs/configuration.md).

## Requirements

- [`uv`](https://docs.astral.sh/uv/) and Python 3.11 or newer.
- The Claude Code CLI (`claude`) in `PATH`; the Codex CLI (`codex`) to see its sessions too.
- macOS for everything that changes something from the page (browser pairing, the builder's sandboxes). Elsewhere
  the page is read-only.
- Optional: `glab` for MR states, VS Code or tmux for typing into a live session's terminal, `vplan` for visual
  plans.

The dashboard writes nothing to `~/.claude` or `~/.codex` itself, except through the setup commands you run. Details:
[docs/configuration.md](docs/configuration.md).

## Documentation

- [User guide](docs/user-guide.md): everything on screen and how to use it.
- [Configuration](docs/configuration.md): commands, ports, environment variables, data locations.
- [Security](docs/security.md): the threat model, guarantees and residual risks.
- [Architecture](docs/ARCHITECTURE.md), [providers](docs/providers.md), [languages](docs/i18n.md),
  [adding a feature](docs/adding-a-feature.md).
- The full index: [docs/README.md](docs/README.md). What changed between versions: [CHANGELOG.md](CHANGELOG.md).

## Contributing

Contributions are welcome. Ways to help:

- **Bug reports**: an issue with your OS, Python, the CLI versions (`claude --version`, `codex --version`), the steps
  and the logs.
- **Feature modules**: a new view, panel, card line or MCP tool.
- **Providers**: support for another agent CLI next to Claude Code and Codex.
- **Translations**: a new UI language, or better wording in an existing one.
- **Docs**: corrections, clearer explanations, screenshots.

How a feature is added: a folder in `src/see_your_agents/features/<id>/` and one line in
`src/see_your_agents/features/__init__.py`; the host and other features are not edited. Walkthrough:
[docs/adding-a-feature.md](docs/adding-a-feature.md). A provider is the same shape, a folder in
`core/providers/<id>/` and one registry line: [docs/providers.md](docs/providers.md).

The rules, in short:

- Work on a branch of your fork; one logical change per commit, subject `Area: what changed` (`todo`, `core`, `app`,
  `docs`, `ci`…), the body says why.
- `uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest -q` passes before you open a
  pull request; CI runs it on Linux and macOS.
- Every UI string is a catalog key with an English string; agent-facing text is English.
- UI changes come with screenshots in light and dark; security-sensitive changes come with tests.

Pull requests written with Claude Code, Codex or another agent are welcome; the repo carries rules for them
([CLAUDE.md](CLAUDE.md), [AGENTS.md](AGENTS.md)). The maintainers may decline any pull request without giving a
reason.

Details: [CONTRIBUTING.md](CONTRIBUTING.md). Keep it civil ([code of conduct](CODE_OF_CONDUCT.md)), and report
vulnerabilities privately as [SECURITY.md](SECURITY.md) describes.

## License

MIT, see [LICENSE](LICENSE).
