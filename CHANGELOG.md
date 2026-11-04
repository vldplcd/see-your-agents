# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/) (while it is 0.x, a minor version may change behaviour).

## [Unreleased]

## [0.1.1] - 2026-11-04

### Added

- `uvx see-your-agents serve` tries the dashboard without installing it: the package now has a `see-your-agents`
  command, the same as `sya`.
- A live demo on GitHub Pages (https://vldplcd.github.io/see-your-agents/), rebuilt from `main`, and screenshots and a
  GIF in the README. In the demo you can answer a permission prompt from the board.

## [0.1.0] - 2026-11-04

The first public release.

### Added

- **Board.** Every Claude Code and Codex CLI session on the machine, grouped by "Needs you", "Working",
  "Your move", "Recently closed" and "Archive". Each card shows its title, project, branch, tickets, MR state, cost or
  tokens, and how full the context window is. The same board is also available in the terminal (`sya ls`).
- **Chat with any session.**
  - Live Claude sessions are reached through a one-shot courier, through tmux, or through the optional VS Code
    bridge.
  - Stopped sessions are continued for one turn.
  - Codex threads are reached through `codex queue`.
  - Quote-reply, file attachments where the program supports them, and a ⌘K switcher.
- **New sessions** from the page («+»), for Claude Code or Codex, with model, effort, permission or sandbox mode and
  an optional worktree. Existing sessions can be forked.
- **Supervisor.**
  - A Claude Code session that watches the others over MCP.
  - It writes digests and keeps the to-do list.
  - It sends messages or starts agents only after you confirm.
  - Optionally it reads Jira (read-only).
- **Group chats** with the supervisor and any sessions, including Codex threads.
- **To-do list** in a side dock, with urgent items, Jira links and a morning review.
- **Permission approvals from the dashboard.**
  - Claude Code's permission prompts, questions and plan approvals appear on the page through a local
    `PermissionRequest` hook (`sya hooks install`). The terminal dialog stays open, and the first answer wins.
  - Deciding needs a browser paired through a one-time link.
- **Paired browser for every control action.** Any page request that changes something needs the pairing secret.
- **Builder.** A Claude Code agent extends the dashboard itself on its own branch. It is sandboxed, and so are its
  checks and preview. You get a preview, a diff, "Accept" and "Roll back"; a watchdog restarts the dashboard and rolls
  back a version that does not come up.
- **GitLab MR state** on cards through `glab` (read-only).
- **Office view**, configurable agent looks, three page styles in light and dark, and a Mac kept awake while agents
  work.
- **UI in six languages:** English (the default), Russian, Spanish, German, French and Simplified Chinese.
- **Providers.** Claude Code and Codex sit behind one provider interface, so another agent CLI is a folder plus a
  registry line.

[Unreleased]: https://github.com/vldplcd/see-your-agents/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/vldplcd/see-your-agents/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/vldplcd/see-your-agents/releases/tag/v0.1.0
