"""Agent programs the dashboard reads and writes to: one provider per CLI, the contract in base.py.

PROVIDERS is explicit and ordered, like FEATURES: the first one is the default where a request names none
("New session"), and a session id two providers report is indexed from the first. A new
provider is a folder here and one line below; docs/providers.md says what it implements.
"""

from __future__ import annotations

import re
import sys

from .base import LiveSession, Provider
from .claude.provider import Claude
from .codex.provider import Codex

PROVIDERS: tuple[Provider, ...] = (Claude(), Codex())

ID = re.compile(r"[a-z][a-z0-9-]{0,31}")


def check(providers: tuple[Provider, ...]) -> None:
    """Refuse a registry the index would mix up (an id out of pattern or used twice), or a provider that does
    not declare what the contract asks of its class."""
    ids = [getattr(p, "id", "") for p in providers]
    if len(set(ids)) != len(ids) or not all(ID.fullmatch(i) for i in ids):
        raise ValueError(f"provider ids must be unique and match {ID.pattern}: {ids}")
    for p in providers:
        if not all(hasattr(p, name) for name in ("title", "caps", "parser_version")):
            raise ValueError(f"provider {p.id!r} must declare title, caps and parser_version")


check(PROVIDERS)


def by_id(provider_id: str) -> Provider:
    """The registered provider with this id; KeyError for any other."""
    for p in PROVIDERS:
        if p.id == provider_id:
            return p
    raise KeyError(f"no provider {provider_id!r}")


def default() -> Provider:
    return PROVIDERS[0]


def available() -> tuple[Provider, ...]:
    return tuple(p for p in PROVIDERS if p.available())


def live() -> dict[str, LiveSession]:
    """Every available provider's running sessions by id; a provider that fails is left out, not fatal."""
    found: dict[str, LiveSession] = {}
    for p in PROVIDERS:
        try:
            sessions = p.live() if p.available() else {}
        except Exception as exc:
            print(f"provider {p.id}: cannot read live sessions: {exc!r}", file=sys.stderr)
            continue
        for sid, item in sessions.items():
            found.setdefault(sid, item)
    return found
