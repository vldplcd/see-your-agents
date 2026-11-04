# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/) (while it is 0.x, a minor version may change behaviour).

## [Unreleased]

### Changed

- A permission prompt for a call that names a session by its id (the supervisor's `stop_session`,
  `archive_session`, `send_to_session`) shows that session's title and project on the board, not only the id.

### Fixed

- The Chats button no longer counts new replies in archived sessions: the list folds the archive away, so the
  number stayed after every visible chat was read.
- A message that reaches a session while its agent is working (typed in its terminal, or sent from the chat)
  shows in the chat: Claude Code records it as a queued command, and the chat dropped it a minute after sending.

## [0.1.2] - 2026-11-04

### Added

- In the office, an empty desk at the end of each room starts a new session in that project: it opens New session
  with the room's folder filled in.
- The supervisor can close sessions: `stop_session` stops a live one and archives it, `archive_session` only
  archives. Each call asks for your confirmation; a session waiting for you is never archived, and the supervisor
  cannot stop itself.
- The session panel has the chat's buttons: "Archive" for any session not archived yet ("Bring back" for one marked
  done) and "Finish" for a live one, which asks first, stops the process and archives the session.

### Changed

- Sending from the chat no longer blocks the input. The message shows in the feed at once as "Sending…" while it is
  delivered (the courier takes several seconds), the next one can be typed and sent right away, and a session's
  messages go in order. A message that is not delivered comes back to the input field with the reason. Before, a
  courier on its way marked the session as busy, which locked the input with "A reply is already being prepared".
- The page loads nothing from the internet: its fonts (Onest, Unbounded, Jost, Sofia Sans Condensed and Extra
  Condensed) ship with the package instead of coming from Google Fonts, and the demo page carries them inline. They
  are under the SIL Open Font License 1.1, so the package's license is now `MIT AND OFL-1.1`.

## [0.1.1] - 2026-11-04

### Added

- `uvx see-your-agents serve` tries the dashboard without installing it: the package now has a `see-your-agents`
  command, the same as `sya`.
- A live demo on GitHub Pages (https://vldplcd.github.io/see-your-agents/), rebuilt from `main`, and screenshots and a
  GIF in the README. In the demo you can answer a permission prompt from the board.

### Changed

- Browser pairing, and with it every action from the page, works on Linux too. The pairing tab opens through a page
  only you can read, so the code never shows in a process's arguments, and `sya serve` prints the pairing link in its
  terminal, for SSH sessions and machines without a desktop. Native Windows is still unsupported.

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

[Unreleased]: https://github.com/vldplcd/see-your-agents/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/vldplcd/see-your-agents/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/vldplcd/see-your-agents/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/vldplcd/see-your-agents/releases/tag/v0.1.0
