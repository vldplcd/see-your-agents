"""Starting background Claude Code sessions: folder rules, names, whitelisted flags."""

import json
import subprocess
from dataclasses import replace

import pytest

from see_your_agents.core.providers.base import MAX_PROMPT, LaunchError, RequestError, Session, Started
from see_your_agents.core.providers.claude import launch as claude_launch

JOB, SID = "ab12cd34", "ab12cd34-0000-4000-8000-000000000000"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "home"
    (home / "work" / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def runs(monkeypatch):
    """Replaces subprocess.run for `claude --bg`: records (argv, cwd, env); outcome[0] is (code, output)."""
    calls, outcome = [], [(0, f"backgrounded · {JOB} · w\n")]

    def fake(cmd, **kw):
        calls.append((cmd, kw["cwd"], kw["env"]))
        return subprocess.CompletedProcess(cmd, outcome[0][0], stdout=outcome[0][1], stderr="")

    monkeypatch.setattr(claude_launch.subprocess, "run", fake)
    monkeypatch.setattr(claude_launch.shutil, "which", lambda n: "/bin/claude")
    monkeypatch.setattr(claude_launch.live, "_agents_json", lambda: [{"id": JOB, "sessionId": SID}])
    return calls, outcome


@pytest.fixture
def request_(home):
    return claude_launch.LaunchRequest("~/work/proj", "x", "w")


# ---- the start ----------------------------------------------------------------------------------


def test_start_passes_every_option_as_flag_equals_value(home, runs, request_, monkeypatch):
    calls, _ = runs
    monkeypatch.setenv("CLAUDECODE", "1")
    picks = {"permission_mode": "plan", "model": "opus", "effort": "high"}  # argv keeps the options' order
    chosen = replace(request_, prompt=" -- сделай ", name="Ревью CI", worktree=True, choices=picks)

    started = claude_launch.start(chosen)

    (argv, ran_in, env), *_ = calls
    assert (started.job_id, started.session_id, started.name) == (JOB, SID, "Ревью CI")
    assert started.worktree.startswith("ci-") and len(started.worktree) == len("ci-") + 4
    assert ran_in == home / "work" / "proj" and "CLAUDECODE" not in env
    assert argv == [
        "/bin/claude", "--bg", "--name=Ревью CI", "--model=opus", "--effort=high", "--permission-mode=plan",
        f"--worktree={started.worktree}", "--", "-- сделай",
    ]  # fmt: skip


def test_start_with_defaults_passes_no_option_flags(home, runs, request_):
    assert claude_launch.start(request_).worktree == ""
    assert runs[0][-1][0] == ["/bin/claude", "--bg", "--name=w", "--", "x"]


def test_the_name_is_one_printable_line_defaults_to_the_folder_and_steps_past_live_ones(home, runs, request_):
    (home / "work" / "my\tproj").mkdir()

    typed = claude_launch.start(replace(request_, name="  ревью\n\x1bCI  "))
    in_folder = replace(request_, cwd="~/work/my\tproj", name="")
    default = claude_launch.start(in_folder, taken=["My Proj", "my proj-2"])

    assert typed.name == "ревью CI"
    assert default.name == "my proj-3"  # case does not make another address


def test_a_long_name_steps_past_a_taken_one_within_the_limit(home, runs, request_):
    long = "n" * claude_launch.MAX_NAME
    started = claude_launch.start(replace(request_, name=long), taken=[long])
    assert started.name == "n" * (claude_launch.MAX_NAME - 2) + "-2"


@pytest.mark.parametrize(
    "change",
    [
        {"choices": {"permission_mode": "bypassPermissions"}},
        {"choices": {"permission_mode": "dontAsk"}},
        {"choices": {"model": "opus --dangerously-skip-permissions"}},
        {"choices": {"effort": "ultra"}},
        {"choices": {"sandbox": "danger-full-access"}},
        {"prompt": "  "},
        {"prompt": "a\x00b"},
        {"prompt": "x" * (MAX_PROMPT + 1)},
        {"name": "n" * (claude_launch.MAX_NAME + 1)},
        {"cwd": "/"},
        {"cwd": "work/proj"},
    ],
)
def test_start_refuses_a_wrong_request_before_running(home, runs, request_, change):
    with pytest.raises(RequestError):
        claude_launch.start(replace(request_, **change))
    assert runs[0] == []


def test_an_untrusted_folder_comes_back_with_a_hint(home, runs, request_):
    _, outcome = runs
    outcome[0] = (1, "Error: Workspace not trusted\n")

    with pytest.raises(LaunchError) as err:
        claude_launch.start(request_)
    assert not isinstance(err.value, RequestError)
    assert err.value.key == claude_launch.UNTRUSTED_FAILED
    assert str(err.value).startswith("Error: Workspace not trusted\nClaude Code doesn’t trust this folder")
    assert "Папка не доверена Claude Code" in err.value.text("ru")


def test_a_start_that_names_no_id_is_an_error(home, runs, request_):
    runs[1][0] = (0, "something else\n")
    with pytest.raises(LaunchError, match="didn’t name the session id"):
        claude_launch.start(request_)


# ---- the fork ---------------------------------------------------------------------------------------

ORIGINAL = "0f1e2d3c-0000-4000-8000-000000000000"


@pytest.fixture
def session(home):
    return Session(ORIGINAL, str(home / "work" / "proj"))


def test_a_fork_resumes_a_copy_of_the_session_in_its_folder_named_after_it(home, runs, session, monkeypatch):
    calls, _ = runs
    monkeypatch.setenv("CLAUDECODE", "1")

    started = claude_launch.fork(session, name="ревью", prompt=" а теперь тесты ")

    (argv, ran_in, env), *_ = calls
    assert argv == [
        "/bin/claude", "--bg", f"--resume={ORIGINAL}", "--fork-session", "--name=ревью-fork",
        "--", "а теперь тесты",
    ]  # fmt: skip
    assert ran_in == home / "work" / "proj" and "CLAUDECODE" not in env
    assert started == Started(JOB, "ревью-fork", "", SID, f"claude attach {JOB}")


def test_a_fork_without_a_message_waits_and_takes_a_free_name_from_the_folder(home, runs, session):
    started = claude_launch.fork(session, taken=["proj-fork"])

    assert started.name == "proj-fork-2"
    assert runs[0][-1][0][-1] == "--name=proj-fork-2"  # no "--" and no prompt: the fork starts idle


@pytest.mark.parametrize(
    "change",
    [
        {"transcript_missing": True},
        {"session_id": "not-a-session --dangerously-skip-permissions"},
        {"cwd": "~/work/proj/.claude/worktrees/wt"},
        {"cwd": "/"},
        {"cwd": ""},
    ],
)
def test_a_fork_refuses_a_session_it_cannot_copy_before_running(home, runs, session, change):
    (home / "work" / "proj" / ".claude" / "worktrees" / "wt").mkdir(parents=True)

    with pytest.raises(RequestError):
        claude_launch.fork(replace(session, **change))
    assert runs[0] == []


@pytest.mark.parametrize("prompt", ["a\x00b", "x" * (MAX_PROMPT + 1)])
def test_a_fork_refuses_a_bad_first_message(home, runs, session, prompt):
    with pytest.raises(RequestError):
        claude_launch.fork(session, prompt=prompt)
    assert runs[0] == []


# ---- what Claude Code's own config says -----------------------------------------------------------


def test_trust_is_read_from_claude_json_and_inherited_by_subfolders(home, tmp_path):
    projects = {str(home / "work"): {"hasTrustDialogAccepted": True, "mcpServers": {"x": {"token": "s"}}}}
    assert claude_launch.trusted([home / "work"]) == set()  # no ~/.claude.json yet
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / ".claude.json").write_text(json.dumps({"projects": projects}))

    assert claude_launch.trusted([home / "work" / "proj", home]) == {home / "work" / "proj"}


def test_options_offer_the_whitelists_and_the_users_default_model(home, tmp_path):
    assert claude_launch.options().default_model == ""  # no settings.json yet
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "settings.json").write_text(json.dumps({"model": "opus", "env": {"X": "1"}}))

    opts = claude_launch.options()
    assert opts.default_model == "opus" and "sonnet" in opts.choices["model"]
    assert not {"bypassPermissions", "dontAsk"} & set(opts.choices["permission_mode"])
