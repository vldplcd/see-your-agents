"""Builder: the dashboard changes itself. A build is a branch, a worktree and a builder session in it.

The supervisor asks (`request_change`); the user accepts, rolls back or rejects with page buttons only. The
checkout is the git repository the running package comes from (an editable install); builds are merged into
whatever branch it had checked out when the build started, and nowhere else.

Tracking is driven by the board polling (`tick`, at most every TICK_EVERY_S): a building build whose session
has finished its turn gets the checks in a thread, then becomes ready or failed. With no page open nothing
moves, and nothing needs to: the banner on that page is where the user acts.

A refusal is a BuildError (a Problem: builder.* key, 409 unless said); an error found later, in a thread, is
kept in the build as a key and params (store.problem) and worded for whoever reads the board.
"""

from __future__ import annotations

import contextlib
import functools
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.env import clean_env
from ...core.providers.base import LaunchError, LiveSession
from ...i18n import Problem, message, request_lang
from . import agent, preview, repo, store
from .store import ACCEPTED, ACTIVE, BUILDING, CHECKING, FAILED, READY, REJECTED, ROLLED_BACK, problem

if TYPE_CHECKING:
    from ...app.server import App

PACKAGE = Path(__file__).resolve().parents[2]  # src/see_your_agents: its checkout is what builds change
MAX_ACTIVE = 3
GRACE_S = 20.0  # a fresh or nudged builder may look idle before its turn starts
TICK_EVERY_S = 2.0
SHOWN_FOR_S = 24 * 3600  # accepted and rolled back builds stay on the banner this long
CHECKS = "uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest -q"
CHECKS_TIMEOUT_S = 900
# The restarted server runs in the checkout's venv, which nothing else updates. --locked: never rewrite
# uv.lock in the user's checkout; a lock that does not match pyproject.toml is not what the checks passed on.
SYNC = ("uv", "sync", "--locked")
SYNC_TIMEOUT_S = 600
DEPENDENCIES = ("pyproject.toml", "uv.lock")
TAIL_CHARS = 4000
SAID_CHARS = 280
IDLE = {"idle", "done"}
WAITING = {"waiting", "blocked"}
VERDICT = re.compile(r"BUILD (READY|FAILED)\b:?\s*(.*)")
LATIN = ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "i", "k", "l", "m", "n", "o", "p", "r", "s"]
LATIN += ["t", "u", "f", "h", "c", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]
TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", LATIN, strict=True))


class BuildError(Problem):
    """A build action refused: a catalog key (builder.*) and its params; 409 unless said otherwise."""

    def __init__(self, key: str, /, status: int = HTTPStatus.CONFLICT, **params: object):
        super().__init__(key, status, **params)


@functools.lru_cache(maxsize=4)
def _toplevel(package: Path) -> Path | None:
    try:
        return repo.toplevel(package)
    except repo.GitError:
        return None


def checkout() -> Path:
    top = _toplevel(PACKAGE)
    if top is None:
        raise BuildError("builder.not_a_checkout", path=str(PACKAGE))
    return top


def mine() -> list[dict]:
    """Builds of the checkout this server runs from; another checkout's builds are its server's business."""
    top = _toplevel(PACKAGE)
    return [b for b in store.load() if top and b.get("checkout") == str(top)]


def find(build_id: str) -> dict:
    found = next((b for b in mine() if b["id"] == build_id), None)
    if found is None:
        raise BuildError("builder.not_found", HTTPStatus.NOT_FOUND)
    return found


def slugify(title: str) -> str:
    text = "".join(TRANSLIT.get(c, c) for c in title.lower())
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:32].strip("-") or "change"
    return f"{slug}-{secrets.token_hex(2)}"


def _age_s(ts: str | None) -> float:
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(str(ts))).total_seconds()
    except ValueError:
        return float("inf")


# ---- a new build ------------------------------------------------------------------------------


def create(title: str, description: str) -> dict:
    top = checkout()
    base_branch = repo.branch(top)
    if not base_branch:
        raise BuildError("builder.detached")
    if sum(b["status"] in ACTIVE for b in mine()) >= MAX_ACTIVE:
        raise BuildError("builder.too_many", n=MAX_ACTIVE)
    slug = slugify(title)
    worktree = top / ".claude" / "worktrees" / f"build-{slug}"
    if not repo.ignored(top, worktree):
        raise BuildError("builder.not_ignored", folder=str(worktree.parent))
    base, branch = repo.head(top), f"build/{slug}"
    repo.add_worktree(top, branch, worktree, base)
    # The builder's uv is offline in its sandbox: its venv is made now, from the checkout's own commit, and
    # never again outside the sandbox (a sync builds the project: the builder's pyproject.toml may run code).
    synced = _sync_dependencies(worktree)
    try:
        if synced is not None:
            raise LaunchError("builder.sync_failed", output=synced)
        job = agent.start(worktree, title, description, branch)
    except LaunchError as exc:
        repo.remove(top, worktree, branch)
        agent.forget(worktree)
        raise BuildError("builder.not_started", reason=message(exc, request_lang())) from exc
    at = store.now()
    return store.add(
        {
            "id": slug,
            "title": title,
            "description": description,
            "branch": branch,
            "worktree": str(worktree),
            "checkout": str(top),
            "base_branch": base_branch,
            "base_sha": base,
            "job_id": job,
            "session_id": agent.full_id(job),
            "status": BUILDING,
            "created_at": at,
            "updated_at": at,
            "busy_seen": False,
            "checks": None,
            "merge_sha": "",
            "prev_sha": "",
            "error": "",
        }
    )


# ---- tracking ---------------------------------------------------------------------------------

_tick_lock = threading.Lock()
_last_tick = 0.0


def _spawn(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def waiting(live: LiveSession | None) -> bool:
    return bool(live and (live.status in WAITING or live.waiting_for))


def live_for(b: dict, live: dict[str, LiveSession]) -> LiveSession | None:
    """The builder's live session: by session id, or by job id (a resume continues under a new session id)."""
    if found := live.get(b.get("session_id") or ""):
        return found
    job = b.get("job_id")
    return next((s for s in live.values() if job and s.job_id == job), None)


def tick(app: App, force: bool = False) -> None:
    """Move building builds along; cheap, and at most one at a time."""
    global _last_tick
    if not _tick_lock.acquire(blocking=False):
        return
    try:
        if not force and time.monotonic() - _last_tick < TICK_EVERY_S:
            return
        _last_tick = time.monotonic()
        builds = [b for b in mine() if b["status"] in (BUILDING, CHECKING)]
        live = app.live() if builds else {}
        for b in builds:
            if b["status"] == BUILDING:
                _track(app, b, live_for(b, live))
            elif not _pid_alive(b.get("checks_pid")):  # checking, but that server is gone (a restart)
                store.update(b["id"], checks_port=app.port)  # this server's port is the one to close now
                _spawn(run_checks, b["id"])
    finally:
        _tick_lock.release()


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _track(app: App, b: dict, live: LiveSession | None) -> None:
    if live and live.session_id != b.get("session_id"):
        b = store.update(b["id"], session_id=live.session_id) or b
    elif not b.get("session_id") and b.get("job_id"):
        sid = agent.full_id(b["job_id"], wait_s=0)
        if sid:
            b = store.update(b["id"], session_id=sid) or b
    if live and live.status not in IDLE:
        if live.status == "busy" and (not b.get("busy_seen") or b.get("question")):
            store.update(b["id"], busy_seen=True, question="")  # a question answered in its chat is gone
        return  # working, or waiting for the user (the banner says so)
    if not b.get("busy_seen") and _age_s(b.get("updated_at")) < GRACE_S:
        return  # may not have started its turn yet
    answer = agent.last_answer(b.get("session_id") or "")
    verdict, reason = answer_verdict(answer)
    said = _clip(answer)
    top, since = Path(b["checkout"]), b.get("comment_tip")
    try:
        ahead = repo.commits_ahead(top, b["base_sha"], b["branch"])
        new = repo.commits_ahead(top, since, b["branch"]) if since else ahead
    except repo.GitError as exc:
        store.move(
            b["id"], FAILED, only_from=(BUILDING,), error=problem("builder.error.branch_gone", error=exc)
        )
        return
    if verdict == "FAILED":  # the builder's reason in its own words, kept as they are
        store.move(
            b["id"], FAILED, only_from=(BUILDING,), error=reason or problem("builder.error.said_failed")
        )
    elif new or (ahead and verdict == "READY"):  # a commit since Rework, or the builder says it is done
        fields = {"checks_pid": os.getpid(), "checks_port": app.port, "checks": None}
        if store.move(b["id"], CHECKING, only_from=(BUILDING,), **fields):
            _spawn(run_checks, b["id"])
    elif since and said:  # Rework got words and no commit: a question, and the branch is not ready
        if b.get("question") != said:
            store.update(b["id"], question=said)  # stays building; shown only while it is
    else:  # often a question to the user: show its start, Rework answers it
        error = (
            problem("builder.error.no_commit_said", said=said) if said else problem("builder.error.no_commit")
        )
        store.move(b["id"], FAILED, only_from=(BUILDING,), error=error)


def _clip(text: str) -> str:
    said = " ".join(text.split())
    return said[:SAID_CHARS] + ("…" if len(said) > SAID_CHARS else "")


def answer_verdict(answer: str) -> tuple[str, str]:
    """("READY" | "FAILED" | "", reason) from the last lines of the builder's answer."""
    lines = [ln.strip(" *`_>") for ln in answer.splitlines() if ln.strip()]
    for line in reversed(lines[-5:]):
        if found := VERDICT.search(line):
            return found[1], found[2].strip()
    return "", ""


def run_checks(build_id: str) -> None:
    """ruff and pytest in the worktree; the build becomes ready or failed with the tail of the output.

    `checked_sha` is the commit they run on: Accept merges that one and nothing the builder added later.
    They run the build's code, so they go under `sandbox-exec` like the builder's own Bash, but outside the
    builder's own session; fail closed if it cannot. See docs/notes/claude-sandbox-notes.md."""
    b = store.get(build_id)
    if b is None or b["status"] != CHECKING:
        return
    worktree = Path(b["worktree"])
    try:
        loose = repo.changes(worktree, keep_local=False)
        checked, branch_tip = repo.head(worktree), repo.tip(worktree, b["branch"])
    except repo.GitError as exc:
        store.move(
            build_id, FAILED, only_from=(CHECKING,), error=problem("builder.error.worktree_gone", error=exc)
        )
        return
    store.update(build_id, checks_pid=os.getpid(), checked_sha=checked)
    if checked != branch_tip:
        error = problem("builder.error.not_tip", branch=b["branch"])
        store.move(build_id, FAILED, only_from=(CHECKING,), error=error)
        return
    if loose:
        tail = "\n".join(loose[:40])
        store.move(
            build_id, FAILED, only_from=(CHECKING,), checks={"ok": False, "tail": tail},
            error=problem("builder.error.uncommitted"),
        )  # fmt: skip
        return
    try:
        argv = agent.confine(["sh", "-c", CHECKS], worktree, b.get("checks_port"))  # no port: refused
    except agent.SandboxError as exc:
        store.move(build_id, FAILED, only_from=(CHECKING,), error=store.problem_of(exc))
        return
    try:
        out = subprocess.run(
            argv, cwd=worktree, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=CHECKS_TIMEOUT_S, env=clean_env(**agent.confined_env(worktree)),
        )  # fmt: skip
        ok, text = out.returncode == 0, out.stdout.decode("utf-8", errors="replace")
        error = problem("builder.error.checks_failed")
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok, text, error = False, str(exc), problem("builder.error.checks_unfinished", error=exc)
    store.move(
        build_id, READY if ok else FAILED, only_from=(CHECKING,),
        checks={"ok": ok, "tail": text[-TAIL_CHARS:]}, error="" if ok else error,
    )  # fmt: skip


# ---- the user's buttons -----------------------------------------------------------------------

_action_lock = threading.Lock()  # one merge, revert or removal at a time


def _need(b: dict, *statuses: str) -> None:
    if b["status"] not in statuses:
        raise BuildError("builder.wrong_status", status=b["status"])


def _clean_checkout(b: dict) -> Path:
    top = Path(b["checkout"])
    loose = repo.changes(top)
    if loose:
        listed = ", ".join(ln[3:] for ln in loose[:5]) + (" …" if len(loose) > 5 else "")
        raise BuildError("builder.checkout_dirty", files=listed)
    return top


def _need_base_branch(top: Path, b: dict) -> None:
    """Merge and revert only on the branch the build came from: a revert elsewhere leaves the build in it."""
    if (current := repo.branch(top)) != b["base_branch"]:
        raise BuildError("builder.other_branch", current=current or "detached HEAD", base=b["base_branch"])


def _checked_or_recheck(app: App, b: dict) -> str:
    """The commit the checks passed on. A branch that moved since is checked again: a commit made after the
    card said ready (the builder still has its chat) would land unchecked."""
    if repo.tip(Path(b["checkout"]), b["branch"]) != b.get("checked_sha"):
        fields = {"checks_pid": os.getpid(), "checks_port": app.port, "checks": None}
        if store.move(b["id"], CHECKING, only_from=(READY,), **fields):
            _spawn(run_checks, b["id"])
        raise BuildError("builder.moved")
    return b["checked_sha"]


def _refuse_hidden_paths(top: Path, head: str, sha: str) -> None:
    """A merge writes over a file the checkout ignores without a word (a build may `git add -f` one), and
    .claude/ outside skills/ is Claude Code's own: settings and permissions."""
    paths = repo.changed_paths(top, head, sha)
    claude = {p for p in paths if p.split("/")[0] == ".claude" and not p.startswith(".claude/skills/")}
    hidden = sorted(claude | set(repo.ignored_paths(top, paths)))
    if hidden:
        listed = ", ".join(hidden[:10]) + (" …" if len(hidden) > 10 else "")
        raise BuildError("builder.hidden_paths", paths=listed)


def _sync_dependencies(top: Path) -> str | None:
    """`uv sync --locked` in the checkout or a fresh build worktree; None when it worked, else the output's
    tail."""
    try:
        out = subprocess.run(
            SYNC, cwd=top, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=SYNC_TIMEOUT_S, env=clean_env(),
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"uv sync: {exc}"  # the output's place: the program's words, not ours
    return None if out.returncode == 0 else out.stdout.decode("utf-8", errors="replace")[-TAIL_CHARS:]


def _undo_unsynced(top: Path, build_id: str, merged: str, tail: str) -> BuildError:
    """The build's dependencies do not install: revert its merge before anything restarts on them.
    Rolled back, not failed: git would skip the reverted commits if the same branch were merged again."""
    checks = {"ok": False, "tail": tail}
    try:
        revert = repo.revert_merge(top, merged)
    except repo.GitError as exc:
        store.update(build_id, checks=checks, error=problem("builder.error.sync_revert_failed"))
        return BuildError("builder.sync_revert_failed", error=str(exc))
    stale = (
        _sync_dependencies(top) is not None
    )  # the venv back to its lock, in case the failed sync changed it
    error = problem("builder.error.sync_rolled_back_stale" if stale else "builder.error.sync_rolled_back")
    store.move(build_id, ROLLED_BACK, revert_sha=revert, checks=checks, error=error)
    return BuildError("builder.sync_rolled_back_stale" if stale else "builder.sync_rolled_back")


def _stop_builder(app: App, b: dict) -> None:
    """Stop the builder session, but only one that is ours: the job id we started, working in our worktree."""
    live = live_for(b, app.live())
    if b.get("job_id") and live and live.job_id == b["job_id"] and live.cwd == b["worktree"]:
        agent.stop(b["job_id"])


def accept(app: App, build_id: str) -> dict:
    with _action_lock:
        b = find(build_id)
        _need(b, READY)
        top = _clean_checkout(b)
        _need_base_branch(top, b)
        checked = _checked_or_recheck(app, b)
        prev = repo.head(top)
        _refuse_hidden_paths(top, prev, checked)
        preview.stop()
        commit_message = f"Accept build: {b['title']}\n\n{b['description'][:1000]}"
        try:
            merged = repo.merge(top, checked, commit_message)
        except repo.GitError as exc:
            raise BuildError("builder.merge_failed", error=str(exc)) from exc
        done = store.move(
            build_id, ACCEPTED, prev_sha=prev, merge_sha=merged, accepted_at=store.now(), error=""
        )
        _stop_builder(app, b)
        _drop_worktree(top, b)
        deps_changed = repo.git(top, "diff", "--name-only", prev, merged, "--", *DEPENDENCIES)
        if deps_changed and (tail := _sync_dependencies(top)) is not None:
            raise _undo_unsynced(top, build_id, merged, tail)
        return {"build": done, "restart": app.request_restart()}


def _drop_worktree(top: Path, b: dict) -> None:
    """After an accept the branch stays (it is merged); its worktree and venv go."""
    worktree = Path(b["worktree"])
    if worktree.exists():
        with contextlib.suppress(repo.GitError):  # left for `git worktree prune`; the merge is what matters
            repo.git(top, "worktree", "remove", "--force", str(worktree))
    agent.forget(worktree)


def rollback(app: App, build_id: str) -> dict:
    with _action_lock:
        b = find(build_id)
        _need(b, ACCEPTED)
        top = _clean_checkout(b)
        _need_base_branch(top, b)
        if not repo.contains(top, b["merge_sha"]):
            raise BuildError("builder.not_merged_here")
        try:
            revert = repo.revert_merge(top, b["merge_sha"])
        except repo.GitError as exc:
            raise BuildError("builder.revert_failed", error=str(exc)) from exc
        done = store.move(build_id, ROLLED_BACK, revert_sha=revert, error="")
        return {"build": done, "restart": app.request_restart()}


def reject(app: App, build_id: str) -> dict:
    with _action_lock:
        b = find(build_id)
        _need(b, *ACTIVE)
        top = Path(b["checkout"])
        # only what the builder made: its own branch name and its own folder under .claude/worktrees
        own = top / ".claude" / "worktrees" / f"build-{b['id']}"
        if b["branch"] != f"build/{b['id']}" or Path(b["worktree"]) != own:
            raise BuildError("builder.not_ours")
        preview.stop(build_id)
        _stop_builder(app, b)
        try:
            repo.remove(top, Path(b["worktree"]), b["branch"])
        except repo.GitError as exc:
            raise BuildError("builder.remove_failed", error=str(exc)) from exc
        agent.forget(Path(b["worktree"]))
        return {"build": store.move(build_id, REJECTED, error="")}


def comment(app: App, build_id: str, text: str) -> dict:
    """Rework: the user's words to the builder; the build goes back to building.

    The branch tip is noted: only a commit after it is new work to check. A turn that ends without one
    answered with words, often a question, and the banner shows it."""
    b = find(build_id)
    _need(b, BUILDING, READY, FAILED)
    tip = repo.tip(Path(b["checkout"]), b["branch"])
    live = live_for(b, app.live())
    if live and live.name:
        sent = agent.tell(live.name, text)
        if not sent.ok:
            reason = message(sent.problem(), request_lang())
            raise BuildError("builder.not_delivered", HTTPStatus.BAD_GATEWAY, reason=reason)
        route = "message"
    elif b.get("session_id"):
        try:
            job = agent.resume(b["session_id"], Path(b["worktree"]), b["title"], text)
        except LaunchError as exc:
            reason = message(exc, request_lang())
            raise BuildError("builder.not_resumed", HTTPStatus.BAD_GATEWAY, reason=reason) from exc
        # the resumed builder is a copy of the conversation under a new session id: follow it
        store.update(build_id, job_id=job, session_id=agent.full_id(job) or b["session_id"])
        route = "resume"
    else:
        raise BuildError("builder.no_session")
    fields = {"busy_seen": False, "checks": None, "error": "", "question": "", "comment_tip": tip}
    done = store.move(build_id, BUILDING, **fields) if b["status"] != BUILDING else None
    return {"build": done or store.update(build_id, **fields), "route": route}


# ---- what the page shows ----------------------------------------------------------------------


def shown(b: dict) -> bool:
    if b["status"] in ACTIVE:
        return True
    return b["status"] in (ACCEPTED, ROLLED_BACK) and _age_s(b.get("updated_at")) < SHOWN_FOR_S


def public(b: dict, live: LiveSession | None) -> dict:
    """A build for the banner; its error worded in the language of the request that reads the board."""
    keys = (
        "id", "title", "description", "status", "branch", "base_branch", "session_id", "created_at",
        "updated_at", "accepted_at", "checks", "question",
    )  # fmt: skip
    return {
        **{k: b.get(k) for k in keys},
        "error": store.worded(b.get("error"), request_lang()),
        "waiting": b["status"] == BUILDING and waiting(live),
    }
