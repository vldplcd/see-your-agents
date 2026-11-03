"""Background Claude Code sessions (`claude --bg`): everything Claude-specific about starting one.

Callers go through the provider: `options()` (what may be chosen), `start(LaunchRequest) -> Started` and
`fork(session, …) -> Started` (the Fork button). They never build `claude` argv themselves. The builder,
whose argv carries its own settings and `--resume`, uses the lower `run()`; `find_session()` serves it too.

`claude --bg` prints a short id, "backgrounded · 5502d3c7 · <name>", and ignores --session-id (checked with
Claude Code 2.1.316). `claude agents --json` lists that short id as `id` next to the full `sessionId`.
Every flag that takes a value goes as `--flag=value`: `--worktree` takes an optional one and would swallow
the next argument.

The options are whitelists, so nothing free-form reaches argv. bypassPermissions and dontAsk are not among
them: a page click cannot ask to skip permission prompts. "Default" passes no flag, so the
session gets the user's own defaultMode, whatever it is.
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from ....i18n import message, request_lang, t
from ...env import clean_env
from ..base import (
    MAX_NAME,
    LaunchError,
    LaunchOptions,
    LaunchRequest,
    RequestError,
    Session,
    Started,
    checked_task,
    project_dir,
)
from . import live  # the module: tests replace live._agents_json
from .paths import claude_home, claude_json

START_TIMEOUT_S = 30
FIND_SESSION_S = 3.0
SHORT_ID = re.compile(r"backgrounded\s*·\s*([0-9a-f]{6,})")
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
FORK_SUFFIX = "-fork"
OPTIONS: dict[str, tuple[str, ...]] = {
    "model": ("opus", "sonnet", "haiku", "fable"),
    "effort": ("low", "medium", "high", "xhigh", "max"),
    "permission_mode": ("auto", "acceptEdits", "plan", "manual"),
}
TRUST_KEY = "hasTrustDialogAccepted"
# Catalog keys (web/i18n/*.json). UNTRUSTED_FAILED: Claude Code's refusal of a folder, and how to trust it.
UNTRUSTED_FAILED = "app.provider.claude.untrusted_failed"
NOTE = "app.provider.claude.launch_note"
UNTRUSTED = "app.provider.claude.untrusted"


def options() -> LaunchOptions:
    """The whitelists a start may choose from and the model Claude Code uses without one; the notes in the
    language of the request that asks."""
    lang = request_lang()
    return LaunchOptions(
        dict(OPTIONS), _default_model(), note=t(lang, NOTE), untrusted_note=t(lang, UNTRUSTED)
    )


def start(request: LaunchRequest, taken: Iterable[str] = ()) -> Started:
    """Start a named background session on the request's first task. `taken`: addresses of live sessions, the
    name gets -2, -3… past them. RequestError when the request is wrong, LaunchError when Claude Code
    refused (an untrusted folder comes with UNTRUSTED_FAILED)."""
    cwd = project_dir(request.cwd)
    prompt = checked_task(request.prompt)
    if len(request.name) > MAX_NAME:
        raise RequestError("app.provider.claude.name_too_long", max=MAX_NAME)
    if not prompt:
        raise RequestError("app.provider.task_needed")
    name = _unique_name(_clean_name(request.name) or _clean_name(cwd.name) or "session", taken)
    if unknown := sorted(set(request.choices) - OPTIONS.keys()):
        raise RequestError("app.provider.unknown_choices", program="Claude Code", names=", ".join(unknown))
    args = [f"--name={name}"]
    for key, allowed in OPTIONS.items():  # in this order: the argv stays the same for the same request
        if not (value := request.choices.get(key, "")):
            continue
        if value not in allowed:
            raise RequestError("app.provider.choice_allowed", key=key, allowed=", ".join(allowed))
        args.append(f"--{key.replace('_', '-')}={value}")
    worktree = _worktree_name(name) if request.worktree else ""
    if worktree:
        args.append(f"--worktree={worktree}")
    job = run([*args, "--", prompt], cwd)
    return Started(job, name, worktree, find_session(job, wait_s=FIND_SESSION_S), f"claude attach {job}")


def fork(session: Session, name: str = "", prompt: str = "", taken: Iterable[str] = ()) -> Started:
    """`claude --bg --resume=<id> --fork-session` in the session's folder: a new session id with a copy of its
    history, named `<name>-fork` (-2, -3… past `taken`). Without a prompt the fork waits idle; Claude Code
    writes its transcript only with its first message, so until then the chat has no history to show."""
    if session.transcript_missing:
        raise RequestError("app.provider.claude.fork_no_transcript")
    if not SESSION_ID.fullmatch(session.session_id):
        raise RequestError("app.provider.claude.fork_short_id")
    try:
        cwd = project_dir(session.cwd)
    except RequestError as exc:
        raise RequestError("app.provider.claude.fork_folder", reason=message(exc, request_lang())) from exc
    prompt = checked_task(prompt)
    base = _clean_name(name) or _clean_name(cwd.name) or "session"
    fork_name = _unique_name(base[: MAX_NAME - len(FORK_SUFFIX)] + FORK_SUFFIX, taken)
    args = [f"--resume={session.session_id}", "--fork-session", f"--name={fork_name}"]
    job = run([*args, "--", prompt] if prompt else args, cwd)
    return Started(job, fork_name, "", find_session(job, wait_s=FIND_SESSION_S), f"claude attach {job}")


def trusted(folders: Iterable[Path]) -> set[Path]:
    """The folders Claude Code trusts: its dialog was accepted there or in a parent. ~/.claude.json holds
    tokens too; only `projects[<path>].<TRUST_KEY>` is read."""
    try:
        projects = json.loads(claude_json().read_text(encoding="utf-8")).get("projects")
    except (OSError, ValueError, AttributeError):
        projects = None
    if not isinstance(projects, dict):
        return set()
    accepted = {p for p, v in projects.items() if isinstance(v, dict) and v.get(TRUST_KEY) is True}
    return {f for f in folders if any(str(p) in accepted for p in (f, *f.parents))}


def _default_model() -> str:
    """`model` from Claude Code's user settings, "" if unset; nothing else is read from that file."""
    try:
        model = json.loads((claude_home() / "settings.json").read_text(encoding="utf-8")).get("model")
    except (OSError, ValueError, AttributeError):
        return ""
    return model if isinstance(model, str) else ""


def _clean_name(raw: str) -> str:
    """One line of printable text: a whitespace run becomes one space, control characters go."""
    return "".join(c for c in " ".join(raw.split()) if c.isprintable())[:MAX_NAME]


def _unique_name(base: str, taken: Iterable[str]) -> str:
    """`base`, or `base-2`, `base-3`… past the names live sessions answer to: SendMessage needs one match."""
    used = {t.casefold() for t in taken if t}
    candidate, n = base, 1
    while candidate.casefold() in used:
        n += 1
        candidate = f"{base[: MAX_NAME - len(str(n)) - 1]}-{n}"
    return candidate


def _worktree_name(name: str) -> str:
    """A slug of the session name plus a random tail: two sessions with one name get two worktrees."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "session"
    return f"{slug}-{secrets.token_hex(2)}"


def claude() -> str:
    exe = shutil.which("claude")
    if not exe:
        raise LaunchError("app.provider.claude.no_claude")
    return exe


def run(args: Sequence[str], cwd: Path) -> str:
    """Run `claude --bg *args` in `cwd` with a clean environment; returns the short id it printed."""
    try:
        out = subprocess.run(
            [claude(), "--bg", *args], cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=START_TIMEOUT_S, env=clean_env(),
        )  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        raise LaunchError("app.provider.claude.bg_timeout", s=START_TIMEOUT_S) from exc
    except OSError as exc:
        raise LaunchError("app.provider.said", text=str(exc)) from exc
    text = (out.stdout + "\n" + out.stderr).strip()
    found = SHORT_ID.search(text)
    if out.returncode == 0 and found:
        return found[1]
    if out.returncode:  # Claude Code's own words, when it said any
        error = (
            LaunchError("app.provider.said", text=text)
            if text
            else LaunchError("app.provider.claude.bg_exit", code=out.returncode)
        )
    else:
        error = LaunchError("app.provider.claude.bg_no_id", output=text[:300])
    if "not trusted" in text.lower():  # «Workspace not trusted»: and how to trust the folder
        raise LaunchError(UNTRUSTED_FAILED, error=message(error, request_lang()))
    raise error


def stop_job(job_id: str) -> None:
    """`claude stop <job>` for the builder: the conversation stays, the process goes. A session already gone
    is fine, so is a missing `claude`."""
    with contextlib.suppress(OSError, subprocess.SubprocessError, LaunchError):
        subprocess.run(
            [claude(), "stop", job_id],
            capture_output=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
            env=clean_env(),
        )


def find_session(job_id: str, wait_s: float = 5.0) -> str:
    """The full session id behind the short one; "" if `claude agents` does not list it yet."""
    deadline = time.monotonic() + wait_s
    while True:
        for a in live._agents_json() or []:
            if a.get("id") == job_id and a.get("sessionId"):
                return str(a["sessionId"])
        if time.monotonic() >= deadline:
            return ""
        time.sleep(0.5)
