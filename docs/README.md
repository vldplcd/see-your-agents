# Documentation

## Using the dashboard

- [user-guide.md](user-guide.md): what is on screen and how to use it: the board, the office, the conversation and
  writing to sessions, groups, to-do, "New session" and "Fork", MR state, permissions from the dashboard and browser
  pairing, the supervisor, the builder, appearance and languages.
- [configuration.md](configuration.md): requirements, the `sya` commands, ports, environment variables, setting up
  MCP, the supervisor, the permission hook and the VS Code bridge, what the dashboard reads and writes.
- [security.md](security.md): the threat model, what is guaranteed and how, the residual risks.

## Contributing

- [CONTRIBUTING.md](../CONTRIBUTING.md): setup, checks, commit and pull request rules.
- [adding-a-feature.md](adding-a-feature.md): a new feature, step by step, with a worked example.
- [ARCHITECTURE.md](ARCHITECTURE.md): layers, the feature manifest, the page's slots, the security invariants, the
  tests.
- [providers.md](providers.md): agent programs (Claude Code, Codex CLI) behind one contract, and how to add one.
- [i18n.md](i18n.md): UI languages, catalogs, how to add a string or a language.

## Reference notes

Verified facts about the agent programs the code depends on, with how each was checked:

- [notes/claude-hooks-notes.md](notes/claude-hooks-notes.md): Claude Code's `PermissionRequest` hook.
- [notes/claude-attach-notes.md](notes/claude-attach-notes.md): typing into a background session through
  `claude attach`.
- [notes/claude-sandbox-notes.md](notes/claude-sandbox-notes.md): Claude Code's Bash sandbox and `sandbox-exec`, for
  the builder.
- [notes/codex-cli-notes.md](notes/codex-cli-notes.md): Codex CLI's files, its daemon's RPC, sending and launching.
