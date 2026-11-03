"""Starting Codex from the New session dialog: options from a synthetic models cache, whitelisted
argv, the thread id from the run's first line. `codex exec` itself is stubbed."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from codex_rollouts import meta, write

from see_your_agents.core.providers.base import LaunchError, LaunchRequest, RequestError, Started
from see_your_agents.core.providers.codex import launch as codex_launch

TID = "01a1a14f-e101-7b72-9f68-a498ecdfbc6e"
LEVELS = [{"effort": e, "description": "…"} for e in ("low", "medium", "high")]


def model(slug, name, visibility="list", levels=LEVELS):
    """One entry of models_cache.json's `models`, in the shape docs/codex-cli-notes.md records."""
    return {
        "slug": slug,
        "display_name": name,
        "visibility": visibility,
        "supported_reasoning_levels": levels,
    }


MODELS = {
    "fetched_at": "2026-11-03T12:00:00Z",
    "identity": {"account": "never read"},
    "models": [
        model("gpt-a", "GPT-A"),
        model("gpt-b", "GPT-B", levels=[*LEVELS, {"effort": "ultra"}]),
        model("gpt-hidden", "Hidden", visibility="hide"),
        model("-x", "a flag in disguise"),
    ],
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "home"
    (home / "work" / "repo" / ".git").mkdir(parents=True)
    (home / "work" / "plain").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    codex = Path(os.environ["CODEX_HOME"])
    codex.mkdir()
    (codex / "models_cache.json").write_text(json.dumps(MODELS))
    (codex / "config.toml").write_text('model = "gpt-b"\n\n[mcp_servers.x]\nenv = { TOKEN = "secret" }\n')
    return home


class Exec:
    """`codex exec`: records argv and keyword arguments and writes `lines` to the log the run was given."""

    def __init__(self, lines: str, code: int | None = None):
        self.lines, self.code = lines, code
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        kw["stdout"].write(self.lines.encode())
        kw["stdout"].flush()
        return self

    def poll(self):
        return self.code


@pytest.fixture
def codex(monkeypatch):
    monkeypatch.setattr(codex_launch.shutil, "which", lambda name: "/bin/codex")
    monkeypatch.setattr(codex_launch, "START_WAIT_S", 0.3)

    def answer(lines=f'{{"type":"thread.started","thread_id":"{TID}"}}\n', code=None):
        fake = Exec(lines, code)
        monkeypatch.setattr(codex_launch.subprocess, "Popen", fake)
        return fake

    return answer


def request(**fields) -> LaunchRequest:
    return LaunchRequest(fields.pop("cwd", "~/work/repo"), fields.pop("prompt", "Почини CI"), **fields)


def test_options_list_the_visible_models_with_their_own_efforts_and_the_users_default(home):
    opts = codex_launch.options()

    assert opts.choices["model"] == ("gpt-a", "gpt-b")  # hidden and flag-shaped slugs are not offered
    assert opts.efforts == {"gpt-a": ("low", "medium", "high"), "gpt-b": ("low", "medium", "high", "ultra")}
    assert opts.default_model == "gpt-b" and opts.labels["gpt-a"] == "GPT-A"
    assert opts.choices["sandbox"] == ("workspace-write", "read-only")
    assert opts.defaults == {"sandbox": "workspace-write"}


def test_without_a_models_cache_or_a_config_only_the_defaults_are_left(home):
    (Path(os.environ["CODEX_HOME"]) / "models_cache.json").unlink()
    (Path(os.environ["CODEX_HOME"]) / "config.toml").write_text("model = [not toml")

    opts = codex_launch.options()

    assert (opts.choices["model"], opts.choices["effort"], opts.default_model) == ((), (), "")


def test_start_runs_codex_exec_detached_with_the_chosen_options_and_names_the_thread(home, codex):
    fake = codex()
    worktree = str(Path(os.environ["CODEX_HOME"]) / "worktrees" / "c475" / "repo")
    write(Path(os.environ["CODEX_HOME"]), TID, meta(TID, cwd=worktree))  # what the run writes first
    choices = {"model": "gpt-a", "effort": "high", "sandbox": "read-only"}

    started = codex_launch.start(request(prompt=" -- Почини CI ", worktree=True, choices=choices))

    assert started == Started(TID, "", worktree, TID, f"codex resume {TID}")  # Codex names no session
    (argv, kw), *_ = fake.calls
    assert argv == [
        "/bin/codex", "exec", "--json", "-C", str(home / "work" / "repo"), "-s", "read-only", "-m", "gpt-a",
        "-c", 'model_reasoning_effort="high"', "--worktree", "--", "-- Почини CI",
    ]  # fmt: skip
    assert kw["start_new_session"] and kw["stdin"] == subprocess.DEVNULL and "VIRTUAL_ENV" not in kw["env"]


def test_by_default_the_sandbox_is_workspace_write_and_nothing_else_is_passed(home, codex):
    fake = codex()

    codex_launch.start(request())

    assert fake.calls[0][0][1:] == [
        "exec", "--json", "-C", str(home / "work" / "repo"), "-s", "workspace-write", "--", "Почини CI",
    ]  # fmt: skip


def test_the_effort_must_be_one_the_model_or_the_default_model_has(home, codex):
    fake = codex()
    with pytest.raises(RequestError, match="effort"):
        codex_launch.start(request(choices={"model": "gpt-a", "effort": "ultra"}))

    codex_launch.start(request(choices={"effort": "ultra"}))  # the default model, gpt-b, has it
    assert fake.calls[0][0][-4:-2] == ["-c", 'model_reasoning_effort="ultra"']


@pytest.mark.parametrize(
    "fields",
    [
        {"choices": {"sandbox": "danger-full-access"}},
        {"choices": {"model": "gpt-hidden"}},
        {"choices": {"approval": "never"}},
        {"name": "ревью"},
        {"prompt": "  "},
        {"prompt": "a\x00b"},
        {"cwd": "~/work/.secret"},
    ],
)
def test_a_request_outside_the_whitelists_starts_nothing(home, codex, fields):
    fake = codex()

    with pytest.raises(RequestError):
        codex_launch.start(request(**fields))
    assert fake.calls == []


def test_codex_refusing_the_folder_comes_back_with_its_words_and_a_hint(home, codex):
    codex(
        "Reading additional input from stdin...\n"
        "Not inside a trusted directory and --skip-git-repo-check was not specified.\n",
        code=1,
    )  # what 0.166.0 printed in a folder outside git

    with pytest.raises(LaunchError) as caught:
        codex_launch.start(request(cwd="~/work/plain"))
    assert str(caught.value).startswith("Not inside a trusted directory")
    assert caught.value.key == codex_launch.UNTRUSTED_FAILED and "stdin" not in str(caught.value)
    assert str(caught.value).endswith("\nCodex starts only in a git repository or in a folder it trusts.")


def test_a_run_that_never_names_its_thread_is_a_launch_error(home, codex):
    codex("")

    with pytest.raises(LaunchError, match="didn’t name the session id"):
        codex_launch.start(request())


def test_only_folders_inside_a_git_work_tree_are_trusted(home):
    repo, plain = home / "work" / "repo", home / "work" / "plain"
    (repo / "sub").mkdir()

    assert codex_launch.trusted([repo, repo / "sub", plain]) == {repo, repo / "sub"}
