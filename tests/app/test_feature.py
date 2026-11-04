"""The feature registry end to end, with the example feature of docs/ARCHITECTURE.md (fixtures/hello)."""

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from fakes import FakeApp, no_processes, pair
from fixtures import hello as hello_feature
from fixtures.hello import hello

from see_your_agents.app import cli, mcp, server
from see_your_agents.app.feature import Feature, check_registry
from see_your_agents.features import FEATURES
from see_your_agents.features.supervisor import launch

HELLO = hello_feature.FEATURE
ALL = (*FEATURES, HELLO)
SID = "s1"
RECORD = {"session_id": SID, "title": "Три слова тут", "cwd": "/w/p", "last_activity": "2026-11-01T00:00:00Z"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SYA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SYA_CACHE_DIR", str(tmp_path / "cache"))
    no_processes(monkeypatch)
    hello.names.clear()


PAIRED: dict[str, dict[str, str]] = {}  # base url -> the paired browser's header; a mutating POST needs it


@pytest.fixture
def base(tmp_path):
    app = FakeApp([RECORD], {}, ALL)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(app, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    PAIRED[url] = pair(app, port)
    yield url
    httpd.shutdown()
    PAIRED.pop(url, None)


def fetch(url, payload=None, headers=None):
    """(status, content type, body); a payload makes it a POST of that JSON."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def test_the_page_carries_every_feature_asset_in_registry_order():
    html = server.page(ALL)

    assert re.findall(r'<link rel="stylesheet" href="([^"]+)">', html) == ["fonts.css", "base.css"] + [
        f"{f.id}/{n}" for f in ALL for n in f.styles
    ]
    assert re.findall(r'<script src="([^"]+)"></script>', html) == ["i18n.js", "figures.js", "app.js"] + [
        f"{f.id}/{n}" for f in ALL for n in f.scripts
    ]


def test_with_a_tag_every_local_asset_url_is_versioned(base):
    """A copy something else served on the port while the dashboard was down is never loaded from cache."""
    html = server.page(ALL, "abc123.f00d")
    local = re.findall(r'(?:href|src)="((?!https?:|data:)[^"]+)"', html)
    assert local and all(u.endswith("?v=abc123.f00d") for u in local), local
    assert fetch(f"{base}/app.js?v=abc123.f00d")[0] == 200 and fetch(f"{base}/hello/hello.js?v=x")[0] == 200


def test_assets_are_served_only_when_the_manifest_lists_them(base):
    status, ctype, body = fetch(f"{base}/hello/hello.js")
    assert status == 200 and ctype.startswith("text/javascript") and b"SYA.feature" in body
    assert fetch(f"{base}/hello/hello.css")[1].startswith("text/css")
    assert fetch(f"{base}/app.js")[0] == 200
    for path in ("/hello/hello.py", "/hello/other.js", "/hello/../hello/hello.js", "/nope/x.js", "/hello.js"):
        assert fetch(base + path)[0] == 404, path


def test_the_page_loads_nothing_from_the_internet(base):
    """The fonts are bundled: opening the dashboard tells no other host about it."""
    assert not re.findall(r'(?:href|src)="(?:https?:)?//', server.page(ALL))
    remote = re.compile(r"@import|url\(\s*['\"]?(?:https?:)?//")
    for path in ["/fonts.css", "/base.css", *(f"/{f.id}/{n}" for f in ALL for n in f.styles)]:
        assert not remote.search(server.asset(ALL, path)[1].decode()), path
    fonts = set(re.findall(r"url\(([^)]+)\)", server.asset(ALL, "/fonts.css")[1].decode()))
    assert fonts and all(u.startswith("fonts/") for u in fonts), fonts
    for url in fonts:
        status, ctype, body = fetch(f"{base}/{url}")
        assert (status, ctype, body[:4]) == (200, "font/woff2", b"wOF2"), url
    for path in ("/fonts/OFL-onest.txt", "/fonts/onest-latin.ttf", "/fonts/../app.js", "/fonts/sub/x.woff2"):
        assert fetch(base + path)[0] == 404, path


# One representative mutating POST per feature that writes; none is in the open allowlist, so each needs the
# paired browser. The host guard runs before the handler, so an empty body is enough to reach (or be stopped
# before) it.
MUTATING_POSTS = [
    "/api/awake",
    "/api/builder/x-0000/reject",
    "/api/chat/s1/stop",
    "/api/groups",
    "/api/launch",
    "/api/looks",
    "/api/todo",
    "/api/approvals/0123456789abcdef/decide",
]


@pytest.mark.parametrize("path", MUTATING_POSTS)
def test_a_mutating_post_needs_the_paired_browser(base, path):
    page = {"Origin": base, "X-SYA": "1", "Content-Type": "application/json"}
    forged = {**page, "X-SYA-Pair": "not-a-real-secret"}
    # no secret, and a fake secret: refused before the feature sees it
    assert fetch(f"{base}{path}", {}, page)[0] == 403
    assert fetch(f"{base}{path}", {}, forged)[0] == 403
    # the paired browser gets past the guard (the handler then answers 400/404/409 — never a pairing 403)
    assert fetch(f"{base}{path}", {}, {**page, **PAIRED[base]})[0] != 403


def test_routes_go_to_the_owner_and_posts_keep_the_page_guard(base):
    page = {"Origin": base, "X-SYA": "1", "Content-Type": "application/json"}
    paired = {**page, **PAIRED[base]}

    assert json.loads(fetch(f"{base}/api/hello")[2]) == {"names": []}
    assert fetch(f"{base}/api/hello", {"name": "Вася"}, {"Origin": base})[0] == 403
    assert fetch(f"{base}/api/hello", {"name": "Вася"}, page)[0] == 403  # Origin and X-SYA but not paired
    status, _, body = fetch(f"{base}/api/hello", {"name": ""}, paired)
    assert (status, json.loads(body)) == (400, {"error": "name: a non-empty string of up to 40 characters"})
    russian = fetch(f"{base}/api/hello", {"name": ""}, {**paired, "X-SYA-Lang": "ru"})[2]
    assert json.loads(russian) == {"error": "name: непустая строка до 40 символов"}
    status, _, body = fetch(f"{base}/api/hello", {"name": "Вася"}, paired)
    assert status == 200 and json.loads(body) == {"names": ["Вася"]}
    assert fetch(f"{base}/api/hello/elsewhere")[0] == 404


def test_cards_and_details_carry_the_feature_fields(base):
    board = json.loads(fetch(f"{base}/api/board")[2])
    detail = json.loads(fetch(f"{base}/api/session/{SID}")[2])

    assert board["cards"][0]["hello_words"] == 3 and "delivery" in board["cards"][0]
    assert detail["hello_words"] == 3
    assert "supervisor_id" in board and "digest" in board  # the supervisor's board_extra


def test_the_tool_is_served_over_mcp(base):
    hello.names.append("Петя")
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "hello_names"}}
    headers = {"Authorization": f"Bearer {mcp.tokens()['read']}", "Content-Type": "application/json"}
    body = json.loads(fetch(f"{base}/mcp", call, headers)[2])

    assert json.loads(body["result"]["content"][0]["text"]) == {"names": ["Петя"]}


def test_the_supervisor_may_call_it_and_is_told_about_it():
    assert "mcp__sya__hello_names" in launch.permissions(ALL)["allow"]
    assert "mcp__sya__hello_names" in launch.read_tools(ALL)
    assert launch.prompt(ALL).endswith((HELLO.folder / "supervisor.md").read_text())


def test_the_subcommand_runs(monkeypatch, capsys):
    monkeypatch.setattr(cli, "FEATURES", ALL)
    cli.main(["hello"])
    assert capsys.readouterr().out == "Hello\n"


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (Feature(id="hello"), "ids repeat"),
        (Feature(id="Bad_id"), "must match"),
        (Feature(id="search", handle_get=lambda app, path, query: None), "answered by the host"),
        (Feature(id="x", scripts=("x.css",)), "must be"),
        (Feature(id="x", scripts=("x.js",)), "missing"),
        (Feature(id="x", tools=(hello.TOOL,)), "tool names repeat"),
        (Feature(id="x", open_posts=("/api/builder/x/accept",)), "must be under /api/x"),
        (Feature(id="x", open_posts=("/api/x/(",)), "not a regex"),
        # each would reach past the /api/x prefix: an alternation opening another feature's route, anchors
        (Feature(id="x", open_posts=("/api/x/a|/api/builder/.*/accept",)), "may not use"),
        (Feature(id="x", open_posts=("/api/x/(a|b)",)), "may not use"),
        (Feature(id="x", open_posts=("/api/x/a$",)), "may not use"),
        (Feature(id="x", open_posts=("/api/x/^a",)), "may not use"),
        (Feature(id="x", open_posts=(r"/api/x/a\Z",)), "may not use"),
    ],
)
def test_a_registry_the_host_could_serve_wrongly_is_refused(bad, message):
    with pytest.raises(ValueError, match=message):
        check_registry((*ALL, bad))


def test_the_hosts_open_allowlist_matches_the_open_routes_and_no_other_post():
    """The host's joined allowlist (each entry in a group of its own, full-matched) for the real registry."""
    allowed = server.open_post_pattern(FEATURES)
    sid = "0123456789abcdef"
    open_paths = [
        "/api/pair",
        "/api/pair/start",
        "/api/pair/forget",
        "/api/approvals/hello",
        "/api/approvals/ask",
        f"/api/approvals/{sid}/wait",
        f"/api/approvals/{sid}/withdraw",
    ]
    paired_paths = [
        f"/api/approvals/{sid}/decide",
        "/api/approvals/hello/x",
        f"/api/approvals/{sid}/wait/x",
        "/api/builder/x-1/accept",
        "/api/launch",
        "/api/chat/s1/send",
        "/api/pairx",
    ]
    assert all(allowed.fullmatch(p) for p in open_paths)
    assert not any(allowed.fullmatch(p) for p in paired_paths)
