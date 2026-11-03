"""UI languages: the catalogs of the page's and the server's texts, `t()`, and the language of a request.

A catalog is a JSON file found by convention: web/i18n/<lang>.json for the shell and the host,
features/<id>/i18n/<lang>.json for a feature. Keys carry their owner's prefix (`app.`, `board.`, `todo.`). A
value is a string with {name} placeholders, or a plural object keyed by CLDR category (one, few, many,
other) that takes its count from {n}. A key missing from a language falls back to English. docs/i18n.md.

Texts for agents (prompts, tool descriptions, courier headers) are English and never catalog strings.
Stdlib only: every layer may import this module, core included.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import logging
import re
from collections.abc import Iterable, Iterator, Mapping
from http import HTTPStatus
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
LANGS = {  # the language select, in this order: a new language is a row here (docs/i18n.md)
    "en": "English",
    "ru": "Русский",
    "es": "Español",
    "de": "Deutsch",
    "fr": "Français",
    "zh-CN": "简体中文",
}
DEFAULT = "en"
HEADER = "X-SYA-Lang"  # the page sends its language with every request
PLACEHOLDER = re.compile(r"\{(\w+)\}")

_log = logging.getLogger(__name__)
_request_lang = contextvars.ContextVar("sya_request_lang", default=DEFAULT)


def plural(lang: str, n: float) -> str:
    """The CLDR plural category of `n`, as Intl.PluralRules(lang) names it on the page."""
    whole = float(n).is_integer()
    i = int(abs(n))
    match lang.split("-")[0]:
        case "ru" | "uk":
            if not whole:
                return "other"
            if i % 10 == 1 and i % 100 != 11:
                return "one"
            if 2 <= i % 10 <= 4 and not 12 <= i % 100 <= 14:
                return "few"
            return "many"
        case "fr":
            return "one" if i in (0, 1) else "many" if whole and i and i % 1_000_000 == 0 else "other"
        case "es" | "it" | "pt":
            return "one" if n == 1 else "many" if whole and i and i % 1_000_000 == 0 else "other"
        case "zh" | "ja" | "ko":
            return "other"
        case _:  # en, de and the like
            return "one" if n == 1 else "other"


def _stamp(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return 0


@functools.lru_cache(maxsize=512)
def _read(path: Path, stamp: int) -> dict:
    """One catalog file (`stamp`: its mtime, so an edit is read again); no file is an empty catalog."""
    if not stamp:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a catalog is a JSON object")
    return data


class Catalogs:
    """The strings of some catalog folders, each holding <lang>.json files: a language over English."""

    def __init__(self, folders: Iterable[Path]):
        self.folders = tuple(folders)
        self._missing: set[str] = set()

    def strings(self, lang: str) -> dict[str, str | dict]:
        merged: dict[str, str | dict] = {}
        for name in dict.fromkeys((DEFAULT, lang)):
            for folder in self.folders:
                path = folder / f"{name}.json"
                merged.update(_read(path, _stamp(path)))
        return merged

    def text(self, lang: str, key: str, params: Mapping[str, object] | None = None) -> str:
        """The string for `key` with its placeholders filled; the key itself (logged once) when no catalog
        has it. An unknown placeholder stays as it is."""
        params = params or {}
        value = self.strings(lang).get(key)
        if value is None:
            if key not in self._missing:
                self._missing.add(key)
                _log.warning("i18n: no string for %s", key)
            return key
        if isinstance(value, dict):
            n = params.get("n")
            form = plural(lang, n) if isinstance(n, int | float) else "other"
            value = value.get(form) or value.get("other", "")
        return PLACEHOLDER.sub(lambda m: str(params[m[1]]) if m[1] in params else m[0], str(value))

    def page(self, lang: str) -> dict:
        """What the page loads: the language, the ones to choose from and its strings over English."""
        lang = lang if lang in LANGS else DEFAULT
        return {"lang": lang, "langs": LANGS, "strings": self.strings(lang)}


def folders(features: Iterable) -> list[Path]:
    """Catalog folders of the shell and of these features (anything with a `folder`), by convention."""
    return [PACKAGE / "web" / "i18n", *(f.folder / "i18n" for f in features if f.folder)]


_registered: list[Path] = []  # catalog folders of registered features outside the package (a test's)


def register(paths: Iterable[Path]) -> None:
    """Catalog folders t() reads besides the package's: App registers its features' folders."""
    known = set(catalogs().folders)
    _registered.extend(p for p in dict.fromkeys(paths) if p not in known)
    catalogs.cache_clear()


@functools.cache
def catalogs() -> Catalogs:
    """What t() reads: the shell's catalogs, every feature's in the package and the registered ones."""
    return Catalogs([PACKAGE / "web" / "i18n", *sorted(PACKAGE.glob("features/*/i18n")), *_registered])


def t(lang: str, key: str, /, **params: object) -> str:
    """A catalog string in `lang`: t("ru", "todo.too_many", n=300)."""
    return catalogs().text(lang, key, params)


# ---- the request's language -------------------------------------------------------------------


def lang_of(headers) -> str:
    """The language a request asks for in X-SYA-Lang; English when it names none we have."""
    asked = (headers.get(HEADER) or "").strip() if headers is not None else ""
    return asked if asked in LANGS else DEFAULT


def request_lang() -> str:
    """The language of the request this thread answers (the host sets it); English outside a request."""
    return _request_lang.get()


@contextlib.contextmanager
def answering(lang: str) -> Iterator[None]:
    """The host answers a request in `lang`: request_lang() says so to the handlers it calls."""
    token = _request_lang.set(lang)
    try:
        yield
    finally:
        _request_lang.reset(token)


# ---- errors the page shows --------------------------------------------------------------------


class Problem(Exception):
    """An error for the person at the page: a catalog key and its params, not a sentence. Raise it anywhere,
    core included; the edge puts it into words: the host answers {"error": text} in the request's language
    with `status`, and str() (logs, MCP tool errors for agents) is English. `status` is not a placeholder."""

    def __init__(self, key: str, /, status: int = HTTPStatus.BAD_REQUEST, **params: object):
        super().__init__(key)
        self.key = key
        self.status = int(status)
        self.params = params

    def text(self, lang: str = DEFAULT) -> str:
        return t(lang, self.key, **self.params)

    def __str__(self) -> str:
        return self.text(DEFAULT)


def message(exc: BaseException, lang: str) -> str:
    """An exception in words for the page: a Problem in `lang`, anything else as it was raised."""
    return exc.text(lang) if isinstance(exc, Problem) else str(exc)
