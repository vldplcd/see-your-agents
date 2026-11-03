"""UI languages (docs/i18n.md): catalogs and their merge, the English fallback, plurals, missing keys, the
request's language, and a scan that keeps the code and the catalogs in step."""

import ast
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fakes import FakeApp

from see_your_agents import i18n
from see_your_agents.app import server
from see_your_agents.app.feature import Feature, check_registry
from see_your_agents.app.replies import json_reply
from see_your_agents.features import FEATURES

PACKAGE = i18n.PACKAGE


def write(folder: Path, lang: str, strings: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{lang}.json").write_text(json.dumps(strings, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def catalogs(tmp_path):
    shell, feature = tmp_path / "web", tmp_path / "x"
    write(shell, "en", {"app.hi": "Hi, {name}", "app.days": {"one": "{n} day", "other": "{n} days"}})
    days = {"one": "{n} день", "few": "{n} дня", "many": "{n} дней", "other": "{n} дня"}
    write(shell, "ru", {"app.hi": "Привет, {name}", "app.days": days})
    write(feature, "en", {"x.only_en": "English only", "x.both": "Both"})
    write(feature, "ru", {"x.both": "Оба"})
    return i18n.Catalogs([shell, feature])


# ---- catalogs, fallback, plurals, missing keys ------------------------------------------------


def test_catalogs_merge_the_shell_and_the_features_a_language_over_english(catalogs):
    ru = catalogs.strings("ru")

    assert ru["app.hi"] == "Привет, {name}" and ru["x.both"] == "Оба"
    assert ru["x.only_en"] == "English only"  # missing in ru: the English string
    assert catalogs.text("ru", "app.hi", {"name": "Вася"}) == "Привет, Вася"
    assert catalogs.text("en", "app.hi", {}) == "Hi, {name}"  # an unknown placeholder stays visible


def test_an_edited_catalog_is_read_again(catalogs, tmp_path):
    assert catalogs.text("ru", "x.both") == "Оба"
    path = tmp_path / "x" / "ru.json"
    write(tmp_path / "x", "ru", {"x.both": "Обе"})
    os.utime(path, ns=(path.stat().st_mtime_ns + 10**9,) * 2)  # a later mtime, as an editor would leave

    assert catalogs.text("ru", "x.both") == "Обе"


@pytest.mark.parametrize(
    ("lang", "n", "text"),
    [
        ("ru", 1, "1 день"), ("ru", 21, "21 день"), ("ru", 3, "3 дня"), ("ru", 24, "24 дня"),
        ("ru", 5, "5 дней"), ("ru", 11, "11 дней"), ("ru", 12, "12 дней"), ("ru", 111, "111 дней"),
        ("ru", 0, "0 дней"), ("ru", 1.5, "1.5 дня"),
        ("en", 1, "1 day"), ("en", 0, "0 days"), ("en", 2, "2 days"), ("en", 21, "21 days"),
    ],
)  # fmt: skip
def test_plurals_pick_the_form_of_the_language(catalogs, lang, n, text):
    assert catalogs.text(lang, "app.days", {"n": n}) == text


# The CLDR plural categories of each UI language, as the browser's (CLDR 48)
# Intl.PluralRules(lang).resolvedOptions().pluralCategories gives them: exactly the forms every plural
# object in that language's catalogs holds.
PLURALS = {
    "en": {"one", "other"},
    "ru": {"one", "few", "many", "other"},
    "es": {"one", "many", "other"},
    "de": {"one", "other"},
    "fr": {"one", "many", "other"},
    "zh-CN": {"other"},
}


@pytest.mark.parametrize(
    ("lang", "counts", "forms"),
    [
        ("ru", (1, 2, 5, 11, 22, 0, 1.5), ("one", "few", "many", "many", "few", "many", "other")),
        ("en", (1, 0, 2, 1.5), ("one", "other", "other", "other")),
        ("de", (1, 0, 2, 1.5, 1_000_000), ("one", "other", "other", "other", "other")),
        ("es", (1, 0, 2, 1.5, 1_000_000, 2_000_000, 1_000_001),
         ("one", "other", "other", "other", "many", "many", "other")),
        ("fr", (0, 1, 1.5, 2, 1_000_000, 1_000_001), ("one", "one", "one", "other", "many", "other")),
        ("zh-CN", (0, 1, 2, 1_000_000), ("other", "other", "other", "other")),
    ],
)  # fmt: skip
def test_the_plural_rules_are_the_cldr_ones_the_page_uses(lang, counts, forms):
    """What Intl.PluralRules(lang).select() gives in the browser for these counts."""
    assert tuple(i18n.plural(lang, n) for n in counts) == forms


def test_every_language_has_its_plural_categories_and_the_server_rules_give_exactly_them():
    assert PLURALS.keys() == i18n.LANGS.keys()
    for lang, categories in PLURALS.items():
        assert {i18n.plural(lang, n) for n in (0, 1, 2, 3, 5, 11, 21, 22, 1.5, 1_000_000)} == categories, lang


def test_a_key_no_catalog_has_shows_itself_and_is_logged_once(catalogs, caplog):
    with caplog.at_level(logging.WARNING, logger="see_your_agents.i18n"):
        assert catalogs.text("ru", "x.nope") == "x.nope"
        assert catalogs.text("en", "x.nope") == "x.nope"

    assert [r.getMessage() for r in caplog.records] == ["i18n: no string for x.nope"]


def test_a_problem_carries_a_key_and_is_worded_at_the_edge():
    problem = i18n.Problem("todo.too_many", HTTPStatus.CONFLICT, n=300)

    assert (problem.key, problem.params, problem.status) == ("todo.too_many", {"n": 300}, 409)
    assert problem.text("ru") == "в списке уже 300 дел: очисти сделанные"
    assert str(problem) == "The list already has 300 to-dos: clear the done ones"  # logs and agents: English
    assert i18n.message(ValueError("как было"), "en") == "как было"


@pytest.mark.parametrize("lang", [lang for lang in i18n.LANGS if lang != i18n.DEFAULT])
def test_every_language_words_a_problem_in_its_own_words_with_the_count(lang):
    for n in (1, 2, 5, 1_000_000):
        problem = i18n.Problem("todo.too_many", HTTPStatus.CONFLICT, n=n)
        assert str(n) in problem.text(lang) and problem.text(lang) != str(problem), (lang, n)


# ---- the request's language -------------------------------------------------------------------


def _probe_get(app, path, query):
    if path == "/api/probe/fail":
        raise i18n.Problem("app.pair.not_paired", HTTPStatus.FORBIDDEN)
    return json_reply({"lang": i18n.request_lang()}) if path == "/api/probe" else None


PROBE = Feature(id="probe", handle_get=_probe_get)


@pytest.fixture
def base():
    app = FakeApp([], {}, (*FEATURES, PROBE))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(app, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


def get(url, lang=None):
    req = urllib.request.Request(url, headers={i18n.HEADER: lang} if lang else {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_the_header_picks_the_language_handlers_see_english_otherwise(base):
    assert json.loads(get(f"{base}/api/probe", "ru")[1]) == {"lang": "ru"}
    assert json.loads(get(f"{base}/api/probe")[1]) == {"lang": "en"}
    assert json.loads(get(f"{base}/api/probe", "xx")[1]) == {"lang": "en"}  # a language we do not have
    assert i18n.request_lang() == "en"  # outside a request


def test_a_problem_a_handler_raises_is_the_pages_error_in_its_language(base):
    status, body = get(f"{base}/api/probe/fail", "ru")

    assert (status, json.loads(body)) == (403, {"error": "браузер не привязан"})
    assert json.loads(get(f"{base}/api/probe/fail")[1]) == {"error": "This browser isn’t paired"}
    for lang in i18n.LANGS:  # every language of the select: its own words
        error = i18n.t(lang, "app.pair.not_paired")
        assert json.loads(get(f"{base}/api/probe/fail", lang)[1]) == {"error": error}
    assert len({i18n.t(lang, "app.pair.not_paired") for lang in i18n.LANGS}) == len(i18n.LANGS)


def test_the_page_loads_its_language_over_english(base):
    status, body = get(f"{base}/i18n/ru.js")
    payload = json.loads(body.removeprefix("window.SYA_I18N = ").rstrip().removesuffix(";"))

    assert status == 200 and payload["lang"] == "ru" and payload["langs"] == i18n.LANGS
    assert payload["strings"]["board.title"] == "Доска" and payload["strings"]["todo.title"] == "Дела"
    assert '"lang": "en"' in get(f"{base}/i18n/xx.js")[1]  # an unknown language gets English
    assert get(f"{base}/i18n/../x.js")[0] == 404


def test_a_feature_catalog_with_a_key_of_someone_else_is_refused(tmp_path):
    write(tmp_path / "i18n", "en", {"board.title": "Mine now"})

    with pytest.raises(ValueError, match="x.json|x\\.\\* keys"):
        check_registry((Feature(id="x", folder=tmp_path),))


# ---- the code and the catalogs in step --------------------------------------------------------

OWNERS = {"app", *(p.name for p in (PACKAGE / "features").iterdir() if (p / "__init__.py").is_file())}
KEY = re.compile(rf"(?:{'|'.join(sorted(OWNERS))})\.[a-z0-9_]+(?:\.[a-z0-9_]+)*")
FILE_NAME = re.compile(r"\.(?:js|css|json|py|md|html)(?:\.|$)")  # "todo.json", "looks.json.bad": not keys
JS_KEY = re.compile(r"""\bt\(\s*"([^"]+)"|data-i18n(?:-[a-z-]+)?="([^"]+)\"""")
PAGE_FILES = ("web/*.js", "web/*.html", "features/*/web/*.js")


def catalog_folders() -> list[Path]:
    return [PACKAGE / "web" / "i18n", *sorted(PACKAGE.glob("features/*/i18n"))]


def read(folder: Path, lang: str) -> dict:
    path = folder / f"{lang}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def is_key(text: str) -> bool:
    return bool(KEY.fullmatch(text)) and not FILE_NAME.search(text)


def used_keys() -> set[str]:
    """Keys the code names literally: t("…") and data-i18n…="…" in the page, key strings in Python."""
    found = set()
    for path in (p for pattern in PAGE_FILES for p in PACKAGE.glob(pattern)):
        found |= {a or b for a, b in JS_KEY.findall(path.read_text(encoding="utf-8")) if is_key(a or b)}
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and is_key(node.value):
                found.add(node.value)
    return found


def placeholders(value) -> set[str]:
    forms = value.values() if isinstance(value, dict) else [value]
    return {name for form in forms for name in i18n.PLACEHOLDER.findall(form)}


def test_every_key_the_code_uses_has_an_english_string_and_every_english_string_is_used():
    english = {k for folder in catalog_folders() for k in read(folder, "en")}
    used = used_keys()

    assert not used - english, f"no English string for {sorted(used - english)}"
    assert not english - used, f"strings no code uses: {sorted(english - used)}"


def test_catalogs_hold_only_their_owners_keys_and_nothing_english_lacks():
    for folder in catalog_folders():
        owner = "app" if folder.parent.name == "web" else folder.parent.name
        english = read(folder, "en")
        for path in sorted(folder.glob("*.json")):
            strings = read(folder, path.stem)
            assert path.stem in i18n.LANGS, f"{path}: not a language of i18n.LANGS"
            assert all(k.startswith(f"{owner}.") for k in strings), (
                f"{path}: a key without the {owner}. prefix"
            )
            assert not strings.keys() - english.keys(), (
                f"{path}: keys English lacks: {strings.keys() - english.keys()}"
            )
            for key, value in strings.items():
                assert placeholders(value) == placeholders(english[key]), f"{path}: {key} placeholders differ"


def test_strings_are_plain_text_and_plurals_have_exactly_the_forms_of_their_language():
    """Catalog text goes into HTML as is (docs/i18n.md): no markup, no character that would need escaping. A
    plural object holds every CLDR form of its language (PLURALS) and no other; a plain string stands for a
    word that does not change with the count (“{n} min”)."""
    for folder in catalog_folders():
        for path in sorted(folder.glob("*.json")):
            for key, value in read(folder, path.stem).items():
                forms = value if isinstance(value, dict) else {"other": value}
                assert set(forms) == (PLURALS[path.stem] if isinstance(value, dict) else {"other"}), (
                    f"{path}: {key} has the forms {sorted(forms)}, not {sorted(PLURALS[path.stem])}"
                )
                plain = all(isinstance(s, str) and not set(s) & set('<>&"') for s in forms.values())
                assert plain, f"{path}: {key} is not plain text"


def test_every_language_has_the_shell_catalog():
    assert all((PACKAGE / "web" / "i18n" / f"{lang}.json").is_file() for lang in i18n.LANGS)
    assert i18n.DEFAULT in i18n.LANGS
