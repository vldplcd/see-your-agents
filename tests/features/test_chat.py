import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from see_your_agents.features.chat import thread as chat

TS = "2026-10-30T10:00:00.000Z"
HUMAN = {"origin": {"kind": "human"}, "promptSource": "typed"}


def user(content, **extra):
    return {"type": "user", "timestamp": TS, "message": {"role": "user", "content": content}, **extra}


def human(text, **extra):
    return user(text, **HUMAN, **extra)


def assistant(*blocks):
    return {"type": "assistant", "timestamp": TS, "message": {"role": "assistant", "content": list(blocks)}}


def text(body):
    return {"type": "text", "text": body}


def tool_use(tool_id, name, **inp):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def tool_result(tool_id, content="ok", result=None, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}
    return user([block], toolUseResult=result or {})


def dump(*entries) -> str:
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)


class Env:
    """A session whose transcript lives in tmp_path, behind the same lookup the server uses."""

    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "s1.jsonl"
        self.path.write_text("")
        rec = {"session_id": "s1", "path": str(self.path), "provider": "claude"}
        self.app = SimpleNamespace(index=SimpleNamespace(record=lambda sid: rec if sid == "s1" else None))

    def append(self, *entries) -> None:
        with self.path.open("a") as fh:
            fh.write(dump(*entries))

    def get(self, query=None, path="/api/chat/s1"):
        status, ctype, body = chat.handle_get(self.app, path, query or {})
        payload = json.loads(body) if ctype.startswith("application/json") else body
        return status, ctype, payload

    def page(self, **query) -> dict:
        status, _, payload = self.get(query)
        assert status == 200
        return payload


@pytest.fixture
def env(tmp_path):
    chat._threads.clear()
    return Env(tmp_path)


def kinds(page):
    return [m["kind"] for m in page["messages"]]


def test_consecutive_tool_calls_form_one_group_and_errors_are_flagged(env):
    env.append(
        human("fix it"),
        assistant(text("Looking.")),
        assistant(tool_use("t1", "Bash", command="ls -la\necho hi", description="List files")),
        tool_result("t1"),
        assistant({"type": "thinking", "thinking": "hm"}),
        assistant(tool_use("t2", "Edit", file_path="/repo/src/app/main.py")),
        tool_result("t2", "boom", is_error=True),
        assistant(tool_use("t3", "mcp__jira__get_issue", key="X-1")),
        assistant(tool_use("t4", "Grep", pattern="foo", path="/repo/src")),
        assistant(text("Done.")),
        assistant(tool_use("t5", "Skill", skill="simplify-code")),
    )

    page = env.page()

    assert kinds(page) == ["you", "agent", "tools", "agent", "tools"]
    first, last = page["messages"][2], page["messages"][4]
    assert [c["summary"] for c in first["calls"]] == [
        "Bash: List files",
        "Edit: src/app/main.py",
        "jira.get_issue",
        "Grep: foo repo/src",
    ]
    assert [c["error"] for c in first["calls"]] == [False, True, False, False]
    assert last["calls"][0]["summary"] == "Skill: simplify-code"


def test_only_what_the_human_typed_is_a_you_message(env):
    env.append(
        human("first ask"),
        user("<task-notification>done</task-notification>", origin={"kind": "task-notification"}),
        human("<command-name>/rename</command-name>\n<command-args>ledger</command-args>"),
        user("<system-reminder>x</system-reminder>", **HUMAN),
        user("compact summary", isCompactSummary=True),
        user("meta", isMeta=True, **HUMAN),
        assistant(tool_use("t1", "Bash", command="ls")),
        tool_result("t1"),
        {**human("from a subagent"), "isSidechain": True},
        human("last ask"),
    )

    page = env.page()

    assert [(m["kind"], m.get("text")) for m in page["messages"] if m["kind"] == "you"] == [
        ("you", "first ask"),
        ("you", "/rename ledger"),
        ("you", "last ask"),
    ]


def test_question_gets_its_answer_and_plan_and_recap_and_compact(env):
    q = {"header": "Scope", "question": "Which?", "options": [{"label": "A", "description": "first"}]}
    env.append(
        assistant(tool_use("q1", "AskUserQuestion", questions=[q])),
        assistant(tool_use("p1", "ExitPlanMode", plan="1. do it")),
        assistant(tool_use("w1", "Write", file_path="/repo/docs/plan-1.md", content="# Plan")),
        {
            "type": "system",
            "subtype": "away_summary",
            "timestamp": TS,
            "content": "Recap. (disable recaps in /config)",
        },
        {"type": "system", "subtype": "compact_boundary", "timestamp": TS},
    )
    before = env.page()
    assert before["messages"][0]["answer"] is None

    env.append(tool_result("q1", result={"answers": {"Which?": "A"}}))
    after = env.page(after=str(before["after"]))

    assert kinds(before) == ["question", "plan", "plan", "recap", "compact"]
    assert before["messages"][0]["items"][0]["options"] == [{"label": "A", "description": "first"}]
    assert (before["messages"][1]["text"], before["messages"][2]["path"]) == (
        "1. do it",
        "/repo/docs/plan-1.md",
    )
    assert before["messages"][3]["text"] == "Recap."
    assert (after["messages"][0]["id"], after["messages"][0]["answer"]) == (0, "A")


def test_paging_back_and_forward(env):
    env.append(*(entry for i in range(10) for entry in (human(f"q{i}"), assistant(text(f"a{i}")))))

    newest = env.page(limit="6")
    older = env.page(limit="6", before=str(newest["before"]))
    oldest = env.page(limit="100", before=str(older["before"]))

    assert [m["id"] for m in newest["messages"]] == list(range(14, 20))
    assert newest["has_more"] and newest["before"] == 14 and newest["after"] == 20
    assert [m["id"] for m in older["messages"]] == list(range(8, 14))
    assert [m["id"] for m in oldest["messages"]] == list(range(8))
    assert oldest["before"] is None and not oldest["has_more"]
    assert env.page(after="20")["messages"] == []


def test_growth_parses_only_new_bytes_and_ignores_a_half_written_line(env):
    env.append(human("one"))
    assert len(env.page()["messages"]) == 1
    thread = chat._threads[str(env.path)]
    done_at = thread.offset
    full = dump(human("two"))
    with env.path.open("a") as fh:
        fh.write(full[:20])

    assert len(env.page()["messages"]) == 1
    assert thread.offset == done_at

    with env.path.open("a") as fh:
        fh.write(full[20:])
    page = env.page(after="1")

    assert [m["text"] for m in page["messages"]] == ["two"]
    assert thread.offset == done_at + len(full)


def test_tool_group_is_re_sent_while_it_grows(env):
    env.append(assistant(tool_use("t1", "Bash", command="a")))
    cursor = env.page()["after"]
    env.append(tool_result("t1"), assistant(tool_use("t2", "Bash", command="b")))

    page = env.page(after=str(cursor))

    assert [(m["id"], len(m["calls"])) for m in page["messages"]] == [(0, 2)]


def test_image_prompts_are_served_by_url_not_inlined(env):
    png = base64.b64encode(b"\x89PNG-bytes").decode()
    shot = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}}
    svg = {"type": "image", "source": {"type": "base64", "media_type": "image/svg+xml", "data": png}}
    env.append(
        human([{"type": "text", "text": "see this"}, shot, svg], uuid="u-1"),
        human([shot], uuid="u-2"),
    )

    page = env.page()

    assert [m["text"] for m in page["messages"]] == ["see this", ""]
    assert page["messages"][0]["images"][0] == {"url": "image/u-1/0", "media_type": "image/png"}
    assert png not in json.dumps(page)
    status, ctype, body = env.get(path="/api/chat/s1/image/u-1/0")
    assert (status, ctype, body) == (200, "image/png", b"\x89PNG-bytes")
    assert env.get(path="/api/chat/s1/image/u-2/0")[0] == 200
    assert env.get(path="/api/chat/s1/image/u-1/1")[0] == 404  # SVG is never served back
    assert env.get(path="/api/chat/s1/image/u-1/5")[0] == 404
    assert env.get(path="/api/chat/s1/image/nope/0")[0] == 404


def test_unknown_session_and_routes(env):
    assert chat.handle_get(env.app, "/api/chat/other", {})[0] == 404
    assert chat.handle_get(env.app, "/api/chat/s1/elsewhere", {}) is None


def test_cross_session_send_is_a_peer_out_message_and_its_result_sets_ok(env):
    send = assistant(tool_use("t1", "SendMessage", to="bob", message="run tests", summary="tests"))
    env.append(human("delegate"), send, send)
    first = env.page()

    env.append(
        tool_result("t1", result={"success": False, "message": "no such session", "msg_id": "m-1"}),
        assistant(tool_use("t2", "Bash", command="ls")),
    )
    page = env.page(after=str(first["after"]))

    assert kinds(first) == ["you", "peer_out"]
    out = first["messages"][1]
    assert (out["to"], out["text"], out["summary"], out["ok"]) == ("bob", "run tests", "tests", None)
    assert first["after"] == out["id"]  # still open: the client re-fetches it
    assert [(m["kind"], m.get("ok")) for m in page["messages"]] == [("peer_out", False), ("tools", None)]
    assert [m["id"] for m in page["messages"]] == [1, 2]


def test_send_to_a_subagent_stays_an_ordinary_tool_line(env):
    env.append(assistant(tool_use("t1", "SendMessage", to="abcf4d454f52987d4", message="fix", summary="fix")))

    page = env.page()

    assert kinds(page) == ["tools"]
    assert page["messages"][0]["calls"][0]["summary"] == "SendMessage"


def test_peer_message_is_a_peer_in_message_and_a_hand_back_is_not(env):
    origin = {
        "kind": "peer",
        "from": "uds:/tmp/cc-socks/1.sock",
        "msg_id": "m-1",
        "name": "alice",
        "body": "hi",
    }
    env.append(
        user("wrapped hi", isMeta=True, origin=origin),
        user("[Subagent hand-back]", isMeta=True, origin={**origin, "senderTaskId": "abc"}),
    )

    page = env.page()

    assert kinds(page) == ["peer_in"]
    msg = page["messages"][0]
    assert (msg["from"], msg["text"], msg["msg_id"]) == ("alice", "hi", "m-1")


def queued(prompt, origin, **extra):
    """A message that came while the agent worked, as Claude Code 2.1.318 writes it."""
    body = {"type": "queued_command", "prompt": prompt, "commandMode": "prompt", "origin": origin, **extra}
    return {"type": "attachment", "timestamp": TS, "attachment": body}


def test_a_message_that_came_mid_turn_is_shown_by_who_sent_it(env):
    peer = {"kind": "peer", "name": "Dashboard", "msg_id": "m-1", "body": "by courier"}
    env.append(
        human("start"),
        queued("typed while it worked", {"kind": "human"}, humanTurn=True),
        queued("<cross-session-message>by courier</cross-session-message>", peer),
        queued("<task-notification>done</task-notification>", {"kind": "task-notification"}),
        queued("[Subagent hand-back]", {**peer, "senderTaskId": "abc"}),
    )

    page = env.page()

    assert [(m["kind"], m["text"]) for m in page["messages"]] == [
        ("you", "start"),
        ("you", "typed while it worked"),
        ("peer_in", "by courier"),
    ]
    assert (page["messages"][2]["from"], page["messages"][2]["msg_id"]) == ("Dashboard", "m-1")


def test_a_named_send_that_reached_a_subagent_becomes_a_tool_line(env):
    env.append(
        assistant(tool_use("t1", "SendMessage", to="researcher", message="dig", summary="dig")),
        tool_result("t1", result={"success": True, "message": "queued for researcher"}),
    )

    page = env.page()

    assert kinds(page) == ["tools"]
    assert page["messages"][0]["calls"][0]["summary"] == "SendMessage: researcher"
