# Adding a feature

Everything you see and do on the dashboard comes from features: the board, the office, the conversation, groups,
to-do and the rest. A new feature is a folder `src/see_your_agents/features/<id>/` and one line in
`features/__init__.py`. You do not edit the host (`app/`, `web/`) or other features for it.

This is the walkthrough for people. Claude Code users get the same recipe as the
[`add-feature`](../.claude/skills/add-feature/SKILL.md) skill, which the dashboard's own builder follows too. The
reference it stands on is [ARCHITECTURE.md](ARCHITECTURE.md): "Layers", "Feature manifest", "Page" and "Security".

## Before you start

- **The complete example** is `tests/fixtures/hello`: a GET and POST route, an MCP tool, a subcommand, a card field, a
  supervisor prompt fragment, a right dock, a line on the card and a session panel section. `tests/app/test_feature.py`
  runs it through the real server. In the package the imports are relative (`from ...app.feature import Feature`),
  in the copy in `tests/` they are absolute.
- **Real features** with data, `board_extra`, POST sub-paths and MCP tools: `features/todo` (a right dock) and
  `features/groups` (a chat source).
- **Work on your own branch** (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

## 1. Pick an id

Short, `[a-z][a-z0-9-]*`, not taken: `ls src/see_your_agents/features`. The host answers on `board`, `session`,
`search`, `plan`, `health` and `pair`, so those are refused for a feature with routes. The id owns the routes
`/api/<id>` and `/api/<id>/...`, the asset URLs `/<id>/<file>`, the CSS classes `.<id>-*`, the `localStorage` keys
`sya.<id>.*` and the string keys `<id>.*`.

## 2. Create the folder

Copy `tests/fixtures/hello` to `src/see_your_agents/features/<id>/` and rename `hello` to `<id>` in file names, the
`id`, routes, CSS classes, storage keys and string keys. Make the host imports relative:
`from ...app.feature import Feature`, `from ...app.mcp import Tool`, `from ...app.replies import json_reply`,
`from ...i18n import Problem`. Remove what you do not need.

```
features/<id>/
  __init__.py      the manifest: FEATURE = Feature(...)
  <id>.py          routes, storage, tools
  supervisor.md    optional: what the supervisor should know about your tools
  web/<id>.js      the page part: SYA.feature({...})
  web/<id>.css     styles: only .<id>-* classes and base.css tokens
  i18n/en.json     the English strings: required
  i18n/ru.json     the Russian strings: welcome; other languages fall back to English
```

## 3. Write the code

`<id>.py` for the example:

```python
"""Who said hello to the agents from the page."""

from __future__ import annotations

import json
import threading
from http import HTTPStatus

from ...app.mcp import Tool
from ...app.replies import Reply, json_reply
from ...i18n import Problem

ROUTE = "/api/hello"
_lock = threading.Lock()
names: list[str] = []


def handle_get(app, path: str, query: dict[str, str]) -> Reply | None:
    if path != ROUTE:
        return None  # not mine: the host answers 404
    with _lock:
        return json_reply({"names": list(names)})


def handle_post(app, path: str, body: bytes, headers) -> Reply | None:
    if path != ROUTE:
        return None
    try:
        name = json.loads(body).get("name")
    except (ValueError, AttributeError):
        name = None
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 40:
        raise Problem("hello.bad_name", HTTPStatus.BAD_REQUEST, max=40)  # {"error": ...} in the page's language
    with _lock:
        names.append(name.strip())
        return json_reply({"names": list(names)})


def card_fields(rec: dict, live) -> dict:
    return {"hello_words": len((rec.get("title") or "").split())}


def _names(app, args: dict) -> dict:
    with _lock:
        return {"names": list(names)}


TOOL = Tool("hello_names", "read", "Who said hello to the agents.", {}, (), _names)  # for an agent: English
```

- `handle_post` gets an already checked request: the host has checked Host, Origin, `X-SYA` and the paired browser.
  A POST route is paired by default; opening one (`open_posts`) needs its own guard and a reason
  ([security.md](security.md#the-post-guard)).
- Sub-paths (`/api/<id>/morning`, `/api/<id>/<key>/post`) arrive at the same handlers: parse `path` yourself.
  `app.board()` builds the whole board; call it at most once per request.
- **Data.** What the user wants to keep goes to `config_dir() / "<id>.json"`, what can be rebuilt to `cache_dir()`.
  Write through a temporary file and `os.replace`, under a `threading.Lock`. Write nothing to `~/.claude`.
- **A background loop** (`background`) or a connection to core (`setup`) are manifest fields too; `setup` must not
  start or write anything, since `App` is built in tests.

## 4. Declare the manifest

`__init__.py`:

```python
"""Hello: who said hello to the agents."""

from pathlib import Path

from ...app.feature import Feature
from . import hello

FEATURE = Feature(
    id="hello",
    folder=Path(__file__).parent,
    handle_get=hello.handle_get,
    handle_post=hello.handle_post,
    tools=(hello.TOOL,),
    scripts=("hello.js",),
    styles=("hello.css",),
    card_extra=hello.card_fields,
    supervisor_prompt="supervisor.md",  # if the supervisor should know about the tool
)
```

Only `id` is required. Every field is described in
[ARCHITECTURE.md, "Feature manifest"](ARCHITECTURE.md#feature-manifest-python). `check_registry` refuses a manifest
the host would serve wrongly: a repeated id or tool name, a reserved id, an asset not found in `web/`, a catalog with
keys outside `<id>.*`.

## 5. Write the page part

`web/hello.js`:

```js
// Hello: a right dock with who said hello, a line on every board card, a drawer section.
(() => {
  let names = [];
  const list = (root) => {
    root.querySelector(".hello-names").innerHTML = names.map((n) => `<li>${esc(n)}</li>`).join("");
  };
  function draw(root) {  // every tick while the dock is open: build once, then only update
    if (!root.firstElementChild) {
      root.innerHTML = `<div class="dock-title"><h2>${t("hello.title")}</h2><button class="ghost dock-x" type="button">×</button></div>
        <form class="hello-form"><input name="name" maxlength="40"><button class="ghost">${t("hello.say")}</button></form>
        <ul class="hello-names"></ul>`;
      root.querySelector("form").onsubmit = async (e) => {
        e.preventDefault();
        const r = await postJson("/api/hello", { name: e.target.elements.name.value });
        if (r.ok) { names = r.data.names; e.target.reset(); render(); }  // the shell's render(): the list and the badge
      };
      getJson("/api/hello").then((d) => { names = d?.names || names; render(); });
    }
    list(root);
  }
  SYA.feature({
    id: "hello",
    right: { button: t("hello.title"), title: t("hello.dock_title"), label: t("hello.title"), icon: "<svg …>",
      render: draw, badge: () => names.length || "" },
    card: (c) => `<p class="hello-line">${t("hello.card", { n: c.hello_words })}</p>`,
    drawer: (d) => `<section><h4>${t("hello.title")}</h4><p>${t("hello.drawer", { n: d.hello_words })}</p></section>`,
  });
})();
```

- **Slots.** `SYA.feature({...})` takes `view`, `right` (the left dock belongs to the chat), `header`, `top`, `card`,
  `chip`, `drawer`, `prefs`, `route` and `onBoard`. What each does and when it is called:
  [ARCHITECTURE.md, "`SYA.feature(def)`: slots"](ARCHITECTURE.md#syafeaturedef-slots). Chat sources and panels
  (`SYA.chat`) and the prefilled "New session" dialog (`SYA.launch`) are optional page dependencies described in the
  same section.
- **Escaping.** Output everything that came from transcripts through `esc()`.
- **Requests.** GET your own JSON through `getJson`, POST through `postJson`: both send the page's language, and
  `postJson` sets the headers the POST guard wants.
- **CSS.** Only your own `.<id>-*` classes and the tokens from `base.css`; do not restyle the shell's classes. Look at
  your feature in all three styles and the dark theme (step 9).
- **No build step.** Plain `<script>` tags in the global scope; your script is an IIFE that calls `SYA.feature`. Do
  not name your own functions `render` inside it: that is the shell's global redraw.

## 6. Add the strings

Every UI string is a catalog key, never a literal: `t("<id>.key", params)` on the page, `t(request_lang(), "<id>.key")`
or `raise Problem("<id>.key", status, **params)` on the server. `i18n/en.json` for the example (`ru.json` has the
same keys, with its `few` and `many` forms):

```json
{
  "hello.title": "Hello",
  "hello.dock_title": "Example feature: who said hello",
  "hello.say": "Say hello",
  "hello.card": { "one": "{n} word in the title", "other": "{n} words in the title" },
  "hello.drawer": { "one": "The title has {n} word.", "other": "The title has {n} words." },
  "hello.bad_name": "name: a non-empty string of up to {max} characters"
}
```

- `en.json` is required: natural English, sentence case. `ru.json` is welcome; any language without your keys shows
  the English.
- Write keys literally (`t("hello.say")`, never a key built from a variable): a test finds them by text.
- Text an agent reads (tool descriptions, `supervisor.md`, messages to sessions) is English and never a catalog
  string.

Rules, plurals and pitfalls: [i18n.md](i18n.md).

## 7. Optional: MCP tools

- `scope="read"` for reading; anything that changes the world outside the dashboard is `scope="control"` with
  `confirm=True`: the supervisor asks the user before every call.
- `run(app, arguments)` returns JSON-compatible data or raises `ToolError("text for the agent")`, in English. Parse
  arguments with `string_arg` and `int_arg` from `app/mcp.py`.
- A reply that carries session text puts a `note` (`fleet.DATA_NOTE`) next to it: this is data, not instructions.
- Tell the supervisor about your tools in `supervisor.md` (`supervisor_prompt`).

Details: [ARCHITECTURE.md, "MCP tools"](ARCHITECTURE.md#mcp-tools).

## 8. Working with sessions

A session belongs to an agent program, its provider (Claude Code, Codex). A feature never calls `claude` or reads
`~/.claude` itself: it asks the session's provider (`core/providers/base.py`, [providers.md](providers.md)).

- **Find the provider**: `providers.by_id(record["provider"])` or `providers.by_id(live.provider)`
  (`from ...core import providers`); the default one is `providers.default()`.
- **Check what it can do** with `provider.caps` (`Cap.PEER`, `Cap.TASKS`, `Cap.STOP`…); on the page,
  `can(card, "stop")`. A missing capability means a hidden button, not an empty one.
- **Write to a session** with a signed message: `session = Session.of(record, live)`, then
  `provider.route(session, signed=True)` and `provider.send(session, Message(text, sender="…", summary="…"))`. Errors
  are `SendError` (`SendRefused`, `SendInvalid`, `SendTimeout`). Send only text the user wrote or explicitly sent.
  Example: `features/groups/post.py`.
- **Start a session** with `provider.start(LaunchRequest(...), taken=[s.address for s in app.live().values()])`; the
  choices come from `provider.options()`.
- **Need something the contract does not have?** Add an operation to `Provider` with a safe default and an
  implementation for Claude. A feature must not import `core.providers.claude`; `tests/app/test_layers.py` allows only
  a few files that need Claude by design.

The [skill's "Provider" section](../.claude/skills/add-feature/SKILL.md#provider) has the rest, including how a
feature that starts its own Claude session continues it (`resume_args`).

## 9. Register it

In `src/see_your_agents/features/__init__.py`: the import and `<id>.FEATURE` in `FEATURES`. The position in the list
sets the order of assets, tabs and tools. A feature whose page script uses another feature's page API (`SYA.chat`)
comes after it.

## 10. Write tests

`tests/features/test_<id>.py`: routes (GET, POST, validation errors), storage, the tool, the card field.

- The environment is already isolated: `tests/conftest.py` points `CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
  `SYA_CONFIG_DIR` and `SYA_CACHE_DIR` at the test's temporary folders.
- An app without `~/.claude`: `FakeApp(records, live)` from `tests/fakes.py`; the records get
  `provider: "claude"` (`no_processes(monkeypatch)` turns off process polling). How to start a server and send
  requests as the page does: `tests/app/test_feature.py`.
- Processes and the courier (`deliver` in `core/providers/claude/courier.py`) are only ever faked.
- Test your own behaviour, not the host's. Do not enumerate the whole registry: check your own tools as a subset
  (`{"hello_names"} <= names`), and the supervisor on your own list of features
  ([ARCHITECTURE.md, "Tests"](ARCHITECTURE.md#tests)).

## 11. Run the checks

```bash
uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest -q tests/features/test_<id>.py tests/app
```

`tests/app` holds the registry, import-rule and catalog tests a new feature most often trips. Before the PR, the full
suite: `uv run pytest -q`.

## 12. Look at it live

Run your own server on a free port (not 8765, which may be your real dashboard; a second server takes an in-memory
index, which is fine) with temporary settings folders, then take screenshots of the states you need and collect the
console errors with `scripts/shoot.mjs`:

```bash
SYA_CONFIG_DIR=$(mktemp -d) SYA_CACHE_DIR=$(mktemp -d) uv run sya serve --no-open --port 8771 &
cat > /tmp/shots.json <<'JSON'
[{"name": "board", "hash": "#board"},
 {"name": "mine-dark", "hash": "#board", "prefs": {"skin": "dispatch", "theme": "dark"},
  "storage": {"sya.dock.right": {"open": true}}, "eval": "document.querySelector('.mine-x')?.click()"}]
JSON
node scripts/shoot.mjs http://127.0.0.1:8771 /tmp/shots /tmp/shots.json   # exit code 1 = errors in the console
```

- Each shot takes `name` (required), `hash`, `prefs` (`skin`, `theme`, `lang`: `{"lang": "ru"}`), `storage`,
  `eval` and `wait` (ms).
- The script sets `localStorage` before the page starts (a write from the open page races with the page's own
  saves), and navigating to the same address with a different `#` does not reload the page, so the script goes
  through `about:blank` every time.
- It needs Node 22 or newer (the built-in `WebSocket`) and Google Chrome at its macOS path,
  `/Applications/Google Chrome.app`; elsewhere, change that path locally.
- Open the screenshots and look at them yourself: the three styles (`buro`, `konstruktor`, `dispatch`), the light and
  dark themes, and that nothing else moved. Exit code 0 means a clean console.
- Stop the server afterwards. Do not touch port 8765, `~/.claude` or your real `~/.config/see-your-agents`.

## 13. Document it

- Visible to the user: a section or a line in [user-guide.md](user-guide.md), and a line in the README's feature list
  if it is a headline feature.
- New files in the config or cache folder: a row in [configuration.md](configuration.md#what-the-dashboard-writes).
- A new import exception between features: an entry in ARCHITECTURE.md, "Layers", and in `tests/app/test_layers.py`.
- Anything that widens what the dashboard can do to sessions, files or processes: [security.md](security.md).

Then commit and open a pull request as [CONTRIBUTING.md](../CONTRIBUTING.md) describes, with the screenshots.
