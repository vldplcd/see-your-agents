import pytest

from see_your_agents.core import providers
from see_your_agents.core.providers.base import LiveSession, Provider
from see_your_agents.core.providers.claude.provider import Claude


def test_a_provider_is_found_by_its_id_and_an_unknown_id_is_an_error():
    assert providers.by_id("claude").id == "claude"
    with pytest.raises(KeyError):
        providers.by_id("nope")


class Odd(Claude):
    id = "Odd"


class NoCaps(Provider):
    """Reads like a provider, but never says what it can do."""

    id, title, parser_version = "nocaps", "Без возможностей", 1

    def available(self):
        return True

    def transcripts(self):
        return {}

    def parse(self, path):
        raise AssertionError

    def live(self):
        return {}


@pytest.mark.parametrize(
    ("registry", "why"),
    [((Claude(), Claude()), "unique"), ((Odd(),), "unique"), ((NoCaps(),), "caps")],
    ids=["repeated id", "malformed id", "no caps"],
)
def test_a_registry_the_index_or_the_page_would_get_wrong_is_refused(registry, why):
    with pytest.raises(ValueError, match=why):
        providers.check(registry)


class Broken(NoCaps):
    id, caps = "broken", frozenset()

    def live(self):
        raise OSError("the program's CLI hangs")


class Running(NoCaps):
    id, caps = "running", frozenset()

    def live(self):
        return {"s1": LiveSession("s1", "interactive", "idle", provider="running")}


def test_a_provider_that_fails_to_tell_who_runs_leaves_the_others_on_the_board(monkeypatch, capsys):
    monkeypatch.setattr(providers, "PROVIDERS", (Broken(), Running()))

    assert list(providers.live()) == ["s1"]
    assert "provider broken" in capsys.readouterr().err
