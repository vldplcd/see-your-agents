"""The builder: a background Claude Code session (`claude --bg`) working in the build's worktree.

Everything the builder knows about Claude Code is here: it starts through the Claude provider's `launch.run`
(the short id, the clean environment), `full_id` finds its session id, `last_answer` reads its verdict from
the end of its transcript.

A background session that finished its turn keeps running, idle: text reaches it through the courier
(SendMessage). A stopped one continues with `claude --bg --resume=<id>`. Despite its help text, that gives a
new short id and a new session id: a copy of the conversation (seen with 2.1.316). The caller follows it.

Anything outside the allowed commands waits for a permission prompt, and the board shows the builder as
waiting. A compound Bash command (a `for` loop over `cat`) did exactly that in the first live run, so the
prompt asks for the Read, Grep and Glob tools instead.

Its tests run code it wrote itself, so every Bash command it runs is confined by Claude Code's own sandbox
(Seatbelt), switched on by its per-build settings file and for nothing else: writes only to its worktree and
its own temp folder, no host but localhost, no way to ask out of the sandbox. Facts and limits:
docs/claude-sandbox-notes.md.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

from ... import config_dir
from ...core.providers.base import LaunchError
from ...core.providers.claude import courier, launch
from ...core.providers.claude.paths import claude_home
from ...core.providers.claude.routes import SENDER
from ...i18n import Problem
from . import repo

NAME = "Builder: {title}"  # the session's name: the board shows it and the courier addresses it
ALLOW = (
    "Bash(uv run pytest:*)",
    "Bash(uv run ruff:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(uv run sya serve --no-open --port:*)",
    'Bash("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new:*)',
)
DENY = (
    "Bash(git push:*)",
    "Bash(git merge:*)",
    "Bash(git checkout main:*)",
    "Bash(git switch main:*)",
    "Bash(git reset --hard:*)",
    "Bash(git branch -D:*)",
)
PROMPT = """A task for the see-your-agents dashboard: “{title}”.

{description}

- Follow the add-feature skill: .claude/skills/add-feature/SKILL.md.
- Work only in this folder, on the branch {branch}. Leave main and every other branch alone: do not switch, \
merge or push.
- Read files with the Read, Grep and Glob tools, not with cat or loops in Bash. Anything outside the allowed \
commands waits for a person to approve it: in Bash only uv run pytest, uv run ruff, uv run sya serve and git \
add, commit, status, diff, log, show.
- Bash runs in a sandbox: you can write only in this folder and in $TMPDIR, and there is no network. uv \
works offline on the ready .venv: do not add dependencies. ps and Chrome do not start in the sandbox; the \
tests that need ps are skipped.
- Checks: uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest -q
- When the checks are green, commit everything to {branch}: nothing uncommitted reaches the dashboard.
- The last line of your answer: BUILD READY or BUILD FAILED: <reason>."""

# Seatbelt matches canonical paths, and on macOS /tmp is a link to /private/tmp: a rule spelled /tmp/... never
# matches. So every path a sandbox rule names is resolved, starting with the temp parent itself.
DEFAULT_TMP_PARENT = Path(os.path.realpath("/tmp"))
TMP_PARENT = DEFAULT_TMP_PARENT
# Claude Code gives sandboxed commands `<CLAUDE_CODE_TMPDIR>/claude-<uid>` as TMPDIR only while that path fits
# 44 bytes (AF_UNIX socket paths); a longer one falls back to the /tmp/claude-<uid> every session shares.
# /private/tmp/sya-build-<8 hex>/claude-<uid> is 39 bytes plus the uid's digits.
CLAUDE_TMP_MAX = 44
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")  # macOS Seatbelt; the checks and the preview run under it


class SandboxError(Problem):
    """The checks or the preview cannot be confined now; fail closed rather than run the build's code free.
    A Problem (builder.sandbox.*): the failed build keeps its key, the preview answers 502 with it."""

    def __init__(self, key: str, /, **params: object):
        super().__init__(key, 502, **params)


def tmp_root(worktree: Path) -> Path:
    """The build's own temp folder, the one place outside its worktree the builder may write: Claude Code's
    temp dir (CLAUDE_CODE_TMPDIR) and the config and cache of the builder's own `sya serve`."""
    digest = hashlib.sha256(str(worktree).encode()).hexdigest()[:8]
    return TMP_PARENT / f"sya-build-{digest}"


def _real(path: Path | str) -> str:
    """`path` as Seatbelt sees it: links resolved (/tmp -> /private/tmp), so a rule naming it can match."""
    return os.path.realpath(path)


def sandbox(worktree: Path) -> dict[str, str]:
    """Environment of the builder's commands and of the dashboard's checks: its own config and cache, never
    the user's, and uv offline on the venv synced before it started (no network, no writable uv cache)."""
    root = tmp_root(worktree)
    return {
        "SYA_CONFIG_DIR": str(root / "config"),
        "SYA_CACHE_DIR": str(root / "cache"),
        "UV_OFFLINE": "1",
        "UV_NO_CACHE": "1",
    }


def confined_env(worktree: Path, **extra: str) -> dict[str, str]:
    """`sandbox()` plus a TMPDIR inside the build's temp root: the checks and the preview run outside Claude
    Code, so nothing moves TMPDIR for them, and their scratch (pytest, uv, sockets) must land where they may
    write. `confine()` wraps the command; this gives it an env that matches the profile."""
    return {**sandbox(worktree), "TMPDIR": str(tmp_root(worktree)), **extra}


def _sbpl(text: str) -> str:
    """A Seatbelt string literal: double-quoted. Our paths hold no quotes or newlines, json quotes them."""
    return json.dumps(text)


def profile(worktree: Path, deny_port: int) -> str:
    """The Seatbelt profile for a process that runs the build's code (the checks, the preview). Everything is
    allowed except: writing outside the worktree and the build's temp root; reading config_dir() (the MCP
    control token, the supervisor's Jira credentials); any network but loopback; and connecting to the
    dashboard's own port (127.0.0.1, localhost and ::1 are all covered by the one `localhost:<port>` rule,
    verified on this macOS). Local bind and other loopback ports stay open: the tests and the preview need
    their own servers. Every path is resolved first: Seatbelt matches canonical paths only."""
    tree, root, secret = _sbpl(_real(worktree)), _sbpl(_real(tmp_root(worktree))), _sbpl(_real(config_dir()))
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            f"(allow file-write* (subpath {tree}))",
            f"(allow file-write* (subpath {root}))",
            "(allow file-write*",
            '  (literal "/dev/null") (literal "/dev/zero") (literal "/dev/random")',
            '  (literal "/dev/urandom") (literal "/dev/tty") (literal "/dev/dtracehelper")',
            '  (regex #"^/dev/fd/") (regex #"^/dev/ttys"))',
            f"(deny file-read* (subpath {secret}))",
            "(deny network*)",
            '(allow network-bind (local ip "*:*"))',
            '(allow network-inbound (local ip "*:*"))',
            f"(allow network-bind (local unix-socket (subpath {root})))",  # the tests' own sockets
            "(allow network-outbound (remote unix-socket))",
            '(allow network-outbound (remote ip "localhost:*"))',
            f'(deny network-outbound (remote ip "localhost:{deny_port}"))',
        ]
    )


def confine(argv: list[str], worktree: Path, deny_port: object) -> list[str]:
    """`argv` wrapped in `sandbox-exec` with the build's profile; raises SandboxError when it cannot, so the
    caller fails closed instead of running the build's code unconfined: no sandbox-exec, or no dashboard port
    to close (`localhost:0` would close nothing). The temp root must exist first (the profile allows writing
    there, and resolving it needs it on disk)."""
    if not SANDBOX_EXEC.exists():
        raise SandboxError("builder.sandbox.missing", path=str(SANDBOX_EXEC))
    if not isinstance(deny_port, int) or isinstance(deny_port, bool) or not 0 < deny_port < 65536:
        raise SandboxError("builder.sandbox.no_port", port=repr(deny_port))
    _own_folder(tmp_root(worktree))
    return [str(SANDBOX_EXEC), "-p", profile(worktree, deny_port), *argv]


def protected_git_paths(worktree: Path) -> list[str]:
    """What the builder may not write in the git directory its worktree shares with the checkout.

    The sandbox lets a worktree write the main .git (all but its config and hooks): its commits land there.
    So the checkout's HEAD, every other branch, tags, the stash and the other worktrees are denied, each file
    with its .lock. Objects stay writable, and a branch made after the build started is not on the list."""
    common = Path(repo.git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    own_admin = Path(repo.git(worktree, "rev-parse", "--path-format=absolute", "--git-dir")).name
    own_ref = f"refs/heads/{repo.branch(worktree)}"
    refs = repo.git(worktree, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines()
    files = [common / name for name in ("HEAD", "packed-refs", *refs) if name != own_ref]
    paths = [p for f in files for p in (f, f.with_name(f"{f.name}.lock"))]
    paths += [common / "refs" / "tags", common / "refs" / "stash"]
    if (common / "worktrees").is_dir():
        paths += sorted(w for w in (common / "worktrees").iterdir() if w.name != own_admin)
    return [_real(p) for p in paths]


def settings(worktree: Path) -> dict:
    root = _real(tmp_root(worktree))
    return {
        "permissions": {"allow": list(ALLOW), "deny": list(DENY)},
        "env": {**sandbox(worktree), "CLAUDE_CODE_TMPDIR": root},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,  # no sandbox, no builder: never a fallback to running unconfined
            "allowUnsandboxedCommands": False,  # dangerouslyDisableSandbox is ignored, not a prompt
            "autoAllowBashIfSandboxed": False,  # ALLOW and DENY still decide: the sandbox is one layer more
            "filesystem": {  # resolved paths: Claude Code canonicalizes too, and then nothing depends on it
                "allowWrite": [root],
                "denyWrite": protected_git_paths(worktree),
                "denyRead": [_real(config_dir())],  # the MCP control token, the supervisor's Jira credentials
            },
            "network": {
                "allowedDomains": [],
                "deniedDomains": ["*"],  # the user's own WebFetch(domain:…) rules merge into the allowlist
                "strictAllowlist": True,  # a host off the list is refused, not asked about
                "allowLocalBinding": True,  # the tests run servers on 127.0.0.1
                "allowUnixSockets": [root],
            },
        },
    }


def _own_folder(path: Path) -> None:
    """`path` as this user's private folder: /tmp is shared, a link or another user's folder is refused."""
    try:
        with contextlib.suppress(FileExistsError):
            path.mkdir(mode=0o700)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise LaunchError("builder.tmp.foreign", path=str(path))
        path.chmod(0o700)
    except OSError as exc:
        raise LaunchError("builder.tmp.failed", path=str(path), error=str(exc)) from exc


def settings_file(worktree: Path) -> Path:
    """The build's settings, written on every start and resume, so a change here reaches the next builder. It
    lives in config_dir(), which the sandbox lets the builder neither write nor read. A temp root too long for
    Claude Code to keep (it would hand the builder the /tmp/claude-<uid> every session shares) is refused."""
    _own_folder(tmp_root(worktree))
    claude_tmp = f"{_real(tmp_root(worktree))}/claude-{os.getuid()}"
    if len(claude_tmp.encode()) > CLAUDE_TMP_MAX:
        raise LaunchError("builder.tmp.too_long", path=claude_tmp, n=CLAUDE_TMP_MAX)
    try:
        content = settings(worktree)
    except repo.GitError as exc:
        raise LaunchError("builder.tmp.git", error=str(exc)) from exc
    path = config_dir() / "builder" / f"{worktree.name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def forget(worktree: Path) -> None:
    """After an accept or a reject: the build's settings file and temp folder go."""
    (config_dir() / "builder" / f"{worktree.name}.json").unlink(missing_ok=True)
    root = tmp_root(worktree)
    with contextlib.suppress(OSError):
        if root.is_dir() and not root.is_symlink() and root.lstat().st_uid == os.getuid():
            shutil.rmtree(root)


def name(title: str) -> str:
    return NAME.format(title=title)


def start(worktree: Path, title: str, description: str, branch: str) -> str:
    prompt = PROMPT.format(title=title, description=description, branch=branch)
    return launch.run(
        [
            "-n",
            name(title),
            "--permission-mode=acceptEdits",
            f"--settings={settings_file(worktree)}",
            "--",
            prompt,
        ],
        worktree,
    )


def resume(session_id: str, worktree: Path, title: str, text: str) -> str:
    """Continue a stopped builder in the background with the user's text, under the same session id."""
    return launch.run(
        [
            f"--resume={session_id}",
            "-n",
            name(title),
            "--permission-mode=acceptEdits",
            f"--settings={settings_file(worktree)}",
            "--",
            text,
        ],  # fmt: skip
        worktree,
    )


def stop(job_id: str) -> None:
    launch.stop_job(job_id)


def tell(to: str, text: str) -> courier.Delivery:
    """The user's comment to a live builder, as a peer message from the dashboard; blocks a few seconds."""
    return courier.deliver(to, text, "Rework from the user", SENDER)


def full_id(job_id: str, wait_s: float = 5.0) -> str:
    """The builder's full session id behind its short job id; "" if `claude agents` does not list it yet."""
    return launch.find_session(job_id, wait_s)


ANSWER_TAIL_BYTES = 256 * 1024


def last_answer(session_id: str) -> str:
    """The text of the session's last assistant message, read from the end of its transcript."""
    path = next((claude_home() / "projects").glob(f"*/{session_id}.jsonl"), None) if session_id else None
    if path is None:
        return ""
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - ANSWER_TAIL_BYTES))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content") or []
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        if any(t.strip() for t in texts):
            return "\n".join(texts)
    return ""
