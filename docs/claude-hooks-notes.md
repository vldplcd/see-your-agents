# The PermissionRequest hook in Claude Code 2.1.317: notes for permissions from the dashboard

Verified 2026-11-03 with probes on haiku. The hook was attached with the `--settings=<file>` flag, not in `~/.claude/settings.json`. The probes ran in `scratchpad/spike/work`. Interactive sessions were launched in a pty with a clean environment: without the parent session's `CLAUDE_CODE_*`, otherwise Claude does not write a transcript ("inherited CLAUDE_CODE_CHILD_SESSION marker") and is not visible in `claude agents`.

Markers: **P**: verified by a probe, **C**: read in the CLI code (`strings` of the binary), not verified by a probe.

## When it fires

- **Order (P).** On a call that needs a permission, Claude calls `PreToolUse` (it has a `tool_use_id`), ~40 ms later `PermissionRequest`, and at the same moment shows the dialog in the terminal. `Notification` with `notification_type: "permission_prompt"` comes later, about 6 s after that.
- **Race (P, C).** The dialog and the hook run in parallel: whoever answers first decides (`claim()` in the code). The hook's answer closes the dialog in the terminal. An answer in the terminal does **not** stop the hook: the process lives until its `timeout`, and its late output is ignored. Probe: "No, and tell Claude…" was chosen in the terminal, then the hook printed `allow`, and the command did not run.
- **Esc in the terminal (P).** Esc interrupts the turn, and the hook gets SIGTERM immediately.
- **Subagent (P, C).** If the call comes from a subagent (the input has `agent_id`), Claude waits for the hooks **before** showing the dialog (`awaitAutomatedChecksBeforeDialog`). While the hook runs there is no dialog, and `claude agents` shows `busy` without `waitingFor`. So the dashboard hook skips such calls right away.
- **`claude -p` (P).** The hook fires too, and the run waits for it. There is no dialog; without a decision from the hook the call is denied. In the hook's environment `CLAUDE_CODE_ENTRYPOINT=sdk-cli`; for an interactive and a background (`--bg`) session it is `cli`. The dashboard hook works only with `cli`: otherwise scripts with `-p` would wait for the dashboard.
- **AskUserQuestion and ExitPlanMode (P)** also go through `PermissionRequest`.

## Input (stdin), verbatim from a probe

Bash in an interactive session:

```json
{
  "session_id": "e7716a8a-0401-40a2-8386-1d38281f28c6",
  "transcript_path": "/Users/…/.claude/projects/-private-tmp-…-spike-work/e7716a8a-0401-40a2-8386-1d38281f28c6.jsonl",
  "cwd": "/private/tmp/…/scratchpad/spike/work",
  "scratchpad_dir": "/private/tmp/claude-501/…/scratchpad",
  "prompt_id": "d16f0dcb-bafe-438e-a846-966475cae55f",
  "permission_mode": "default",
  "effort": {"level": "medium"},
  "hook_event_name": "PermissionRequest",
  "tool_name": "Bash",
  "tool_input": {"command": "touch probe1.txt", "description": "Create empty file probe1.txt in the working directory"},
  "permission_suggestions": [
    {"type": "addDirectories", "directories": ["/private/tmp/…/spike/work"], "destination": "session"},
    {"type": "setMode", "mode": "acceptEdits", "destination": "session"}
  ]
}
```

- **No `tool_use_id` (P).** The event description in the CLI promises it, but the input schema (`hook_event_name, tool_name, tool_input, permission_suggestions, mcp_server`) and the probe do not have it. But the `tool_use` is already written to the transcript by the time the hook runs (P): the hook finds it by name and input, and finds its `tool_result` by `tool_use_id`. That is how it learns that the terminal answered.
- **Subagent:** `agent_id` and `agent_type` are added (P).
- **AskUserQuestion (P):** `"tool_input": {"questions": [{"question": "Which color do you prefer?", "header": "Color", "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cool"}], "multiSelect": false}]}`, without `permission_suggestions`.
- **ExitPlanMode (P):** `"permission_mode": "plan"`, `"tool_input": {"plan": "# Plan\n…", "planFilePath": "/Users/…/.claude/plans/plan-how-to-create-flickering-emerson.md"}`. The model called the tool with `{"plan": …}`, and the CLI added `planFilePath` itself.

## Output

The schema in the CLI (C), all of it verified by a probe (P):

```json
{"hookSpecificOutput": {"hookEventName": "PermissionRequest",
  "decision": {"behavior": "allow", "updatedInput": {}, "updatedPermissions": []}}}
{"hookSpecificOutput": {"hookEventName": "PermissionRequest",
  "decision": {"behavior": "deny", "message": "…", "interrupt": false}}}
```

- **`allow` (P).** `{"behavior": "allow"}`: the dialog closed, the command ran, the terminal shows "Allowed by PermissionRequest hook".
- **`deny` with `message` (P).** The model gets the text ("Reason: Denied from the dashboard: …") and continues the turn. With `"interrupt": true` the turn is interrupted, like Esc in the terminal (C).
- **Empty output and exit code 0 (P, C).** No decision, the dialog stays. Other exit codes: stderr is shown to the user, and the dialog stays as well (C).
- **AskUserQuestion (P).** A plain `allow` is ignored for it (C: `requiresUserInteraction`). What works is `allow` with `updatedInput`: the original input plus `answers` ("question → answer", multiSelect answers comma-separated):
  ```json
  {"behavior": "allow", "updatedInput": {"questions": […as in the input…], "answers": {"Which color do you prefer?": "Blue"}}}
  ```
  In the terminal: "User answered Claude's questions: · Which color do you prefer? → Blue". The model got the answer.
- **ExitPlanMode (P).** A plain `allow` is ignored too: the "Exit plan mode?" dialog stayed. This works:
  ```json
  {"behavior": "allow", "updatedInput": {"plan": "…", "planFilePath": "…"},
   "updatedPermissions": [{"type": "setMode", "mode": "default", "destination": "session"}]}
  ```
  Result: "User approved Claude's plan", the session is in `default` mode ("manual mode on"), the next Write asked for permission. Without `setMode` this was not verified.

## Time

- **The `timeout` field (C).** Seconds for one hook: `"timeout": 3600`. Without the field: 600 s (`Mu=600000`). The schema has no upper bound for a command hook (`E().positive()`).
- **On timeout (P)** the hook gets SIGTERM exactly at `timeout` (40.0 s in the probe). The dialog stays: in a background session `claude agents` keeps showing the wait.

## State in `claude agents --json` while the hook waits (P)

| Session | `claude agents` answer |
|---|---|
| interactive, Bash/Write/plan | `"status": "waiting", "waitingFor": "permission prompt"` |
| interactive, AskUserQuestion | `"status": "waiting", "waitingFor": "input needed"` |
| background `--bg` | `"kind": "background", "status": "waiting", "state": "blocked", "waitingFor": "permission prompt"`; in `jobs/<id>/state.json`: `"needs": "approve Bash: touch p6a.txt"` |
| after an answer in the terminal | `"status": "idle"` or `"busy"`, `"waitingFor": null`; the hook is still alive |
| `claude -p` | listed as `"kind": "interactive"`, `"status": "busy"`, name `work-64` (automatic) |

## Probes

| Probe | Session | What it checked |
|---|---|---|
| P1 | `3f0eb9e0-d436-4b78-a43d-654401329ef8` | the first one; the hook lives on after an answer in the terminal, SIGTERM on timeout |
| P2 | `e7716a8a-0401-40a2-8386-1d38281f28c6` | allow and deny from the hook, the state in `claude agents` |
| P3 | `7f1c88a6-152f-44e5-b055-2332bc777918` | AskUserQuestion via `updatedInput.answers` |
| P4 | `431e6c0a-39f8-4114-9f40-efda716ab7a0` | ExitPlanMode |
| P5 | `31da93a9-4569-4f60-9c15-6f59c072fa59` | `claude -p`, the hook's environment |
| P6 | `e292166d-0b72-41c7-a1ff-99223e97c7ad` (`--bg`, stopped with `claude stop`) | background session: allow, timeout |
| P7 | `ded9e7ce-1a27-46c9-9025-b1ed6ee444fe` | subagent: the dialog waits for the hook |
| P8 | `49e5efe0-4a33-453d-bec6-51195b11065f` | Esc kills the hook; a late allow after "No" is ignored |

## Live check with the dashboard

The hook from `sya hooks install` (written to a temporary `CLAUDE_CONFIG_DIR`, its `settings.json` passed to the sessions via `--settings`), the dashboard on port 8780 with temporary `SYA_CONFIG_DIR` and `SYA_CACHE_DIR`.

| Session | What was checked |
|---|---|
| `afee3c63-fb5c-4810-ad5a-c6cfac2a74bf` (`--bg`, stopped with `claude stop`) | "Allow" on the page: Bash ran; the AskUserQuestion question was answered with the form, the agent got `"Which color do you prefer?"="Blue"` |
| `106f4786-2dd2-4874-afc7-af5f633929cf` (`--bg`, stopped with `claude stop`) | "Approve the plan": the session left plan mode, Write asked for permission; "Deny with a comment": the agent carried out the comment; "Deny": "[Request interrupted by user for tool use]", the session is `idle` |
| `b0e2e654-36dc-4ac0-800f-3e29daede5ff` (in a terminal) | an answer in the terminal: the request left the board in ~2 s, the next one appeared without the old one; an answer from the page closed the dialog in the terminal; dashboard turned off: the hook exited immediately, the usual dialog |
