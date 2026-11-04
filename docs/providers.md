# Providers

A provider is the agent program whose sessions the dashboard sees: Claude Code and Codex CLI. Everything that depends on the program lives in the provider: where the transcripts are and how to read them, who is running now, how to message a session, how to stop it and how to launch a new one. The index, the board, the chat, "New session", groups and the supervisor ask the session's provider or the registry and never call the program themselves.

A new provider is a folder in `core/providers/` and one line in the registry, like a feature.

```
src/see_your_agents/core/
  uploads.py      files attached to a chat message: cache_dir()/uploads/<session_id>
  providers/
    __init__.py   registry: PROVIDERS, check, by_id, default, available, live
    base.py       contract: Provider, Cap, Kind, Status, Digest, TaskItem, Event, LiveSession, Session,
                  Route, Message, Sent, SendError…, Thread, LaunchRequest, LaunchOptions, Started, project_dir,
                  checked_task
    claude/       Claude Code
      provider.py   class Claude(Provider): the contract over the modules below
      paths.py      claude_home(), claude_json(); CLAUDE_CONFIG_DIR moves both
      transcript.py transcript → Digest; transcripts(home)
      thread.py     transcript → chat messages (ClaudeThread)
      live.py       claude agents --json, the ~/.claude/sessions registry, jobs, tasks
      routes.py     routes vscode, tmux, attach, peer, resume, takeover; stop, focus; plug: the dashboard side
      attach.py     the attach route: the text typed into a background session through `claude attach`
      courier.py    the courier: a one-off claude -p (haiku) calls SendMessage
      launch.py     claude --bg: options, start, fork, trusted; run, find_session, stop_job for the builder
    codex/        Codex CLI
      provider.py   class Codex(Provider): caps = {ATTACH, LAUNCH}
      paths.py      codex_home(); CODEX_HOME moves it
      rollout.py    rollout → Digest; Replay (a fork's inherited history); transcripts without
                    subagents and service threads
      names.py      thread names: state_5.sqlite (mode=ro), else session_index.jsonl
      thread.py     rollout → chat messages (CodexThread)
      live.py       thread-writer-locks + lsof: what runs and who runs it; whether a turn runs: from the rollout's tail
      daemon.py     whether a daemon thread waits on a permission or an answer: the daemon's JSON-RPC over WebSocket,
                    read-only
      queue.py      route, send: codex queue --resume; a turn in the rollout confirms delivery
      launch.py     codex exec --json in the background: options from models_cache.json, trusted, start
```

## Contract

`Provider` in `base.py` is an abstract class. The four read operations are required; the rest refuse or return nothing by default. So a "read-only" provider already works: its sessions show on the board, but you cannot message them.

All operations name a session the same way: `Session`. All of them report a write error the same way: with the `SendError` exception, not with a return value.

| Operation | Required | What it does | Default |
|---|---|---|---|
| `id`, `title`, `caps`, `parser_version` | yes | `id` is `[a-z][a-z0-9-]{0,31}` and is written to the index; bump `parser_version` when `parse` changes | — |
| `available()` | yes | the program or its data exists on this machine; an unavailable provider is skipped | — |
| `transcripts()` | yes | `{session_id: Path}` of all transcripts on disk | — |
| `parse(path) -> Digest` | yes | a transcript into a `Digest`; it does not fail on an unknown record but skips it | — |
| `live() -> {session_id: LiveSession}` | yes | who is running now; called every couple of seconds | — |
| `names() -> {session_id: name}` | | session names the program keeps outside transcripts; the index reads them when it returns records, and the name becomes the title (a rename shows without a new parse, and search finds it) | `{}` |
| `tasks(Session) -> list[TaskItem]` | | the task list from disk (`Cap.TASKS`) | `None` |
| `attach_command(Session)` | | the command that opens the session in a terminal (`Cap.ATTACH`) | `""` |
| `thread() -> Thread` | | a new reader of the transcript as a chat | `None`: the chat shows "no transcript" |
| `route(Session, signed=False) -> Route` | | how a message would get through now; called for every card, so it must be cheap | no route |
| `send(Session, Message) -> Sent` | | send via `route(session, signed=bool(message.sender))` | `SendRefused` |
| `resume(Session, Message) -> Sent` | | one turn in a stopped session, in the background | `SendRefused` |
| `stop(Session)` | | stop the process, only after making sure it belongs to that session | `SendRefused` |
| `focus(Session)` | | show the window where the session runs | `SendRefused` |
| `options() -> LaunchOptions` | | what you can choose at launch: allowlists, the default model, value labels, each model's efforts, a note line | empty |
| `trusted(folders)` | | folders where a launch will not be refused over trust | all |
| `start(LaunchRequest, taken) -> Started` | | a new background session on a first task | `LaunchError` |
| `fork(Session, name, prompt, taken) -> Started` | | a new background session with a copy of this one's history (`Cap.FORK`); its name comes from the session's `name`; without `prompt` the copy waits | `LaunchError` |

Types, in the same `base.py`:
- `Digest`: what happened in the session: title, prompts, replies, tasks (`TaskItem`), plans, questions, money (`cost_usd`) or tokens (`tokens`, for a program without `Cap.COST`), messages between sessions, search events (`Event`). A field the program has no data for stays empty.
  - `last_turn_aborted`: the last turn was interrupted, not finished (`turn_aborted` in Codex); `Session.of` carries it into `Session`, and the Codex route refuses on it without reading the rollout.
  - `queued_turns`: turns started by the program's own queue (`codex queue` in Codex); they are not messages between sessions, so they are not `messages_in` and not a "message" line on the card. A record: `ts`, `msg_id` (the turn id; the same one `send` returned in `Sent.msg_id`), `reply` and `reply_at`: the turn's last reply. By `msg_id` a group finds a member's reply in `messages_in` or `queued_turns` without asking whose it is.
  - `context_tokens` out of `context_window`: how full the context window is: "context 63%" on the card and in the details. `context_window = 0` means the program does not report it, and the page shows nothing (Codex, for now). In Claude: the `usage` of the last reply (input + cache_creation + cache_read), after a `compact_boundary` its `postTokens`; the window is 1M or 200k ([configuration.md, "Context on the card"](configuration.md#context-on-the-card)).
- `LiveSession`: a running session: `kind` (`Kind`), `status` (`Status` or the program's own word), `waiting_for`, `name`, `pid`, `cwd`, `job_id`, `host` (who holds the session: in Codex `daemon`, `vscode`, `exec`, `tui`)… and `provider`. The `provider` field is required and holds the name only. `Kind` and `Status` are `StrEnum`s and equal plain strings: the board lays out columns by them, and Claude picks a route by them.
- `Session`: a session the provider acts on: `session_id`, `cwd`, `transcript_missing`, `live`, `last_turn_aborted`. `Session.of(record, live)` builds it from an index record.
- `Route(route, can_send, reason, target, alt, pending, params)`: a route. `reason` is a catalog key (`app.provider.<id>.*` in `web/i18n/*.json`, [i18n.md](i18n.md)) with its `params`; `why()` words it in the request's language, `refusal()` is the `SendRefused` a send along a closed route raises. `pending`: a reply the dashboard started is still running; on the card it is `running_reply`. `public()` gives the card `delivery` without `target` and `pending`, the reason worded.
- `Message(text, files, takeover, sender, summary)`: what to send:
  - without `sender`, the user wrote it in the chat, and the provider may type it into the session's terminal;
  - with `sender` (a group, "Morning review") it is a signed message: a live session must see it as a message from the sender, not as typed text;
  - `summary`: a line for the recipient's message list.
- `Sent(route, log, msg_id)`: where it went, the log of a `resume` turn, and the message id under which the reply will be found: in Claude the recipient's `messages_in` record, in Codex a turn in `queued_turns`. A group writes `route` to the delivery log.
- Send errors: `SendError` (the program or a tool failed; the chat answers 502), `SendRefused` (not possible now: 409), `SendInvalid` (never possible this way: 400), `SendTimeout` (504). They are `Problem`s: a catalog key and params (`SendRefused("app.provider.claude.no_process")`), shown to the user in the page's language and to an agent in English. Launch errors the same way: `LaunchError` (502), `RequestError` (400).
- `Thread`: the transcript as chat messages. Its docstring describes the message kinds: `you`, `agent`, `tools`, `question`, `plan`, `recap`, `compact`, `peer_in`, `peer_out`. The chat keeps one reader per file and pages through it itself; it serves only raster images, whatever the provider returns.
- Launch:
  - `LaunchRequest(cwd, prompt, name, worktree, choices)`. The fields are what any program understands. The rest goes into `choices`, by the keys of the provider's `options().choices` (in Claude `model`, `effort`, `permission_mode`; in Codex `model`, `effort`, `sandbox`). The provider rejects an unknown key or value (`RequestError`).
  - `LaunchOptions(choices, default_model, labels, efforts, defaults, note, untrusted_note)`:
    - `choices`: allowlists by key; `default_model`: the model the program takes when nothing is chosen;
    - `labels`: how to name a value on the page (in Codex, model and sandbox names);
    - `efforts`: each model's efforts, narrower than `choices["effort"]`: the "Effort" field follows the model;
    - `defaults`: which value an empty choice gives for a key other than the model (in Codex, sandbox `workspace-write`): the page shows it by its own name, not as "Default";
    - `note`: a line under the settings in the dialog; `untrusted_note`: a hint for a folder outside `trusted()`.
  - `Started` (with the `attach` command), `LaunchError`, `RequestError`, `MAX_PROMPT`, `MAX_NAME`, `MAX_TEXT`.
  - `project_dir(raw)`: the folder rule for any launch: it exists, is inside the home folder, is not the home folder itself, has no hidden parts. `checked_task(prompt)`: the first task, stripped of surrounding whitespace, no longer than `MAX_PROMPT`, with no NUL character.

## Capabilities (`caps`)

Providers share one interface but have different capabilities. If a capability is missing, the page hides the button or field instead of showing it empty.

| `Cap` | Meaning | Where it shows | Claude Code | Codex |
|---|---|---|---|---|
| `TASKS` | a task list on disk | progress on the card, a checklist in the panel and in the office | yes | no |
| `COST` | cost in dollars (and lines) in the transcript | the total on the card, "Cost" and "Lines" in the details; without it, tokens | yes | no, tokens only |
| `PEER` | live sessions write to each other | the supervisor's `SendMessage`; the group header: names for `SendMessage`, and without the capability “Reply in plain text … You cannot message the other members” | yes | no: the supervisor writes to it with `send_to_session` |
| `PERMISSION_WAIT` | a permission request reaches the dashboard in full (what is being asked), and you can answer it from the page. Any provider reports that a session is waiting even without it: `LiveSession.status = waiting` and `waiting_for` put the card in "Needs you" | answering a request from the page | yes | no: the wait is visible through the Codex server (`daemon.py`), the request itself is not |
| `BACKGROUND` | the program itself keeps a background session | "Open in terminal" on a background session | `--bg` | no, `exec` in the background |
| `NAME` | a name at launch | the "Name" field in "New session" | `-n` | no |
| `ATTACH` | a command to open the session in a terminal | "Open in terminal", `resume_command` in the details | `claude attach`, `claude --resume` | `codex resume` |
| `STOP` | the dashboard stops the session's process | "Finish" in the chat | yes | no |
| `FORK` | a new session with a copy of this one's history | "Fork" in the chat header and in the session panel | `claude --bg --resume=<id> --fork-session` | no (not verified) |
| `LAUNCH` | "New session" launches it | the program choice in the dialog, when more than one can be launched | `claude --bg` | `codex exec` in the background |
| `FILES` | a chat message carries files (where the route takes them) | the paperclip in the chat; without the capability it is disabled with a hint | yes | no, `codex queue` takes text only |

The page learns the capabilities from `/api/board`: the `providers` field is `{id: {title, caps}}` of all providers in the registry, and the first one is the default provider (`core.sessions.provider_caps`). `app.js` has three helpers for this:
- `can(card, cap)`: whether the session's program has the capability; without the `providers` field (demo) it has all of them;
- `programTag(card)`: a label with the program's name (`.chip`) on sessions of any provider except the first: on the card, in the session panel and in the chat header;
- `spend(card)`: dollars for a program with `COST`, otherwise tokens.

"Fork" is hidden by `can_fork`, which `launch` puts on the card (`Cap.FORK` of the session's provider, its transcript on disk and a folder that passes the `project_dir` rule). The provider itself decides how to deliver a message (`route`): a refusal with a reason disables the chat input, and the reason of an allowed route becomes the hint under it.

"New session" takes the programs from `GET /api/launch`: `programs` is the available providers with `Cap.LAUNCH` in registry order, each with `caps`, everything from `options()` and `trusted` (which of the recent folders the program considers trusted). The program choice shows only when there is more than one; the "Name" field only for a program with `Cap.NAME`. `POST /api/launch` takes `provider` (without it, the default provider) and the keys of its `choices`.

"Fork" in Claude (verified by a probe on Claude Code 2.1.317): `claude --bg --resume=<id> --fork-session --name=<name>-fork` in the session's folder gives a new id, and the history stays. Without a first message the copy waits (`idle`), and Claude Code writes the transcript only with the first message: until then the chat shows no history, and a copy stopped before it keeps no history. The folder follows the `project_dir` rule, so you cannot fork a session in `.claude/worktrees/…` (it has no button).

## How a session finds its provider

- **Index.** The provider is a record field (`record["provider"]`) next to the `session_id` key, not a composite key. Both programs give UUIDs (Claude v4, Codex v7), and all dashboard addresses (`/api/session/<id>`, `/api/chat/<id>`, "done" marks, groups, page state) name a session by one id.
  - If two providers return the same id, the first one in the registry gets into the index.
  - A record without a provider (written before providers existed) is Claude.
  - Records of a provider that is no longer in the registry stay in the file, but `records()` and `record()` do not return them: the board, the chat and the details never see that provider.
  - Each provider has its own parser version in `meta` (`parser_version.<id>`): when it changes, the index rereads only that provider's sessions.
  - A provider that failed to list its transcripts is skipped, and its sessions are not marked deleted. A transcript whose `Digest` cannot be saved is skipped until the file changes, like an unreadable one.
- **Live.** `providers.live()` collects `live()` from all available providers. A failed provider is skipped; the others stay on the board.
- **The card and the details** carry `provider`. A session without a transcript (just launched) takes it from `LiveSession`.
- **A feature** gets the provider like this: `providers.by_id(record["provider"])` or `providers.by_id(live.provider)`. The chat does it in `chat.delivery._session`, groups in `post.member`.
- **Signed messages.** Groups and the supervisor (`send_to_session`, via `chat.delivery.send_signed`) write to a session through its provider's `send` with `Message.sender`; the route is `route(session, signed=True)`, and the group writes its name to the log (`peer`, `resume`, `queue`). The group decides what to tell the recipient in the header by its provider's `Cap.PEER`, not by the program's name.

## Dashboard side: the chat's `Feature.setup`

Claude's routes need things only the dashboard knows. Core does not import features, so a feature wires this up: the manifest field `setup(app)`, which `App` calls once at build time, and only for registered features.

```python
# features/chat/__init__.py
def wire(app):
    claude_routes.plug(bridge, app.live, app.resume_args)

FEATURE = Feature(id="chat", ..., setup=wire)
```

- **`bridge`**: the VS Code bridge module (`features/chat/bridge.py`). It implements the `Terminals` protocol itself:
  - `shells()`: pids of the shells the bridge can type into; `None` if no bridge answers;
  - `send(shell_pid, text)`: paste the text and press Enter;
  - `focus(shell_pid)`: show the terminal and raise the VS Code window.

  Bridge errors are `BridgeError`, a subclass of `SendError`. Without the bridge there is no `vscode` route: a session in VS Code gets `peer` or `takeover`.
- **`app.live`**: all live sessions. A name for a message between sessions must be unique among live Claude sessions; sessions of other programs do not count.
- **`app.resume_args`**: the flags a feature asks for on a `resume` turn in a session it launched itself (`Feature.resume_args`; this is how the supervisor continues with its own `--tools`, `--settings`, `--mcp-config`). Without this wiring a `resume` turn refuses (`SendRefused`) instead of running the supervisor with the user's tools.

## How to add a provider

1. **Facts about the program.** Where the transcripts are, how to tell who is running, how to message a session and how to launch a new one. Check with probes in a separate folder and write down what is verified and what is inferred (example: [codex-cli-notes.md](notes/codex-cli-notes.md)).
2. **Folder** `core/providers/<id>/`: `__init__.py` with `ID = "<id>"`, `provider.py` with `class <Name>(Provider)`, the rest as modules by purpose, like `claude/`. Take the paths to the program's data from its environment variable, if it has one (like `CLAUDE_CONFIG_DIR`): that is how tests substitute them.
3. **Reading.** `available`, `transcripts`, `parse`, `live`. `parse` does not fail on an unknown event. If the program's format changes, understand both. Bump `parser_version` when the `Digest` changes.
4. **Registry.** One line in `providers/__init__.py`: an import and an instance in `PROVIDERS`. The first in the list is the default provider. `check()` rejects a repeated `id`, an `id` that does not match the pattern, and a provider without `title`, `caps` or `parser_version`.
5. **Then, by capability.** `caps`: honestly, only what exists.
   - **Sending:** `route`, `send`, `resume`, `stop`, `focus`. A live session must see a signed message (`Message.sender`) as a message from the sender; if the program cannot do that, send it the way it can (in Codex, `codex queue`). Return in `Sent.msg_id` the key under which `parse` records the reply (`messages_in` or `queued_turns`): that is how the reply gets into the group's feed.
   - **What the dashboard never does:** it does not send text to a session that is waiting for permission, and it does not stop a process until it is sure the process belongs to that session.
   - **Launch:** `options`, `trusted`, `start`. A launch checks the folder via `project_dir`, takes values only from the `options()` allowlists and never offers modes that turn off confirmations. The process environment is `core.env.clean_env()`.
   - If the provider needs something from the dashboard, give it its own wiring function and call it from the `Feature.setup` of the feature that owns it, the way the chat calls `claude_routes.plug`.
6. **Tests**: `tests/providers/test_<id>_*.py`. Put the program's data in a temporary folder through its environment variable: `tests/conftest.py` already does this for `CLAUDE_CONFIG_DIR` and `CODEX_HOME`; add yours there. Synthetic program files follow the shape from the notes and are not copies of someone else's data (example: `tests/codex_rollouts.py`). In tests, only ever fake the program's processes. How to test the index with a fake provider: `Folder` in `tests/core/test_index.py`.
7. **Docs.** A row in the tables above and the "Provider" section in the `add-feature` skill, if features need something new.

## What knows about Claude outside `core/providers/claude/`

These are the places where Claude Code is needed by design. The test `tests/app/test_layers.py` (`CLAUDE_BY_DESIGN`) lists these files and lets nothing else outside `core/providers` import `core.providers.claude`.

- **`features/chat/__init__.py`**: the chat's `setup` wires the bridge and the dashboard into Claude's routes (the section above).
- **`features/supervisor/launch.py`**: the supervisor is itself a Claude Code session: `sya supervisor` builds a `claude` argv with its own `--tools`, `--settings`, `--mcp-config`. It reads `~/.claude.json` (the user's Jira server) and `~/.claude/agents`. The `start_agent` tool launches `claude --bg` per its description: it takes the provider (`PROVIDER`) and the model list (`MODELS`) from here, so it starts Claude Code sessions only.
- **`features/builder/agent.py`**: the builder is also a Claude Code session (`claude --bg` with its own `--settings`). From here come `run`, `full_id`, `stop`, the courier for review remarks and the tail of its transcript (`last_answer`); `builder.py` does not know about Claude.
- **`features/tools/toolbox.py`**: "Tools" shows the Claude Code configuration: skills, agents, MCP servers and hooks from `~/.claude` and `~/.claude.json`.
- **`features/approvals/install.py`**: `sya hooks install` writes the `PermissionRequest` hook into `claude_home()/settings.json`. The hook itself (`features/approvals/hook.py`) imports nothing: Claude Code runs it, and it knows the format of the hook's input and output and of the transcript. Requests and decisions in `approvals.py` do not depend on the program; in Codex a permission wait is visible only through RPC, and it will be wired up separately.
- **The host**, without an import: `sya mcp install` registers the dashboard's MCP server in Claude Code (`claude mcp add`); Codex has its own command, which will come with it. `LEGACY_PROVIDER = "claude"` in `core/index.py` is for index records made before providers.
- **The page**: nothing about programs is hardcoded in `launch.js`: the hints (`note`, `untrusted_note`) and value labels come from the provider's `options()`, and field labels go by key (`model`, `effort`, `permission_mode`, `sandbox`; an unknown key is labeled with its own name). In `chat.js` the only Claude-specific parts are what shows with its capabilities: "Open in terminal" by a background session's `job_id`, and the "Finish" confirmation (`Cap.STOP`) with `claude --resume`. In looks (`looks.js`), sessions of a provider other than the first have their own default color: `PROGRAM_HUE`.

## Codex

Facts about Codex CLI 0.166.0 and how it differs from Claude are in [codex-cli-notes.md](notes/codex-cli-notes.md). The dashboard's code does not write to `~/.codex`: only `codex queue` and `codex exec` write there, and the dashboard runs them when you click a button.

- `transcripts`: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` and `archived_sessions/`, the id from the file name. A live rollout wins over an archived one, `.jsonl` over a compressed one. `.jsonl.zst` is not read: `parse` raises `ValueError`, and the index treats the file as unreadable until it changes. Threads that are not sessions in their own right get into neither the list nor the live set (`rollout.is_hidden`): any subagent (`source.subagent`), that is, one spawned by an agent (`thread_spawn`), which shows only under "Subagents" of its parent, as in Claude, and the approval reviewer (`other: guardian`); and also the title generator of the old VS Code extension (`source: exec` from `codex_vscode`). The first line of each file is read once per process.
- `parse` (`rollout.py`):
  - identity, folder, branch: from the first `session_meta`; a repeat of the same `session_meta` (a resume) is skipped;
  - prompts: `event_msg/item_completed` with `UserMessage` or `event_msg/user_message`, never `response_item role=user`. The parser strips the VS Code extension's wrapper ("# Context from my IDE setup: … ## My request for Codex:", in 0.154 builds "## My request:");
  - replies: `AgentMessage` or `event_msg/agent_message`; the last reply is `task_complete.last_agent_message`, otherwise the last one that is not `commentary`;
  - tokens: the last `token_count.info.total_token_usage.total_tokens` minus the inherited ones (a fork's count continues from the parent's);
  - title: the first prompt; the index substitutes the thread name when it returns records (`Codex.names`, `names.py`): `threads.name` from `state_5.sqlite`, otherwise `thread_name` from `session_index.jsonl`. A rename shows at once, without a change to the rollout. The database is opened read-only: without `state_5.sqlite-wal`, with `mode=ro&immutable=1` (otherwise sqlite would create `-wal` and `-shm` next to it); with it, with `mode=ro`; a locked or foreign database is skipped;
  - the parent's subagents: its `spawn_agent` calls, by `task_name` (Codex stores the task text encrypted);
  - queued turns (`task_started` with `turn_attribution.turn_trigger == "queue"`) go to `queued_turns`: the turn id and its last reply (`AgentMessage`, and `commentary` too while the turn runs; at the end, `task_complete.last_agent_message`).
- **Inherited history** (`Replay`): a fork's file (like a subagent's; verified on those) starts with its own `session_meta`, then the parent's `session_meta` and the parent's lines up to the fork (all stamped with the fork time), then its own first turn. A turn is its own if it started after the thread was created: a turn id is a UUIDv7, and its time is no earlier than the time of the thread id; for old CLIs (turn ids `rollout-N`) `task_started.started_at` in seconds is compared. A turn whose start cannot be determined counts as its own. Everything before the first own turn is skipped, both in the digest and in the chat.
- `live` (`live.py`): the `thread-writer-locks/<id>.lock` files; whether anyone holds a file and who: `lsof -F pn` (2 s); the holder's command: `ps`. `host`: `daemon` (pid from `app-server-daemon/daemon.pid` or `--managed-daemon`; TUIs are its clients), `vscode` (the own `app-server` of a VS Code window or of the desktop app), `exec`, `tui` (`--no-daemon`). `kind` is `background` for `exec`, otherwise `interactive`. If `lsof` fails, every thread whose lock file exists counts as running. `busy` while its last own turn in the rollout is not closed by `task_complete` or `turn_aborted`, otherwise `idle` (the parent's open turn at the start of a fork is not the fork's: the same `Replay` rule); the rollout is read on from where reading stopped. A thread without a rollout (no first prompt yet) is a "New session" card. "Waiting for permission" is not visible on disk; the daemon reports it (below).
- **Awaiting permission** (`daemon.py`; the protocol is "The daemon's RPC" in [codex-cli-notes.md](notes/codex-cli-notes.md)). A thread the daemon holds and that has a turn running per the rollout may be waiting for permission or for an answer to a question: this exists only in the daemon's `Thread.status`, `active{activeFlags: [waitingOnApproval | waitingOnUserInput]}`. Then `LiveSession.status = waiting`, and `waiting_for` is `permission prompt` or `input needed`, the same words as Claude's: the card goes to "Needs you" with "waiting for permission" (and the tool, if its open call is in the rollout). The `queue` route refuses such a thread (text never goes into a pending request). How it asks:
  - A WebSocket straight into the daemon's socket `app-server-control/app-server-control.sock` (a link into `/private/tmp/codex-daemon-<uid>/`), JSON-RPC in text frames; `clientInfo.name` is `see_your_agents`. Only `initialize`, `initialized`, `thread/loaded/list` and `thread/read` without turns, for candidate threads from the loaded list. No `turn/*`, no `thread/start`/`resume`, no answers to the daemon's requests: the client skips the daemon's notifications and requests.
  - Only while there is a candidate (a daemon thread that is `busy` per the rollout); at most once every 3 s, on its own worker thread, and `live()` takes the last answer no older than 10 s and never waits for the daemon. Each step takes at most 1 s, the whole conversation 3 s.
  - No `daemon.pid`, process or socket: it does not ask; it never starts the daemon. A connection error (refused, timeout, dropped) means 30 s of silence; a foreign protocol (handshake, frame, JSON, an error reply, an unknown status shape) means silence while this daemon runs (until `daemon.pid` changes). The board is then the same as without polling. A new flag or status type is not a wait, but not an error either.
  - Why not `Cap.PERMISSION_WAIT`: the board does not need the capability; `status` sets the column. The feature that answers requests from the page looks at exactly this capability, and Codex is left out of it: the dashboard does not see what exactly Codex asks and cannot answer.
  - Not visible: VS Code threads (each window has its own `app-server`), `codex exec` and `--no-daemon` (they hold the thread themselves).
- `attach_command`: `cd <folder> && codex resume <id>`; for a thread without a rollout (no prompt yet), empty: Codex has nothing to resume.
- `thread` (`thread.py`): prompts (`you`), replies (`agent`, and `commentary` too), tool calls in "N actions" groups with an error by exit code, a subagent's report to its parent (`peer_in` from `/root/<task>`), context compaction. Images are not served.
- `route` (`queue.py`): a single `queue` route, signed message or not: "Goes to Codex through codex queue: the answer shows up here". A refusal with a reason while Codex would not start the turn right away:
  - another process holds the thread (`host`: `vscode`, `tui`, that is `--no-daemon`, `exec`): the daemon will not take its lock, and the message would stay in Codex's queue (an `exec` run even takes it on exit and aborts it);
  - the thread is busy (`busy`): the message would start only after the current turn, later than any wait; waiting for permission or an answer (`waiting`, from the daemon): same, with the reason "answer in the terminal where it’s open";
  - the thread's last turn was aborted (the record's `last_turn_aborted`: the parser sees `turn_aborted` after the last `task_started`): Codex resumes such a thread but does not start a queued turn; the reason sends you to the terminal, `codex resume <id>`;
  - there is no transcript, or `codex` is not found.
  While a message is being sent, the route is busy (`pending`: "A message to this session is already on its way").
- `send` and `resume`: `codex queue --thread=<id> --message=<text> --resume` (`stdin` from `/dev/null`, `clean_env()`, 20 s timeout). Files: `SendInvalid`. The command exits 0 even on failure, so only the rollout confirms delivery: a new turn with `turn_attribution.turn_trigger == "queue"` whose prompt contains the text. `send` waits for it on the request's thread for at most `CONFIRM_S` (20 s; in practice the turn starts within a fraction of a second) instead of returning "sent" at once: a failure brings the text back to the input field with the error, not as a "sent" bubble that silently disappears a minute later. What can happen:
  - a turn with the text appeared: `Sent("queue", msg_id=<turn id>)`; the chat takes the reply from the same rollout, a group from `queued_turns` under this id;
  - "already has an active writer" in the output: another process holds the thread: `SendRefused` (409), the message stays in Codex's queue;
  - any other "could not resume thread": `SendError` (502) with Codex's text;
  - no turn started within 20 s: `SendTimeout` (504): the message is in Codex's queue; do not repeat it, it arrives when the session is resumed (`codex resume <id>`).
- `start` (`launch.py`): `codex exec --json -C <folder> -s <sandbox> [-m model] [-c model_reasoning_effort="…"] [--worktree] -- <task>` in its own process group (`start_new_session`), `stdin` from `/dev/null`, output to the log `cache_dir()/runs/codex-*.log`. The thread id comes from the first `thread.started` line, with a wait of at most 20 s; a process that exits earlier returns its output (outside git, "Not inside a trusted directory…" with a hint). `Started`: `job_id` and `session_id` are the thread id, there is no name (the card title is the task), `attach` is `codex resume <id>`, `worktree` is the folder Codex made itself (`~/.codex/worktrees/…`).
  - `options()`: models are the visible ones (`visibility: list`) from `models_cache.json`, with their `display_name` and `supported_reasoning_levels`; the default model is the `model` key from `config.toml`, and nothing else is taken from that file; `auth.json` is not read. Sandbox: `workspace-write` (the default, always passed) or `read-only`; `danger-full-access` is not offered, and the server rejects it; `--dangerously-bypass-approvals-and-sandbox` is never passed. A name at launch is a `RequestError`.
  - `trusted()`: folders inside a git repository: in other folders `codex exec` refuses (trust from `config.toml` is not read).
- `stop`, `focus`, `fork`: refuse by default.
- **Groups and the supervisor.** Codex has no `Cap.PEER`: a Codex member gets a group message via `codex queue` with the header "Reply in plain text: the group sees your reply. You cannot message the other members: …" (without `SendMessage`), and in Claude members' headers it is marked "SendMessage does not reach it". The supervisor writes to it with `send_to_session`. The group writes route refusals (another process holds the thread, busy, last turn aborted) to the feed as a failed delivery with the reason.
