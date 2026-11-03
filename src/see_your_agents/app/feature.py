"""A feature's manifest: everything the host plugs in for it. See docs/ARCHITECTURE.md.

A feature is a folder under features/ with a `FEATURE = Feature(...)` in its __init__.py and a line in
features/__init__.py. The host (server, MCP, CLI, page) reads only these fields; it never imports a feature.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.sessions import CardExtra
from .mcp import Tool
from .replies import Reply

if TYPE_CHECKING:
    from email.message import Message

    from .server import App

GetHandler = Callable[["App", str, dict[str, str]], Reply | None]
PostHandler = Callable[["App", str, bytes, "Message"], Reply | None]
ResumeArgs = Callable[["App", str], list[str] | None]

ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
ASSET_NAME = re.compile(r"[a-z0-9-]+\.(js|css)")
# Paths the host answers itself; a feature with HTTP handlers cannot take one of these ids.
HOST_ROUTES = ("board", "session", "search", "plan", "health", "pair")
OPEN_POST_FORBIDDEN = ("|", "^", "$", r"\A", r"\Z", r"\z")  # not in an open_posts entry: see check_registry


@dataclass(frozen=True)
class Feature:
    """One feature. Only `id` is required; every other field is a way to plug in, used or not.

    id            owns /api/<id> and /api/<id>/...; its page assets are served at /<id>/<file>
    folder        the feature's folder: web/<file>, i18n/<lang>.json (its strings, docs/i18n.md) and the
                  supervisor prompt are read from it
    handle_get    (app, path, query) -> Reply | None for GET /api/<id>...; None falls through to 404
    handle_post   (app, path, body, headers) -> Reply | None; the host has checked Host, Origin, X-SYA and,
                  unless the path matches `open_posts`, that it comes from the paired browser (pairing.py)
    open_posts    full-match regexes for this feature's POST paths that do NOT need the paired browser,
                  being guarded another way (the approvals hook signs its routes with the hook token). Each
                  must start with the feature's own /api/<id>; every other POST requires the paired browser
    tools         MCP tools; `read` scope for every session, `control` only for the supervisor
    add_cli       (subparsers) -> None: add `sya <name>`; a subparser sets run=fn(args, features)
    scripts       files in web/ added to the page as <script>, in registry order
    styles        files in web/ added to the page as <link rel=stylesheet>, in registry order
    board_extra   (app) -> dict merged into /api/board
    card_extra    (record, live) -> dict merged into every board card and session detail
    supervisor_prompt  a markdown file in `folder`, appended to the supervisor's system prompt
    resume_args   (app, session_id) -> extra flags for a one-turn resume of a session the feature launched
                  itself (`claude -p --resume`), or None for any other session
    setup         (app) -> None, run once when App is built: wire the feature into core (the chat plugs the
                  VS Code bridge and the host into the Claude provider's routes). Only a registered
                  feature's setup runs
    background    (app) -> None, a loop serve() runs in its own daemon thread while the server serves (awake
                  holds caffeinate); an App built alone, as in tests, never starts it
    """

    id: str
    folder: Path | None = None
    handle_get: GetHandler | None = None
    handle_post: PostHandler | None = None
    open_posts: tuple[str, ...] = ()
    tools: tuple[Tool, ...] = ()
    add_cli: Callable[[argparse._SubParsersAction], None] | None = None
    scripts: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    board_extra: Callable[[App], dict] | None = None
    card_extra: CardExtra | None = None
    supervisor_prompt: str | None = None
    resume_args: ResumeArgs | None = None
    setup: Callable[[App], None] | None = None
    background: Callable[[App], None] | None = None

    def owns(self, path: str) -> bool:
        prefix = f"/api/{self.id}"
        return path == prefix or path.startswith(prefix + "/")

    def asset(self, name: str) -> Path | None:
        """A page asset by file name: only the names the manifest lists, only from web/."""
        if name not in (*self.scripts, *self.styles) or self.folder is None:
            return None
        return self.folder / "web" / name

    def prompt_path(self) -> Path | None:
        return self.folder / self.supervisor_prompt if self.folder and self.supervisor_prompt else None

    def prompt(self) -> str:
        """The fragment for the supervisor's prompt; "" when the feature adds none."""
        path = self.prompt_path()
        return path.read_text(encoding="utf-8") if path else ""


def check_registry(features: Sequence[Feature]) -> None:
    """Refuse a registry the host could serve wrongly: duplicate ids or tools, unsafe or missing files."""
    ids = [f.id for f in features]
    if len(set(ids)) != len(ids):
        raise ValueError(f"feature ids repeat: {ids}")
    names = [t.name for f in features for t in f.tools]
    if len(set(names)) != len(names):
        raise ValueError(f"MCP tool names repeat: {names}")
    for f in features:
        if not ID.fullmatch(f.id):
            raise ValueError(f"feature id {f.id!r} must match {ID.pattern}")
        if (f.handle_get or f.handle_post) and f.id in HOST_ROUTES:
            raise ValueError(f"feature {f.id!r}: /api/{f.id} is answered by the host")
        for pattern in f.open_posts:  # a feature may open only its own POST paths, and the regex must compile
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"feature {f.id!r}: open_posts {pattern!r} is not a regex: {exc}") from exc
            # An alternation or an anchor could reach past the /api/<id> prefix checked below: one path per
            # entry, full-matched by the host, so neither is ever needed.
            if any(bad in pattern for bad in OPEN_POST_FORBIDDEN):
                raise ValueError(f"feature {f.id!r}: open_posts {pattern!r} may not use | or anchors")
            if not pattern.startswith(f"/api/{f.id}/") and pattern != f"/api/{f.id}":
                raise ValueError(f"feature {f.id!r}: open_posts {pattern!r} must be under /api/{f.id}")
        for name in (*f.scripts, *f.styles):
            kind = ASSET_NAME.fullmatch(name)
            if not kind or (name in f.scripts) != (kind[1] == "js"):
                raise ValueError(
                    f"feature {f.id!r}: asset {name!r} must be a [a-z0-9-] .js script or .css style"
                )
        files = [f.asset(n) for n in (*f.scripts, *f.styles)]
        if f.supervisor_prompt:
            files.append(f.prompt_path())
        if any(p is None or not p.is_file() for p in files):
            raise ValueError(f"feature {f.id!r}: a listed file is missing (is `folder` set?)")
        _check_catalogs(f)


def _check_catalogs(f: Feature) -> None:
    """A feature's catalogs (i18n/<lang>.json) hold only its own keys: a stray one would shadow another's."""
    for path in sorted(f.folder.glob("i18n/*.json")) if f.folder else ():
        strings = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(strings, dict) or any(not k.startswith(f"{f.id}.") for k in strings):
            raise ValueError(f"feature {f.id!r}: {path.name} must be an object of {f.id}.* keys")
