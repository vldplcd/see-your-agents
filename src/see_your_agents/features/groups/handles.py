"""Short @handles for group members: lowercase letters (Latin or Cyrillic), digits and dashes, up to 16.

Two words are reserved: the supervisor's handle and @all. Agents read them in English (SUPERVISOR,
EVERYONE); the page shows them in its language (groups.handle.*); a post may use any of them.
"""

from __future__ import annotations

import re

from ...i18n import LANGS, request_lang, t

MAX_LEN = 16
SUPERVISOR = "supervisor"
EVERYONE = "all"
NOT_HANDLE = re.compile(r"[^a-zа-яё0-9]+")
MENTION = re.compile(r"(?<![\w.@-])@([a-zа-яё0-9][a-zа-яё0-9-]*)", re.IGNORECASE)


def _words(word: str, key: str) -> set[str]:
    """`word` as agents read it and as every UI language writes it."""
    return {word, *(t(lang, key).lower() for lang in LANGS)}


def supervisor_words() -> set[str]:
    return _words(SUPERVISOR, "groups.handle.supervisor")


def everyone_words() -> set[str]:
    return _words(EVERYONE, "groups.handle.all")


def slug(text: str) -> str:
    """A handle body from free text, cut at a word break if that keeps most of it; "" if nothing is left."""
    body = NOT_HANDLE.sub("-", (text or "").lower()).strip("-")
    if len(body) <= MAX_LEN:
        return body
    cut = body[:MAX_LEN]
    head = cut.rsplit("-", 1)[0] if body[MAX_LEN] != "-" and "-" in cut else cut
    return (head if len(head) >= 4 else cut).strip("-")


def pick(sources: list[str], taken: set[str]) -> str:
    """The first usable source made unique among `taken` (and the reserved words) with a numeric suffix."""
    base = next((s for s in map(slug, sources) if len(s) >= 2), t(request_lang(), "groups.handle.fallback"))
    handle, n, reserved = base, 1, supervisor_words() | everyone_words()
    while handle in taken or handle in reserved:
        n += 1
        suffix = f"-{n}"
        handle = base[: MAX_LEN - len(suffix)].rstrip("-") + suffix
    return handle


def mentions(text: str) -> list[str]:
    """Lowercased @words in the text, in order, without repeats."""
    found: list[str] = []
    for m in MENTION.finditer(text):
        word = m.group(1).lower().rstrip("-")
        if word not in found:
            found.append(word)
    return found
