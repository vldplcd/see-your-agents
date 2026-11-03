"""MR states: glab JSON parsing, the read-only calls, the cache and its timing, errors, the board never
waiting for glab, and marking done a stopped session whose MRs are all merged. glab never runs here."""

import json
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeApp, no_processes

from see_your_agents.core import done
from see_your_agents.core.providers.base import LiveSession
from see_your_agents.features.mr import glab, watch
from see_your_agents.i18n import answering

FIXTURES = Path(__file__).parent.parent / "fixtures" / "glab"
HOST = "gitlab.example.com"
URL = f"https://{HOST}/acme/pipelines/-/merge_requests/1140"
URL2 = f"https://{HOST}/acme/infra/deploy-tools/-/merge_requests/7"
MR = "projects/acme%2Fpipelines/merge_requests/1140"
MR2 = "projects/acme%2Finfra%2Fdeploy-tools/merge_requests/7"
DISC = MR + "/discussions?per_page=100"
OPENED, MERGED, APPROVALS, DISCUSSIONS = (
    (FIXTURES / name).read_text()
    for name in ("mr-opened.json", "mr-merged.json", "approvals.json", "discussions.json")
)
NOT_FOUND = (1, '{"message":"404 Not found"}', "glab: 404 Not found (HTTP 404)")
EXPIRED = (1, '{"error":"invalid_token"}', "glab: HTTP 401")


def ok(body: str) -> tuple[int, str, str]:
    return 0, body, ""


def opened_without_notes() -> str:
    return OPENED.replace('"user_notes_count": 4', '"user_notes_count": 0')


class Glab:
    """`glab api` as a table: endpoint -> (exit code, stdout, stderr) or an exception. Records every argv."""

    def __init__(self, answers: dict):
        self.answers, self.argv = answers, []

    def __call__(self, argv):
        self.argv.append(argv)
        answer = self.answers[argv[-1]]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def endpoints(self):
        return [a[-1] for a in self.argv]


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


def record(sid="s1", urls=(URL,), last=None) -> dict:
    return {
        "session_id": sid,
        "title": sid,
        "cwd": "/w/p",
        "last_activity": last or iso(time.time()),
        "prs": [{"url": u, "number": int(u.rsplit("/", 1)[1]), "repo": ""} for u in urls],
    }


def on_path(name: str) -> str:
    return f"/usr/bin/{name}"


def live(sid="s1") -> LiveSession:
    return LiveSession(provider="claude", session_id=sid, kind="interactive", status="idle")


@pytest.fixture
def make(monkeypatch):
    """A watcher on a fake glab that never starts its thread; the test runs cycle() itself."""

    def build(answers, clock=None):
        w = watch.Watcher(run=Glab(answers), which=on_path, clock=clock or Clock(), background=False)
        monkeypatch.setattr(watch, "WATCHER", w)
        return w

    return build


def mr_of(card: dict, url=URL) -> dict:
    return next(i for i in card["mr"]["items"] if i["url"] == url)


# --- links and parsing --------------------------------------------------------------------------------
def test_only_gitlab_mr_links_are_refs():
    assert glab.ref_of(URL) == glab.Ref(HOST, "acme/pipelines", 1140)
    assert glab.ref_of(URL2).project == "acme/infra/deploy-tools"
    assert glab.ref_of(f"https://{HOST}/acme/pipelines/merge_requests/3").iid == 3  # an old link without "/-"
    assert glab.ref_of(f"https://{HOST}/acme/pipelines/-/merge_requests/3#note_1").iid == 3
    for url in ("https://github.com/o/r/pull/5", "https://-x/de/a/-/merge_requests/1", "", None, 5):
        assert glab.ref_of(url) is None
    assert glab.ref_of(URL2).argv() == ["glab", "api", "--hostname", HOST, MR2]


def test_card_has_no_mr_field_without_a_gitlab_link(make):
    make({})
    rec = {**record(), "prs": [{"url": "https://github.com/o/r/pull/5", "number": 5}]}
    assert watch.card_fields(rec, None) == {}


def test_parses_glab_json():
    assert glab.parse_mr(json.loads(OPENED)) == {
        "state": "opened",
        "draft": True,
        "title": "PROJ-1: retry limits and a run report",
        "pipeline": "success",
        "notes": 4,
    }
    assert glab.parse_mr({"state": "opened", "head_pipeline": None})["pipeline"] == ""
    assert glab.parse_approvals(json.loads(APPROVALS)) == {
        "approved": False,
        "approvals_left": 1,
        "approvals_required": 1,
    }
    # two open threads; system notes, the resolved thread and plain comments do not count
    assert glab.unresolved(json.loads(DISCUSSIONS)) == 2
    assert glab.unresolved({"message": "odd"}) == 0


# --- calls --------------------------------------------------------------------------------------------
def test_each_mr_costs_only_the_calls_its_state_needs():
    merged = Glab({MR: ok(MERGED)})
    state, calls = glab.fetch(merged, glab.ref_of(URL))
    assert (state["state"], calls, merged.endpoints) == ("merged", 1, [MR])
    assert state["approved"] is None and state["unresolved"] is None

    quiet = Glab({MR: ok(opened_without_notes()), MR + "/approvals": ok(APPROVALS)})
    state, calls = glab.fetch(quiet, glab.ref_of(URL))
    assert (calls, state["unresolved"], state["approvals_left"]) == (2, 0, 1)

    full = Glab({MR: ok(OPENED), MR + "/approvals": ok(APPROVALS), DISC: ok(DISCUSSIONS)})
    state, calls = glab.fetch(full, glab.ref_of(URL))
    assert (calls, state["unresolved"], state["pipeline"], state["draft"]) == (3, 2, "success", True)


def test_calls_are_plain_gets():
    """No method, field, body or form flag: `glab api` sends GET without them."""
    full = Glab({MR: ok(OPENED), MR + "/approvals": ok(APPROVALS), DISC: ok(DISCUSSIONS)})
    glab.fetch(full, glab.ref_of(URL))
    for argv in full.argv:
        assert argv[:4] == ["glab", "api", "--hostname", HOST] and len(argv) == 5
        assert argv[4].startswith("projects/")


def test_a_failed_side_call_keeps_the_mr():
    g = Glab({MR: ok(OPENED), MR + "/approvals": NOT_FOUND, DISC: ok(DISCUSSIONS)})
    state, calls = glab.fetch(g, glab.ref_of(URL))
    assert state["state"] == "opened" and state["approved"] is None and state["unresolved"] == 2
    assert glab.words(state["error"], "en") == "approvals: MR not found, or no access to it"
    assert glab.words(state["error"], "ru") == "одобрения: MR не найден или нет доступа"


def test_run_has_a_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(glab.subprocess, "run", fake_run)
    with pytest.raises(glab.GlabError) as err:
        glab.call(glab.run, glab.ref_of(URL))
    assert seen["timeout"] == glab.TIMEOUT_S and seen["stdin"] == subprocess.DEVNULL
    assert err.value.host and str(err.value) == "GitLab didn’t answer in 8 s"


# --- the cache ----------------------------------------------------------------------------------------
def test_merged_is_asked_once_open_every_minute(make):
    clock = Clock()
    w = make(
        {
            MR: ok(opened_without_notes()),
            MR + "/approvals": ok(APPROVALS),
            MR2: ok(MERGED),
        },
        clock,
    )
    rec = record(urls=(URL, URL2))
    card = watch.card_fields(rec, live())
    assert mr_of(card)["state"] == "" and card["mr"]["off"] == ""  # nothing asked yet
    assert w.cycle() == 3
    card = watch.card_fields(rec, live())
    assert (mr_of(card)["state"], mr_of(card, URL2)["state"]) == ("opened", "merged")
    assert card["mr"]["all_merged"] is False

    clock.now += watch.OPEN_S - 1
    watch.card_fields(rec, live())
    assert w.cycle() == 0
    clock.now += 2
    watch.card_fields(rec, live())
    assert w.cycle() == 2  # the open one again; the merged one never
    clock.now += 10 * watch.OPEN_S
    watch.card_fields(rec, live())
    w.cycle()
    assert w.run.endpoints.count(MR2) == 1


def test_mrs_of_old_sessions_are_asked_rarely(make):
    clock = Clock()
    w = make({MR: ok(opened_without_notes()), MR + "/approvals": ok(APPROVALS)}, clock)
    old = record(last=iso(clock.now - 10 * 86400))
    watch.card_fields(old, None)
    assert w.cycle() == 2
    clock.now += watch.OPEN_S * 2
    watch.card_fields(old, None)
    assert w.cycle() == 0
    clock.now += watch.COLD_S
    watch.card_fields(old, None)
    assert w.cycle() == 2


def test_mrs_no_board_shows_are_not_asked(make):
    clock = Clock()
    w = make({MR: ok(opened_without_notes()), MR + "/approvals": ok(APPROVALS)}, clock)
    watch.card_fields(record(), live())
    w.cycle()
    clock.now += watch.SEEN_S + 1
    assert w.cycle() == 0


def test_states_survive_a_restart(make):
    w = make({MR: ok(MERGED)})
    watch.card_fields(record(), live())
    w.cycle()
    again = make({})  # a fresh process: no answers at all, so any call would fail the test
    card = watch.card_fields(record(), live())
    assert mr_of(card)["state"] == "merged" and mr_of(card)["checked_at"]
    assert again.cycle() == 0


def test_a_cycle_makes_a_bounded_number_of_calls(make):
    urls = [f"https://{HOST}/de/p{i}/-/merge_requests/1" for i in range(20)]
    answers = {f"projects/de%2Fp{i}/merge_requests/1": ok(MERGED) for i in range(20)}
    w = make(answers)
    watch.card_fields(record(urls=urls), None)
    first = w.cycle()
    assert 0 < first <= watch.CALLS_PER_CYCLE
    while w.cycle():
        pass
    assert len(w.run.argv) == 20  # the rest came in the next cycles, each once


# --- errors -------------------------------------------------------------------------------------------
def test_not_found_is_no_data(make):
    w = make({MR: NOT_FOUND})
    watch.card_fields(record(), live())
    w.cycle()
    item = mr_of(watch.card_fields(record(), live()))
    assert item["state"] == "" and item["error"] == "MR not found, or no access to it" and item["off"] == ""
    with answering("ru"):  # the cache keeps the error, not its words: each reader gets their language
        assert mr_of(watch.card_fields(record(), live()))["error"] == "MR не найден или нет доступа"


def test_not_logged_in_hides_the_host_and_says_why(make):
    clock = Clock()
    w = make({MR: EXPIRED, MR2: ok(MERGED)}, clock)
    rec = record(urls=(URL, URL2))
    watch.card_fields(rec, live())
    assert w.cycle() == 1  # the second MR on the same host is not asked
    card = watch.card_fields(rec, live())
    assert "glab auth login --hostname gitlab.example.com" in mr_of(card)["off"]
    assert mr_of(card, URL2)["off"] == mr_of(card)["off"]
    clock.now += watch.OPEN_S * 2
    watch.card_fields(rec, live())
    assert w.cycle() == 0  # the host rests longer than a minute


def test_a_hanging_host_is_skipped_for_the_cycle(make):
    clock = Clock()
    w = make({MR: subprocess.TimeoutExpired("glab", glab.TIMEOUT_S), MR2: ok(MERGED)}, clock)
    rec = record(urls=(URL, URL2))
    watch.card_fields(rec, live())
    assert w.cycle() == 1
    assert "didn’t answer" in mr_of(watch.card_fields(rec, live()))["error"]
    w.run.answers[MR] = ok(MERGED)
    clock.now += watch.OPEN_S + 1
    watch.card_fields(rec, live())
    w.cycle()
    card = watch.card_fields(rec, live())
    assert [i["state"] for i in card["mr"]["items"]] == ["merged", "merged"] and mr_of(card)["error"] == ""


def test_without_glab_the_feature_says_why_and_starts_nothing():
    card = watch.card_fields(record(), live())  # conftest: glab is not on PATH
    assert card["mr"]["off"] == "glab isn’t on PATH"
    with answering("ru"):
        assert watch.card_fields(record(), live())["mr"]["off"] == "glab не найден в PATH"
    assert not any(t.name == "sya-mr" for t in threading.enumerate())


def test_the_board_does_not_wait_for_a_hanging_glab(monkeypatch):
    no_processes(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def hang(argv):
        entered.set()
        release.wait(10)
        return ok(MERGED)

    w = watch.Watcher(run=hang, which=on_path)
    monkeypatch.setattr(watch, "WATCHER", w)
    app = FakeApp([record()], {})
    try:
        app.board()  # starts the refresher
        assert entered.wait(5)
        t = time.monotonic()
        card = next(c for c in app.board()["cards"] if c["session_id"] == "s1")
        assert time.monotonic() - t < 1
        assert mr_of(card)["state"] == ""
    finally:
        release.set()
        w.stop()


# --- done ---------------------------------------------------------------------------------------------
def board_card(app, sid):
    return next(c for c in app.board()["cards"] if c["session_id"] == sid)


@pytest.fixture
def merged_app(make, monkeypatch):
    """Sessions on a fake glab where !1140 is merged and !7 is open; the app is built after the watcher, so
    the feature's setup hands it over."""
    no_processes(monkeypatch)
    w = make({MR: ok(MERGED), MR2: ok(opened_without_notes()), MR2 + "/approvals": ok(APPROVALS)})

    def build(records, live_sessions=None):
        app = FakeApp(records, live_sessions or {})
        app.board()  # the cards note their MRs and who is stopped
        return app, w

    return build


def test_a_stopped_session_with_all_mrs_merged_is_done(merged_app):
    app, w = merged_app([record("stopped")])
    w.cycle()
    assert "stopped" in done.load()
    assert board_card(app, "stopped")["column"] == "archive"


def test_a_live_session_gets_the_badge_not_done(merged_app):
    app, w = merged_app([record("alive")], {"alive": live("alive")})
    w.cycle()
    card = board_card(app, "alive")
    assert done.load() == {} and card["mr"]["all_merged"] is True and card["column"] != "archive"


@pytest.mark.parametrize(
    "rec",
    [
        record("half", urls=(URL, URL2)),  # one MR still open
        record("old", last=iso(time.time() - 10 * 86400)),  # already in the archive
    ],
)
def test_no_done_unless_stopped_recent_and_all_merged(merged_app, rec):
    _, w = merged_app([rec])
    w.cycle()
    assert done.load() == {}


def test_a_session_that_started_meanwhile_is_not_done(merged_app):
    app, w = merged_app([record("s")])
    app.fake_live = {"s": live("s")}  # the card was built while it was stopped; by the mark it runs
    w.cycle()
    assert done.load() == {}


def test_done_once_per_mr_set(merged_app):
    app, w = merged_app([record("s")])
    w.cycle()
    done.mark("s", False)  # the user put it back
    app.board()
    w.cycle()
    assert done.load() == {}


# mr.js in node with the page's globals stubbed: what its chip and drawer slots make of an item.
MR_PAGE = """
const fs = require("fs"), vm = require("vm");
let def = null;
const ENT = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ENT[c]);
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), { esc, ago: () => "", t: (key) => key,
  capital: (s) => s, SYA: { feature: (d) => { def = d; } } });
const items = JSON.parse(process.argv[3]);
const card = { mr: { items } };
console.log(JSON.stringify({
  chips: items.map((it) => def.chip({ url: it.url }, card)?.cls),
  drawer: def.drawer({ mr: { items } }),
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_puts_only_known_states_in_classes_and_only_web_urls_in_links(tmp_path):
    harness = tmp_path / "mr-page.js"
    harness.write_text(MR_PAGE)
    odd = 'x" onmouseover="alert(1)'
    items = [
        {"url": URL, "iid": 1140, "state": "merged", "title": "ok"},
        {"url": f"{URL}0", "iid": 11400, "state": odd, "title": "odd state"},
        {"url": "javascript:alert(1)", "iid": 7, "state": "opened", "title": "odd link"},
        {"url": f"{URL}1", "iid": 11401, "state": "constructor", "title": "a name every object has"},
    ]
    script = Path(watch.__file__).parent / "web" / "mr.js"
    run = subprocess.run(
        ["node", str(harness), str(script), json.dumps(items)], capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["chips"] == [
        "mr-chip mr-merged",
        "mr-chip mr-unknown",
        "mr-chip mr-opened",
        "mr-chip mr-unknown",
    ]
    assert "onmouseover" not in out["drawer"].replace("&quot; onmouseover", "")
    assert "mr-s-unknown" in out["drawer"] and 'href="javascript' not in out["drawer"]
