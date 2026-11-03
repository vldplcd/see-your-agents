"""«Новая сессия» ("New session"): recent folders, suggestions and the start through the real server;
`claude` is faked."""

import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from fakes import FakeApp, no_processes, pair

from see_your_agents.app import server
from see_your_agents.core.providers.base import LiveSession
from see_your_agents.core.providers.claude import launch as claude_launch
from see_your_agents.core.providers.codex import launch as codex_launch

JOB, SID = "ab12cd34", "ab12cd34-0000-4000-8000-000000000000"
FORKED = "0f1e2d3c-0000-4000-8000-000000000000"  # a session with its transcript on disk


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SYA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SYA_CACHE_DIR", str(tmp_path / "cache"))
    no_processes(monkeypatch)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "home"
    for folder in ("work/proj", "work/other", "work/.secret", "workshop", "proj/.claude/worktrees/wt"):
        (home / folder).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    claude = tmp_path / "claude"
    claude.mkdir()
    projects = {str(home / "work"): {"hasTrustDialogAccepted": True}}
    (claude / ".claude.json").write_text(json.dumps({"projects": projects}))
    (claude / "settings.json").write_text(json.dumps({"model": "opus"}))
    return home


def rec(sid, cwd, at):
    at = f"2026-11-0{at}T10:00:00Z"
    return {"session_id": sid, "title": sid, "cwd": str(cwd), "last_activity": at, "path": f"/t/{sid}.jsonl"}


@pytest.fixture
def app(tmp_path, home):
    records = [
        rec("a", home / "work" / "other", 1),
        rec("b", home / "work" / "proj", 2),
        {**rec(FORKED, home / "work" / "proj", 3), "custom_name": "ревью", "path": "/t/forked.jsonl"},
        rec("d", home / "workshop", 4),  # untrusted
        rec("e", home / "proj" / ".claude" / "worktrees" / "wt", 5),  # hidden
        rec("f", home / "gone", 6),
        rec("g", tmp_path / "outside", 7),
        rec("h", home / ("x" * 300), 8),  # the disk refuses to stat it
        rec("i", f"{home}/work//proj/", 1),  # the same folder, written another way
    ]
    live = {
        "x": LiveSession("x", "interactive", "idle", name="proj", name_source="derived", provider="claude"),
        "y": LiveSession("y", "background", "idle", name="other", name_source="auto", provider="claude"),
    }
    return FakeApp(records, live)


PAIRED: dict[str, dict[str, str]] = {}  # base url -> the paired browser's header; a mutating POST needs it


@pytest.fixture
def base(app):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(app, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    PAIRED[url] = pair(app, port)
    yield url
    httpd.shutdown()
    PAIRED.pop(url, None)


@pytest.fixture
def claude(monkeypatch):
    """`claude --bg` and `claude agents`: records argv; outcome[0] is (exit code, output)."""
    calls, outcome = [], [(0, f"backgrounded · {JOB} · proj-2\n")]

    def fake(cmd, **kw):
        calls.append((cmd, kw["cwd"]))
        return subprocess.CompletedProcess(cmd, outcome[0][0], stdout=outcome[0][1], stderr="")

    monkeypatch.setattr(claude_launch.subprocess, "run", fake)
    monkeypatch.setattr(claude_launch.shutil, "which", lambda n: "/bin/claude")
    monkeypatch.setattr(claude_launch.live, "_agents_json", lambda: [{"id": JOB, "sessionId": SID}])
    return calls, outcome


def fetch(base, path, payload=None, lang=None):
    data = json.dumps(payload).encode() if payload is not None else None
    base_headers = {"Origin": base, "X-SYA": "1", "Content-Type": "application/json", **PAIRED.get(base, {})}
    headers = base_headers if data else {}
    headers |= {"X-SYA-Lang": lang} if lang else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers)) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# ---- what the dialog shows ----------------------------------------------------------------------


def test_recent_folders_newest_first_counted_with_trust_and_only_valid_ones(base, home):
    status, body = fetch(base, "/api/launch")

    assert status == 200
    folders = [(f["name"], f["sessions"]) for f in body["folders"]]
    assert folders == [("workshop", 1), ("proj", 3), ("other", 1)]
    # hidden worktrees, gone and unreadable folders and folders outside home are left out
    claude = body["programs"][0]  # the default program comes first
    assert claude["id"] == "claude" and claude["default_model"] == "opus" and claude["choices"]["effort"]
    assert claude["trusted"] == [str(home / "work" / "proj"), str(home / "work" / "other")]
    assert body["home"] == str(home)


def test_suggestions_complete_a_folder_inside_home_and_never_a_hidden_one(base, home, tmp_path):
    def dirs(typed):
        return fetch(base, f"/api/launch/dirs?path={urllib.request.quote(typed)}")[1]["dirs"]

    assert dirs("~/wo") == [str(home / "work"), str(home / "workshop")]
    assert dirs("~/work/") == [str(home / "work" / "other"), str(home / "work" / "proj")]
    assert dirs("~") == [str(home / p) for p in ("proj", "work", "workshop")]
    assert dirs("~/work/.") == [] and dirs("~/proj/.claude/") == []
    assert dirs(str(tmp_path) + "/") == [] and dirs("/etc/") == []


# ---- the start ------------------------------------------------------------------------------------


def test_launch_starts_claude_bg_with_a_unique_name_and_the_chosen_options(base, home, claude):
    calls, _ = claude
    body = {"cwd": "~/work/proj", "prompt": " Почини CI ", "worktree": True, "model": "sonnet"}
    body["effort"] = "high"

    status, reply = fetch(base, "/api/launch", body)

    assert status == 200 and reply["ok"] and (reply["job_id"], reply["session_id"]) == (JOB, SID)
    assert reply["name"] == "proj-2"  # a live session is called «proj» already
    assert reply["worktree"].startswith("proj-2-")
    (argv, cwd), *_ = calls
    assert cwd == home / "work" / "proj"
    assert argv == [
        "/bin/claude", "--bg", "--name=proj-2", "--model=sonnet", "--effort=high",
        f"--worktree={reply['worktree']}", "--", "Почини CI",
    ]  # fmt: skip


def test_launch_takes_the_typed_name_and_works_in_the_folder_by_default(base, claude):
    calls, _ = claude
    status, reply = fetch(base, "/api/launch", {"cwd": "~/work/proj", "prompt": "x", "name": " ревью\nCI "})

    assert status == 200 and reply["name"] == "ревью CI" and reply["worktree"] == ""
    assert calls[0][0] == ["/bin/claude", "--bg", "--name=ревью CI", "--", "x"]


def test_a_name_claude_code_made_up_is_not_an_address_to_step_past(base, claude):
    status, reply = fetch(base, "/api/launch", {"cwd": "~/work/other", "prompt": "x"})

    assert status == 200 and reply["name"] == "other"  # the live «other» has nameSource "auto"


@pytest.mark.parametrize(
    "body",
    [
        {"cwd": "~/work/proj", "prompt": "  "},
        {"cwd": "~/work/proj", "prompt": "x", "permission_mode": "bypassPermissions"},
        {"cwd": "~/work/proj", "prompt": "x", "model": "gpt-5"},
        {"cwd": "~/work/proj", "prompt": "x", "effort": ["high"]},
        {"cwd": "~/work/proj", "prompt": "x", "worktree": "yes"},
        {"cwd": "~/work/proj", "prompt": "x", "name": "n" * (claude_launch.MAX_NAME + 1)},
        {"cwd": "~/work/proj", "prompt": "x", "name": None},
        {"cwd": "~/work/proj", "prompt": 5},
        {"cwd": "~/work/.secret", "prompt": "x"},
        {"cwd": "work/proj", "prompt": "x"},
        {"cwd": "~", "prompt": "x"},
        {"prompt": "x"},
        ["not", "an", "object"],
    ],
)
def test_launch_refuses_a_bad_request_without_running_claude(base, claude, body):
    status, reply = fetch(base, "/api/launch", body)

    assert status == 400 and not reply["ok"] and reply["error"]
    assert claude[0] == []


def test_an_untrusted_folder_is_passed_through_with_the_hint(base, claude):
    _, outcome = claude
    outcome[0] = (1, "Error: Workspace not trusted. Run claude here first.\n")

    status, reply = fetch(base, "/api/launch", {"cwd": "~/workshop", "prompt": "x"})

    assert status == 502 and "Workspace not trusted" in reply["error"]
    assert "Claude Code doesn’t trust this folder" in reply["error"]


# ---- «Ответвить» ("Fork") ---------------------------------------------------------------------------


def test_only_a_card_with_its_transcript_in_a_startable_folder_offers_a_fork(base):
    offers = {c["session_id"]: c["can_fork"] for c in fetch(base, "/api/board")[1]["cards"]}

    assert offers[FORKED] is True and offers["a"] is True
    assert offers["x"] is False  # only running: no transcript to copy
    assert not offers["e"] and not offers["f"] and not offers["g"]  # hidden worktree, gone, outside home
    assert offers["h"] is False  # a name too long to stat: no folder, and no error for the board


def test_a_fork_starts_a_copy_named_after_the_session_in_its_folder(base, home, claude):
    calls, _ = claude
    status, reply = fetch(base, "/api/launch/fork", {"session_id": FORKED, "prompt": " дальше "})

    assert status == 200 and reply["ok"] and (reply["job_id"], reply["session_id"]) == (JOB, SID)
    assert reply["name"] == "ревью-fork"  # its /rename
    (argv, cwd), *_ = calls
    assert cwd == home / "work" / "proj"
    assert argv == [
        "/bin/claude", "--bg", f"--resume={FORKED}", "--fork-session", "--name=ревью-fork", "--", "дальше",
    ]  # fmt: skip


def test_a_fork_takes_the_live_name_and_steps_past_names_in_use(app, base, claude):
    running = LiveSession(FORKED, "interactive", "idle", name="ci", name_source="user", provider="claude")
    taken = LiveSession("z", "background", "busy", name="ci-fork", name_source="user", provider="claude")
    app.fake_live = {**app.fake_live, FORKED: running, "z": taken}

    status, reply = fetch(base, "/api/launch/fork", {"session_id": FORKED})

    assert status == 200 and reply["name"] == "ci-fork-2"
    assert claude[0][0][0][-1] == "--name=ci-fork-2"  # no first message: the fork waits idle


@pytest.mark.parametrize(
    "body",
    [{"session_id": "x"}, {"session_id": "nope"}, {"session_id": FORKED, "prompt": 5}, {}, ["x"]],
)
def test_a_fork_refuses_what_it_cannot_copy_without_running_claude(base, claude, body):
    status, reply = fetch(base, "/api/launch/fork", body)

    assert status == 400 and not reply["ok"] and reply["error"]
    assert claude[0] == []


def test_a_refusal_comes_in_the_page_language(base, claude):
    english = fetch(base, "/api/launch/fork", {"session_id": "nope"})
    russian = fetch(base, "/api/launch/fork", {"session_id": "nope"}, lang="ru")

    assert english[1] == {
        "ok": False,
        "error": "This session isn’t in the index: only a session with a transcript can be forked",
    }
    assert russian[1]["error"] == "этой сессии нет в индексе: ответвить можно сессию с транскриптом"


# ---- the program: Claude Code or Codex ---------------------------------------------------------------


@pytest.fixture
def codex(home, tmp_path, monkeypatch):
    """Codex next to Claude: its models cache, `codex` on PATH, `codex exec` recording argv and naming a
    thread on its first line."""
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    levels = [{"effort": e} for e in ("low", "high")]
    models = [{"slug": "gpt-a", "visibility": "list", "supported_reasoning_levels": levels}]
    (codex_home / "models_cache.json").write_text(json.dumps({"models": models}))
    (home / "work" / "proj" / ".git").mkdir()
    monkeypatch.setattr(codex_launch.shutil, "which", lambda name: f"/bin/{name}")
    runs = []

    class Run:
        def __init__(self, argv, **kw):
            runs.append(argv)
            kw["stdout"].write(f'{{"type":"thread.started","thread_id":"{CODEX_SID}"}}\n'.encode())

        def poll(self):
            return 0

    monkeypatch.setattr(codex_launch.subprocess, "Popen", Run)
    return runs


CODEX_SID = "01a1a14f-e101-7b72-9f68-a498ecdfbc6e"


def test_the_dialog_offers_each_program_with_its_own_options_and_trust(base, home, codex):
    programs = {p["id"]: p for p in fetch(base, "/api/launch")[1]["programs"]}

    assert list(programs) == ["claude", "codex"]
    assert "name" in programs["claude"]["caps"] and "name" not in programs["codex"]["caps"]
    assert programs["codex"]["choices"]["sandbox"] == ["workspace-write", "read-only"]
    assert programs["codex"]["efforts"] == {"gpt-a": ["low", "high"]}
    assert programs["codex"]["trusted"] == [str(home / "work" / "proj")]  # the only git repo


def test_the_chosen_program_starts_the_session(base, claude, codex):
    body = {"provider": "codex", "cwd": "~/work/proj", "prompt": "Почини CI", "model": "gpt-a"}
    body["effort"] = "high"

    status, reply = fetch(base, "/api/launch", body)

    assert status == 200 and reply["ok"] and reply["session_id"] == CODEX_SID
    assert reply["attach"] == f"codex resume {CODEX_SID}" and reply["name"] == ""
    (run,) = codex  # the dialog's choices reached Codex; the argv itself is test_codex_launch's
    assert run[1] == "exec" and "gpt-a" in run and 'model_reasoning_effort="high"' in run
    assert claude[0] == []


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "codex", "cwd": "~/work/proj", "prompt": "x", "sandbox": "danger-full-access"},
        {"provider": "gemini", "cwd": "~/work/proj", "prompt": "x"},
        {"provider": ["codex"], "cwd": "~/work/proj", "prompt": "x"},
    ],
)
def test_a_program_or_option_the_dialog_does_not_offer_is_refused(base, claude, codex, body):
    status, reply = fetch(base, "/api/launch", body)

    assert status == 400 and not reply["ok"] and reply["error"]
    assert codex == [] and claude[0] == []
