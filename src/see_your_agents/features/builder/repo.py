"""Git for the builder: the checkout the dashboard runs from, a build's branch and worktree, merge and revert.

Every command runs with stdin closed and GIT_EDITOR=true, so nothing waits for a terminal.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

MAX_DIFF_CHARS = 200_000
LOCAL_UNTRACKED = "?? .claude/"  # worktrees and local Claude Code state; not the user's changes


class GitError(Exception):
    pass


def git(
    cwd: Path | str, *args: str, timeout: float = 60, feed: str | None = None, ok: tuple[int, ...] = (0,)
) -> str:
    """stdout of a git command; `feed` goes to its stdin, `ok` lists the exit codes that are not errors."""
    stdin = {"input": feed} if feed is not None else {"stdin": subprocess.DEVNULL}
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
            env={**os.environ, "GIT_EDITOR": "true", "GIT_TERMINAL_PROMPT": "0"}, **stdin,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]}: {exc}") from exc
    if out.returncode not in ok:
        raise GitError((out.stderr or out.stdout).strip() or f"git {args[0]}: exit code {out.returncode}")
    return out.stdout.rstrip("\n")


def toplevel(folder: Path) -> Path:
    return Path(git(folder, "rev-parse", "--show-toplevel"))


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def tip(repo: Path, name: str) -> str:
    """The commit branch `name` points at."""
    return git(repo, "rev-parse", "--verify", f"refs/heads/{name}^{{commit}}")


def branch(repo: Path) -> str:
    """The checked-out branch; "" when HEAD is detached."""
    return git(repo, "branch", "--show-current")


def ignored(repo: Path, path: Path) -> bool:
    try:
        git(repo, "check-ignore", "-q", str(path))
    except GitError:
        return False
    return True


def ignored_paths(repo: Path, paths: list[str]) -> list[str]:
    """Those of `paths` that .gitignore hides in this checkout; a tracked file is never one of them."""
    if not paths:
        return []
    # exit 1: none of them is ignored. -z works only with --stdin
    out = git(repo, "check-ignore", "-z", "--stdin", feed="\0".join(paths) + "\0", ok=(0, 1))
    return [p for p in out.split("\0") if p]


def changed_paths(repo: Path, since: str, sha: str) -> list[str]:
    """What a merge of `sha` brings in: each path it changed since it forked from `since`, a rename as two."""
    out = git(repo, "diff", "--name-only", "--no-renames", "-z", f"{since}...{sha}")
    return [p for p in out.split("\0") if p]


def add_worktree(repo: Path, name: str, worktree: Path, base: str) -> None:
    git(repo, "worktree", "add", "-q", "-b", name, str(worktree), base)


def commits_ahead(repo: Path, base: str, name: str) -> int:
    return int(git(repo, "rev-list", "--count", f"{base}..{name}") or 0)


def changes(repo: Path, keep_local: bool = True) -> list[str]:
    """`git status --porcelain` lines; with keep_local, untracked files under .claude/ do not count."""
    lines = git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    return [ln for ln in lines if ln and not (keep_local and ln.startswith(LOCAL_UNTRACKED))]


def diff(repo: Path, base: str, name: str, cap: int = MAX_DIFF_CHARS) -> dict:
    stat = git(repo, "diff", "--stat", f"{base}...{name}")
    patch = git(repo, "diff", f"{base}...{name}")
    return {"stat": stat, "patch": patch[:cap], "truncated": len(patch) > cap}


def merge(repo: Path, sha: str, message: str) -> str:
    """`git merge --no-ff` of one commit; a merge that fails is aborted, so the checkout stays as it was."""
    try:
        git(repo, "merge", "--no-ff", "-m", message, sha)
    except GitError:
        _abort(repo, "merge")
        raise
    return head(repo)


def contains(repo: Path, sha: str) -> bool:
    try:
        git(repo, "merge-base", "--is-ancestor", sha, "HEAD")
    except GitError:
        return False
    return True


def revert_merge(repo: Path, sha: str) -> str:
    try:
        git(repo, "revert", "-m", "1", "--no-edit", sha)
    except GitError:
        _abort(repo, "revert")
        raise
    return head(repo)


def _abort(repo: Path, what: str) -> None:
    with contextlib.suppress(GitError):  # nothing was in progress
        git(repo, what, "--abort")


def remove(repo: Path, worktree: Path, name: str) -> None:
    """Drop the worktree (with whatever is in it) and the branch."""
    if worktree.exists():
        git(repo, "worktree", "remove", "--force", str(worktree))
    else:
        git(repo, "worktree", "prune")
    try:
        git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{name}")
    except GitError:
        return  # already gone
    git(repo, "branch", "-D", name)
