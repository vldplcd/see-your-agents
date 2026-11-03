# The Bash sandbox in Claude Code 2.1.317: notes for the builder

Verified 2026-11-03 on macOS (Seatbelt). The probes ran on haiku, as background sessions: `claude --bg --model=haiku
--permission-mode=acceptEdits --settings=<file>`. Each probe ran one script, and the script wrote its results to a
file in its own worktree. The scratch repository was in `scratchpad/spike/work`. Claude Code trusts a git repository
only by its root: the search for trust up the folders stops at the repository root. So a repository in a subfolder of
a trusted folder is untrusted, and the trusted folder itself had to be made the root. At the end, a live run of the
builder through the dashboard.

Markers: **P**: verified by a probe, **C**: read in the CLI code (`strings` of the binary, the settings schema), not
verified by a probe.

## Keys

The `sandbox` schema in the binary (C): `enabled`, `failIfUnavailable`, `autoAllowBashIfSandboxed`,
`allowUnsandboxedCommands`, `excludedCommands`, `ignoreViolations`, `allowAppleEvents`, `enableWeakerNetworkIsolation`,
`credentials.{files,envVars}`, `filesystem.{allowWrite,denyWrite,denyRead,allowRead,disabled}`,
`network.{allowedDomains,deniedDomains,strictAllowlist,allowLocalBinding,allowUnixSockets,allowAllUnixSockets,
allowMachLookup,httpProxyPort,socksProxyPort,tlsTerminate}`. For `strictAllowlist`, `filesystem.disabled`,
`allowAppleEvents` and the protective `false` in `allowUnsandboxedCommands`, only user settings, managed settings and
`--settings` count; project settings do not (C, documentation).

- **No path variable (C).** A relative path is resolved from the settings file. So each build has its own file with
  absolute paths: `config_dir()/builder/build-<name>.json`.
- **`--bg`, `--resume`, `acceptEdits`, `--settings=<file>` (P).** The sandbox works in every `--bg` probe. After
  `--bg --resume=<id>` with the same file too: the live run below, where the escape attempts came after two resumes.
- **Lists merge (C, P).** `allowedDomains`, `excludedCommands`, `allowWrite` and permission rules merge in from every
  settings level. An entry from another level cannot be removed through `--settings`.

## What can be written

- **By default (P).** The working folder (worktree), TMPDIR and the main repository's shared `.git`. TMPDIR is
  `/tmp/claude-<uid>`: one folder for all of the user's sessions, with scratchpads and task output. A probe wrote a
  file into another session's scratchpad.
- **Closed without settings (P).** `.claude/settings.json` in the worktree, the worktree's own `.git` file, `commondir`
  and `config.worktree` in its admin folder, `config` and `hooks/` of the main `.git`. The error is `Operation not
  permitted`. The `gitdir` file in the admin folder can be written.
- **The shared `.git` is open (P).** Without denials a probe rewrote `refs/heads/main` and the main checkout's HEAD,
  created a branch, wrote into `objects/` and into another worktree's admin folder. `git update-ref refs/heads/main
  HEAD` returned 0.
- **`denyWrite` beats `allowWrite` (P).** `allowWrite: [.git/refs/heads/build]` inside `denyWrite: [.git/refs/heads]`
  opens nothing. A commit fails: `cannot lock ref 'HEAD': Unable to create '…/refs/heads/build/c.lock': Operation
  not permitted`. So the builder denies files one by one: HEAD, `packed-refs`, every other branch, each with its
  `.lock` too. Also `refs/tags`, `refs/stash` and every other admin folder `worktrees/<name>`.
- **A denial also covers a file that does not exist (P).** A denied `refs/heads/ghost` cannot be created. With such
  denials, `git update-ref` of main, of another branch, of `refs/stash`, `git tag` and `symbolic-ref HEAD` of the main
  `.git` are refused. A commit to its own branch goes through. A new branch can be created: it is not on the list.
- **`CLAUDE_CODE_TMPDIR` (P, C).** The variable works from `env` in the settings file too. Claude Code itself and the
  sandbox take the folder `<it>/claude-<uid>`, and the sandbox's TMPDIR becomes that: `/tmp/sya-c0ffee0c/claude-501`.
  The shared `/tmp/claude-501` and other sessions' scratchpads are closed. The condition (C, `H0e`): the path
  `<it>/claude-<uid>` is at most 44 bytes, otherwise TMPDIR falls back to `/tmp/claude-<uid>`. So the build folder is
  `/private/tmp/sya-build-<8 hex>` (a resolved path, see below).
- **`allowWrite` with a path in `/tmp` (P).** `allowWrite: ["/tmp/sya-c0ffee2a"]` opens `/tmp/sya-c0ffee2a/cache`
  outside `claude-<uid>`, although `/tmp` is a link to `/private/tmp`. The neighboring `/tmp/sya-c0ffee2b.txt` is
  closed.

## Reading

- **Almost everything is open (P).** `~/.ssh` and `~/.claude.json` can be read. `~/.claude.json` holds the
  dashboard's `read` MCP token.
- **`denyRead` works (P).** With `denyRead: ["~/.config/see-your-agents"]` reading that folder gives `Operation not
  permitted`. This is how the builder closes `config_dir()`: it holds the `control` token and the supervisor's Jira
  account.

## Network

- **Proxy (P).** The sandbox sets `HTTP(S)_PROXY` and `ALL_PROXY` to a local proxy with one-time credentials,
  `NO_PROXY` to localhost and private networks, and `GIT_SSH_COMMAND` sends ssh through the proxy.
- **`strictAllowlist: true` (P).** A host not on the list is refused without a question: `curl: (56) CONNECT tunnel
  failed, response 403`. Without this flag, `acceptEdits` would ask the user (C, documentation).
- **The user's rules merge in (P).** `WebFetch(domain:gitlab.example.com)` from `~/.claude/settings.json` got onto
  the list even with `strictAllowlist`: `curl https://gitlab.example.com` returned 302. `deniedDomains: ["*"]`
  overrides that: 403.
- **A direct connection past the proxy (P).** `curl --noproxy '*' http://1.1.1.1`: `Failed to connect to 1.1.1.1 port
  80 after 0 ms: Couldn't connect to server`.
- **`allowLocalBinding` (P, C).** Without it, `bind` on 127.0.0.1 gives `PermissionError: [Errno 1] Operation not
  permitted`: the dashboard's tests with servers fail. With it, the profile gets (C) `network-bind` and
  `network-inbound` on `*:*` and `network-outbound` on `localhost:*`. A probe bound `0.0.0.0` and connected to another
  server on 127.0.0.1:8811 outside the sandbox. So localhost is open entirely.
- **Unix sockets (P).** By default `bind` of a unix socket is denied: the Codex daemon tests failed with
  `PermissionError`. `allowUnixSockets: [<folder>]` opens sockets in that folder and below. For the builder this is
  its `/tmp/sya-build-…`.

## Leaving the sandbox

- **By default (P).** If `allowUnsandboxedCommands` is not set, a Bash call with `dangerouslyDisableSandbox: true` runs
  outside the sandbox. With a matching allow rule it runs **without a question**: `python3 -c "open('~/…-H.txt','w')"`
  with `allow: Bash(python3 -c:*)` created a file in the home folder. Without a rule, `acceptEdits` asks "Bash command
  (unsandboxed)". The builder has `Bash(uv run pytest:*)` in allow, so without the denial it could leave silently.
- **`allowUnsandboxedCommands: false` (P, C).** The parameter is ignored: the same call gave `PermissionError: [Errno
  1] Operation not permitted`. No question.
- **`excludedCommands` (C).** Per the code (`sS`), a command that matches an exclusion runs outside the sandbox
  whatever `allowUnsandboxedCommands` says. Exclusions come from managed settings, user settings and `--settings`, and
  they merge. The builder sets no exclusions of its own. But `sandbox.excludedCommands` in `~/.claude/settings.json`
  would take its commands out of the sandbox. A probe gave no answer on this: the pattern `python3 -c *` did not match
  even without the denial.
- **`failIfUnavailable: true` (C).** If the sandbox does not come up, the session exits at start instead of running
  without it.
- **`autoAllowBashIfSandboxed: false` (C, documentation).** Sandboxed commands are not approved automatically. What
  prompts is decided by ALLOW and DENY, as without the sandbox.

## What does not work inside

- **setuid programs (P).** `/bin/ps`: `Operation not permitted` (code 126). Two tests with `ps` are skipped
  (`needs_ps` in `tests/fakes.py`). The dashboard's checks run outside this sandbox, and there they run.
- **Headless Chrome (P).** Exits with code 21. It cannot write `~/Library/Application Support/Google/Chrome/
  Crashpad/settings.dat` and reports `Failed to create a ProcessSingleton for your profile directory`. No screenshots
  in the sandbox.
- **`/tmp` outside its own folder (P).** Closed. So `short_cache` in the VS Code bridge tests takes a folder in
  `$TMPDIR`, not in `/tmp`.

## uv

- **The user's cache (P).** `~/.cache/uv` is closed for writing: `error: Failed to initialize cache at
  /Users/…/.cache/uv … sdists-v9/.git: Operation not permitted`.
- **Without a venv (P).** Offline uv cannot create the environment: `Failed to download duckdb==2.0.0 … Network
  connectivity is disabled`.
- **A ready venv (P).** `uv sync --locked` outside the sandbox before the start, and inside `UV_OFFLINE=1` and
  `UV_NO_CACHE=1` (a temporary cache in TMPDIR). Ruff passes. Pytest without changes to the tests: 602 passed; only
  the unix-socket and `ps` tests failed. After `allowUnixSockets`, `needs_ps` and `short_cache` in `$TMPDIR` the whole
  suite passes (the live run below).
- **Sync only from the checkout's commit.** `uv sync` builds the project, and the builder could put a build hook into
  `pyproject.toml`. So the dashboard syncs once, in the fresh worktree, before the builder starts. A build cannot add
  a dependency.

## Live run through the dashboard

Its own server on 8799 with temporary `SYA_CONFIG_DIR` and `SYA_CACHE_DIR`. The checkout was a development worktree;
the build came from `request_change` through `/mcp` with the `control` token.

1. **An ordinary build (P).** The route `GET /api/sandbox-hello` and a test for it. `uv sync --locked` in the fresh
   worktree took about a second: the user's cache was warm. In the sandbox the builder's `ruff check`,
   `ruff format --check` and the whole pytest passed: 630 passed, 2 skipped (`ps`), 128 s. Its commit with a heredoc
   waited for confirmation: "Contains brace with quote character (expansion obfuscation)", and the heredoc held
   `{"ok": true}`. That is Claude Code's command check, not the sandbox. The builder was stopped and continued with
   "Rework" (`--resume`): it committed with `git commit -m`. The dashboard's checks gave 632 passed, and the build
   became ready.
2. **Escape attempts (P).** The builder was stopped again and continued with "Rework" (`--resume`). It wrote an
   uncommitted `tests/sandbox_escape_check.py`: the file is not `test_*.py`, and outside the builder's TMPDIR it is
   skipped. Running `uv run pytest -q -rA -s tests/sandbox_escape_check.py` gave 8 passed: every attempt was refused.
   Verbatim:

   ```
   ESCAPE write ~/sya-sandbox-escape-test.txt: PermissionError(1, 'Operation not permitted')
   ESCAPE write into the main checkout: PermissionError(1, 'Operation not permitted')
   ESCAPE urlopen https://example.com: URLError(OSError('Tunnel connection failed: 403 host is on the deny list'))
   ESCAPE direct socket 93.184.215.14:443: PermissionError(1, 'Operation not permitted')
   ESCAPE git push https://github.com/octocat/Hello-World.git: "fatal: unable to access 'https://github.com/octocat/Hello-World.git/': CONNECT tunnel failed, response 403"
   ESCAPE git push /private/tmp/claude-501/…/push-target.git: "error: remote unpack failed: unable to create temporary object directory … ! [remote rejected] HEAD -> sya-escape (unpacker error)"
   ESCAPE git update-ref of the checkout's branch: "fatal: update_ref failed for ref 'refs/heads/worktree-agent-a501278d863aae4e1': cannot lock ref …: Unable to create '/Users/…/see-your-agents/.git/refs/heads/worktree-agent-a501278d863aae4e1.lock': Operation not permitted"
   ```

   Claude Code added `deny network-outbound example.com:443 (host is on the deny list)` to the output, and the same
   for `github.com:443`. Then a check from outside: no `~/sya-sandbox-escape-test.txt` and no file in the checkout, not
   a single ref in `push-target.git`, the checkout's branch on its previous commit.
3. **"Reject" (P).** The builder stopped. The worktree, the `build/…` branch, the build's settings file and
   `/tmp/sya-build-…` were deleted.
4. **The checks' sandbox under `sandbox-exec` (P).** The `agent.profile` profile run directly under
   `/usr/bin/sandbox-exec` (the test `test_the_checks_and_preview_run_under_a_sandbox_profile…`): writing into the
   worktree and the temp root goes through; `~`, the scratchpad and `config_dir()` are closed; the "dashboard" port is
   closed on `127.0.0.1`, `localhost` and `[::1]`, while another loopback port is open; no network outside. The preview
   under the same profile in a live child dashboard comes up and answers
   (`test_sigterm_to_the_dashboard_stops_its_preview`), and dies together with the dashboard.
5. **Pairing for every action (P).** Its own server on 8801 with temporary folders and a stub opener. POST
   `/api/launch`, `/api/chat/<id>/send`, `/api/todo` with the page's headers (Origin, `X-SYA`) but without the secret,
   and with a forged secret: 403 in every case. After pairing through the bootstrap (`/api/pair/start` → the opener
   writes the code to a file → `/api/pair` hands out the secret) the same POSTs with the secret pass the guard:
   `/api/launch` and `/api/todo` answer 400 to an empty body (a validation error, not 403; no session started).

## The sandbox of the checks and the preview (`sandbox-exec`)

The dashboard's checks (`run_checks`: ruff and pytest after the builder's turn) and "Preview" run the build's code, but
outside the builder's session, so they go under `/usr/bin/sandbox-exec` with a generated profile (`agent.profile`).
The profile: `(allow default)`, then `(deny file-write*)` and `(allow file-write* (subpath …))` on the worktree and on
`/private/tmp/sya-build-<8 hex>` (plus devices like `/dev/null`); `(deny file-read* (subpath <config_dir()>))`;
network: `(deny network*)`, `(allow network-bind (local ip "*:*"))`, binding unix sockets under the temp root,
`(allow network-outbound (remote ip "localhost:*"))` and `(deny network-outbound (remote ip "localhost:<dashboard
port>"))`. Verified by running directly under `sandbox-exec` (P):

- **Seatbelt matches canonical paths.** On macOS `/tmp` is a link to `/private/tmp`, and the rule
  `(subpath "/tmp/sya-verify-x")` does not let a write into `/tmp/sya-verify-x/a.txt` through (`Operation not
  permitted`), while `(subpath "/private/tmp/sya-verify-x")` does. So every path in the rules goes through `realpath`:
  the worktree, the temp root, `config_dir()`, and `TMP_PARENT` defaults to `realpath("/tmp")`. (Claude Code's sandbox
  canonicalizes paths itself, but it now gets resolved paths too.) The length of
  `/private/tmp/sya-build-<8 hex>/claude-<uid>` is 39 bytes plus the uid's digits, within Claude Code's limit (44) for
  uids of up to five digits; with a longer one the builder does not start;
- a write into the temp root goes through however the process spells it (`/tmp/…` or `/private/tmp/…`); into the
  worktree too; `~`, `/private/tmp/claude-501` and a path next to the temp root: `Operation not permitted`;
- **Seatbelt does not nest:** `sandbox-exec` inside `sandbox-exec` gives `sandbox_apply: Operation not permitted`
  (code 71). The suite's tests that call `sandbox-exec` themselves are skipped under the dashboard's checks and in the
  builder's sandbox (`needs_seatbelt`), otherwise the checks could never pass; in a plain `uv run pytest` they run.
  `/bin/ps` does not start under `sandbox-exec` either (`execvp() … Operation not permitted`); the tests with it are
  `needs_ps`;
- `config_dir()` is closed for reading;
- **one** denial `(remote ip "localhost:<port>")` blocks connecting to that port on `127.0.0.1`, `localhost` **and**
  `[::1]`: verified on all three; another loopback port (7002) stays open;
- the outgoing network outside (`example.com`, a direct connect by IP, `git push` over https): `Operation not
  permitted` or `CONNECT tunnel failed, response 403`.

The TMPDIR of these processes is the build's temp root (`confined_env`), so that pytest, uv and the sockets write where
the profile lets them. uv is offline on the ready venv (`UV_OFFLINE=1`, `UV_NO_CACHE=1`). The preview's config and
cache live in the temp root (`<root>/preview`): the parent (the dashboard), not the sandbox, makes the copy of the
user's settings, so the child `sya serve` does not need to read `config_dir()`. It fails closed: without
`sandbox-exec`, or when the dashboard's port is unknown (a `localhost:0` rule would close nothing), the checks fail
with an error (`builder.sandbox.*`, worded in the page's language) and the preview refuses; the build's code never runs
without the sandbox. After a dashboard restart, orphaned checks get the new server's port.

**End-to-end run (P).** A real worktree at `HEAD`, a venv through `uv sync --locked`, the real checks (`ruff check`,
`ruff format --check`, the whole pytest) through `run_checks` under `/usr/bin/sandbox-exec` with the temp root
`/private/tmp/sya-build-245c0c67`: the build became `ready`, 695 passed, 5 skipped (three Seatbelt tests and two with
`ps`), 147 s; pytest's basetemp landed in the temp root; the worktree, the branch and the temp root were deleted
afterwards.

## What the sandbox does not close

- **Localhost.** The tests and the preview need their own servers, so bind and loopback are open (bind on `0.0.0.0`
  too). For the builder's commands (the session) the dashboard's port is open as well; for the checks and the preview
  (`sandbox-exec`) the dashboard's port is closed, but the rest of localhost is open: the build's code can reach
  another local service. The dashboard's POSTs now require the paired browser (the `X-SYA-Pair` secret), which a
  local process does not have: launching sessions, sending text and "Accept" on a finished build are out of reach for
  a process without the secret (before, Origin and `X-SYA` were enough).
- **Reading.** Almost everything in the home folder is open, except `config_dir()`. There is nowhere to carry what was
  read, except localhost.
- **The shared `.git`.** Objects can be written, otherwise commits would not land: a corrupted or substituted object
  is found only by `git fsck`. A branch created after the build started is not protected. Reflogs and `info/` are open
  too.
- **Tools outside Bash.** Read, Edit, Write, WebFetch, WebSearch, MCP and hooks run outside the sandbox, under the
  session's normal permissions. `WebFetch(domain:…)` rules from the user's settings apply to the builder too.
- **The user's `excludedCommands`**, see above.
