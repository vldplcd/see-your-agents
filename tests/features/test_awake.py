"""Keep the Mac awake: the caffeinate child follows Working and the switch; subprocess is faked."""

import json
import os

import pytest
from fakes import FakeApp

from see_your_agents import config_dir
from see_your_agents.core.providers.base import LiveSession
from see_your_agents.features.awake import awake
from see_your_agents.i18n import Problem


class Child:
    """A fake caffeinate: it runs until terminated, or until a test ends it (`code`)."""

    def __init__(self, argv):
        self.argv, self.code, self.signals = argv, None, []

    def poll(self):
        return self.code

    def terminate(self):
        self.signals.append("TERM")
        self.code = -15

    def wait(self, timeout=None):
        return self.code


@pytest.fixture
def spawned(monkeypatch):
    children = []

    def popen(argv, **kw):
        children.append(Child(argv))
        return children[-1]

    monkeypatch.setattr(awake, "available", lambda: True)
    monkeypatch.setattr(awake, "keeper", awake.Keeper(spawn=popen))
    return children


def session(status, needs=""):
    return LiveSession("a", "interactive", status, needs=needs, provider="claude")


def post(app, body):
    status, _, text = awake.handle_post(app, awake.ROUTE, json.dumps(body).encode(), {})
    return status, json.loads(text)


def test_one_child_runs_while_a_session_works_and_stops_when_none_does(spawned):
    app = FakeApp([], {"a": session("busy")})

    awake.tick(app)
    awake.tick(app)
    (child,) = spawned
    assert child.argv[0].endswith("caffeinate") and child.argv[1:] == ["-i", "-w", str(os.getpid())]
    assert awake.keeper.active

    app.fake_live = {"a": session("idle")}
    awake.tick(app)
    awake.tick(app)
    assert spawned[0].signals == ["TERM"] and not awake.keeper.active and len(spawned) == 1


def test_a_child_that_died_on_its_own_is_started_again(spawned):
    app = FakeApp([], {"a": session("shell")})
    awake.tick(app)
    spawned[0].code = 1  # someone killed caffeinate

    awake.tick(app)

    assert len(spawned) == 2 and awake.keeper.active


@pytest.mark.parametrize(
    ("status", "needs", "works"),
    [
        ("busy", "", True),
        ("shell", "", True),
        ("idle", "", False),
        ("waiting", "", False),
        ("busy", "ok?", False),  # a background job that needs you
    ],
)
def test_only_a_session_in_working_keeps_the_mac_awake(status, needs, works):
    assert awake.working({"a": session(status, needs)}) is works


def test_the_switch_is_on_by_default_and_off_stops_the_child_and_is_kept(spawned):
    app = FakeApp([], {"a": session("busy")})
    assert awake.state()["enabled"] is True  # nothing saved yet
    awake.tick(app)

    assert post(app, {"enabled": False}) == (200, {"available": True, "enabled": False, "active": False})
    awake.tick(app)

    assert spawned[0].signals == ["TERM"] and len(spawned) == 1
    assert json.loads((config_dir() / awake.FILE).read_text()) == {"enabled": False}
    assert post(app, {"enabled": True})[1]["active"] is True  # a session works: on at once


@pytest.mark.parametrize("body", [{"enabled": "yes"}, {}, ["enabled"]])
def test_a_switch_that_is_not_true_or_false_is_refused(spawned, body):
    with pytest.raises(Problem) as refused:  # the host answers it as {"error": ...} in the page's language
        post(FakeApp([], {}), body)
    assert (refused.value.key, refused.value.status) == ("awake.bad_enabled", 400)
    assert not (config_dir() / awake.FILE).exists()


def test_after_exit_nothing_starts_again(spawned):
    app = FakeApp([], {"a": session("busy")})
    awake.tick(app)

    awake.keeper.close()
    awake.tick(app)

    assert spawned[0].signals == ["TERM"] and len(spawned) == 1


def test_where_caffeinate_is_not_the_loop_ends_at_once(monkeypatch):
    monkeypatch.setattr(awake, "available", lambda: False)
    monkeypatch.setattr(awake.time, "sleep", lambda s: pytest.fail("the loop must not run"))

    awake.run(FakeApp([], {"a": session("busy")}))
