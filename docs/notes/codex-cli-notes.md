# Codex CLI 0.166.0: notes for the provider design

Researched 2026-11-03. V means verified (by a probe, `--help` or a file), I means inferred.

## Transcripts
- **Rollouts (V).**
  - Path: `~/.codex/sessions/YYYY/MM/DD/rollout-<LOCAL time>-<uuidv7>.jsonl`. The file appears at the first user message, not at session start.
  - `codex archive` moves rollouts into the flat folder `~/.codex/archived_sessions/`.
  - Rollouts may later become `.jsonl.zst` (I).
- **Other files (V).**
  - `session_index.jsonl`: `{id, thread_name, updated_at}`, only for named threads.
  - `history.jsonl`: TUI prompt recall; ignore it.
  - `state_5.sqlite` table `threads`: `id, rollout_path, created_at_ms, updated_at_ms, source (vscode|exec|cli|subagent json), originator, cwd, title, name, first_user_message, model, reasoning_effort, approval_mode, sandbox_policy, tokens_used, git_branch, archived, agent_nickname`. It is a cache rebuilt from the JSONL. It runs in WAL mode. A `mode=ro` connection to it still creates `-wal` and `-shm` when they are missing (V, by a test with a closed WAL database); `mode=ro&immutable=1` touches nothing and is safe while no `-wal` exists, that is, while no writer has it open. `name` can be empty while `session_index.jsonl` has the thread's name, and a subagent's `title`/`first_user_message` is the parent's first prompt, not its own (V). Rollouts never record the name (no `thread_name` event) (V).
- **Line shape (V).** Each line is `{timestamp, type, payload}`. Lines that matter:
  - `session_meta`: use the FIRST line only. It has `id`, `session_id` (the root for subagents), `timestamp`, `cwd`, `originator`, `source`, `cli_version`, `git.branch`, `forked_from_id`, `agent_nickname`.
  - `turn_context`: `model`, `effort`, `approval_policy`, `sandbox_policy.type`.
  - `event_msg/task_started`: the turn begins. It has `turn_id` and `turn_attribution.turn_trigger` (`user` or `queue`).
  - `event_msg/task_complete`: the turn ends. It has `last_agent_message`, `completed_at`, `duration_ms`.
  - `event_msg/turn_aborted`: `{reason: "interrupted"}`.
  - `event_msg/item_completed`:
    - `item.type` `UserMessage` gives the prompt in `content[].text`;
    - `item.type` `AgentMessage` gives the reply, with `phase: "final_answer"`;
    - some CLIs write `event_msg/user_message` (`message`) and `event_msg/agent_message` (`message`, `phase`) instead. Seen on this machine in the VS Code extension's 0.154.0-alpha builds; the 0.81–0.115 files here all have items (V). AgentMessage items before ~0.100 carry no `phase`: treat them as final (V).
  - Do NOT take prompts from `response_item/message role=user`: that also carries harness text (environment_context, AGENTS.md).
  - Tools: `response_item/function_call`, `custom_tool_call` (+`*_output`), `web_search_call`.
  - Tokens: `event_msg/token_count`. There is no USD cost.
- **Forks and subagents (V).** Their files replay the parent's history (the parent's `session_meta`, then turns stamped with the fork time) before their own turns, and a resumed file may repeat its `session_meta`. The parser must take the first meta as identity and skip the inherited prefix.
  - The boundary, from the five subagent files here (0.154.0-alpha.6.2): line 1 is the thread's own meta (`forked_from_id` = parent, `source.subagent.thread_spawn {parent_thread_id, depth, agent_path, agent_nickname}`, `thread_source: subagent`); line 2 is the parent's meta; then the parent's lines up to the spawn, all with the fork's timestamp to the millisecond, including the parent's turn that spawned it, left open (`task_started` with no `task_complete`). The thread's own first `task_started` follows a `thread_settings_applied` with its own `thread_id`, and its `turn_id` (UUIDv7) is later than the thread id; every replayed `turn_id` is earlier. That comparison is the rule the provider uses (V).
  - `token_count.total_token_usage` in a fork continues from the parent's total at the fork: the fork's own use is the last total minus the last replayed one. A resumed file keeps counting (no reset at the repeated meta) (V).
  - Old CLIs (≤0.99) numbered turns `rollout-N` and gave `task_started.started_at` in epoch seconds; 0.98–0.115 have UUID turn ids and no `started_at` (V).
  - The subagent's task arrives as `response_item/agent_message {author: "/root", recipient: "/root/<task>", content: [input_text "Message Type: NEW_TASK\nTask name: …", encrypted_content]}`, with `inter_agent_communication_metadata` before it. The parent's `function_call spawn_agent` has `task_name`, `fork_turns` and `message`; `message` is encrypted (Fernet, `gAAAAA…`) (V).
- **Helper threads (V).** Codex keeps rollouts of threads the user never typed into: the approval reviewer (`source: {subagent: {other: "guardian"}}`, `thread_source: guardian_review`, name "Guardian review") and, in 0.89, the VS Code extension's title generator (`source: exec`, `originator: codex_vscode`, prompt "You are a helpful assistant… title…"; its answer becomes the parent's `threads.name`). `session_meta.thread_source` (`user | subagent | guardian_review`) exists only in newer CLIs.
- **Prompts from VS Code (V)** start with `# Context from my IDE setup:` (active file, open tabs, selection) and put the request after `## My request for Codex:`; the extension's 0.154.0-alpha builds write `## My request:` instead (V: 34 prompts, all in 0.154 files).
- **Tool outputs (V).** `exec_command`: "Process exited with code N" (or "Exit code: N" from `shell_command`); `apply_patch`: JSON `{output, metadata: {exit_code}}` or "apply_patch verification failed"; `exec_command failed: …` when it could not start. The patch's files are its `*** Add|Update|Delete File:` lines.
- **Ids (V).** The UUIDv7 thread id is the same in the filename, `session_meta.id`, `threads.id`, exec `--json` `thread_id` and `codex queue --thread`.

## Live state
- **The daemon (V).**
  - Process: `codex app-server --listen unix:// --managed-daemon`. Its pid is in `~/.codex/app-server-daemon/daemon.pid`; its socket is `~/.codex/app-server-control/app-server-control.sock`.
  - A TUI is only a client of the daemon unless it was started with `--no-daemon`. VS Code runs its own app-server per window.
- **Liveness from disk (V).**
  - Running: `~/.codex/thread-writer-locks/<thread>.lock` exists and is held. `lsof` gives the holder: daemon, vscode, exec or a `--no-daemon` TUI.
  - `daemon.pid` is JSON (`{"pid", "processStartTime", "processIdentity", …}`), not a bare number. A daemon-hosted TUI shows only the daemon as the holder; the TUI process itself holds nothing.
  - A thread the TUI opened holds its lock before its rollout exists: the rollout appears with the first prompt.
  - Lock files of threads that ended seem to be removed (I: the folder holds only the live thread's); `.coordination.lock` in the same folder is not a thread's. `lsof -F pn -- <files>` exits 1 when any listed file is open by nobody, and prints resolved paths (`/private/tmp/…` for `/tmp/…`).
  - Busy or idle: while the lock is held, busy means a `task_started` with no later `task_complete` or `turn_aborted`.
- **Waiting for approval.**
  - It is NOT on disk.
  - Only the daemon RPC shows it: `Thread.status = notLoaded | idle | systemError | active{activeFlags: [waitingOnApproval | waitingOnUserInput]}`. See «The daemon's RPC» below.
  - VS Code-hosted threads are invisible to it.

## The daemon's RPC
Probed 2026-11-03 against the running 0.166.0 daemon, read-only (V unless marked).
- **Schema.** `codex app-server generate-json-schema --out DIR` (add `--experimental` for experimental methods) and `generate-ts --out DIR` write the protocol without touching the daemon. `ThreadStatus` and `ThreadActiveFlag` are the same with and without `--experimental`.
- **Transport: WebSocket over the unix socket.** `~/.codex/app-server-control/app-server-control.sock` links to `/private/tmp/codex-daemon-<uid>/<64 hex>` (94 characters: under the 104-byte `AF_UNIX` limit; `CODEX_HOME`'s own path may not be). `codex app-server proxy` only copies stdio bytes to that socket, so its client must speak WebSocket itself: a bare JSON-RPC line gets "Handshake not finished". No proxy is needed; a client connects to the socket directly.
  - Handshake, client to daemon (the path is not checked; Codex's own clients use `ws://localhost/` and `ws://localhost/rpc`):
    ```
    GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: <16 random bytes, base64>\r\nSec-WebSocket-Version: 13\r\n\r\n
    ```
    Reply: `HTTP/1.1 101 Switching Protocols`, `connection: Upgrade`, `upgrade: websocket`, `sec-websocket-accept: <RFC 6455: base64(sha1(key + 258EAFA5-E914-47DA-95CA-C5AB0DC85B11))>` (the RFC's sample key gives the RFC's sample answer), `x-codex-websocket-max-unfragmented-message-bytes: 16777216`.
  - Then one JSON-RPC message per text frame; client frames masked, the daemon's not. Messages carry no `"jsonrpc"` field back. Compression is opt-in by a header (`x-codex-app-server-compression`, I) that a client need not send.
- **Calls (read-only).**
  ```
  > {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"see_your_agents","title":"…","version":"0.1.0"},"capabilities":null}}
  < {"id":1,"result":{"userAgent":"codex-tui/0.166.0 (Mac OS 26.4.0; arm64) vscode/1.130.0 (see_your_agents; 0.1.0)","codexHome":"/Users/<user>/.codex","platformFamily":"unix","platformOs":"macos"}}
  > {"jsonrpc":"2.0","method":"initialized"}
  > {"jsonrpc":"2.0","id":2,"method":"thread/loaded/list","params":{}}
  < {"id":2,"result":{"data":["<thread id>", …],"nextCursor":null}}
  > {"jsonrpc":"2.0","id":3,"method":"thread/read","params":{"threadId":"<id>","includeTurns":false}}
  < {"id":3,"result":{"thread":{"id","sessionId","preview","model","status":{"type":"idle"},"path","cwd","cliVersion","originator","source","turns":[],…},"readState":{"firstUnread":…,"revision":…}}}
  ```
  The whole exchange takes about 1 ms. `thread/read` should neither move `readState` (that is `thread/readState/update`) nor subscribe the connection to the thread (`thread/start`, `thread/resume` and `thread/fork` do, `thread/unsubscribe` undoes it) (I, from the schema).
- **What else arrives.** Unasked notifications on every new connection: `remoteControl/status/changed` (`{status: "disabled", serverName, installationId, environmentId}`), and `account/updated` (`{authMode, planType}`) once the daemon has loaded auth. Server requests (`item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, `item/tool/requestUserInput`…) go to the client that runs the thread (I: by the protocol's design; not seen on a connection that only reads). A read-only client skips anything with a `method` and never answers.
- **Statuses seen.** A thread `codex queue --resume` woke: `{"type":"active","activeFlags":[]}` while its turn ran, `{"type":"idle"}` after; it stayed in `thread/loaded/list` about a minute, then the daemon unloaded it. `waitingOnApproval` / `waitingOnUserInput` are from the schema only, see below.
- **Probe thread `01a1a182-0360-73c2-ba67-5c7c688c2174`** (in `…/scratchpad/codex-approval-probe`). No CLI path put a daemon-hosted thread into `waitingOnApproval` without a person at a TUI:
  - `codex exec` always runs with approval `never` (`-c approval_policy="on-request"` is ignored: `turn_context.approval_policy: never`), and the thread keeps it: `thread_settings_applied` persists `approval_policy`, and a queued turn later ran with `never` too.
  - `codex queue -c …` refuses while the daemon runs ("cannot queue through an embedded app server while a local app-server daemon is running; remove configuration overrides or use --remote"); with `--remote unix://` it queues, but the override does not reach the turn.
  - A TUI (`codex -a on-request -s read-only "<prompt>"`, or `codex resume -a on-request <id>`) under a pty first asks «Trust this folder?» for a new folder, and the answer is saved to `~/.codex/config.toml`. The TUI reads trust from the daemon (`tui-project-trust-read`), so `-c 'projects."<path>".trust_level="trusted"'` does not skip it. The probe stopped there: the user's config stays untouched.
  - So the dashboard's tests answer with recorded frames, and the two wait flags in the schema's shape (`tests/codex_daemon.py`). To see it live: in a folder Codex already trusts, `codex -a on-request -s read-only` and ask for a command that needs escalation; the card moves to "Needs you" within a few seconds.
- `codex agents` is a TUI with no `--json`.

## Send
- **Recommended route: `codex queue --thread <id|name> --message <text> --resume` (V, probed on a stopped thread).**
  - The daemon runs the turn and appends it to the same rollout (`turn_trigger: "queue"`).
  - It has no model, image or file options.
  - It EXITS 0 even when the resume fails ("Warning: message was queued, but could not resume thread"), so verify the turn in the rollout.
  - A live daemon-hosted TUI gets the message at the end of its current turn (I).
  - "This thread is open elsewhere. Close it there and retry resume to continue." is the TUI's own text (V: in the binary next to its other messages), not what `codex queue` prints; see below for what it does print.
- **`codex exec resume <id> "<prompt>"` (I).** It runs one turn in-process and needs the single writer lock, so it fails while the daemon holds the thread. Run it with `stdin=DEVNULL`. Pick one route; queue is the one.
- **What `codex queue` does, probed 2026-11-03 on the dashboard's own threads (V unless marked).**
  - Success: exit 0, stdout `Queued message <item id> for thread <thread id>.`, stderr empty. Codex's log shows `thread/queue/add`, then `thread/resume` 1 ms later; the queued turn's `task_started` came 17 ms after that and its `UserMessage` (the text exactly as sent) 1.6 s after. The answer followed in the same rollout, and the daemon kept the thread (its lock) while idle.
  - The thread held by a running `codex exec`: exit 0, the same stdout, and stderr `Warning: message was queued, but could not resume thread <id>: thread/resume failed: thread <id> already has an active writer (code -32600)`. The item stays in `queue_1.sqlite` (`queued_items`); the exec run takes it at the end of its own turn (a `queue` turn with the text) and aborts it at once as it exits (`turn_aborted`): the message is gone without an answer. A VS Code or `--no-daemon` holder takes the same lock, so it gives the same warning (I).
  - A stopped thread whose last turn was aborted: exit 0, no warning; the daemon loads the thread (takes its lock, writes `thread_settings_applied`) but starts no turn, and the items stay queued. A second `codex queue` to the loaded thread did not start them either. The daemon unloaded the idle thread about 60 s after the resume (log: listener cleared, `Shutdown`). Why: an aborted last turn seems to hold the queue, as the TUI holds queued messages after an interrupt (I).
  - The app-server protocol has `thread/queue/{add,list,start,delete,reorder,update}` (only in `codex app-server generate-json-schema --experimental`); `thread/queue/start {threadId}` would start a queued item, `codex queue` never calls it. `codex app-server proxy` relays bytes to the daemon's WebSocket (see «The daemon's RPC»). Not used for sending.
  - So the dashboard confirms a delivery by the rollout only, and sends only where the turn can start now: not to a thread another process holds, not to a busy one (its message would start only when the current turn ends), not to one whose last turn was aborted (the parser records it as `last_turn_aborted`).
- **Probed 2026-11-03: groups and the supervisor on a dashboard thread (V).**
  - A thread the daemon already holds (loaded, idle after an earlier queue turn): `task_started` came ~30 ms after `codex queue`, its `UserMessage` 7 ms later, the text byte for byte (a multi-line group header included). `task_started` carries `turn_id` both in the payload and in `turn_attribution`, with `turn_trigger: "queue"`; the dashboard uses that id as the message's id (`Sent.msg_id`) and finds the turn's answer under it.
  - A message queued while a queue turn of the same thread ran started as its own turn 3 ms after that turn's `task_complete`, and was answered. The dashboard still refuses a busy thread; this one slipped through because its live state was a poll behind, and the send was confirmed inside its 20 s window.

## Launch
- **No `--bg` and no name flag (V).**
- **Simplest launch: detached `codex exec` (V).**
  - Command: `Popen(start_new_session=True, stdin=DEVNULL)` running `codex exec --json -C <cwd> -s workspace-write [-m model] [-c model_reasoning_effort="high"] [--worktree] -- <prompt>`.
  - The first stdout line is `{"type":"thread.started","thread_id":...}`.
  - Exec defaults: approval `never`, sandbox `read-only`.
  - Follow-ups via `queue --resume` move the thread onto the daemon (V).
- **Closest to `claude --bg`: RPC (I).** Over the proxy: `thread/start{cwd, model, approvalPolicy, sandbox}`, then `thread/name/set`, then `turn/start`. The protocol is marked experimental.
- **Models (V).** `~/.codex/models_cache.json` `models[]` gives `slug`, `display_name`, `visibility` (list|hide), `supported_reasoning_levels` and `default_reasoning_level`. The default is `model` in `~/.codex/config.toml`. Skip the `identity` key.
  - `supported_reasoning_levels` is a list of `{effort, description}`; some models add `ultra` ("Maximum reasoning with automatic task delegation") to low…max (V).
- **Attach:** `codex resume <id>`.
- **What `codex exec --json` does, probed 2026-11-03 (V).**
  - `{"type":"thread.started","thread_id":…}` is the first stdout line, 0.2 s after the start; `turn.started`, `item.*` and `turn.completed` follow, and the process exits after its one turn. Its lock file is gone then.
  - It prints `Reading additional input from stdin...` to stderr even with stdin from /dev/null.
  - The rollout's `session_meta` has `originator: codex_exec`, `source: exec`, `thread_source: user`; its turn has `turn_trigger: "exec"` and `approval_policy: never`. `-c model_reasoning_effort="low"` gives `turn_context.effort: low`; `-s workspace-write` gives `sandbox_policy.type: workspace-write`.
  - Outside a git repository: exit 1 at once, nothing on stdout, stderr `Not inside a trusted directory and --skip-git-repo-check was not specified.`; no thread.
  - `--worktree` makes `~/.codex/worktrees/<4 hex>/<repo folder name>` and works there: `session_meta.cwd` is that path, `git.branch` is empty.
- **Probe thread (2026-11-03, groups and the supervisor).** `01a1a18c-c2c3-79d1-b271-2ac8f8ad87e7`: `codex exec` in a git-inited scratch folder, then six group posts (five delivered and answered, one refused as busy) and one answered `send_to_session` from the dashboard, all through `codex queue`.
- **Probe threads (2026-11-03, launch and the chat).** `01a1a14f-e101-7b72-9f68-a498ecdfbc6e`: `exec --worktree` (worktree `~/.codex/worktrees/c475/repo`), then three queued messages: one taken and aborted by the exec run, two left in its queue. `01a1a166-bd60-7840-974f-ef8bd18a44d6`: started from "New session", then one message from the chat, answered. The run outside git made no thread.

## Gaps vs Claude
- no peer messages between root sessions (no `SendMessage`);
- no task list and no AI title on disk;
- no cost;
- no `--bg`;
- approvals are visible only via the daemon's RPC, and not for VS Code's threads;
- no pid-to-thread mapping for TUIs;
- `queue` exits 0 on failure;
- subagent files replay parent history;
- one writer lock per thread.
