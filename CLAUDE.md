# see-your-agents

A local dashboard for Claude Code and Codex CLI sessions: a stdlib Python server (`uv run sya serve`, 127.0.0.1:8765),
a DuckDB index of transcripts, a build-free page in plain JS. What is on screen: `docs/user-guide.md`; the docs index:
`docs/README.md`. These rules are for every coding agent: `AGENTS.md` points here.

- Structure: `docs/ARCHITECTURE.md`: layers (`core/` data, `app/` host, `features/<id>/` features, `web/` page
  shell), the `Feature` manifest, the `SYA.feature` slots, security invariants.
- A new feature or module: the `add-feature` skill (`.claude/skills/add-feature/SKILL.md`): a folder in `features/`
  and one line in `features/__init__.py`; the host and other features are not edited.
- Agent programs (Claude Code, Codex) are providers, `docs/providers.md`: a folder in
  `core/providers/` and one line in the registry; features go through the session's provider, not to `claude` directly.
- Checks. On a work branch, before each commit: ruff plus the tests of what you touched
  (`uv run pytest -q tests/<area>`). The full suite (about 2.5 minutes) runs once before handing work back and
  again before merging into `main`:
  ```bash
  uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest -q
  ```
- Live check: your own server on a free port (`uv run sya serve --no-open --port 8771`), not 8765: that is the user's
  dashboard. Write nothing to the user's `~/.claude`, `~/.codex` or `~/.config/see-your-agents`.
- Style: short "why" docstrings, no noisy comments, ruff line-length 110. Docs, code comments and commit messages are
  in English. Commits: one logical change each, subject `Area: what changed` (a feature id, `core`, `app`, `docs`,
  `ci`), the body says why; pull requests and the security-sensitive areas: `CONTRIBUTING.md`.
- UI text goes through `t()` (page and server), never a literal: keys in the owner's `i18n/en.json` and `i18n/ru.json`,
  English by default; how-to in `docs/i18n.md`. Text an agent reads (prompts, tool descriptions, courier headers) is
  English and never a catalog string.
- The builder (an agent on its own branch) does not commit to `main` or merge into it: the user accepts the change.
