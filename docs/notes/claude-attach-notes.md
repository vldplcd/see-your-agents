# Typing into a background session through `claude attach`, Claude Code 2.1.318: notes for the `attach` route

Verified 2026-11-04 with probes on haiku background sessions (`claude --bg`), started in this repository and removed
after. The client was `claude attach <job>` on a pseudo-terminal (TERM=xterm-256color, a set window size), the
text written to its master side. The route built on this is `core/providers/claude/attach.py`.

Markers: **P**: verified by a probe; **R**: seen on the route itself, once it was written.

## Why

- **Who the message is from (P).** Text typed through attach is recorded as `origin: {kind: "human"}`,
  `promptSource: "typed"`, the same as in a terminal. The courier's message is `origin.kind: "peer"` with the name
  "Dashboard", and the session's system reminder says a peer message is never the user's approval: auto mode refused
  external writes (creating a Jira ticket) asked for from the chat.
- **Mid-turn (P).** Text typed while the agent works is queued: a `queue-operation` enqueue at Enter, then a
  `queued_command` attachment with `origin.kind: "human"` and `humanTurn: true` when the turn takes it in (after the
  running tool call). No user entry follows. A courier's message mid-turn is the same attachment with the peer origin.
- **The session (P).** SIGTERM to the attach client detaches it; the session keeps running. Attach needs no
  controlling terminal: a child started with `start_new_session` on the slave side works (R).

## The input box

- **Shared (P).** Every attach client sees the same input; unsent text stays in it after the client goes, and the next
  text typed is joined to it.
- **Long or multi-line paste (P).** One bracketed paste of about 1000 characters or more, or of several lines,
  collapses into "[Pasted text #N +K lines]" and reaches the agent wrapped in `<pasted_content>`. Each line as pastes
  of at most 200 characters, lines joined by LF (Ctrl+J), arrives as plain text: 3034 characters, 12 lines,
  Cyrillic and emoji intact. ESC CR and `\` + CR between pastes do not make a new line.
- **Commands (P).** A text that starts with `/` runs as a slash command even when pasted ("Unknown command:
  /PROBE-11"). Raw keystrokes also open the `@` file picker and the `!` shell mode; pastes do not.
- **@-mention at the end (R).** A text whose last word is an `@/path` keeps the file suggestions open, and the first
  Enter picks a suggestion instead of sending. A space after the text closes them. A file outside the session's
  folder, mentioned this way, is read without a permission prompt.
- **Drawing (P).** Spaces and line wraps are drawn as cursor moves, so an echo check compares text without whitespace.

## A permission dialog

- **(P)** With a dialog open, pastes and LF are ignored (digits in the text do not pick an option), but CR answers the
  highlighted "Yes". So CR goes only after `claude agents --json` (about 80 ms) says the session is not on a prompt,
  asked again right before CR. A short text can match the dialog's own words in an echo check, so the echo check alone
  is not a guard.

## Timing (R)

About 0.8 s per message, from the start of the client to the transcript having the text, idle or mid-turn; the
courier takes 3–10 s.
