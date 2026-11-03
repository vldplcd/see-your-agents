import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
from codex_rollouts import meta, turn_complete, turn_started, uid, user_item, write
from fakes import FakeApp, codex_record, fake_codex_queue, no_processes, record_resume_argv

from see_your_agents.app import cli
from see_your_agents.app.feature import Feature
from see_your_agents.app.mcp import Tool, ToolError
from see_your_agents.core.providers.base import LiveSession
from see_your_agents.core.providers.claude import routes as claude_routes
from see_your_agents.core.providers.codex import queue as codex_queue
from see_your_agents.features import FEATURES
from see_your_agents.features.chat import delivery
from see_your_agents.features.supervisor import launch as supervisor
from see_your_agents.features.supervisor import mcp_tools
from see_your_agents.i18n import t

SUPERVISOR_ONLY = [f for f in FEATURES if f.id == "supervisor"]  # other features add tools and prompt text
READS = [
    "mcp__sya__fleet_overview",
    "mcp__sya__session_detail",
    "mcp__sya__session_thread",
    "mcp__sya__search_sessions",
    "mcp__sya__list_delegations",
]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SYA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SYA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(supervisor.mcp, "tokens", lambda: {"read": "R-tok", "control": "C-tok"})
    yield
    claude_routes._runs.clear()


def test_launch_files_content_and_mode():
    files = supervisor.launch_files(9000, FEATURES)
    mcp = json.loads(files["mcp"].read_text())
    assert mcp["mcpServers"] == {
        "sya": {
            "type": "http",
            "url": "http://127.0.0.1:9000/mcp",
            "headers": {"Authorization": "Bearer R-tok"},
        },
        "sya_control": {
            "type": "http",
            "url": "http://127.0.0.1:9000/mcp",
            "headers": {"Authorization": "Bearer C-tok"},
        },
    }
    assert stat.S_IMODE(files["mcp"].stat().st_mode) == 0o600
    perms = json.loads(files["settings"].read_text())["permissions"]
    assert perms == supervisor.permissions(FEATURES)
    assert "mcp__sya_control__post_digest" in perms["allow"] and "SendMessage" not in perms["allow"]
    prompt = files["prompt"].read_text()
    assert prompt == supervisor.prompt(FEATURES) and "post_digest" in prompt


def test_the_supervisor_reads_english_and_answers_in_the_users_language():
    """Agent-facing text is English whatever the page's language; old Russian names it may meet are quoted."""
    others = [f for f in FEATURES if f.id not in ("builder", "approvals")]  # converted on their own branches
    quoted = ("`[Группа «…»`", '"Дашборд"')
    text = supervisor.prompt(others)
    for name in quoted:
        text = text.replace(name, "")

    assert "Answer the user in the language they write in." in text
    assert not re.search("[А-Яа-яЁё]", text)
    tools = json.dumps([t.listing() for f in others for t in f.tools], ensure_ascii=False)
    assert not re.search("[А-Яа-яЁё]", mcp_tools.SIGNED + supervisor.mcp.INSTRUCTIONS + tools)


def test_the_subagent_file_has_a_one_line_frontmatter_per_field(tmp_path):
    """Claude Code reads the frontmatter as YAML: a ": " inside a plain value would break the description."""
    head = supervisor.install_agent(FEATURES).read_text().split("---\n")[1]
    fields = dict(line.split(": ", 1) for line in head.splitlines())

    assert set(fields) == {"name", "description", "tools"} and ": " not in fields["description"]


def test_the_users_atlassian_server_comes_along_read_only(tmp_path):
    atlassian = {"command": "uvx", "args": ["mcp-atlassian@0.23.1"], "env": {"JIRA_API_TOKEN": "secret"}}
    config = {"mcpServers": {"slack": {"type": "http", "url": "https://x"}, "work-jira": atlassian}}
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / ".claude.json").write_text(json.dumps(config))

    servers = json.loads(supervisor.launch_files(9000, FEATURES)["mcp"].read_text())["mcpServers"]

    assert set(servers) == {"sya", "sya_control", "jira"}
    assert servers["jira"]["args"] == ["mcp-atlassian@0.23.1"]
    assert servers["jira"]["env"] == {
        "JIRA_API_TOKEN": "secret",
        "READ_ONLY_MODE": "true",
        "ENABLED_TOOLS": ",".join(supervisor.JIRA_TOOLS),
    }


def test_an_atlassian_server_in_a_container_is_left_out(tmp_path):
    docker = {"command": "docker", "args": ["run", "-i", "ghcr.io/sooperset/mcp-atlassian:latest"]}
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / ".claude.json").write_text(json.dumps({"mcpServers": {"mcp-atlassian": docker}}))

    assert supervisor.jira_server() is None


def test_supervisor_gets_no_shell_and_no_other_mcp_servers():
    cmd = supervisor.command(8765, FEATURES)
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob,ListAgents,SendMessage,ToolSearch"
    assert "--strict-mcp-config" in cmd


def started(tmp_path, sid):
    """Claude Code writes the transcript once the session has started."""
    path = tmp_path / "claude" / "projects" / "-work" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")


def test_command_new_resume_and_bg(tmp_path):
    assert supervisor.session_id() is None
    first = supervisor.command(8765, FEATURES, new=True)
    sid = first[first.index("--session-id") + 1]
    started(tmp_path, sid)
    assert supervisor.session_id() == sid
    assert first[:5] == ["claude", "-n", supervisor.NAME, "--permission-mode", "default"]
    assert "--append-system-prompt-file" in first and "--mcp-config" in first and "--bg" not in first

    again = supervisor.command(8765, FEATURES, background=True)
    assert again[-1] == "--bg" and f"--resume={sid}" in again
    assert "--session-id" not in again

    fresh = supervisor.command(8765, FEATURES, new=True)
    assert fresh[fresh.index("--session-id") + 1] != sid


def test_the_supervisor_is_named_in_english_and_still_answers_to_its_old_name():
    """A supervisor started before is titled «Супервизор» until its next start, and old transcripts say so:
    the board lists both names for the features that recognize it by name (groups, the page)."""
    board = FakeApp([], {}).board()

    assert supervisor.NAME == "Supervisor" and supervisor.command(8765, FEATURES)[2] == "Supervisor"
    assert board["supervisor_names"] == ["Supervisor", "Супервизор"]


OTHER = "22222222-3333-4444-5555-666666666666"


def resumable_supervisor(tmp_path):
    first = supervisor.command(8765, FEATURES, new=True)
    sid = first[first.index("--session-id") + 1]
    started(tmp_path, sid)
    app = FakeApp([{"session_id": s, "cwd": str(tmp_path)} for s in (sid, OTHER)], {})
    app.port = 9000
    return app, sid


def test_a_resumed_supervisor_keeps_its_own_tools_servers_and_mode(tmp_path, monkeypatch):
    spawned = record_resume_argv(monkeypatch)
    app, sid = resumable_supervisor(tmp_path)

    delivery.start_resume(app, sid, "сводку")

    (argv,) = spawned.argv
    assert argv[:3] == ["/usr/bin/claude", "-p", f"--resume={sid}"] and argv[-2:] == ["--", "сводку"]
    assert argv[argv.index("--tools") + 1] == supervisor.BUILTIN_TOOLS
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert "--strict-mcp-config" in argv and "--append-system-prompt-file" in argv
    servers = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())["mcpServers"]
    assert servers["sya_control"]["url"] == "http://127.0.0.1:9000/mcp"  # the dashboard that resumed it


def test_a_resume_of_any_other_session_gets_no_extra_flags(tmp_path, monkeypatch):
    spawned = record_resume_argv(monkeypatch)
    app, _ = resumable_supervisor(tmp_path)
    delivery.start_resume(app, OTHER, "hi")

    assert spawned.argv == [["/usr/bin/claude", "-p", f"--resume={OTHER}", "--", "hi"]]


def test_a_supervisor_that_never_started_is_started_anew():
    first = supervisor.command(8765, FEATURES)
    assert supervisor.session_id() is None
    again = supervisor.command(8765, FEATURES)
    assert not any(a.startswith("--resume") for a in again)
    assert again[again.index("--session-id") + 1] != first[first.index("--session-id") + 1]


def test_session_id_ignores_garbage(tmp_path):
    d = tmp_path / "config" / "supervisor"
    d.mkdir(parents=True)
    (d / "session.json").write_text("not json")
    assert supervisor.session_id() is None


def test_install_agent_refuses_foreign_file_unless_forced(tmp_path):
    path = tmp_path / "claude" / "agents" / "fleet-supervisor.md"
    path.parent.mkdir(parents=True)
    path.write_text("mine")
    with pytest.raises(FileExistsError):
        supervisor.install_agent(FEATURES)
    assert path.read_text() == "mine"
    assert supervisor.install_agent(FEATURES, force=True) == path
    assert path.read_text().startswith("---\nname: fleet-supervisor")
    supervisor.install_agent(FEATURES)  # our own file is replaced without force


def test_cli_print_exec_and_bg(tmp_path, monkeypatch, capsys):
    cli.main(["supervisor", "--new", "--print"])
    assert capsys.readouterr().out.startswith("claude -n ")
    started(
        tmp_path, json.loads((tmp_path / "config" / "supervisor" / "session.json").read_text())["session_id"]
    )

    seen = {}
    monkeypatch.setattr(supervisor.os, "execvp", lambda f, a: seen.update(file=f, argv=a))
    cli.main(["supervisor"])
    assert seen["file"] == "claude" and any(a.startswith("--resume=") for a in seen["argv"])

    monkeypatch.setattr(
        supervisor.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="bg-id\n", stderr=""),
    )
    cli.main(["supervisor", "--bg"])
    assert "bg-id" in capsys.readouterr().out


def tool(name, scope, confirm=False):
    return Tool(name, scope, "", {}, (), lambda app, args: {}, confirm=confirm)


def test_permissions_follow_the_registry():
    extra = Feature(
        id="extra",
        tools=(tool("peek", "read"), tool("wipe", "control", confirm=True), tool("note", "control")),
    )
    perms = supervisor.permissions([*SUPERVISOR_ONLY, extra])

    allowed = {*READS, "mcp__sya__peek", "ListAgents", "mcp__sya_control__post_digest"}
    allowed |= {"mcp__sya_control__note", *(f"mcp__jira__{name}" for name in supervisor.JIRA_TOOLS)}
    assert allowed <= set(perms["allow"])
    assert perms["ask"][0] == "SendMessage" and "mcp__sya_control__wipe" in perms["ask"]
    assert not set(perms["ask"]) & set(perms["allow"])


def test_the_subagent_gets_every_read_tool_and_list_agents(tmp_path):
    path = supervisor.install_agent(SUPERVISOR_ONLY)
    assert f"\ntools: {', '.join([*READS, 'ListAgents'])}\n" in path.read_text()


def test_prompt_is_the_base_plus_each_feature_fragment(tmp_path):
    (tmp_path / "notes.md").write_text("## Notes\n- notes_list\n")
    extra = Feature(id="notes", folder=tmp_path, supervisor_prompt="notes.md")
    base = supervisor.prompt(SUPERVISOR_ONLY)

    assert base == (supervisor.PROMPTS / "supervisor.md").read_text()
    assert supervisor.prompt([*SUPERVISOR_ONLY, extra]) == base.rstrip("\n") + "\n\n## Notes\n- notes_list\n"


# ---- send_to_session -----------------------------------------------------------------------------

CX = uid(0)  # a Codex thread
SEND = next(t for t in mcp_tools.TOOLS if t.name == "send_to_session")
RESUME = next(t for t in mcp_tools.TOOLS if t.name == "resume_session")


def codex_app(live=None):
    """The board with one Codex thread whose first turn ended, as `codex exec` leaves it."""
    turn = uid(1, "a")
    home = Path(os.environ["CODEX_HOME"])
    write(home, CX, meta(CX), turn_started(turn, 1), user_item("start", 1), turn_complete(turn, "done", 1))
    return FakeApp([codex_record(CX)], {CX: live} if live else {})


def codex_live(host="daemon", status="idle"):
    return LiveSession(CX, "interactive", status, host=host, provider="codex")


def test_send_to_session_asks_the_user_first_also_in_a_one_turn_resume_of_the_supervisor(tmp_path):
    qualified = "mcp__sya_control__send_to_session"
    assert (SEND.scope, SEND.confirm, SEND.read_only) == ("control", True, False)
    perms = supervisor.permissions(FEATURES)
    assert qualified in perms["ask"] and qualified not in perms["allow"]

    app, sid = resumable_supervisor(tmp_path)
    args = supervisor.resume_args(app, sid)
    settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
    assert qualified in settings["permissions"]["ask"]


def test_send_to_session_writes_to_codex_through_codex_queue_signed_by_the_supervisor(monkeypatch):
    queue = fake_codex_queue(monkeypatch)

    out = SEND.run(codex_app(codex_live()), {"session_id": CX, "text": "Проверь CI и ответь коротко"})

    assert queue.argv[0][:3] == ["/bin/codex", "queue", f"--thread={CX}"]
    assert queue.message() == mcp_tools.SIGNED + "Проверь CI и ответь коротко"
    assert "SendMessage" not in queue.message()
    assert (out["route"], out["msg_id"]) == ("queue", queue.turns[0]) and "session_thread" in out["result"]


@pytest.mark.parametrize(
    ("live", "reason"),
    [(codex_live("vscode"), codex_queue.HELD["vscode"]), (codex_live(status="busy"), codex_queue.BUSY)],
)
def test_send_to_session_returns_codexs_refusal_and_queues_nothing(monkeypatch, live, reason):
    queue = fake_codex_queue(monkeypatch)

    with pytest.raises(ToolError) as refused:
        SEND.run(codex_app(live), {"session_id": CX, "text": "привет"})

    assert str(refused.value) == f"not sent: {t('en', reason)}" and queue.argv == []


def test_send_to_session_says_when_codex_queued_the_text_but_another_process_holds_the_thread(monkeypatch):
    held = "Warning: message was queued, but could not resume thread {tid}: … already has an active writer\n"
    fake_codex_queue(monkeypatch, answer=None, said=held)

    with pytest.raises(ToolError, match="Another Codex process holds"):
        SEND.run(codex_app(), {"session_id": CX, "text": "привет"})


def test_send_to_session_continues_a_closed_claude_session_with_one_signed_turn(tmp_path, monkeypatch):
    no_processes(monkeypatch)
    spawned = record_resume_argv(monkeypatch)
    app, _ = resumable_supervisor(tmp_path)

    out = SEND.run(app, {"session_id": OTHER, "text": "сводку"})

    (argv,) = spawned.argv
    assert argv[:3] == ["/usr/bin/claude", "-p", f"--resume={OTHER}"]
    assert argv[-1] == mcp_tools.SIGNED + "сводку"
    assert out["route"] == "resume" and out["log"]


def test_send_to_session_refuses_the_supervisors_own_session_and_an_unknown_one(tmp_path, monkeypatch):
    app, sid = resumable_supervisor(tmp_path)

    with pytest.raises(ToolError, match="your own session"):
        SEND.run(app, {"session_id": sid, "text": "привет"})
    with pytest.raises(ToolError, match="^not sent: Session not found"):
        SEND.run(app, {"session_id": "nope", "text": "привет"})


def test_resume_session_sends_the_supervisor_to_send_to_session_for_codex(monkeypatch):
    fake_codex_queue(monkeypatch)

    for app in (codex_app(), codex_app(codex_live())):
        with pytest.raises(ToolError, match="send_to_session"):
            RESUME.run(app, {"session_id": CX, "text": "привет"})
