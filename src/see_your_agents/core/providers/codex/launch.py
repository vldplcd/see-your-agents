"""Starting a Codex session from the New session dialog: a detached `codex exec --json` on the
first task.

Codex has no --bg and no name flag (docs/codex-cli-notes.md, «Launch»). The dashboard starts
`codex exec --json -C <cwd> -s <sandbox> [-m model] [-c model_reasoning_effort="…"] [--worktree] -- <prompt>`
in a session of its own, stdout to a log under cache_dir()/runs, and reads the thread id from its first
`thread.started` line. The run ends with that first turn; the thread stays, and the chat continues it through
the daemon (queue.py). The card's title is the task: Codex keeps no name the dashboard could set.

The options are whitelists from Codex's own files: the visible models of models_cache.json and each one's
reasoning levels, the default model from config.toml's `model` (no other key is read; auth.json never is).
Sandbox: workspace-write, the default here, or read-only. danger-full-access is neither offered nor accepted,
and --dangerously-bypass-approvals-and-sandbox is never passed.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import threading
import time
import tomllib
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from .... import cache_dir
from ....i18n import request_lang, t
from ...env import clean_env
from ..base import (
    LaunchError,
    LaunchOptions,
    LaunchRequest,
    RequestError,
    Started,
    checked_task,
    project_dir,
)
from .paths import codex_home
from .rollout import THREAD_ID, find_rollout, first_meta

# -s value: the catalog key of its name (web/i18n/*.json)
SANDBOXES = {
    "workspace-write": "app.provider.codex.sandbox_write",
    "read-only": "app.provider.codex.sandbox_read",
}
DEFAULT_SANDBOX = "workspace-write"
START_WAIT_S = 20.0
POLL_S = 0.1
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # a model as argv may take it: never a flag
LEVEL = re.compile(r"[a-z]{1,16}")  # goes into -c model_reasoning_effort="…": no quotes, no escapes
# Catalog keys. UNTRUSTED_FAILED: Codex's refusal of a folder, with where it starts.
NOTE = "app.provider.codex.launch_note"
UNTRUSTED = "app.provider.codex.untrusted"
UNTRUSTED_FAILED = "app.provider.codex.untrusted_failed"
NOISE = "Reading additional input from stdin..."  # printed by every exec, stdin or not

_runs: list[subprocess.Popen] = []  # started runs, reaped as they end
_runs_lock = threading.Lock()


class Model(NamedTuple):
    """A model Codex lists in its picker."""

    name: str  # display_name
    levels: tuple[str, ...]  # supported_reasoning_levels


def options() -> LaunchOptions:
    """The whitelists from Codex's own files; names and notes in the language of the request that asks."""
    models, lang = _models(), request_lang()
    return LaunchOptions(
        {
            "model": tuple(models),
            "effort": tuple(dict.fromkeys(level for m in models.values() for level in m.levels)),
            "sandbox": tuple(SANDBOXES),
        },
        _default_model(),
        labels={
            **{value: t(lang, key) for value, key in SANDBOXES.items()},
            **{slug: m.name for slug, m in models.items() if m.name != slug},
        },
        efforts={slug: m.levels for slug, m in models.items()},
        defaults={"sandbox": DEFAULT_SANDBOX},
        note=t(lang, NOTE),
        untrusted_note=t(lang, UNTRUSTED),
    )


def _models() -> dict[str, Model]:
    """The models Codex lists in its picker by slug, in its order."""
    try:
        data = json.loads((codex_home() / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found: dict[str, Model] = {}
    for model in data.get("models") or [] if isinstance(data, dict) else []:
        if not isinstance(model, dict) or model.get("visibility") != "list":
            continue
        slug, name = model.get("slug"), model.get("display_name")
        if not isinstance(slug, str) or not SLUG.fullmatch(slug):
            continue
        raw = model.get("supported_reasoning_levels")
        levels = [lv.get("effort") for lv in raw if isinstance(lv, dict)] if isinstance(raw, list) else []
        name = name if isinstance(name, str) and name.strip() else slug
        found[slug] = Model(name, tuple(lv for lv in levels if isinstance(lv, str) and LEVEL.fullmatch(lv)))
    return found


def _default_model() -> str:
    """`model` from config.toml, "" if unset or unreadable."""
    try:
        with (codex_home() / "config.toml").open("rb") as fh:
            model = tomllib.load(fh).get("model")
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    return model if isinstance(model, str) else ""


def trusted(folders: Iterable[Path]) -> set[Path]:
    """The folders inside a git work tree: `codex exec` refuses anywhere else unless config.toml trusts the
    folder, and that file is not read here for more than the model."""
    return {f for f in folders if any((p / ".git").exists() for p in (f, *f.parents))}


def _argv(request: LaunchRequest, options: LaunchOptions) -> tuple[Path, list[str]]:
    """The folder and `codex exec …` without the executable; RequestError for anything outside the
    whitelists."""
    cwd = project_dir(request.cwd)
    prompt = checked_task(request.prompt)
    if not prompt:
        raise RequestError("app.provider.task_needed")
    if request.name.strip():
        raise RequestError("app.provider.codex.no_name")
    picked = request.choices
    if unknown := sorted(set(picked) - options.choices.keys()):
        raise RequestError("app.provider.unknown_choices", program="Codex", names=", ".join(unknown))
    model, effort = picked.get("model", ""), picked.get("effort", "")
    sandbox = picked.get("sandbox", "") or DEFAULT_SANDBOX
    if model and model not in options.choices["model"]:
        raise RequestError("app.provider.codex.unknown_model", model=repr(model))
    levels = options.efforts.get(model or options.default_model) or options.choices["effort"]
    if effort and effort not in levels:
        raise RequestError("app.provider.codex.effort_allowed", allowed=", ".join(levels))
    if sandbox not in SANDBOXES:
        raise RequestError("app.provider.codex.sandbox_allowed")
    args = ["exec", "--json", "-C", str(cwd), "-s", sandbox]
    if model:
        args += ["-m", model]
    if effort:
        args += ["-c", f'model_reasoning_effort="{effort}"']
    if request.worktree:
        args.append("--worktree")
    return cwd, [*args, "--", prompt]


def start(request: LaunchRequest) -> Started:
    """The detached run; returns once Codex named the thread. RequestError for a wrong request, LaunchError
    when Codex refused or said nothing in START_WAIT_S."""
    cwd, args = _argv(request, options())
    exe = shutil.which("codex")
    if not exe:
        raise LaunchError("app.provider.codex.no_codex")
    log = cache_dir() / "runs" / f"codex-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with _runs_lock:
        _runs[:] = [p for p in _runs if p.poll() is None]
        try:
            with log.open("wb") as out:
                proc = subprocess.Popen(
                    [exe, *args], cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                    start_new_session=True, env=clean_env(),
                )  # fmt: skip
        except OSError as exc:
            raise LaunchError("app.provider.codex.exec_failed", error=str(exc)) from exc
        _runs.append(proc)
    tid = _thread_started(proc, log)
    meta = first_meta(path) if (path := find_rollout(codex_home(), tid)) else {}
    worktree = str(meta.get("cwd") or "") if request.worktree else ""  # Codex picks it: ~/.codex/worktrees/…
    return Started(tid, "", worktree, tid, f"codex resume {tid}")


def _thread_started(proc: subprocess.Popen, log: Path) -> str:
    """The thread id from the run's `thread.started` line, waited for at most START_WAIT_S."""
    deadline = time.monotonic() + START_WAIT_S
    while True:
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            if (tid := _started_id(line)) is not None:
                return tid
        if proc.poll() is not None:
            said = "\n".join(ln for ln in lines if ln.strip() and ln.strip() != NOISE)[-400:]
            if "trusted directory" in said:  # its own words, and where Codex starts
                raise LaunchError(UNTRUSTED_FAILED, error=said)
            if said:
                raise LaunchError("app.provider.said", text=said)
            raise LaunchError("app.provider.codex.exec_exit", code=proc.returncode)
        if time.monotonic() >= deadline:
            raise LaunchError("app.provider.codex.exec_no_id", s=f"{START_WAIT_S:.0f}", log=str(log))
        time.sleep(POLL_S)


def _started_id(line: str) -> str | None:
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("type") != "thread.started":
        return None
    tid = event.get("thread_id")
    return tid if isinstance(tid, str) and re.fullmatch(THREAD_ID, tid) else None
