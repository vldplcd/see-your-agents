import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from fakes import FakeIndex, pair

from see_your_agents.app import cli, server, watchdog
from see_your_agents.app.feature import Feature


@pytest.fixture
def base_url(tmp_path):
    seen = []
    # `probe` needs the paired browser for a POST; its /api/probe/open is declared open (guarded elsewhere).
    probe = Feature(
        id="probe",
        handle_post=lambda app, path, body, hdrs: seen.append((path, body)) or None,
        open_posts=(r"/api/probe/open",),
    )
    app = server.App(FakeIndex([]), [probe])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(app, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    header = pair(app, port)
    yield f"http://127.0.0.1:{port}", seen, header
    httpd.shutdown()


def post(url, headers):
    req = urllib.request.Request(url, data=b"{}", method="POST", headers=headers)
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_post_reaches_a_feature_only_from_the_page_itself(base_url):
    url, seen, pair_header = base_url
    page = {"Origin": url, "X-SYA": "1"}
    paired = {**page, **pair_header}

    assert post(f"{url}/api/probe/x", {"Origin": "http://evil.example", "X-SYA": "1"}) == 403
    assert post(f"{url}/api/probe/x", {"Origin": url}) == 403
    assert post(f"{url}/api/probe/x", page) == 403  # Origin and X-SYA but not the paired browser
    assert seen == []
    assert post(f"{url}/api/probe/x", paired) == 404  # paired: the feature declines, but it got through
    assert seen == [("/api/probe/x", b"{}")]
    assert post(f"{url}/api/elsewhere", paired) == 404 and len(seen) == 1  # no owner, no call


def test_an_open_post_skips_pairing_but_a_mutating_one_does_not(base_url):
    url, seen, _ = base_url
    page = {"Origin": url, "X-SYA": "1"}

    # the open path reaches the feature without the paired browser; a sibling path still needs it
    assert post(f"{url}/api/probe/open", page) == 404 and seen == [("/api/probe/open", b"{}")]
    assert post(f"{url}/api/probe/other", page) == 403 and len(seen) == 1


def test_health_and_board_carry_the_running_version(base_url):
    url, _, _ = base_url
    health = json.loads(urllib.request.urlopen(f"{url}/api/health").read())
    board = json.loads(urllib.request.urlopen(f"{url}/api/board").read())

    assert health == {"ok": True, "version": server.code_version()}
    assert board["version"] == health["version"] and health["version"]


def test_a_restart_is_asked_only_under_the_watchdog(tmp_path, monkeypatch):
    app = server.App(FakeIndex([]), [])
    stopped = threading.Event()
    app.shutdown = stopped.set
    monkeypatch.delenv(watchdog.ENV, raising=False)
    assert app.request_restart() is False and not app.restart_requested

    monkeypatch.setenv(watchdog.ENV, "1")
    assert app.request_restart() is True and app.restart_requested
    assert stopped.wait(5)


def test_serve_starts_the_watchdog_before_opening_the_index(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.watchdog, "exec_parent", lambda port, argv: seen.update(port=port, argv=argv))
    monkeypatch.setattr(cli, "Index", lambda *a: pytest.fail("the parent must not open the index"))
    cli.main(["serve", "--port", "8799", "--no-open"])
    assert seen == {"port": 8799, "argv": ["serve", "--port", "8799", "--no-open"]}
