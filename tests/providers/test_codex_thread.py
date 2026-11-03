"""A Codex rollout in the chat panel: through the chat's own handler, as the page asks for it."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from codex_rollouts import (
    agent_item,
    call,
    dump,
    event,
    fork_rollout,
    harness_text,
    line,
    meta,
    output,
    patch,
    turn_started,
    uid,
    user_item,
    write,
)

from see_your_agents.features.chat import thread as chat

THREAD = uid(0)


class Env:
    def __init__(self, home: Path, *entries):
        self.path = write(home, THREAD, *entries)
        rec = {"session_id": THREAD, "path": str(self.path), "provider": "codex"}
        self.app = SimpleNamespace(index=SimpleNamespace(record=lambda sid: rec if sid == THREAD else None))

    def append(self, *entries):
        with self.path.open("a") as fh:
            fh.write(dump(*entries))

    def page(self, **query) -> dict:
        status, _, body = chat.handle_get(self.app, f"/api/chat/{THREAD}", query)
        assert status == 200
        return json.loads(body)


@pytest.fixture
def home():
    chat._threads.clear()
    return Path(os.environ["CODEX_HOME"])


def test_prompts_answers_and_grouped_tool_calls_make_the_chat(home):
    env = Env(
        home,
        meta(THREAD),
        turn_started(uid(1), 1),
        harness_text("# AGENTS.md instructions for /work/app", 1),
        user_item("run the tests", 1),
        agent_item("on it", "commentary", 1),
        call("c1", "exec_command", 1, cmd="uv run pytest\n-q"),
        output("c1", "Chunk ID: 1\nWall time: 3 seconds\nProcess exited with code 1\n2 failed", 1),
        patch("c2", "src/app/core/a.py", "tests/b.py", minute=2),
        output("c2", json.dumps({"output": "Success.", "metadata": {"exit_code": 0}}), 2, custom=True),
        line(
            "response_item",
            {"type": "web_search_call", "status": "completed", "action": {"query": "pytest -q"}},
            2,
        ),
        event("an_event_from_a_later_cli", 2),
        agent_item("fixed both", minute=3),
        line("compacted", {"message": ""}, 4),
    )

    msgs = env.page()["messages"]

    assert [m["kind"] for m in msgs] == ["you", "agent", "tools", "agent", "compact"]
    assert msgs[0]["text"] == "run the tests"
    assert [(c["summary"], c["error"]) for c in msgs[2]["calls"]] == [
        ("exec_command: uv run pytest", True),
        ("apply_patch: app/core/a.py, tests/b.py", False),  # the last three parts, as for Claude
        ("web_search: pytest -q", False),
    ]


def test_a_call_still_running_is_re_sent_until_its_output_arrives(home):
    env = Env(
        home,
        meta(THREAD),
        turn_started(uid(1), 1),
        user_item("go", 1),
        call("c1", "exec_command", 1, cmd="make"),
    )

    first = env.page()
    assert first["after"] == 1  # the tool group may still change

    env.append(output("c1", "Exit code: 2\nWall time: 1 seconds", 2), agent_item("make failed", minute=2))
    later = env.page(after=str(first["after"]))
    assert [(m["kind"], m.get("calls", [{}])[0].get("error")) for m in later["messages"]] == [
        ("tools", True),
        ("agent", None),
    ]


def test_a_fork_chat_starts_with_its_own_turn_not_with_the_parents_history(home):
    child = uid(5)
    path = write(home, child, *fork_rollout(THREAD, child, at=5))
    rec = {"session_id": child, "path": str(path), "provider": "codex"}
    app = SimpleNamespace(index=SimpleNamespace(record=lambda sid: rec))

    _, _, body = chat.handle_get(app, f"/api/chat/{child}", {})
    msgs = json.loads(body)["messages"]

    assert [(m["kind"], m.get("text")) for m in msgs] == [
        ("you", "the fork's ask"),
        ("tools", None),
        ("agent", "CI is green"),
    ]


def test_a_subagents_report_reaches_the_parents_chat(home):
    text = [{"type": "input_text", "text": "Message Type: FINAL_ANSWER\nCI is green"}]
    report = {"type": "agent_message", "author": "/root/ci_runner", "recipient": "/root", "content": text}
    env = Env(
        home,
        meta(THREAD),
        turn_started(uid(1), 1),
        user_item("review it", 1),
        line("response_item", report, 2),
    )

    msgs = env.page()["messages"]

    assert (msgs[-1]["kind"], msgs[-1]["from"]) == ("peer_in", "/root/ci_runner")
