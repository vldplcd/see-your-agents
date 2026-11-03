"""Browser pairing: a one-time code that only the dashboard's own tab on 127.0.0.1 gets becomes a secret the
page keeps in localStorage and sends as X-SYA-Pair: the proof that a request comes from a person at the page,
not from a process that forges headers, a page on another port, or whatever answers on localhost."""

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from fakes import FakeApp, FakeIndex, no_processes

from see_your_agents import config_dir
from see_your_agents.app import pairing, server


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    no_processes(monkeypatch)
    monkeypatch.setattr(pairing, "START_EVERY_S", 0)
    # The flow is the same on every OS; only opening the browser is macOS's, and conftest records the tab
    # instead. A configured opener makes pairing supported elsewhere too, so these tests run on Linux CI.
    monkeypatch.setenv(pairing.OPENER_ENV, "unused")


def serve(app):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(app, port)
    app.pairing = pairing.Pairing(port)  # App() learns the port at construction; here it is known only now
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


@pytest.fixture
def base():
    httpd, url = serve(FakeApp([], {}, ()))
    yield url
    httpd.shutdown()


def call(url, payload=None, headers=None):
    """(status, headers, body) of a GET, or of a POST when there is a payload."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data, {"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def page(base, secret=""):
    return {"Origin": base, "X-SYA": "1", **({pairing.HEADER: secret} if secret else {})}


def localhost(base):
    """The same server addressed as localhost: what a process on [::1]:<port> would have the browser use."""
    host = base.replace("127.0.0.1", "localhost").removeprefix("http://")
    return {"Host": host, "Origin": f"http://{host}", "X-SYA": "1"}


def code_from(tab):
    return re.fullmatch(r"http://127\.0\.0\.1:\d+/#pair=([A-Za-z0-9_-]+)", tab)[1]


def paired(base, secret, headers=None):
    return json.loads(call(f"{base}/api/pair", headers={pairing.HEADER: secret, **(headers or {})})[2])[
        "paired"
    ]


def pair(base, opened_tabs):
    """The real flow: the page asks, the server opens a tab with a code, that tab trades it for the secret."""
    assert call(f"{base}/api/pair/start", {}, page(base))[0] == 200
    status, headers, body = call(f"{base}/api/pair", {"code": code_from(opened_tabs[-1])}, page(base))
    assert status == 200 and "Set-Cookie" not in headers
    return json.loads(body)["secret"]


def test_pair_start_wants_the_page_headers_and_keeps_the_code_out_of_the_reply(base, opened_tabs):
    assert call(f"{base}/api/pair/start", {}, {})[0] == 403  # no Origin, no X-SYA: refused by the host
    assert call(f"{base}/api/pair/start", {}, {"Origin": base})[0] == 403
    assert opened_tabs == []
    status, headers, body = call(f"{base}/api/pair/start", {}, page(base))
    [tab] = opened_tabs
    code = code_from(tab)
    assert status == 200 and len(code) >= 43 and code.encode() not in body and code not in str(headers)


def test_localhost_never_gets_a_pairing_tab_nor_counts_as_paired(base, opened_tabs):
    status, _, body = call(f"{base}/api/pair/start", {}, localhost(base))
    assert status == 403 and "127.0.0.1" in json.loads(body)["error"] and opened_tabs == []
    secret = pair(base, opened_tabs)
    assert opened_tabs[-1].startswith(f"{base}/#pair=")  # always 127.0.0.1 and this port, never from Host
    assert paired(base, secret) and not paired(base, secret, localhost(base))
    assert call(f"{base}/api/pair", {"code": "x" * 43}, localhost(base))[0] == 403


def test_the_secret_counts_only_as_the_header_never_as_a_cookie(base, opened_tabs):
    secret = pair(base, opened_tabs)
    assert paired(base, secret)
    assert not paired(base, "") and not paired(base, secret[:-1] + ("A" if secret[-1] != "A" else "B"))
    status, _, body = call(f"{base}/api/pair", headers={"Cookie": f"{pairing.HEADER}={secret}"})
    assert json.loads(body) == {"paired": False}


def test_a_code_is_single_use_and_a_wrong_guess_leaves_it_valid(base, opened_tabs):
    call(f"{base}/api/pair/start", {}, page(base))
    code = code_from(opened_tabs[-1])
    assert call(f"{base}/api/pair", {"code": "x" * 43}, page(base))[0] == 403  # a guess does not spend it
    assert call(f"{base}/api/pair", {"code": code}, {})[0] == 403  # never without the page's headers
    assert call(f"{base}/api/pair", {"code": code}, page(base))[0] == 200
    assert call(f"{base}/api/pair", {"code": code}, page(base))[0] == 403


def test_one_code_at_a_time_and_it_expires():
    now = [0.0]
    p = pairing.Pairing(8765, clock=lambda: now[0])
    old, current = p.new_code(), p.new_code()
    assert p.redeem(old) is None  # a new code replaces the open one
    assert pairing.Pairing(8765).redeem(current) is None  # codes live in the process that made them
    late = p.new_code()
    now[0] = pairing.CODE_TTL_S + 1
    assert p.redeem(late) is None
    assert p.redeem(p.new_code())


def test_a_pairing_tab_opens_at_most_once_a_minute(monkeypatch, opened_tabs):
    monkeypatch.setattr(pairing, "START_EVERY_S", 60.0)
    now = [0.0]
    p = pairing.Pairing(8765, clock=lambda: now[0])
    p.start()
    now[0] = 59
    with pytest.raises(pairing.PairingError) as refused:
        p.start()
    problem = refused.value  # a key and params, worded by the host in the page's language
    assert (problem.key, problem.params, problem.status) == ("app.pair.too_soon", {"n": 2}, 409)
    now[0] = 61
    p.start()
    assert len(opened_tabs) == 2


def test_pairing_is_refused_off_macos(base, opened_tabs, monkeypatch):
    monkeypatch.setattr(pairing.sys, "platform", "linux")
    monkeypatch.delenv(pairing.OPENER_ENV, raising=False)
    status, _, body = call(f"{base}/api/pair/start", {}, page(base))
    assert status == 409 and "only on macOS" in json.loads(body)["error"] and opened_tabs == []
    russian = {**page(base), "X-SYA-Lang": "ru"}  # the page's language words the refusal
    assert "только на macOS" in json.loads(call(f"{base}/api/pair/start", {}, russian)[2])["error"]


def test_a_restart_forgets_every_pairing(opened_tabs):
    """A secret a squatter read while the port was free belongs to a process that is gone."""
    app = FakeApp([], {}, ())
    httpd, url = serve(app)
    secret = pair(url, opened_tabs)
    assert paired(url, secret)
    app.pairing = pairing.Pairing(int(url.rsplit(":", 1)[1]))  # what `sya serve` builds in a new process
    try:
        assert not paired(url, secret)
        assert call(f"{url}/api/pair/forget", {}, page(url, secret))[0] == 403
    finally:
        httpd.shutdown()
    assert not (config_dir() / "paired.json").exists()  # nothing on disk to outlive the process


def test_only_a_few_tabs_are_remembered_the_least_recently_used_goes():
    now = [0.0]
    p = pairing.Pairing(8765, clock=lambda: now[0])
    on = {"Host": "127.0.0.1:8765"}

    def redeem():
        now[0] += 1
        return p.redeem(p.new_code())

    secrets = [redeem() for _ in range(pairing.MAX_TABS)]
    now[0] += 1
    assert p.paired({**on, pairing.HEADER: secrets[0]})  # the first one is used again: now the freshest
    newest = redeem()
    assert not p.paired({**on, pairing.HEADER: secrets[1]})  # the least recently used went
    assert all(p.paired({**on, pairing.HEADER: s}) for s in (secrets[0], *secrets[2:], newest))


def test_unpairing_forgets_every_browser_and_needs_a_paired_one(base, opened_tabs):
    first, second = pair(base, opened_tabs), pair(base, opened_tabs)
    assert call(f"{base}/api/pair/forget", {}, page(base))[0] == 403  # not paired: cannot unpair others
    assert call(f"{base}/api/pair/forget", {}, page(base, second))[0] == 200
    assert not paired(base, first) and not paired(base, second)


def test_html_is_never_framed_and_a_plan_only_by_the_dashboard_sandboxed(base):
    status, headers, _ = call(f"{base}/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert (
        headers["X-Frame-Options"] == "DENY"
        and headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    )
    index = FakeIndex(
        [{"session_id": "s1", "title": "t", "last_activity": "", "plans": [{"text": "<b>plan</b>"}]}]
    )
    httpd, url = serve(server.App(index, ()))
    try:
        status, headers, body = call(f"{url}/api/plan?session=s1&i=0")
    finally:
        httpd.shutdown()
    assert (status, body) == (200, b"<b>plan</b>") and headers["X-Frame-Options"] == "SAMEORIGIN"
    assert headers["Content-Security-Policy"] == "sandbox allow-scripts; frame-ancestors 'self'"
