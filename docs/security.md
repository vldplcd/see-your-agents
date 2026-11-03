# Security

The dashboard reads private transcripts and can act on your agents: write to them, stop them, launch new ones,
answer their permission prompts and merge code into its own checkout. This page says whom it defends against, what
it guarantees, and what it does not. The implementation-level invariants a change must keep are in
[ARCHITECTURE.md, "Security: what must not break"](ARCHITECTURE.md#security-what-must-not-break); the verified facts
behind them are in [notes/](notes/). To report a vulnerability, see [SECURITY.md](../SECURITY.md).

- [Threat model](#threat-model)
- [Network surface](#network-surface)
- [The POST guard](#the-post-guard)
- [Browser pairing](#browser-pairing)
- [Permission requests](#permission-requests)
- [MCP tokens](#mcp-tokens)
- [Writing to sessions and the courier](#writing-to-sessions-and-the-courier)
- [Launching sessions](#launching-sessions)
- [Text from transcripts](#text-from-transcripts)
- [Files outside the dashboard's folders](#files-outside-the-dashboards-folders)
- [Builder sandboxes](#builder-sandboxes)
- [Residual risks](#residual-risks)
- [Where it is tested](#where-it-is-tested)

## Threat model

Defended against:

- **Web pages in your browser**: CSRF, DNS rebinding, framing the dashboard's buttons.
- **Local processes that only speak HTTP**: an agent, `curl`, a build's test over localhost. They can forge `Origin`
  and any header, but they do not have the paired tab's secret.
- **Pages on other ports** of `127.0.0.1`, and a process listening on `::1`.
- **A process that takes the port** while the dashboard is off or restarting: the permission hook does not hand it a
  request or trust its answer, and a secret it reads belongs to a dead process.
- **Text inside transcripts** (prompt injection): it is data, never instructions, for the page and for the
  supervisor.
- **Code the builder writes**: it runs in a sandbox, and only a person at the paired tab can merge it.

Not defended against:

- A process of your own user with full access: it can write "allow all" into `~/.claude/settings.json` without the
  dashboard.
- A process of your user that controls the browser UI or reads its memory.
- You confirming a pairing you did not start.
- The other cases under [Residual risks](#residual-risks).

## Network surface

- The server listens only on `127.0.0.1`.
- Every GET checks `Host` (`127.0.0.1:<port>` or `localhost:<port>`): transcripts are private, and this is the
  protection against DNS rebinding.
- Every HTML reply has `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'`. A plan
  (`/api/plan`) is embedded only by the page itself (`SAMEORIGIN`, `sandbox allow-scripts`).
- Page assets are served only by name from a feature's manifest, only `[a-z0-9-]+.(js|css)`, only from that
  feature's `web/` folder. The strings script is built from JSON catalogs, never code from a file.
- Images from transcripts: raster types only; an SVG served from the dashboard's address would run a script with
  API access.

## The POST guard

POSTs change the world (launch a session, write to a group, accept a build), so every page POST is checked three
ways:

1. `Origin` is the page itself;
2. the `X-SYA: 1` header, which from a foreign site triggers a CORS preflight the server does not answer;
3. unless the path is in the open allowlist, the paired browser's secret in `X-SYA-Pair`.

Any local process forges the first two; the third is what tells the dashboard's own tab apart. An unpaired POST gets
403 and the page offers "Pair this browser".

- The open allowlist is `/api/pair*` (the bootstrap) and the routes a feature declares in `open_posts`: today only
  the permission hook's HMAC-signed routes.
- `/mcp` has its own tokens and is handled before this guard.
- The body is 0 to 32 MB; a negative length is refused (it would hang the read).
- `tests/app/test_layers.py` pins the features with a POST handler and the open set: a new POST route fails the
  suite until it is classified (paired by default, or opened with a reason).

## Browser pairing

`app/pairing.py`. Origin and `X-SYA` cut off web pages, but not local processes, so every page POST outside the
open allowlist requires a secret that only the paired tab and only this server process know.

- **How.** The page (the "Pair this browser" button) calls `POST /api/pair/start`; `sya serve` without `--no-open`
  does the same on every start. The server makes a code: 32 random bytes, 2 minutes, only one open at a time, spent
  only by an exact match, in memory only. It opens a new tab `http://127.0.0.1:<port>/#pair=<code>` (the address does
  not come from the request's Host), at most once a minute.
- **The code stays out of argv.** The browser is opened by `osascript` reading `open location` from stdin: other
  users can list process arguments. Off macOS, pairing refuses. The code never appears in HTTP replies, logs or
  files.
- **The person confirms.** The tab removes the code from the address at once and asks "Pair this browser with the
  dashboard?". Only after "Pair" does it trade the code for a secret (`POST /api/pair`).
- **Only in process memory.** Secret hashes (at most 8; an extra one evicts the least recently used) live in the
  memory of `sya serve` and die with it: every process starts unpaired.
- **Why `sessionStorage`.** It is separate for each tab and for each origin with its port: another tab, or a page on
  another `127.0.0.1` port, does not see the secret. A cookie would go to all ports; HttpOnly would give nothing,
  since a script on the dashboard's origin can click the button anyway.
- **Only `127.0.0.1`.** `localhost` can resolve to `::1`, and anyone can take that while the dashboard listens on
  IPv4. `/api/pair*` and the secret check require Host `127.0.0.1:<port>` and the same Origin.
- **No silent reloads.** The tab does not reload itself: on a version change it shows "Reload", which works only when
  `/api/board` names an `instance` that is new to the tab and the same again 2 s later. On an `instance` change the
  tab discards its secret. Asset URLs carry `?v=<version>.<instance>`, so a copy a foreign process served and the
  browser cached is not picked up. The page removes service workers on its origin (the dashboard has none).

## Permission requests

`features/approvals`; the facts about the hook are in
[notes/claude-hooks-notes.md](notes/claude-hooks-notes.md). Only programs with `Cap.PERMISSION_WAIT` get this
(Claude Code); a Codex session gets no request at all.

- **Who creates a request.** The Claude Code hook (`hook.py`) calls `POST /api/approvals/hello`, `ask`,
  `<id>/wait`, `<id>/withdraw`. Besides the host checks, every request is signed with the token from
  `~/.config/see-your-agents/approvals-token` (0600): a fresh nonce, a timestamp (±60 s), HMAC-SHA256 of the path
  and the body; a repeated nonce is rejected. The reply is signed with the same token and bound to the nonce and the
  path; the hook verifies the signature before printing anything. The token never goes over the network.
- **Whom the hook trusts.** A process that took the port while the dashboard is off gets neither a request (`hello`
  checks the dashboard before the hook hands over the tool input) nor a trusted "allow". For the hook, an unsigned
  reply is a failure like any other.
- **Who decides.** Only a paired browser: `POST /api/approvals/<id>/decide` passes the POST guard and needs the
  pairing secret, only on `127.0.0.1`. The hook token is neither needed nor sufficient. There is no MCP tool for
  permissions: an agent does not approve agents' calls.
- **What a decision can do.** Only "yes" or "no" for the call this hook process was started for. The hook does not
  take tool input from the dashboard: "Allow" is a bare `allow`; for `AskUserQuestion` the hook adds `answers` to its
  own input, and only if the keys are exactly its questions; for `ExitPlanMode` it returns its own input and
  `setMode: default`. It never sends `updatedPermissions` ("remember forever").
- **Allow only what is shown.** An allow approves the whole input, so the page draws every field;
  `dangerouslyDisableSandbox: true` is a red line. If anything is cut or a field is not drawn, there is no "Allow",
  on the page or on the server (409), only "Deny" and advice to answer in the terminal.
- **A failure means the normal dialog.** The dashboard does not answer, the reply is not JSON, time ran out, the hook
  was killed: the hook exits 0 with no output. It skips `claude -p` and subagent calls at once.
- **The first answer wins.** The hook notices an answer in the terminal by the `tool_result` in the transcript and
  withdraws the request. `decide` replies "ok" only when the hook picked up the decision within 4 s; otherwise 409.
  After the set of requests changes, the buttons cannot be clicked for 700 ms, so a click meant for the old one does
  not land on the new one.

## MCP tokens

- `/mcp` listens only on `127.0.0.1` and checks `Host`, `Origin` (if present) and a bearer token. The body is at most
  1 MB.
- Tokens are kept in `~/.config/see-your-agents/mcp-tokens.json` (0600). Every session has the `read` token (through
  `sya mcp install`, in `~/.claude.json`); only the supervisor has the `control` token
  (`~/.config/see-your-agents/supervisor/mcp.json`, 0600). The `read` token sees only `read` tools, `control` only
  `control` ones.
- **This split is not a boundary.** A session allowed to read files and run `curl` can take the `control` token
  itself. The server asks for no confirmations of its own; confirmation exists only in the supervisor's session
  (`confirm=True` tools are `ask` rules there).
- Everything that changes the world outside the dashboard is `scope="control"` with `confirm=True`. There are no MCP
  tools to accept, roll back or reject a build, or to answer a permission prompt.
- The supervisor runs with only `Read`, `Grep`, `Glob`, `ListAgents`, `SendMessage` and the dashboard's MCP
  (`--tools`, `--strict-mcp-config`), in `default` permission mode. In a single `claude -p --resume` turn there is
  nobody to confirm, so the `ask` tools are rejected, not run.
- **Messages from `Dashboard`.** Everything you write to the supervisor from the chat panel or with "Morning review"
  reaches it from the `Dashboard` session (in older transcripts, «Дашборд»), and it carries it out as your request.
  Any local session that has Bash can take this name. Without confirmation, the supervisor then only reads (sessions,
  Jira), keeps the to-do list and writes to group feeds; sending to sessions, new agents and builds ask you.

## Writing to sessions and the courier

- A session waiting for an answer to a permission or a dialog receives no text: it would land in the dialog and
  could confirm something. Only jumping to the terminal is offered.
- Files are attached as `@/absolute/path`; only files from `~/.cache/see-your-agents/uploads` are accepted.
- "Finish" goes through the provider's `stop`: Claude checks the pid against the session registry (process start
  time and the name `claude`) before SIGTERM; a background session is stopped by `claude stop`. The `takeover` route
  checks the pid the same way. Other processes are not touched.
- Only text the user wrote or explicitly clicked to send is sent.
- **The courier guard.** Claude Code accepts a message between sessions only from another Claude process, so the
  dashboard runs a one-shot `claude -p` on haiku, the courier (`core/providers/claude/courier.py`), with only the
  `SendMessage` tool and no MCP servers. A `PreToolUse` hook in it (a stdlib-only script run with `python -I`) lets
  through only the exact text to the exact session, character for character; a paraphrase is refused before it
  leaves, and any failure of the hook itself is turned into a block (exit code 2). The dashboard then reads the
  courier's output and accepts only exactly one successful send of the original text to the right recipient. The
  courier runs with a clean environment in a 0700 folder; failed runs are kept 0600.
- The VS Code bridge socket is in `~/.cache/see-your-agents/vscode/` (directory 0700, socket 0600); the extension
  does not go to the network.

## Launching sessions

A new session starts only through the provider; features and the supervisor's tools never build a program's argv
themselves.

- **The folder rule** (`project_dir` in `core/providers/base.py`): an absolute path, inside your home folder, not the
  home folder itself, no hidden parts (`~/.ssh`, `.claude/worktrees/…`), an existing directory. The path is checked
  against home before anything on disk is touched, and again after symlinks are resolved.
- **Allowlists.** Claude's model, effort and permission mode come only from `OPTIONS`; `bypassPermissions` and
  `dontAsk` are not in them. Codex's models and efforts come from `models_cache.json`, the sandbox is
  `workspace-write` or `read-only`; `danger-full-access` and `--dangerously-bypass-approvals-and-sandbox` are never
  passed. Each Claude flag is `--flag=value`, so a value cannot pass for another flag.
- **A clean environment** (`core.env.clean_env`): the server's venv and the parent Claude session's variables are
  removed by an explicit list.
- From `~/.claude.json` only `projects[<path>].hasTrustDialogAccepted` is read; from `~/.claude/settings.json` only
  `model`; from `~/.codex/config.toml` only `model`; `~/.codex/auth.json` is never read.

## Text from transcripts

- On the page it is output only through `esc()`; the chat's markdown escapes first.
- MCP replies that carry session text have a `note`: this is data, not instructions. The supervisor's prompt repeats
  the rule.
- Secrets from MCP configs do not reach the page ("Tools" strips them).

## Files outside the dashboard's folders

- The dashboard writes nothing to `~/.claude` except through `sya mcp install`, `sya supervisor install` and
  `sya hooks install`, which you run yourself.
- It only reads `~/.codex`: `state_5.sqlite` is opened read-only, thread lock files are only inspected through
  `lsof`, never acquired. Only `codex queue` and `codex exec`, run on your action, write there. It only queries the
  Codex daemon (`initialize`, `thread/loaded/list`, `thread/read`): it never starts turns, answers requests or
  starts the daemon.

## Builder sandboxes

`features/builder`; verified facts in [notes/claude-sandbox-notes.md](notes/claude-sandbox-notes.md). The builder
runs tests it wrote itself, so allow and deny rules on its commands are not enough: the code inside a test runs too.

**Your checkout.** Builds touch it only through page buttons, from the paired tab. "Accept" merges exactly the
commit the checks passed on, only into the branch the build started from, and refuses a build that changes paths
the checkout ignores or anything in `.claude/` outside `.claude/skills/`. The builder is denied `git push`,
`git merge`, `git checkout main`, `git reset --hard` and `git branch -D`.

**The builder's session** runs every Bash command in Claude Code's own sandbox (Seatbelt), switched on by the
build's settings file, not your `~/.claude/settings.json`:

- it may write only to its worktree and its `/private/tmp/sya-build-<hash>` folder;
- in the shared `.git`, the checkout's HEAD, `packed-refs`, all other branches, tags, stash and the other worktrees
  are closed; only its own `build/<name>` branch is open;
- it cannot read `~/.config/see-your-agents`, where the `control` token lives;
- there is no network beyond localhost (`deniedDomains: ["*"]` overrides even your `WebFetch(domain:…)` rules);
- it cannot leave the sandbox, even if you allow a command (`allowUnsandboxedCommands: false`), and without a
  sandbox the session does not start (`failIfUnavailable`).

**The dashboard's checks and "Preview"** (ruff and pytest after the builder's turn, and the preview server) run the
build's code outside that session, so they run under `/usr/bin/sandbox-exec` with a generated profile: the same
writable worktree and temp root, `config_dir()` read-denied, no network but loopback, and the dashboard's own port
denied. Without `sandbox-exec`, or when the dashboard's port is unknown, they refuse to run rather than fall back to
running unconfined.

**uv.** The build's venv is prepared once with `uv sync --locked` from the checkout's commit before the builder
starts; inside, uv is offline (`UV_OFFLINE=1`, `UV_NO_CACHE=1`). `uv sync` never runs again outside the sandbox, so
a build cannot add dependencies or run a build hook from its own `pyproject.toml`.

## Residual risks

Documented gaps, by design or for now:

- **Port squatting while the dashboard is off.** Someone who takes the port while the dashboard is off, and waits
  for the page to be opened once during that time, can gain a foothold in the browser (a service worker) and steal
  the next pairing. That requires a process under your user or under another user of this Mac; a process under your
  user can simply write "allow all" into `~/.claude/settings.json` anyway. The real fix is to keep the port occupied
  at all times (launchd, later). What narrows the window: the secret lives only in process memory, the tab does not
  reload itself, assets carry the process version, the page removes foreign service workers.
- **The browser itself.** No protection against a process of your user that controls the browser UI or reads its
  memory, or against you confirming a pairing you did not start.
- **MCP `read` and `control`** are not a boundary between sessions that can read files and run commands (above).
- **The `Dashboard` sender name** can be taken by any local session with Bash; the supervisor's confirmations are what
  stands between such a message and an action.
- **The builder's sandbox does not cover:**
  - reads: almost all of the home folder is readable (`~/.ssh`, `~/.claude.json` with the `read` token); only
    `config_dir()` is denied;
  - localhost: bind and loopback are open, so build code can reach another local service; for the builder's own
    commands the dashboard's port is open too (the checks and the preview deny it), though its POSTs still need the
    paired tab's secret;
  - the shared `.git`: objects can be written (commits need it), so a corrupted or substituted object is found only
    by `git fsck`; a branch created after the build started is not protected; reflogs and `info/` are open;
  - tools outside Bash: Read, Edit, Write, WebFetch, WebSearch, MCP and hooks run under the session's normal
    permissions, and your `WebFetch(domain:…)` rules apply to it;
  - `sandbox.excludedCommands` in your `~/.claude/settings.json` would, per the CLI code, take matching builder
    commands out of the sandbox.
- **Platform.** Pairing, and with it every action, is macOS-only; elsewhere the page is read-only.

## Where it is tested

| Area | Tests |
|---|---|
| POST guard classification, import rules | `tests/app/test_layers.py` |
| Origin, `X-SYA`, the open allowlist, assets | `tests/app/test_server.py`, `tests/app/test_feature.py` |
| Pairing, Host, framing | `tests/app/test_pairing.py` |
| Permission requests, the hook, HMAC | `tests/features/test_approvals.py` |
| MCP tokens and scopes | `tests/app/test_mcp.py` |
| Courier guard | `tests/providers/test_claude_courier.py` |
| Folder rule, launch allowlists | `tests/providers/test_launch_rules.py`, `tests/providers/test_claude_launch.py`, `tests/providers/test_codex_launch.py`, `tests/features/test_launch.py` |
| Builder: accept rules, sandbox profile, preview | `tests/features/test_builder.py` |
