import json
import subprocess

from see_your_agents.core.providers.claude import courier

TEXT = 'строка с "кавычками"\nи `кодом`'


def send(n: int, to="misc-27", message=TEXT) -> list[dict]:
    use = {"type": "tool_use", "id": f"toolu_{n}", "name": "SendMessage"}
    use["input"] = {"to": to, "summary": "группа", "message": message}
    # the CLI may emit an assistant entry twice
    return [{"type": "assistant", "message": {"content": [use]}}] * 2


def result(n: int, success=True, said="sent") -> dict:
    """Claude Code's answer to a send that got past the hook (shape of CLI 2.1.314)."""
    block = {"type": "tool_result", "tool_use_id": f"toolu_{n}", "content": said}
    outcome = {"success": success, "message": said, "msg_id": f"m-{n}"}
    return {"type": "user", "message": {"content": [block]}, "tool_use_result": outcome}


def refused(n: int) -> dict:
    """The hook refused the send: an error result without Claude Code's outcome object."""
    block = {"type": "tool_result", "tool_use_id": f"toolu_{n}", "is_error": True, "content": courier.REFUSAL}
    return {"type": "user", "message": {"content": [block]}, "tool_use_result": "Error: " + courier.REFUSAL}


def stream(*entries) -> str:
    lines = [{"type": "system", "subtype": "init"}, *entries, {"type": "result", "subtype": "success"}]
    return "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\nnot json\n"


def test_delivered_when_the_courier_sent_the_text_verbatim_to_the_right_session():
    got = courier.read_stream(stream(*send(0), result(0)), "misc-27", TEXT)
    assert got == courier.Delivery(True, msg_id="m-0")


def test_delivered_after_the_hook_refused_a_paraphrase_and_the_courier_copied_again():
    entries = [*send(0, message="строка с кавычками"), refused(0), *send(1), result(1)]
    assert courier.read_stream(stream(*entries), "misc-27", TEXT) == courier.Delivery(True, msg_id="m-1")


def test_nothing_delivered_when_every_attempt_was_refused():
    entries = [*send(0, message="x"), refused(0), *send(1, message="y"), refused(1)]
    got = courier.read_stream(stream(*entries), "misc-27", TEXT)
    assert (got.ok, got.error) == (False, "app.provider.claude.courier.not_verbatim")
    assert got.problem().text("en") == "The courier couldn’t copy the text verbatim"
    assert got.problem().text("ru") == "курьер не смог переписать текст дословно"


def test_a_paraphrase_that_got_out_anyway_is_not_delivered():
    got = courier.read_stream(stream(*send(0, message="строка"), result(0)), "misc-27", TEXT)
    assert (got.ok, got.error) == (False, "app.provider.claude.courier.distorted")


def test_a_send_to_another_session_is_not_delivered():
    got = courier.read_stream(stream(*send(0, to="misc-ea"), result(0)), "misc-27", TEXT)
    assert not got.ok and "misc-ea" in str(got.problem())


def test_two_sends_that_went_out_are_a_failure():
    got = courier.read_stream(stream(*send(0), result(0), *send(1), result(1)), "misc-27", TEXT)
    assert (got.ok, got.error) == (False, "app.provider.claude.courier.sent_twice")


def test_claude_code_refusal_is_passed_on():
    said = "No agent named 'misc-27' is reachable."
    got = courier.read_stream(stream(*send(0), result(0, success=False, said=said)), "misc-27", TEXT)
    assert not got.ok and got.problem().text("en") == got.problem().text("ru") == said


def test_no_send_at_all():
    assert courier.read_stream(stream(), "misc-27", TEXT).error == "app.provider.claude.courier.no_send"


def test_the_fence_never_occurs_in_the_text(tmp_path):
    cmd = courier.command("claude", "misc-27", "a\n========\nb", "s", "Группа", tmp_path / "s.json")
    assert "\n=========\n" in cmd[2] and "--tools=SendMessage" in cmd


def run_guard(tmp_path, stdin: str) -> subprocess.CompletedProcess:
    """The hook exactly as Claude Code runs it: the settings' command, the call as JSON on stdin."""
    settings = json.loads(courier.write_guard(tmp_path, "misc-27", TEXT).read_text())
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    return subprocess.run(hook, shell=True, input=stdin, capture_output=True, text=True)


def call(**tool_input) -> str:
    return json.dumps({"tool_name": "SendMessage", "tool_input": tool_input})


def test_the_hook_lets_through_only_the_exact_send(tmp_path):
    assert run_guard(tmp_path, call(to="misc-27", message=TEXT, summary="s")).returncode == 0
    paraphrase = run_guard(tmp_path, call(to="misc-27", message=TEXT.replace('"', "«")))
    assert (paraphrase.returncode, paraphrase.stderr.strip()) == (2, courier.REFUSAL)
    assert run_guard(tmp_path, call(to="misc-ea", message=TEXT)).returncode == 2


def test_the_hook_blocks_when_it_cannot_judge(tmp_path):
    """Claude Code lets a call through on any hook failure but exit 2, so every failure must be a 2."""
    for stdin in ("not json", "[]", '{"tool_input": "text"}'):
        assert run_guard(tmp_path, stdin).returncode == 2


def test_the_courier_runs_with_the_clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SYA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(courier.shutil, "which", lambda n: "/bin/claude")
    envs = []

    def fake(cmd, **kw):
        envs.append(kw["env"])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(courier.subprocess, "run", fake)
    courier.deliver("w", "hi", "hi", "Dashboard")

    assert envs and "CLAUDECODE" not in envs[0]
