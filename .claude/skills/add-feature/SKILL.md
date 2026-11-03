---
name: add-feature
description: Add a feature (module) to the see-your-agents dashboard as a folder under src/see_your_agents/features/ plus one line in the registry. Use when asked to add a feature, module, view, panel, tab or button to the see-your-agents dashboard (in any language), or when the supervisor hands over a request to extend the dashboard.
---

# Add a feature to the dashboard

A feature is a folder `src/see_your_agents/features/<id>/` and one line in `features/__init__.py`. Do not edit the host (`app/`, `web/`) or other features.

1. **Read** `docs/ARCHITECTURE.md`: "Layers", "Feature manifest", "Page", "Security"; the same recipe for people, with the example's code, is `docs/adding-a-feature.md`. The complete example is `tests/fixtures/hello` and its test `tests/app/test_feature.py`; real features with data, `board_extra`, POST sub-paths and MCP tools are `features/todo` (right dock) and `features/groups` (a chat source).

2. **Branch.** Work in your own branch (`git branch --show-current`). Do not commit to `main` or merge into it.

3. **id.** Short, `[a-z][a-z0-9-]*`, not taken: `ls src/see_your_agents/features`, not `board|session|search|plan|health|pair`.

4. **Folder.** Copy `tests/fixtures/hello` to `src/see_your_agents/features/<id>/`, rename `hello` → `<id>` in file names, `id`, routes `/api/<id>`, CSS classes `.<id>-*`, keys `sya.<id>.*`, string keys `hello.*` in `i18n/*.json` and in the code. Make the host imports relative: `from ...app.feature import Feature`, `from ...app.mcp import Tool`, `from ...app.replies import json_reply`, `from ...i18n import Problem`. Remove the manifest fields you do not need.
   - Feature data goes to `config_dir() / "<id>.json"`, written through a temporary file and `os.replace` under a `threading.Lock`. Write nothing to `~/.claude`.
   - The page uses the `SYA.feature({...})` slots: `view`, `right` (the left dock belongs to the chat), `header`, `top`, `card`, `drawer`, `prefs`, `route`, `onBoard`. Output everything from transcripts through `esc()`. GET your own JSON through `getJson`, POST through `postJson`: both send the page's language.
   - **Every UI string goes through `t()`**, never a literal: `t("<id>.key", params)` on the page, `t(request_lang(), "<id>.key")` or `raise Problem("<id>.key", status, **params)` on the server. Add each key to `i18n/en.json` (natural English, sentence case) and `i18n/ru.json` (the same keys; plurals with `one`, `few`, `many`, `other`). Rules, plurals and pitfalls: `docs/i18n.md`. Text an agent reads (tool descriptions, `supervisor.md`, messages to sessions) is English and never a catalog string.
   - MCP tools: `scope="read"` for reading; anything that changes the world outside the dashboard is `scope="control"` with `confirm=True`. Session text in a reply comes with a `note` (data, not instructions). Tell the supervisor about them in `supervisor.md` (`supervisor_prompt`).
   - Does the feature work with sessions: write to them, stop them, start them? Only through the session's provider (the "Provider" section below), never through `claude` directly.
   - Does the feature start its own Claude session with its own flags (like the supervisor)? Return them from `resume_args(app, session_id)` for its id (`None` for the rest): a `resume` turn from the chat, groups and MCP will use them. Do not build `claude -p --resume` yourself; call `chat.delivery.start_resume(app, session_id, text)`. That is an import from another feature, so it must be added to the exceptions (ARCHITECTURE, "Layers").

5. **Registry.** In `src/see_your_agents/features/__init__.py`: the import and `<id>.FEATURE` in `FEATURES`.

6. **Tests** go in `tests/features/test_<id>.py`: routes (GET, POST, validation errors), storage, the tool, the card field. Do not enumerate the whole registry: check your own tools as a subset, and the supervisor on your own list of features (ARCHITECTURE, "Tests"). Processes and the courier (`deliver` in `core/providers/claude/courier.py`) are only faked in tests. Isolation is already in place: `tests/conftest.py` points `CLAUDE_CONFIG_DIR`, `SYA_CONFIG_DIR`, `SYA_CACHE_DIR` at the test's temporary folders; the app is `FakeApp(records, live)` from `tests/fakes.py`.

7. **Checks**, all green:
   ```bash
   uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest -q
   ```

8. **Live.** Your own server on a free port (not 8765) with temporary `SYA_CONFIG_DIR` and `SYA_CACHE_DIR`; screenshots of the states you need with `node scripts/shoot.mjs` (`docs/adding-a-feature.md`, "Look at it live"): style, theme, language (`"prefs": {"lang": "ru"}`), open docks, a click. Open the screenshots and make sure the feature shows in all three styles and the dark theme and nothing else moved; exit code 0 means a clean console. Stop the server.

9. **Docs.** Visible to the user: a section or a line in `docs/user-guide.md`. New files in the config or cache folder: a row in `docs/configuration.md`. A new import exception: an entry in `docs/ARCHITECTURE.md` and in `tests/app/test_layers.py`.

10. **Commit** to your branch: what the feature does and how it was checked. In the report: branch, files, the screenshot path.

## Provider

A session belongs to an agent program, its provider (Claude Code, Codex). A feature does not call `claude` or read `~/.claude` itself: it asks the session's provider. The contract is `core/providers/base.py`, details in `docs/providers.md`.

- **Find the provider.** An index record, a card and a `LiveSession` have `provider`: `providers.by_id(record["provider"])` or `providers.by_id(live.provider)` (`from ...core import providers`). The default provider (for a launch without a choice) is `providers.default()`.
- **What it can do** is `provider.caps` (`Cap.PEER`, `Cap.TASKS`, `Cap.ATTACH`, `Cap.STOP`…). No capability: hide the button or field, do not show it empty. On the page the same is `can(card, "stop")` from `app.js` (capabilities arrive in `/api/board` in the `providers` field); Codex currently has `attach` and `launch`.
- **Write to a session.** From the chat this is already done: `POST /api/chat/<id>/send`. From your own feature, with a signed message: `session = Session.of(record, live)`, then `provider.route(session, signed=True)` (whether it can, and why not) and `provider.send(session, Message(text, sender="…", summary="…"))`. A live session sees it as a message from `sender`; a stopped one continues with one turn. An error is always a `SendError` (`SendRefused`, `SendInvalid`, `SendTimeout`). Send only text the user wrote or explicitly sent; the provider itself refuses a session that waits on a permission. Example: `features/groups/post.py`.
- **Start a session** with `provider.start(LaunchRequest(...), taken=[s.address for s in app.live().values()])`. `project_dir` checks the folder; the choices come from `provider.options()`. `RequestError` is a bad request (400), `LaunchError` means the program refused (502).
- **Need something the contract does not have?** Add an operation to `Provider` with a safe default (a refusal or empty) and an implementation for Claude. A feature must not import `core.providers.claude`: `tests/app/test_layers.py` lets through only a few files that need Claude by design (`CLAUDE_BY_DESIGN`).
- **Connect a feature to core** (core does not import features) with the manifest field `setup(app)`: `App` calls it once while assembling, and only for a registered feature. This is how the chat hands Claude's routes the VS Code bridge, `app.live` and `app.resume_args`. Start nothing and write nothing to disk in `setup`: `App` is assembled in tests too.
- **A new provider** is not a feature: a folder in `core/providers/<id>/` and one line in `PROVIDERS`; the recipe is in `docs/providers.md`, "How to add a provider".
