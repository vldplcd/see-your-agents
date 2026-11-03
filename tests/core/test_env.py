import os

from see_your_agents.core.env import clean_env


def test_clean_env_drops_the_servers_own_variables_and_its_venv_on_path(monkeypatch):
    own = ("SYA_WATCHDOG", "UV_RUN_RECURSION_DEPTH", "CLAUDECODE", "CLAUDE_EFFORT", "CLAUDE_CODE_EXECPATH",
           "CLAUDE_CODE_INVOKED_SKILLS", "CLAUDE_CODE_SESSION_ID")  # fmt: skip
    for key in own:
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("VIRTUAL_ENV", "/srv/sya/.venv")
    monkeypatch.setenv("PATH", os.pathsep.join(["/srv/sya/.venv/bin/", "/usr/bin", "/bin"]))
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")

    env = clean_env(SYA_CONFIG_DIR="/sandbox")

    assert not {*own, "VIRTUAL_ENV"} & env.keys()
    assert env["PATH"] == os.pathsep.join(["/usr/bin", "/bin"])
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1" and env["SYA_CONFIG_DIR"] == "/sandbox"
