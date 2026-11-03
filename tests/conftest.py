import subprocess

import pytest

from see_your_agents.app import pairing
from see_your_agents.core.providers.claude import routes as claude_routes
from see_your_agents.features.mr import watch as mr_watch


@pytest.fixture(autouse=True)
def no_glab(monkeypatch):
    """No test runs the real glab: the MR feature finds none unless a test hands it a fake."""
    monkeypatch.setattr(mr_watch, "WATCHER", mr_watch.Watcher(which=lambda name: None))


@pytest.fixture(autouse=True)
def claude_home(tmp_path, monkeypatch):
    """Every test in its own folders: no test reads the user's ~/.claude or ~/.codex or touches their
    dashboard's config and cache. Claude's routes start unplugged, as before any App is built."""
    home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("SYA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SYA_CACHE_DIR", str(tmp_path / "cache"))
    claude_routes.unplug()
    yield home
    claude_routes.unplug()


@pytest.fixture(autouse=True)
def opened_tabs(monkeypatch):
    """Browser tabs pairing would open: recorded, never opened."""
    tabs: list[str] = []
    monkeypatch.setattr(pairing, "open_browser", tabs.append)
    return tabs


@pytest.fixture
def sleeper():
    """A process that is surely not `claude`, for the checks that guard a pid."""
    proc = subprocess.Popen(["sleep", "60"])
    yield proc
    proc.kill()
    proc.wait()
