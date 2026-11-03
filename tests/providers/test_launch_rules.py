"""The folder rule every provider's start shares: an existing, unhidden project folder inside home."""

import pytest

from see_your_agents.core.providers.base import RequestError, project_dir


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "home"
    (home / "work" / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def test_project_dir_is_an_existing_unhidden_folder_inside_home(home, tmp_path):
    (home / ".ssh").mkdir()
    assert project_dir("~/work/proj") == home / "work" / "proj"
    bad = (home, home / ".ssh", tmp_path, home / "gone", "~nosuchuser/x", f"{home}/bad\x00path")
    for raw in map(str, bad):
        with pytest.raises(RequestError):
            project_dir(raw)


def test_a_relative_path_is_refused_even_when_it_exists_from_here(home, monkeypatch):
    monkeypatch.chdir(home)
    with pytest.raises(RequestError, match="full path") as refused:
        project_dir("work/proj")
    assert refused.value.text("ru") == "нужен полный путь к папке: /… или ~/…"


def test_a_folder_the_disk_refuses_to_stat_is_a_request_error(home):
    with pytest.raises(RequestError, match="can’t be read"):  # ENAMETOOLONG from stat
        project_dir(str(home / ("x" * 300)))
