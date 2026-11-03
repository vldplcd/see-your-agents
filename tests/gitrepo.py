"""A real throwaway git repository for tests: no user config, no hooks, no signing."""

import subprocess
from pathlib import Path


def isolate_git(monkeypatch) -> None:
    for key, value in {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(key, value)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def make_repo(path: Path) -> Path:
    """main with one commit; .claude/ ignored except skills/, like the real checkout."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / ".gitignore").write_text(".claude/*\n!.claude/skills/\n")
    (path / "app.txt").write_text("one\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    return path


def commit(repo: Path, name: str, text: str, message: str = "change") -> str:
    (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")
