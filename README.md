# see-your-agents

A local dashboard for Claude Code and Codex CLI sessions: who is working, who is waiting for you, which ticket a
session belongs to, what was done and where the agent is in its plan. It reads what the programs already keep on
disk, so it works with any session, including plain `claude` or `codex` in any terminal. From the page you can also
write to sessions, answer Claude Code's permission prompts, start new sessions and talk to a supervisor session that
watches all the others. The dashboard itself runs on your machine: a Python server on `127.0.0.1` and a page in
your browser.

## Quick start

```bash
uv run sya serve      # http://127.0.0.1:8765, opens a browser tab
uv run sya ls         # the same board in the terminal (--all adds the archive)
uv run sya index      # refresh the index once
```

Run them from a clone of this repository. The page is in English by default; Appearance → Language switches it.

## Requirements

- [`uv`](https://docs.astral.sh/uv/) and Python 3.11 or newer.
- The Claude Code CLI (`claude`) in `PATH`: live statuses, writing to sessions, launching them, the supervisor and
  the builder.
- The Codex CLI (`codex`), if you want to see its sessions too: they come from `~/.codex`, live statuses via `lsof`;
  writing and launching go through `codex queue` and `codex exec`.
- macOS for everything that changes something from the page. Browser pairing opens the tab through `osascript`, and
  every action needs a paired tab (see "Permissions from the dashboard"); the builder's sandboxes need
  `sandbox-exec`; keeping the Mac awake needs `caffeinate`. Elsewhere the dashboard refuses to pair, so the page is
  read-only; the builder's checks and preview refuse to run unconfined.
- Optional: `glab` for MR states ("GitLab MRs"), VS Code or tmux for typing into a live session's terminal
  ("Messaging agents from the dashboard"), `vplan` to render `.mdx` visual plans right in the session panel.

The dashboard writes nothing to `~/.claude` and `~/.codex` itself. The exceptions are the commands
`sya mcp install`, `sya supervisor install` (see "Supervisor and MCP"), `sya hooks install` (see "Permissions from
the dashboard"), and the `codex queue` and `codex exec` commands the dashboard runs when you write to a Codex session
or launch a new one.

Environment variables:

| Variable | Why |
|---|---|
| `SYA_JIRA_URL` | Jira address for ticket links. By default it is found automatically from `…/browse/KEY-N` links in transcripts |
| `SYA_JIRA_PROJECTS` | A list of project keys (`PROJ,OPS`), if junk like `UTF-8` gets into tickets |
| `CLAUDE_CONFIG_DIR` | If Claude Code keeps its data somewhere other than `~/.claude` |
| `CODEX_HOME` | If Codex CLI keeps its data somewhere other than `~/.codex` |
| `SYA_CONFIG_DIR`, `SYA_CACHE_DIR` | Where "done" marks (`~/.config/see-your-agents`) and uploads, logs, bridge sockets (`~/.cache/see-your-agents`) live |

The index lives in `~/.cache/see-your-agents/index.duckdb` (`--db` changes the path). You can delete it at any time.
Only the history of sessions whose transcripts Claude Code has already deleted itself is lost.

## What's on screen

- **Board** — columns by what is needed from you:
  - **Needs you** — the agent is stopped on a permission, a question or a dialog, or a background session needs
    something;
  - **Working**;
  - **Your move** — the agent has finished its turn;
  - **Recently closed** — the process exited on its own no more than 3 days ago; "Archive" removes it right away;
  - **Archive** — old sessions and those marked "Done"; collapsed into a strip on the right by default.

  At the bottom of the card, next to the cost: "context 63%", how full the session's context window is (from 80 % it
  is highlighted: compaction is coming soon). The session panel shows the same in tokens under "Details". How it is
  computed: "Where the data comes from".

  Codex CLI sessions (terminal, VS Code, `codex exec`) sit on the same board with a "Codex" label: "working" while a
  turn runs, "your move" when it is over. A session held by the Codex daemon (plain `codex` in a terminal) that waits
  for a permission or an answer moves to "Needs you" within a few seconds: only the
  Codex server reports this, and the dashboard asks it without changing anything. You can answer such a session only
  in its terminal; VS Code sessions are not visible this way. Instead of cost they show tokens, and they have no task
  list. The chat shows the conversation, and you can write into it (the `queue` route, below); Codex does not accept
  files from the dashboard, so the paperclip is disabled with a hint. Codex subagents show in the parent's panel
  ("Subagents"), as with Claude, not as separate cards. Codex service threads (the permission reviewer,
  the title generator of the old VS Code extension) do not get on the board. In the office, Codex sessions have their
  own color until you pick another one.
- **Office** — the same sessions on a floor plan: a room is a project, a desk is a session, a raised hand is waiting
  for you. The pencil on a desk (or "Change" in the "Look" section of the session panel, or "Customize looks")
  opens the editor: figure, color, desk, items on the desk and the agent's caption, one at a time or for the
  whole project. Looks are stored in `~/.config/see-your-agents/looks.json`.
- **Conversation** — a panel on the left next to any view; it hides and resizes (drag its edge): agents as a list,
  like in a messenger, and the session's feed: your prompts, agent replies, collapsed actions, questions, summaries,
  images. At the bottom, an input field with files (see below). A board card has a "Chat" link, and an
  address like `#chat/<id>` opens the panel with that session. When the chat is open, a narrow strip of avatars sits
  to its left: the supervisor, then chat sources, then sessions ("Needs you", "Working", "Your move", the rest); a
  click or Alt+↑ / Alt+↓ switches the chat. The badge on an avatar and in the list is the number of new agent replies
  since you last opened the chat (the browser remembers this in `sya.chat.seen`); a red dot means the session is
  waiting for you. Select text in an agent message (in a session or group feed) and "Reply to this" appears above
  it (or press R): the selection goes into the input field as a `> …` quote, with the cursor
  after it.
- **Groups** — shared chats: you, the supervisor (by default) and several sessions in one feed. A group is a row in
  the chat list, with “New group” above the list. A member has a handle `@ledger`; `@ledger` writes to that member,
  `@all` to everyone (the word of another UI language works too: `@todos`, `@alle`, `@tous`), and a message without
  a mention goes to the supervisor. In the header of
  each message, a member learns its own handle, the others' handles and the name for `SendMessage`; for a Codex
  member the header says to reply in plain text: it cannot write to other members. In the feed: your messages with
  delivery marks per recipient, members' replies and members' messages to each other. Text reaches live Claude
  sessions via the courier, closed ones via a single `--resume` turn, Codex sessions via `codex queue` (the reply is
  taken from the turn the queue started, each message has its own). It is not delivered to a session that waits for a
  permission, nor to a Codex session that is held by another process, is busy with a turn or whose last turn was
  interrupted: the feed shows a refusal with the reason. Data lives in `~/.config/see-your-agents/groups.json` and
  `groups/<id>.jsonl` (0600).
- **Tools** — skills, subagents, MCP servers, plugins, commands and hooks as a list: where they came from, how many
  times agents called them and in which sessions; never-called ones are collapsed at the bottom. Secrets from MCP
  configs are not shown.
- **To-do** — a header button with the count of urgent items opens a panel on the right (remembered, shifts the
  page): quick add (`!` at the start means urgent), "Urgent" and "Everything else", "Done" collapsed. A key like
  `PROJ-123` in the text becomes a Jira link, and you can attach a session to an item. The supervisor keeps the same
  list; "Morning review" asks it to put together the picture of the day (if it is not running, it is continued with a
  single turn). "Start" (▷ on an item) opens "New session" already filled in: the folder is where the session with
  the item's key last worked (above its worktree), the task is the text with the key at the start, the name is the
  key (`proj-123`) or the first
  words, worktree if there is a key. The launched session is attached to the item. Stored in
  `~/.config/see-your-agents/todo.json`.
- **New session** (the "+" button in the header) — launches a background Claude Code session (`claude --bg`) or a
  Codex one (`codex exec`) and opens its chat, see "New session" below.
- **Keys** — ⌘K / Ctrl+K: quick switch (sessions, groups, supervisor; filter by title, project, branch, Jira key and
  `!MR`; ↑ / ↓, Enter opens the chat, Esc closes), works in the input field too. `?` lists all shortcuts; single keys
  (`?`, `/`, R) do nothing while the cursor is in an input field.
- **Appearance** (the button with sliders) — style ("Bureau", "Constructor", "Control room"), theme, palette,
  default figures and desks, and the language of the interface: English (the default), Russian, Spanish, German,
  French or Simplified Chinese, kept per browser; switching reloads the page (adding a language:
  [docs/i18n.md](docs/i18n.md#how-to-add-a-language)). On a Mac it also has "Keep the Mac awake while agents work"
  (on by default): while at least one session is in "Working", the server keeps one `caffeinate -i -w <server pid>`
  running and stops it when
  nothing is working. `-w` stops it together with the server, even if the server is killed; a closed lid still puts
  the Mac to sleep. The choice is stored in `~/.config/see-your-agents/awake.json`; without `caffeinate` (not macOS) there is
  no switch.
- **Session panel** (click a card):
  - what is needed from you;
  - the command that opens the session in a terminal (`claude --resume`, `codex resume`);
  - "Where things stand" — the checklist and the latest recap;
  - plans (vplan is rendered);
  - subagents, agent looks, details;
  - a collapsed timeline of prompts, recaps, questions and MRs (the whole conversation is in the chat).
- **Dashboard builds** — a strip under the header while the builder works on the dashboard: status, preview, diff,
  "Accept", "Roll back", "Reject" (see "Flexible dashboard").
- **Search** by titles, tickets, prompts and recaps, including sessions whose transcripts are already deleted.
- **Notifications** in the browser when a session moves to "Needs you" (while the tab is open).
- **MR state** — on the `!138` chip and in the session panel, see "GitLab MRs" below.
- **Permissions** — in Claude Code sessions with the hook installed: "Allow" / "Deny", an answer to the agent's
  question and "Approve the plan" right in the conversation; the card shows what is
  waiting for an answer (see "Permissions from the dashboard").

## GitLab MRs

The MR chip on a card (`!138`) shows the live state of the MR in GitLab: draft, merged (merge icon), closed (cross);
for an open one: the pipeline (circle: passed, failed, running or pending), approvals (thumb: approved, or "0/1", how
many there are out of how many are needed) and the number of unresolved discussions. The hover tooltip spells this
out in words; the session panel shows it as a list with links and the time of the check.

- When all of a session's MRs are merged: a stopped session in "Recently closed" is marked "Done" automatically
  and goes to the archive (once: if you bring it back or continue it, the dashboard will not remove it a second
  time); a live one gets the "MR merged" badge.
- Data is read-only via `glab api --hostname <host> projects/<path>/merge_requests/<number>` (plus `/approvals` for
  an open MR and `/discussions` if it has comments). The host and the project come from the MR link in the
  transcript, so this works for sessions of any provider that have MR links.
- Polling runs in the background, the board does not wait for it: an open MR once a minute while a live or recent
  session shows it (once every 15 minutes if only old ones do), a merged or closed one once. No more than 12 `glab`
  calls per pass every 10 s, each with an 8 s timeout. MRs the board has not shown for 5 minutes are not polled.
  States are kept in `~/.cache/see-your-agents/mr.json`.
- No `glab` in `PATH`: chips stay plain links, and the session panel says why. `glab` is not authenticated on the
  host (`glab auth login --hostname <host>`): the same, and the check repeats in 15 minutes. Another error (MR not
  found, GitLab did not respond): "?" on the chip, "no data" and the reason in the panel.

## Messaging agents from the dashboard

From a session's chat you can send text and files, stop the process ("Finish"), mark the session "Done" and jump
to its terminal ("Show terminal"). The route is picked per session (`card.delivery`):

| Route | When | How it gets there |
|---|---|---|
| `vscode` | live `claude` in a VS Code terminal | the bridge extension pastes the text into the terminal (bracketed paste) and presses Enter |
| `tmux` | live `claude` in tmux | `tmux load-buffer` + `paste-buffer -p` + `send-keys Enter` |
| `resume` | no process, but there is a transcript and a folder | `claude -p --resume <id> -- <text>` as a single background turn; the reply is appended to the same transcript, the log goes to `~/.cache/see-your-agents/runs/` |
| `peer` | live named `claude` outside VS Code and tmux, or a named background session (`claude --bg`), not waiting for a permission | the courier (`core/providers/claude/courier.py`, a short haiku turn) sends the text via `SendMessage` as a message from the `Dashboard` session (transcripts from before agent-facing text was English name it «Дашборд», and the chat still treats it as yours); text only, no files; the agent writes its reply in its own session and it shows up in the chat. Messages to the supervisor from any terminal go the same way |
| `takeover` | live `claude` has finished its turn, and there is no way to type into its terminal | a second action next to `peer` (and the main one for a session without a name): after confirmation, SIGTERM to the process in the terminal (the pid is checked against the registry), then `resume` |
| `queue` | Codex session: stopped, or held by the Codex daemon with its turn finished | `codex queue --thread <id> --message <text> --resume`: the daemon continues the session with a turn carrying this text. The command exits with code 0 even on failure, so sending waits (up to 20 s) for this turn to appear in the rollout; if it does not, the input field shows an error and the text stays. Text only. It does not write to a session open in VS Code, in `codex --no-daemon` or still running `codex exec`, a session busy with a turn, or a session whose last turn was interrupted (Codex will not start a new one from the queue: continue it via `codex resume <id>`); the reason is visible |
| none | the session is waiting for a permission answer, a background session without a name, an unnamed agent working in a terminal | sending is disabled and the reason is shown; for a background session, with the `claude attach <id>` command |

The bridge is installed once and is picked up after VS Code windows reload:

```bash
uv run sya bridge install   # builds the .vsix and runs `code --install-extension`
uv run sya bridge status    # live bridges and their terminals
```

Security:
- A session waiting for an answer to a permission or a dialog does not receive text: it would land in the dialog and
  could confirm something. For such a session, only jumping to the terminal is available.
- Files are attached as `@/absolute/path`; only files from `~/.cache/see-your-agents/uploads` are accepted.
- "Finish" (confirmed with "Stop") checks the pid against the session registry (process start time and the name `claude`) and
  sends SIGTERM; a background session is stopped by `claude stop`. Other processes are not touched.
- The bridge socket lives in `~/.cache/see-your-agents/vscode/` (directory 0700, socket 0600); the extension does not
  go to the network.
- "Done" is stored in `~/.config/see-your-agents/done.json` and clears itself when new activity appears in the
  session.

## Permissions from the dashboard

When an agent asks for a permission (a command, a file edit, an MCP tool), asks a question (`AskUserQuestion`) or
shows a plan (`ExitPlanMode`), a block appears above the input field in the conversation with what it wants and
buttons:

- **Allow** (for a question, **Answer**; for a plan, **Approve the plan**: the session leaves plan mode for the
  normal mode, where every action asks again);
- **Deny** — like "No" in the terminal: the turn is interrupted, the agent waits for you;
- **Deny with a comment** — the agent gets your text and continues with it.

The terminal dialog stays open meanwhile: the first answer wins, the second does nothing. If you answered in the
terminal, the block in the dashboard disappears on its own. The main case is background sessions (`claude --bg`):
before, their permissions could only be answered via `claude attach`.

It works through the Claude Code `PermissionRequest` hook, without Remote Control or the cloud: the hook hands the
request to the dashboard on `127.0.0.1` and waits up to an hour for the answer. If the dashboard is not running, does
not respond or answers something unclear, the hook exits right away without a decision, and the normal dialog
remains. The hook does not touch `claude -p` or subagent permissions: there Claude waits for the hook before showing
the dialog. Details and verified facts: [docs/claude-hooks-notes.md](docs/claude-hooks-notes.md).

```bash
uv run sya hooks install --print   # show what would change in ~/.claude/settings.json
uv run sya hooks install           # add the hook (dashboard port: --port, default 8765)
uv run sya hooks status            # is the hook in place and will it run: Python, hook file, token
uv run sya hooks uninstall         # remove it
```

`install` adds one entry to `hooks.PermissionRequest` in `~/.claude/settings.json` (or
`$CLAUDE_CONFIG_DIR/settings.json`) and touches nothing else; running it again changes nothing. Before writing, the
old file is copied alongside (`settings.json.sya-backup-<time>`) and the change is printed as a diff; a
`settings.json` symlink (dotfiles) stays a symlink, and its target is written. The hook runs with the Python of the
dashboard's environment from this checkout: install it from the directory you run `sya serve` from. From an agent
worktree (`.claude/worktrees/`) `install` refuses: the hook would vanish together with the worktree
(`--from-worktree` if you really need it).

You can answer only from a **paired tab**, on `http://127.0.0.1:<port>` (not `localhost`; from there the page gives
you a link). On start, `sya serve` opens the dashboard itself with a one-time code in the address (`#pair=…`); after
that, use the "Pair this browser" button in the request block or in "Appearance" (where "Unpair all tabs" also
lives): the server opens a new tab, no more than once a minute. The tab asks "Pair this browser with the
dashboard?"; press "Pair" only if you have just asked for the pairing yourself. Pairing applies in the tab that
confirmed it, until the dashboard restarts: the secret is kept only in the server's memory and in the tab's
`sessionStorage`, nothing on disk. After a restart, "Pair this browser" again. Pairing is macOS-only for now.
Without pairing, the page shows the request but the buttons are disabled, and the server answers 403 "This browser
isn’t paired".

The same paired tab is required for **every action that changes something**, not just answering a prompt: launching
a session, forking, writing to a session or a group, the to-do list and the morning brief, the builder's buttons,
the "Keep the Mac awake while agents work" toggle, appearance. A local process (an agent, `curl`, a build's test over localhost)
can forge the page's `Origin` and `X-SYA` headers but not the pairing secret, so without it those POSTs get 403 and
the page offers "Pair this browser". Reading the board stays open.

What this gives. It protects against: curl and any processes that only send HTTP with forged headers; foreign sites
(CSRF, DNS rebinding, framing); pages on other ports.

What it does not give (residual risk). Someone who takes the port while the dashboard is off, and waits for the page
to be opened once during that time, can gain a foothold in the browser (a service worker) and steal the next pairing.
That requires a process under your user or under another user of this Mac; a process under your user can simply
write "allow all" into `~/.claude/settings.json` anyway. The real fix is to keep the port occupied
at all times (launchd, later). What is already done to narrow the window: the secret lives only in the process
memory and dies with it; the tab does not reload itself (on an update there is a "Reload" button,
and it waits until the new dashboard responds); page assets are loaded from addresses carrying the process version;
the page removes foreign service workers. It also does not protect against a process of the same user that controls
the browser UI or reads its memory, nor against you yourself if you confirm a pairing you did not start.

Security: requests can be created only by a process that can read `~/.config/see-your-agents/approvals-token` (0600,
created by `install`): the hook and the dashboard sign requests and answers with it, and the token itself is never
sent. If a foreign process has taken the dashboard's port (the dashboard is off or restarting), the hook will not hand
it the call and will not trust its "allow": the normal dialog remains. A decision applies only to its own call: the
dashboard cannot change the command or the file, and for a question the hook checks the answers against its own
questions. "Allow" is there only when the call is shown in full: a long call, or one with a field the page does
not render, can be allowed only in the terminal. A session without the sandbox (`dangerouslyDisableSandbox`) is
marked red. There is no MCP tool for permissions.

## New session

"+" in the header opens a dialog: the program (Claude Code or Codex; the choice is visible if both are available),
recent project folders (from the index, newest first, with session counts; hidden folders like `.claude/worktrees/…`
are not offered), a folder field with suggestions, the first task, program settings, a name and
"In a separate branch (worktree)". "Start" starts `claude --bg` or
`codex exec` in that folder; once the session appears on the board, its chat opens, and you can keep writing to it
from there (the `peer` route for Claude, `queue` for Codex).

- The folder must exist, be inside your home directory, not be the home directory itself and not be hidden. A folder
  where the selected program would refuse to launch is marked "not trusted" in the list.
- The session starts with a clean environment: without the dashboard's venv and without the variables of the Claude
  session `sya serve` was started from; the courier is launched the same way. The supervisor launches agents with the
  same code (`start_agent`).
- The browser remembers the last program and the choices for each (`sya.launch.last`).

Claude Code:
- Not trusted means a folder where `claude` has not been opened yet and the trust dialog has not been accepted.
- The default name is the folder name. If a live session already has that name, `-2`, `-3`… is appended:
  `SendMessage` finds a session by name (the supervisor's `start_agent` does the same). If two live sessions do end
  up with the same name, the chat will not send to either of them: the courier would not know which one to write to.
- Model, effort and permissions come only from lists (`core/providers/claude/launch.py`, `OPTIONS`); "Default"
  passes no flag, and next to the model it shows which one that is (`model` from `~/.claude/settings.json`).
  `bypassPermissions` and `dontAsk` are not offered; the server rejects them. "Default" for permissions passes no
  flag: the session runs in your `defaultMode`, whatever it is.
- A background session waiting for a permission answers only in the terminal: "Open in terminal" in the chat copies
  `claude attach <id>`. If it ended its turn with a question ("Needed from you: …" in the chat header), the answer
  goes via the courier.
- Worktree: `--worktree=<session-name>-<4 hex>`; Claude Code creates it in the repository's `.claude/worktrees/`.

Codex:
- Settings: model (visible models from `~/.codex/models_cache.json`; "Default" is `model` from
  `~/.codex/config.toml`, nothing else is read from it), effort (the selected model's levels,
  `-c model_reasoning_effort=…`) and sandbox: "Write in the folder" (`workspace-write`, the default) or "Read only". `danger-full-access` is not offered; the server rejects it.
- `codex exec` runs the first task and exits; in the background Codex does not ask for permissions: what the sandbox
  does not allow, it will not do. After that, the Codex daemon continues the session on messages from the chat; to
  open it in a terminal, use `codex resume <id>` (in the session panel).
- The session has no name (Codex has no such flag): the card title is the task. Not trusted means a folder outside a
  git repository: Codex refuses there. Codex creates the worktree itself, in `~/.codex/worktrees/`.

**Fork**. A button in the chat header and in the session panel: a new background session starts from a copy of
this session's conversation (`claude --bg --resume=<id> --fork-session --name=<name>-fork` in the session's
folder); the session itself does not change. In the popup by the button you can write the first message; without it
the copy waits for you, and its history appears in the chat with the first message (until then Claude Code does not
write a transcript, so a copy stopped earlier keeps no history). The name is the session's live name or its
`/rename`, otherwise the folder name, plus `-fork` (`-fork-2`… if taken). Like "New session", it opens the copy's
chat. There is no button for a session without a transcript on disk, or for a session whose folder is not fit for
launching (hidden, like `.claude/worktrees/…`, deleted, outside home).

## Supervisor and MCP

The supervisor is a separate Claude Code session. It watches all the other sessions via the dashboard's MCP, answers
"who is doing what, who is waiting for what", and hands tasks to live sessions via Claude Code's built-in messages
(`SendMessage`), and to Codex sessions via the `send_to_session` tool (through `codex queue`).

```bash
uv run sya serve                # MCP runs inside the dashboard: http://127.0.0.1:8765/mcp
uv run sya mcp install          # once: the sya server in Claude Code at user level (~/.claude.json)
uv run sya supervisor install   # once: the fleet-supervisor subagent in ~/.claude/agents
uv run sya supervisor           # start or resume the supervisor in this terminal (--bg in the background, --new from scratch)
uv run sya mcp status           # check the connection and the tool list
```

- **Sending.** Claude Code shows you every `SendMessage` from the supervisor first: the session settings have an
  `ask` rule, and the permission mode is `default`, even if you usually work in auto. The recipient sees that the
  message came from another session. Such a message cannot confirm the recipient's own permission request or change
  settings.
- **Supervisor tools.** It has only file reading (`Read`, `Grep`, `Glob`), messages (`ListAgents`, `SendMessage`)
  and our MCP: no Bash, edits or web, and none of your other MCP servers (`--tools`, `--strict-mcp-config`). It reads
  other sessions' transcripts, and your general permissions do not apply to it.
- **Closed sessions and new agents.** For them there are the `resume_session` (one turn via `claude -p --resume`) and
  `start_agent` (`claude --bg`) tools. Both also ask for confirmation.
- **Codex and everything `SendMessage` cannot reach.** `send_to_session` writes to any session through its program:
  Codex via `codex queue`, a live Claude Code session via the courier on behalf of `Supervisor`, a closed one via
  a `resume` turn. (A supervisor session started before agent-facing text was English is still named «Супервизор»;
  the dashboard recognizes both names.) Above the text is a note that it comes from the supervisor and should be answered
  in plain text; the supervisor reads the reply via `session_thread`. A refusal from the program (the session is busy,
  open in VS Code, waiting for a permission) reaches it as an error with the reason. It also asks for confirmation;
  to live Claude Code sessions the supervisor still writes with `SendMessage`.
- **Supervisor not running.** The chat, a group (a message without @ or with its handle) and "Morning review"
  continue it with a single `claude -p --resume` turn, but with its own settings: the same `--tools`, rules, MCP
  (`--strict-mcp-config`), prompt and `default` mode. There is no one to ask for confirmation in such a turn, so
  `SendMessage`, `send_to_session`, `resume_session`, `start_agent` and `request_change` are rejected: the supervisor
  will ask you to open it in a terminal (`uv run sya supervisor`).
- **Digest.** The dashboard shows the supervisor's latest digest (`post_digest`) above the board. The supervisor is
  pinned first in the chat panel. Messages between sessions are visible in both sides' conversations and as a line on
  the card.
- **From any session.** Asking for the supervisor ("ask the supervisor", "who is doing what", in any language)
  invokes the `fleet-supervisor` subagent. It only reads and returns message drafts; it sends nothing itself.

| MCP tool | Token | What it does |
|---|---|---|
| `fleet_overview`, `session_detail`, `session_thread`, `search_sessions`, `list_delegations` | read | session overview, details, conversation, search, who delegated what to whom |
| `todo_list` | read | the to-do list: open items and those done within the last day |
| `list_groups`, `group_feed` | read | group chats and their feed |
| `send_to_session` | control | a message to any session through its program (Codex via `codex queue`), with confirmation |
| `resume_session`, `start_agent`, `post_digest` | control | a turn in a closed session, a new background agent, a digest on the board |
| `group_post` | control | a supervisor entry in a group feed (not delivered to members, no confirmation) |
| `todo_add`, `todo_update` | control | a to-do item into the list, the "done" mark, urgency, text; no confirmation |
| `request_change` | control | a build: change the dashboard itself (see "Flexible dashboard") |

Security:
- **Tokens.** Kept in `~/.config/see-your-agents/mcp-tokens.json` (0600). Every session has the `read` token; only
  the supervisor has the `control` token (`~/.config/see-your-agents/supervisor/mcp.json`, 0600). This split is not a
  boundary: a session allowed to read files and run `curl` can take the `control` token itself. The server asks for
  no confirmations of its own; confirmation exists only in the supervisor's session.
- **`/mcp` checks.** The endpoint listens only on 127.0.0.1 and checks the token, Host and Origin.
- **Messages from `Dashboard`.** Everything you write to the supervisor from the chat panel or with the
  "Morning review" button reaches it from the `Dashboard` session (in transcripts from before agent-facing text was
  English, «Дашборд»), and it carries it out as your request. Any local session that has Bash can take this name.
  Without confirmation, the supervisor then only reads (sessions, Jira), keeps the to-do list and writes to group
  feeds; sending to sessions, new agents and builds ask you.
- **Text from transcripts.** It is data, not instructions. MCP replies are marked that way, and the supervisor's
  prompt repeats this rule.

## Flexible dashboard

The dashboard improves itself. Tell the supervisor, for example: "I want a module here, a to-do list".

1. The supervisor calls `request_change` and asks you first. The dashboard creates a `build/<name>` branch from the
   current commit of its checkout and a worktree `.claude/worktrees/build-<name>`. There a background agent
   "Builder: …" (`claude --bg`) builds the module following the `add-feature` skill and commits to its
   branch.
2. A build card appears under the header. When the builder finishes its turn, the dashboard runs ruff and the tests
   in the worktree itself. Green: "Ready to review"; red: "Didn’t work" with the check output. If the builder
   needs a permission, the card says "The builder is waiting for you" and links to its chat.
3. Buttons on the card:
   - "Preview" — the dashboard from the worktree on port 8766 (or the next free one) in a new tab, with a copy of
     your settings;
   - "Diff" — what changed in the branch;
   - "Rework" — your comment to the builder; it continues in the same branch. Checks run when a new commit appears
     in the branch. If the builder replied without a commit, the card shows "The builder asks: …" and links to its
     chat. A message from its chat gets through too, but does not rerun the checks; "Rework" does that;
   - "Accept" — `git merge --no-ff` of the checked commit into the checkout and a dashboard restart;
   - "Roll back" (for a day after "Accept") — `git revert -m 1` of that merge and a restart;
   - "Reject" — delete the worktree and the `build/<name>` branch.

What happens to your git checkout:
- Builds live in `build/*` branches and in `.claude/worktrees/`. They do not touch the checkout itself until you
  press "Accept".
- "Accept" merges the build into the branch that was current when the build started. It needs the same branch and a
  clean tree: `git status` without changes; only untracked files in `.claude/` do not get in the way. Otherwise the
  dashboard refuses and says why.
- Exactly the commit the checks passed on is merged. If commits appeared in the branch after them (the builder is
  reachable in its chat even after "Ready to review"), "Accept" refuses and reruns the checks.
- "Accept" refuses and names the paths if the build changes files the checkout ignores (`git check-ignore`), or
  anything in `.claude/` other than `.claude/skills/`. The merge would overwrite such files silently, and `.claude/`
  holds Claude Code settings and permissions.
- If the build changed `pyproject.toml` or `uv.lock`, after the merge the dashboard runs `uv sync --locked` in the
  checkout, and only then restarts: the new server runs in the same venv. If that fails, the merge is reverted
  (`git revert`) and the build becomes "Rolled back" with the `uv sync` output.
- "Roll back" works only on the branch the build was merged into (on another branch or a detached HEAD the dashboard
  refuses, and the button stays). It is an ordinary `git revert` commit; history is not rewritten.
- `sya serve` is now a watchdog: it keeps the server as a child process and restarts it after "Accept" and
  "Roll back". If the new version does not answer `/api/health` within 15 s, the watchdog reverts the merge itself
  (`git revert`), marks the build "Rolled back" and starts the previous version. If it did not start because another
  process holds the port, the watchdog does not revert; it reports this and stops. Ctrl+C, SIGTERM or SIGHUP during
  this check also revert nothing. When the server version changes, the open page asks you to reload ("The
  dashboard has been updated. Reload the page.") but does not reload itself.
- The preview stops together with the dashboard: on Ctrl+C, SIGTERM and SIGHUP.
- The builder is forbidden `git push`, `git merge`, `git checkout main`, `git reset --hard`, `git branch -D`
  (its settings file is `~/.config/see-your-agents/builder/build-<name>.json`, one per build). Edits in its own
  worktree, ruff, pytest and commits it does itself; everything else it asks about. It runs its own `sya serve` and
  tests with separate settings and cache folders, not yours.
- Every Bash command the builder runs, and the code its tests run, goes inside Claude Code's sandbox (Seatbelt):
  - it may write only to its worktree and its `/private/tmp/sya-build-<hash>` folder;
  - in the shared `.git`, the checkout's HEAD, all other branches, tags, stash and the other worktrees are closed;
    only its own `build/<name>` branch is open;
  - it cannot read `~/.config/see-your-agents` (where the MCP `control` token lives);
  - there is no network beyond localhost;
  - it cannot leave the sandbox, even if you allow a command.

  The sandbox is on for the builder only: your sessions and `~/.claude/settings.json` do not change. There is no
  network inside, so the dashboard prepares the build's venv before it starts (`uv sync --locked` from the checkout's
  commit) and the builder's uv is offline: a build cannot change dependencies. Details:
  [docs/claude-sandbox-notes.md](docs/claude-sandbox-notes.md).
- The dashboard's own checks (ruff and pytest after the builder's turn) and "Preview" run the build's code in a second
  sandbox too, with the same writable paths and no network beyond localhost, and they cannot reach the dashboard's own
  port. Without `sandbox-exec` they refuse to run rather than fall back to running unconfined.
- Only a human can accept, roll back or reject, with a button on the page, and only from the paired browser (see
  "Permissions from the dashboard"). There are no MCP tools for this.

Requires: the dashboard running from a git clone in editable mode (`uv run sya serve` in the clone), with the clone
folder trusted in Claude Code.

## Where the data comes from

| Source | What we take |
|---|---|
| `claude agents --json` | Live sessions (interactive and background): `status`, `waitingFor`, `pid`, `name` |
| `~/.claude/sessions/<pid>.json` | Since when the session has been in its current status |
| `~/.claude/jobs/<id>/state.json` | What a background session needs, the suggested answer |
| `~/.claude/projects/*/<id>.jsonl` | Title (`ai-title`, `/rename`), recaps, prompts, questions, MRs, plans, subagents, cost |
| `~/.claude/tasks/<id>/` | The session's task checklist |
| `~/.codex/sessions/**/rollout-*.jsonl`, `archived_sessions/` | Codex sessions: prompts, replies, actions, tokens, subagents |
| `~/.codex/state_5.sqlite` (read-only), `session_index.jsonl` | Codex session names |
| `~/.codex/thread-writer-locks/`, `lsof` | Which Codex sessions are running and by whom (daemon, VS Code, `exec`, TUI) |
| `~/.codex/models_cache.json`, `config.toml` (only `model`) | Models and their effort levels for "New session", the default model |

Transcript formats are internal and change between Claude Code and Codex versions. Parsers read fields defensively,
and when a parser changes (the provider's `PARSER_VERSION`), the index rereads that provider's transcripts.

Context on the card (verified on Claude Code 2.1.317 transcripts):
- filled — the `usage` of the last reply in the main thread (not subagents): `input_tokens` +
  `cache_creation_input_tokens` + `cache_read_input_tokens`, i.e. everything that went into the last request.
  Placeholder replies (`<synthetic>`, all zeros) are skipped;
- after compaction (`system` / `compact_boundary`) and until the next reply — `compactMetadata.postTokens`: what
  remained after compaction (without the system prompt and tools, so slightly less than the real figure);
- window — 1M if the model has the `[1m]` suffix (`message.model` or the `modelUsage` keys in `cost-state`), if it is
  Claude 5 or newer (they have only a 1M window), or if the session's context has already exceeded 200k; otherwise
  200k. For comparison: for a session on Haiku 5.5 with 59 263 tokens, the dashboard shows 6 %, as does Claude Code's
  own status line.

## Development

```bash
uv run pytest -q
uv run ruff check src tests && uv run ruff format --check src tests
uv run python demo/build.py   # a static demo on made-up data: demo/dist/see-your-agents.html
```

The code is organized in layers; details in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md):
- `core/` — data without HTTP: `index.py` (the DuckDB index of all providers), `sessions.py` (columns and cards),
  `done.py` ("done" marks), `providers/` — the agent programs: the `base.py` contract, the `__init__.py` registry and
  `claude/` (transcript → digest, live statuses, launch, delivery routes, courier), `codex/` (rollout → digest and
  chat, liveness from lock files, sending via `codex queue`, launching via `codex exec`); how to add a provider:
  [docs/providers.md](docs/providers.md);
- `app/` — the host: `server.py` (HTTP, page assembly), `mcp.py` (MCP protocol, tokens), `cli.py` (`sya`),
  `watchdog.py` (the `sya serve` watchdog), `pairing.py` (browser pairing), `feature.py` (the feature manifest);
- `features/` — one folder per feature: `board`, `office`, `chat` (conversation, delivery, the VS Code bridge),
  `looks`, `tools`, `supervisor` (launch, MCP tools, prompts), `groups` (group chats), `todo` (to-do), `builder`
  (dashboard builds), `launch` (new session, fork), `awake` (keep the Mac awake), `mr` (MR state in GitLab), `keys`
  (shortcuts, quick switch), `approvals` (permissions from the dashboard). List and order: `features/__init__.py`;
- `web/` — the page shell without a build step: `index.html`, `base.css`, `app.js`, `figures.js`, `i18n.js`;
  features have their own `web/*.js|css`.

UI text lives in i18n catalogs (`web/i18n/<lang>.json`, `features/<id>/i18n/<lang>.json`), English by default; how
to add a string or a language: [docs/i18n.md](docs/i18n.md).

A new feature is a folder in `features/` and a line in the registry; the recipe for an agent is the `add-feature`
skill (`.claude/skills/add-feature/SKILL.md`).

## License

MIT, see [LICENSE](LICENSE).
