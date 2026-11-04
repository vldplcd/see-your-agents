# Contributing

Thanks for helping. This page is the practical part: setup, checks, commits, pull requests and the few rules that
keep the dashboard safe and translatable. The design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); the
documentation index is [docs/README.md](docs/README.md).

Keep it civil ([code of conduct](CODE_OF_CONDUCT.md)). Security problems go through private
reporting, not issues: [SECURITY.md](SECURITY.md).

## Ways to contribute

- **Bug reports.** Open an issue with the bug form: OS, Python, which agent CLIs you run and their versions
  (`claude --version`, `codex --version`), the steps, what you expected, and the logs (the terminal running
  `sya serve`, the browser console). Transcripts are private: trim them to the lines that matter and remove paths,
  names and ticket keys you do not want to publish.
- **Feature modules.** A new view, dock, card line, session panel section or MCP tool. For anything larger than a
  small change, open a "Feature or module idea" issue first, so we agree on the shape before you build it.
- **Providers.** Support for another agent CLI next to Claude Code and Codex. See
  [Proposing a provider or a language](#proposing-a-provider-or-a-language).
- **Translations.** A new UI language or better wording in an existing one; the "Translation" issue form helps to
  coordinate. All current UI text, in every language, was written by Claude Opus 5.5, so if a translation or a name
  sounds off to you, don't hesitate to propose a better one.
- **Docs.** Corrections and clearer explanations are welcome as plain pull requests.

## Development setup

You need [`uv`](https://docs.astral.sh/uv/) and Python 3.11 or newer. Node 22 or newer is optional: a few tests run
the page's JavaScript in Node and skip without it, and `scripts/shoot.mjs` (screenshots) needs it too, plus Google
Chrome.

```bash
git clone https://github.com/<you>/see-your-agents.git && cd see-your-agents
uv sync                 # the venv with the dev tools (pytest, ruff)
uv run sya serve --no-open --port 8771
```

Run your development server on its own port and with its own folders, so it never mixes with the dashboard you use
for real work on 8765:

```bash
SYA_CONFIG_DIR=$(mktemp -d) SYA_CACHE_DIR=$(mktemp -d) uv run sya serve --no-open --port 8771
```

A second server next to a running one takes an in-memory index, which is fine. `uv run python demo/build.py` builds a
static demo on made-up data (`demo/dist/see-your-agents.html`), handy for screenshots without real sessions; the same
page is published on GitHub Pages from `main` (`.github/workflows/pages.yml`).

Most actions on the page need a paired browser. Pairing works on macOS and Linux; when no tab opens (no desktop, an
SSH session), open the link `sya serve` prints in its terminal. The builder's sandboxes are macOS-only.

## Where things are

The code is organized in layers ([ARCHITECTURE.md, "Layers"](docs/ARCHITECTURE.md#layers)):

- `core/`: data without HTTP. `index.py` (the DuckDB index of all providers), `sessions.py` (columns and cards),
  `done.py` ("done" marks), and `providers/`, the agent programs: the `base.py` contract, the `__init__.py` registry,
  `claude/` (transcript → digest, live statuses, launch, delivery routes, courier) and `codex/` (rollout → digest and
  chat, liveness from lock files, sending via `codex queue`, launching via `codex exec`).
- `app/`: the host. `server.py` (HTTP, page assembly), `mcp.py` (MCP protocol, tokens), `cli.py` (`sya`),
  `watchdog.py` (the `sya serve` watchdog), `pairing.py` (browser pairing), `feature.py` (the feature manifest).
- `features/`: one folder per feature: `board`, `office`, `chat` (conversation, delivery, the VS Code bridge),
  `looks`, `tools`, `supervisor` (launch, MCP tools, prompts), `groups` (group chats), `todo` (to-do), `builder`
  (dashboard builds), `launch` (new session, fork), `awake` (keep the Mac awake), `mr` (MR state in GitLab), `keys`
  (shortcuts, quick switch), `approvals` (permissions from the dashboard). List and order: `features/__init__.py`.
- `web/`: the page shell without a build step: `index.html`, `base.css`, `app.js`, `figures.js`, `i18n.js`. Features
  have their own `web/*.js|css`.
- UI strings: `web/i18n/<lang>.json` and `features/<id>/i18n/<lang>.json`.
- `tests/` mirrors the layers; `tests/fixtures/hello` is the example feature.
- `vscode-bridge/`: the VS Code extension behind `sya bridge install`. `demo/`: the static demo.

## Checks

While you work on a branch, run ruff and the tests of what you touched:

```bash
uv run ruff check src tests && uv run ruff format --check src tests
uv run pytest -q tests/features/test_todo.py      # or tests/<area>: app, core, providers, features
```

Before you open a pull request, run the full suite once (about 2.5 minutes):

```bash
uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest -q
```

CI runs the same on every pull request and on pushes to `main`: Ubuntu and macOS, Python 3.11 and 3.13. Tests that
need macOS tools (`sandbox-exec`, `ps` inside a sandbox) skip where they cannot run, as do the Node tests without
Node. If `ruff format --check` fails, run `uv run ruff format src tests` and commit the result.

Tests never touch your real data: `tests/conftest.py` points `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `SYA_CONFIG_DIR`
and `SYA_CACHE_DIR` at temporary folders, and processes (`claude`, `codex`, `glab`, the courier) are faked. Keep it
that way in new tests.

## Code style

- Python: stdlib only besides `duckdb`; ruff with line length 110. Short docstrings that say why; no comments that
  repeat the code.
- JavaScript: plain scripts, no build step and no framework.
- Docs, code comments and commit messages are in English.

## Commits

- One logical change per commit. A rename and a behaviour change are two commits.
- Subject: `Area: what changed`, where the area is the feature id (`chat`, `builder`, `todo`…), `core`, `app`,
  `docs` or `ci`. From the history: `Builder: sandbox the dashboard's own checks and the preview`,
  `Approvals: sign the hook's traffic, allow only what the page shows in full`.
- The body says why: the problem, the reason for this approach, what you checked. Wrap it at about 72 characters.
- Each commit passes ruff and the tests of its area.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Make the change in commits as above, with tests for new behaviour and for every bug you fix.
3. Run the full checks, push the branch to your fork and open a pull request against `main`. Fill in the template's
   checklist.
4. CI must be green. A maintainer reviews; please answer review comments in their threads.

A change users notice gets a line under "Unreleased" in [CHANGELOG.md](CHANGELOG.md).

**Releases** (maintainers): bump `version` in `pyproject.toml`, move "Unreleased" to a new version section in the
changelog, merge, then publish a GitHub release tagged `vX.Y.Z`. The `Release` workflow checks that the tag matches
the version, builds the package and uploads it to PyPI through Trusted Publishing.

**UI changes need screenshots**, light and dark, made with `scripts/shoot.mjs` against your development server
([adding-a-feature.md, "Look at it live"](docs/adding-a-feature.md#12-look-at-it-live)). Check the three styles
("Bureau", "Constructor", "Control room") and attach at least one light and one dark shot of what changed. The script
exits 1 when the page logged a console error: that has to be fixed too.

The README's pictures come from the demo page (`demo/demo-api.js` supplies its data): `board-*.png` from
`scripts/shoot.mjs` pointed at the built demo, `approve-*.gif` from `node scripts/record-demo.mjs` (needs gifski).
Retake them when a change makes them look out of date.

The page's fonts ship with the package, in `src/see_your_agents/web/fonts/` with their SIL Open Font License texts,
so the page loads nothing from the internet. `uv run python scripts/fonts.py` fetches them again from Google Fonts;
change its list to add a family, a weight or a subset.

**Docs.** A change users can see updates [docs/user-guide.md](docs/user-guide.md); a new command, variable or file
updates [docs/configuration.md](docs/configuration.md); a change to what the dashboard guards updates
[docs/security.md](docs/security.md).

**No secrets or personal data** in code, tests, fixtures, screenshots or logs: no tokens, no real transcripts, no
real names, paths, hosts or ticket keys. Fixtures are synthetic (see `tests/codex_rollouts.py`); examples use
`PROJ-123`, `acme` and neutral session names.

## UI strings

- Every UI string is a catalog key, never a literal: `t("<id>.key")` on the page, `t(request_lang(), "<id>.key")` or
  `Problem("<id>.key", status)` on the server.
- `en.json` is required for every key: natural English, sentence case. `ru.json` is welcome: the Russian copy is
  maintained by hand. Other languages fall back to English for a key they lack, so a new feature does not need all
  six.
- `uv run pytest -q tests/app/test_i18n.py` checks that every key the code uses exists in English, every English key
  is used, and translations keep the placeholders and plural forms.

Rules and pitfalls: [docs/i18n.md](docs/i18n.md).

## Agent-facing text is English

Whatever an agent reads stays English and is never a catalog string, whatever the viewer's language: the
supervisor's prompts (`supervisor.md`), MCP tool and argument descriptions, `ToolError` texts, courier messages and
group headers, the builder's prompts, anything written into a transcript. Terminal output of `sya` commands is
English too. When a string is shown on the page and also read by an agent, make it a `Problem`: the page gets its
language, the agent gets English. See [i18n.md, "Agent-facing text"](docs/i18n.md#agent-facing-text).

## Security-sensitive areas

These parts decide who may act on your agents. Changes there need extra care, a clear explanation in the pull
request, and tests that pin the new behaviour, including the refusals:

- **The POST guard** (`app/server.py`, a feature's `open_posts`): every mutating POST needs the paired browser unless
  it is explicitly opened with its own guard. `tests/app/test_layers.py` fails until a new POST route is classified.
- **Browser pairing** (`app/pairing.py`, `SYA.pairing` in `web/app.js`): the code, the secret, `127.0.0.1` only.
- **Permission requests** (`features/approvals`): the hook's HMAC signing, what a decision may contain, "Allow" only
  for a call shown in full.
- **The builder's sandboxes** (`features/builder/agent.py`, `builder.py`): the Claude Code sandbox settings, the
  `sandbox-exec` profile, failing closed.
- **The courier guard** (`core/providers/claude/courier.py`): only the exact text to the exact session.
- **Launching and stopping** (`project_dir` in `core/providers/base.py`, the providers' `launch.py`, pid checks before
  a stop): the folder rule and the option allowlists.
- **MCP** (`app/mcp.py`): token scopes, `confirm=True` on anything that changes the world.

Never weaken a guard to make a test pass, and never add a mode that turns off confirmations. What each guard
promises: [docs/security.md](docs/security.md); the invariants:
[ARCHITECTURE.md, "Security: what must not break"](docs/ARCHITECTURE.md#security-what-must-not-break).

## Proposing a provider or a language

**A provider** (another agent CLI) is a folder `core/providers/<id>/` and one line in the registry, behind the
contract in `core/providers/base.py`.

1. Open a "Feature or module idea" issue: the program, where it keeps its transcripts, how to tell what runs, and
   whether it can be messaged and launched without a person at a terminal.
2. Write down the facts you verified with probes, and what you only inferred, like
   [docs/notes/codex-cli-notes.md](docs/notes/codex-cli-notes.md).
3. Start read-only: `available`, `transcripts`, `parse`, `live`. Its sessions then show on the board. Sending and
   launching come after, capability by capability.

Recipe: [docs/providers.md, "How to add a provider"](docs/providers.md#how-to-add-a-provider).

**A language** is a `<lang>.json` next to every `en.json`, a row in `LANGS` in `src/see_your_agents/i18n.py` and its
plural categories in the tests. Open a "Translation" issue first so two people do not translate the same thing. A
partial translation is fine: missing keys show English. Recipe:
[docs/i18n.md, "How to add a language"](docs/i18n.md#how-to-add-a-language).

## Working with an AI agent

Agent-assisted contributions are welcome: the project is built that way itself. The repository carries its rules for
coding agents: [CLAUDE.md](CLAUDE.md) (Claude Code) and [AGENTS.md](AGENTS.md) (Codex and other CLIs), and the
`add-feature` skill in `.claude/skills/`. The same rules apply to the result: checks green, tests added, screenshots
for UI changes, and a human who has read the diff before it is submitted.

## Decisions

The maintainers may decline any pull request or issue, with or without an explanation. A "no" isn't a judgement of
you or your work; often it's about scope, maintenance cost or direction. Opening an issue to discuss a bigger change
first saves everyone time.
