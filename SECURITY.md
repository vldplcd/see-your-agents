# Security policy

## Supported versions

Only the latest `main` is supported. Fixes land there; there are no maintained release branches.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Do not open a public issue, pull request or discussion for a vulnerability.

Include what you can:

- what an attacker can do, and from where (a web page, a local process, another user of the machine, text in a
  transcript, code a build runs);
- the steps to reproduce, with the commit you tested;
- your OS, Python, and the Claude Code and Codex CLI versions, if they matter.

The maintainer answers in the private advisory thread. Once a fix is on `main`, the advisory can be published, with
credit to you if you want it.

## Scope

In scope: anything that lets someone other than the person at the paired dashboard tab act on agents, read what they
should not, or get out of a sandbox. In particular:

- **the POST guard and browser pairing**: a mutating request accepted without the paired tab's secret, a way to
  obtain the pairing code or secret, CSRF, DNS rebinding, framing;
- **permission requests and the hook's HMAC**: forging, replaying or redirecting a request or a decision, an "allow"
  wider than what the page showed;
- **the builder's sandboxes**: a build's code writing outside its worktree and temp folder, reaching the network or
  the dashboard's port, reading the dashboard's config, or getting merged without a person pressing "Accept";
- **the courier**: getting it to send anything other than the exact text to the exact session;
- **path validation in launch**: starting a session outside the home folder, in a hidden folder, or with options
  outside the allowlists;
- **MCP tokens**: a `read` token reaching `control` tools through the dashboard, a token leaking from the dashboard,
  `/mcp` answering a request it should refuse.

Known limitations are documented and are not vulnerabilities by themselves: see
[docs/security.md, "Residual risks"](docs/security.md#residual-risks). A way to make one of them worse, or a cheap fix
for one, is very welcome.

Out of scope: attacks that need full control of your user account (such a process can edit
`~/.claude/settings.json` directly), and bugs in Claude Code, Codex CLI or other programs themselves. Report those to
their maintainers.

The threat model and the guarantees in detail: [docs/security.md](docs/security.md).
