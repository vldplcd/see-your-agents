# Agent instructions

The project's rules for coding agents are kept in one place, [CLAUDE.md](CLAUDE.md). They apply to every agent
CLI, not only Claude Code: read that file before you change anything, and follow it as if it were this one.

Then, depending on the task:

- checks, commit style and pull requests: [CONTRIBUTING.md](CONTRIBUTING.md);
- a new feature or module: [docs/adding-a-feature.md](docs/adding-a-feature.md) (the same recipe as the
  `.claude/skills/add-feature/SKILL.md` skill);
- the design and the security invariants: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

This file only points there, so the rules cannot drift apart: change CLAUDE.md, not this file.
