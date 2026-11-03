"""`sya hooks install | uninstall | status`: the dashboard's PermissionRequest hook in Claude Code's settings.

The entry goes into `<claude home>/settings.json` (CLAUDE_CONFIG_DIR moves it). Install and uninstall touch
only this hook's own entries, found by the hook's path; other hooks and settings stay as they are. Before a
write the old file is copied next to it, and the diff is printed; `--print` only prints it.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shlex
import shutil
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from ...app.feature import Feature
from ...core.providers.claude.paths import claude_home
from . import approvals, hook

EVENT = "PermissionRequest"
TIMEOUT_S = 3600  # Claude Code's limit for the hook; hook.WAIT_S stays under it
OURS = "/features/approvals/hook.py"  # how an entry of this hook is recognized, wherever the checkout is
WORKTREE = "/.claude/worktrees/"


def settings_path() -> Path:
    return claude_home() / "settings.json"


def command(port: int) -> str:
    """The hook's command line: this Python and this hook file, the dashboard's port and token file."""
    return shlex.join(
        [sys.executable, "-I", str(Path(hook.__file__).resolve()), "--port", str(port),
         "--token-file", str(approvals.token_path())]
    )  # fmt: skip


def _ours(entry: object) -> bool:
    return isinstance(entry, dict) and OURS in str(entry.get("command", ""))


def _without_ours(groups: list) -> list:
    """The event's matcher groups minus this hook's entries; a group left empty goes too."""
    kept = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        hooks = [h for h in group["hooks"] if not _ours(h)]
        if hooks:
            kept.append({**group, "hooks": hooks})
        elif len(hooks) == len(group["hooks"]):  # an empty group that was not ours
            kept.append(group)
    return kept


def change(settings: dict, port: int | None) -> dict:
    """The settings with this hook installed (`port` given) or removed (None)."""
    data = dict(settings)
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict) or not isinstance(hooks.get(EVENT, []), list):
        raise ValueError(
            f'"hooks"/"{EVENT}" in the Claude Code settings has an unexpected shape: edit it by hand'
        )
    hooks = dict(hooks)
    groups = _without_ours(hooks.get(EVENT, []))
    if port is not None:
        groups.append(
            {"matcher": "*", "hooks": [{"type": "command", "command": command(port), "timeout": TIMEOUT_S}]}
        )
    if groups:
        hooks[EVENT] = groups
    else:
        hooks.pop(EVENT, None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    return data


def _backup(path: Path) -> Path:
    """A copy of the file next to it, under a name no earlier backup has (microseconds, O_EXCL)."""
    while True:
        backup = path.with_name(f"{path.name}.sya-backup-{datetime.now():%Y%m%d-%H%M%S-%f}")
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(path.read_bytes())
        shutil.copymode(path, backup)
        return backup


def _write(path: Path, text: str) -> Path | None:
    """Replace the file atomically, keeping its mode; the previous one stays as a backup next to it. A
    symlinked settings.json (dotfiles) is written through: the link stays, its target gets the new text, and
    the backup lands next to the link, where the user looks, not in the dotfiles repository."""
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup(path) if target.exists() else None
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if backup:
        shutil.copymode(backup, tmp)
    else:
        os.chmod(tmp, 0o644)
    os.replace(tmp, target)
    return backup


def from_worktree() -> bool:
    """This hook file lives in an agent's worktree, which goes away when the agent's branch is done."""
    return WORKTREE in Path(hook.__file__).resolve().as_posix()


def _settings(path: Path) -> tuple[str, dict]:
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        current = json.loads(before) if before.strip() else {}
    except ValueError as exc:
        raise SystemExit(f"{path}: not JSON ({exc}); changing nothing") from None
    if not isinstance(current, dict):
        raise SystemExit(f"{path}: not a JSON object; changing nothing")
    return before, current


def apply(port: int | None, write: bool, allow_worktree: bool = False) -> str:
    """Install (`port`) or uninstall (None); returns what to print: the diff and where things went."""
    path = settings_path()
    warn = ""
    if port is not None and from_worktree():
        where = Path(hook.__file__).resolve()
        warn = f"The hook lives in an agent's worktree ({where}): when the worktree goes, so does the hook.\n"
        if write and not allow_worktree:
            raise SystemExit(
                warn + "Install it from the main checkout (where `sya serve` runs) or add --from-worktree."
            )
    before, current = _settings(path)
    try:
        after = json.dumps(change(current, port), indent=2, ensure_ascii=False) + "\n"
    except ValueError as exc:
        raise SystemExit(f"{path}: {exc}") from None
    if json.loads(after) == current:
        already = "already installed" if port is not None else "no dashboard hook there"
        return warn + f"{path}: {already}, changing nothing"
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True), str(path), f"{path} (new)"
        )
    )
    if not write:
        return warn + diff + "\n(--print: nothing written)"
    if port is not None:
        approvals.ensure_token()
    target = path.resolve()
    backup = _write(path, after)
    through = f" (through the link to {target})" if target != path.absolute() else ""
    saved = f"\nThe old file: {backup}" if backup else ""
    return f"{warn}{diff}\nWritten: {path}{through}{saved}"


def status() -> str:
    """Is the hook installed, and would it run: its Python, its file and its token file exist."""
    path = settings_path()
    _before, current = _settings(path)
    groups = (current.get("hooks") or {}).get(EVENT) if isinstance(current.get("hooks"), dict) else None
    commands = [
        h.get("command", "")
        for g in (groups if isinstance(groups, list) else [])
        if isinstance(g, dict) and isinstance(g.get("hooks"), list)
        for h in g["hooks"]
        if _ours(h)
    ]
    if not commands:
        return f"not installed: {path}"
    lines = [f"installed: {path}" + (f" ({len(commands)} entries)" if len(commands) > 1 else "")]
    for cmd in commands:
        try:
            argv = shlex.split(cmd)
        except ValueError:
            lines.append(f"  the command does not parse: {cmd}")
            continue
        flag = {a: argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("--port", "--token-file")}
        token = Path(flag.get("--token-file", ""))
        for name, file in (
            ("python", Path(argv[0])),
            ("hook", Path(argv[2] if len(argv) > 2 else "")),
            ("token", token),
        ):
            lines.append(f"  {name}: {file} — {'found' if file.is_file() else 'MISSING'}")
        lines.append(f"  port: {flag.get('--port', '?')}")
        if WORKTREE in cmd:
            lines.append("  the hook is in an agent's worktree: it goes away with it")
    return "\n".join(lines)


def add_cli(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "hooks", help="install | uninstall | status of the hook that answers permission prompts from the page"
    )
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.add_argument("--port", type=int, default=8765, help="the port `sya serve` listens on")
    p.add_argument("--print", action="store_true", help="only print the change to Claude Code's settings")
    p.add_argument("--from-worktree", action="store_true", help="install from an agent's worktree anyway")
    p.set_defaults(run=run_cli)


def run_cli(args: argparse.Namespace, features: Sequence[Feature]) -> None:
    if args.action == "status":
        print(status())
        return
    port = args.port if args.action == "install" else None
    print(apply(port, write=not args.print, allow_worktree=args.from_worktree))
