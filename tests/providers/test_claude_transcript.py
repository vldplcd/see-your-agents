import json
from pathlib import Path

import pytest

from see_your_agents.core.jira import jira_keys_in
from see_your_agents.core.providers.claude.transcript import parse_transcript

TS = "2026-10-30T10:00:00.000Z"


def user(content, **extra):
    return {"type": "user", "timestamp": TS, "message": {"role": "user", "content": content}, **extra}


def assistant(*blocks):
    return {"type": "assistant", "timestamp": TS, "message": {"role": "assistant", "content": list(blocks)}}


def tool_use(tool_id, name, **inp):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def tool_result(tool_id, content="ok", result=None):
    return user(
        [{"type": "tool_result", "tool_use_id": tool_id, "content": content}], toolUseResult=result or {}
    )


def write(tmp_path: Path, *entries) -> Path:
    path = tmp_path / "1b2c3d4e-0000-0000-0000-000000000000.jsonl"
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n")
    return path


HUMAN = {"origin": {"kind": "human"}, "promptSource": "typed"}


def test_only_web_addresses_become_pr_links(tmp_path):
    """A PR/MR link ends up as an href on the page: javascript: or data: from a transcript is dropped."""
    links = [
        {"type": "pr-link", "prUrl": "javascript:alert(1)", "prNumber": 1},
        {"type": "pr-link", "prUrl": "data:text/html,<script>1</script>", "prNumber": 2},
        {"type": "pr-link", "prUrl": "https://gitlab.example.com/g/p/-/merge_requests/3", "prNumber": 3},
    ]
    digest = parse_transcript(write(tmp_path, user("hi", **HUMAN), *links))
    assert [p["number"] for p in digest.prs] == [3]


def test_counts_only_what_the_human_typed(tmp_path):
    digest = parse_transcript(
        write(
            tmp_path,
            user("first ask", **HUMAN),
            user(
                "<task-notification>agent done</task-notification>",
                origin={"kind": "task-notification"},
                promptSource="system",
            ),
            user("<command-name>/rename</command-name>\n<command-args>ledger fix</command-args>", **HUMAN),
            user('\n<pasted_content id="ab">long paste</pasted_content>', **HUMAN),
            user("This session is being continued from a previous conversation", isCompactSummary=True),
            assistant(tool_use("t1", "Bash", command="ls")),
            tool_result("t1"),
        )
    )

    assert [e["text"] for e in digest.events if e["kind"] == "prompt"] == [
        "first ask",
        "/rename ledger fix",
        "long paste",
    ]
    assert digest.first_prompt == "first ask"
    assert digest.n_prompts == 3


def test_ticket_from_title_outranks_prompts_and_shared_checkout_branch(tmp_path):
    digest = parse_transcript(
        write(
            tmp_path,
            {**user("look at PROJ-12 and PROJ-12 again", **HUMAN), "gitBranch": "PROJ-99-cold-storage"},
            {"type": "ai-title", "aiTitle": "PROJ-7 ledger-gc fixes"},
        )
    )

    assert digest.jira_keys == ["PROJ-7", "PROJ-12", "PROJ-99"]


@pytest.mark.parametrize(
    ("text", "any_case", "keys"),
    [
        ("chore/release-0.38.0", True, []),
        ("decode as UTF-8, hash with SHA-256", False, []),
        ("proj-123-fixes", True, ["PROJ-123"]),
        ("see PROJ-456 and MR !18", False, ["PROJ-456"]),
    ],
)
def test_ticket_keys_skip_version_and_encoding_lookalikes(text, any_case, keys):
    assert jira_keys_in(text, any_case=any_case) == keys


def test_recap_is_kept_without_the_config_hint(tmp_path):
    digest = parse_transcript(
        write(
            tmp_path,
            {
                "type": "system",
                "subtype": "away_summary",
                "timestamp": TS,
                "content": "MR !101 merged; next step is yours. (disable recaps in /config)",
            },
        )
    )

    assert digest.recap == "MR !101 merged; next step is yours."


def test_question_answer_and_unanswered_tool(tmp_path):
    digest = parse_transcript(
        write(
            tmp_path,
            assistant(
                tool_use(
                    "q1",
                    "AskUserQuestion",
                    questions=[
                        {
                            "header": "Scope",
                            "question": "Which source?",
                            "options": [{"label": "A"}, {"label": "B"}],
                        }
                    ],
                )
            ),
            tool_result("q1", result={"answers": {"Which source?": "B"}}),
            assistant(
                tool_use("q2", "AskUserQuestion", questions=[{"question": "Merge now?", "options": []}])
            ),
        )
    )

    assert [q["answer"] for q in digest.questions] == ["B", None]
    assert digest.pending_tool == "AskUserQuestion"


def test_task_list_replay_uses_ids_from_tool_results(tmp_path):
    digest = parse_transcript(
        write(
            tmp_path,
            assistant(tool_use("c1", "TaskCreate", subject="Fix GC", activeForm="Fixing GC")),
            tool_result("c1", "Task #4 created successfully: Fix GC"),
            assistant(tool_use("c2", "TaskCreate", subject="Write docs", activeForm="Writing docs")),
            tool_result("c2", "Task #5 created successfully: Write docs"),
            assistant(tool_use("u1", "TaskUpdate", taskId="4", status="completed")),
            assistant(tool_use("u2", "TaskUpdate", taskId="5", status="in_progress")),
        )
    )

    assert [(t["id"], t["status"]) for t in digest.tasks] == [("4", "completed"), ("5", "in_progress")]


def test_plan_files_are_collected_relative_to_cwd(tmp_path):
    digest = parse_transcript(
        write(
            tmp_path,
            {
                **assistant(tool_use("w1", "Write", file_path="/repo/docs/plan-1-guards.md", content="x")),
                "cwd": "/repo",
            },
            {**assistant(tool_use("b1", "Bash", command="vplan check plans/status.mdx")), "cwd": "/repo"},
            assistant(tool_use("w2", "Write", file_path="/repo/src/app.py", content="x")),
        )
    )

    assert [p["path"] for p in digest.plans] == ["/repo/docs/plan-1-guards.md", "/repo/plans/status.mdx"]


def test_half_written_last_line_is_ignored(tmp_path):
    path = write(tmp_path, user("hello", **HUMAN))
    with path.open("a") as fh:
        fh.write('{"type":"user","message":{"content":"unfinish')

    assert parse_transcript(path).n_prompts == 1


def test_tool_usage_counts_skills_agents_mcp_and_typed_commands(tmp_path):
    later = "2026-10-31T09:00:00.000Z"
    digest = parse_transcript(
        write(
            tmp_path,
            user("<command-name>/model</command-name>\n<command-args>opus</command-args>", **HUMAN),
            assistant(
                tool_use("s1", "Skill", skill="superpowers:brainstorming"),
                tool_use("a1", "Agent", description="x", subagent_type="Explore"),
                tool_use("a2", "Task", description="y"),
                tool_use("m1", "mcp__slack-mcp__channels_list"),
                tool_use("m2", "mcp__slack-mcp__messages_search"),
            ),
            {**assistant(tool_use("s2", "Skill", skill="superpowers:brainstorming")), "timestamp": later},
        )
    )

    assert digest.tool_usage == {
        "command:model": [1, TS],
        "skill:superpowers:brainstorming": [2, later],
        "agent:Explore": [1, TS],
        "agent:general-purpose": [1, TS],
        "mcp:slack-mcp": [2, TS],
        "mcp_tool:slack-mcp/channels_list": [1, TS],
        "mcp_tool:slack-mcp/messages_search": [1, TS],
    }


def send_result(
    tool_id, success=True, said="sent to bob — another Claude session on this machine", msg_id="m-1"
):
    return tool_result(
        tool_id,
        result={"success": success, "message": said, "display": said, "msg_id": msg_id},
    )


def peer(body, name="alice", msg_id="m-1", **origin):
    return user(
        f"Another Claude session sent a message:\n{body}",
        isMeta=True,
        origin={
            "kind": "peer",
            "from": "uds:/tmp/cc-socks/1.sock",
            "msg_id": msg_id,
            "name": name,
            "body": body,
        }
        | origin,
        turnOrigin="peer",
    )


def test_cross_session_send_is_recorded_with_its_msg_id_and_an_event(tmp_path):
    send = assistant(tool_use("t1", "SendMessage", to="bob", message="run the tests", summary="tests"))

    digest = parse_transcript(write(tmp_path, send, send_result("t1")))

    assert digest.messages_out == [
        {"ts": TS, "to": "bob", "summary": "tests", "text": "run the tests", "msg_id": "m-1", "ok": True}
    ]
    assert [(e["kind"], e["text"]) for e in digest.events] == [("msg_out", "→ bob: tests")]


def test_a_send_written_twice_by_claude_code_is_counted_once(tmp_path):
    send = assistant(tool_use("t1", "SendMessage", to="bob", message="hi"))

    digest = parse_transcript(write(tmp_path, send, send, send_result("t1")))

    assert len(digest.messages_out) == 1
    assert [e["kind"] for e in digest.events] == ["msg_out"]


def test_send_to_a_subagent_id_is_not_a_cross_session_message(tmp_path):
    digest = parse_transcript(
        write(
            tmp_path,
            assistant(tool_use("t1", "SendMessage", to="abcf4d454f52987d4", message="fix it")),
            assistant(tool_use("t2", "SendMessage", to="afork-0123456789abcdef", message="and this")),
        )
    )

    assert digest.messages_out == []
    assert digest.events == []


def test_a_named_target_that_the_result_calls_a_subagent_is_dropped(tmp_path):
    digest = parse_transcript(
        write(
            tmp_path,
            assistant(tool_use("t1", "SendMessage", to="reviewer", message="go")),
            send_result("t1", said="queued for subagent reviewer"),
        )
    )

    assert digest.messages_out == []
    assert digest.events == []


def test_failed_send_is_kept_with_ok_false(tmp_path):
    digest = parse_transcript(
        write(
            tmp_path,
            assistant(tool_use("t1", "SendMessage", to="bob", message="hi")),
            send_result("t1", success=False, said="no such session"),
        )
    )

    assert [m["ok"] for m in digest.messages_out] == [False]


def test_peer_message_is_recorded_as_received_and_is_not_a_prompt(tmp_path):
    digest = parse_transcript(write(tmp_path, peer("please review", name="alice", msg_id="m-7")))

    assert digest.messages_in == [{"ts": TS, "from": "alice", "text": "please review", "msg_id": "m-7"}]
    assert [(e["kind"], e["text"]) for e in digest.events] == [("msg_in", "← alice: please review")]
    assert digest.n_prompts == 0 and digest.last_prompt == ""


def test_a_peer_message_keeps_the_last_text_the_session_wrote_before_the_human_spoke(tmp_path):
    digest = parse_transcript(
        write(
            tmp_path,
            peer("что с CI?", msg_id="m-7"),
            assistant({"type": "text", "text": "смотрю"}),
            assistant({"type": "text", "text": "CI зелёный"}),
            user("а теперь другое", **HUMAN),
            assistant({"type": "text", "text": "делаю другое"}),
        )
    )

    assert digest.messages_in[0]["reply"] == "CI зелёный"


def test_replies_count_assistant_messages_with_text_once_each(tmp_path):
    text = assistant({"type": "text", "text": "готово"})
    text["message"]["id"] = "msg_1"
    only_tool = assistant(tool_use("t1", "Read", file_path="/x"))

    digest = parse_transcript(write(tmp_path, text, text, only_tool))

    assert digest.n_replies == 1


def test_subagent_hand_back_is_not_a_received_message(tmp_path):
    digest = parse_transcript(write(tmp_path, peer("[Subagent hand-back] report", senderTaskId="abc")))

    assert digest.messages_in == []
    assert digest.events == []


def test_a_named_send_that_reached_a_subagent_is_dropped_even_when_its_result_repeats(tmp_path):
    send = assistant(tool_use("t1", "SendMessage", to="researcher", message="dig", summary="dig"))
    result = tool_result("t1", result={"success": True, "message": "queued for researcher"})

    digest = parse_transcript(write(tmp_path, send, result, result, send))

    assert digest.messages_out == [] and not [e for e in digest.events if e["kind"] == "msg_out"]


def test_a_send_waiting_for_approval_has_no_outcome_yet(tmp_path):
    digest = parse_transcript(
        write(tmp_path, assistant(tool_use("t1", "SendMessage", to="bob", message="go")))
    )

    assert digest.messages_out[0]["ok"] is None


def test_joined_peer_messages_keep_their_text_and_sender(tmp_path):
    envelope = (
        '<cross-session-message from="uds:/tmp/cc-socks/1.sock" from-name="alice" from-mode="prompting">'
    )
    joined = user(
        f"Another Claude session sent a message:\n{envelope}\na\n</cross-session-message>",
        isMeta=True,
        origin={
            "kind": "peer",
            "from": "uds:/tmp/cc-socks/1.sock",
            "msg_id": "m",
            "members": [{"body": "a"}, {"body": "b"}],
        },
    )

    msg = parse_transcript(write(tmp_path, joined)).messages_in[0]

    assert (msg["from"], msg["text"]) == ("alice", "a\n\nb")


# ---- the context window ----------------------------------------------------------------------------


def reply(model, fresh=0, written=0, read=0, **extra):
    """An assistant reply with the usage of the request behind it."""
    entry = assistant({"type": "text", "text": "ok"})
    usage = {"input_tokens": fresh, "cache_creation_input_tokens": written, "cache_read_input_tokens": read}
    entry["message"].update(model=model, usage={**usage, "output_tokens": 900})
    return {**entry, **extra}


def compacted(post):
    meta = {"trigger": "auto", "preTokens": 190_000, "postTokens": post}
    return {"type": "system", "subtype": "compact_boundary", "timestamp": TS, "compactMetadata": meta}


def context(tmp_path, *entries):
    digest = parse_transcript(write(tmp_path, *entries))
    return digest.context_tokens, digest.context_window


def test_context_is_what_the_latest_request_carried_input_cache_writes_and_reads(tmp_path):
    haiku = "claude-haiku-4-5-20251001"
    entries = [
        reply(haiku, fresh=9_000),
        reply(haiku, fresh=3, written=2_000, read=120_000),
        reply(haiku, read=190_000, isSidechain=True),  # a subagent's own context
        reply("<synthetic>"),  # an API error Claude Code writes as a reply: all zeros
    ]

    assert context(tmp_path, *entries) == (122_003, 200_000)


def test_a_compaction_leaves_what_it_kept_until_the_next_reply(tmp_path):
    before = reply("claude-opus-4-6", read=185_000)
    after = reply("claude-opus-4-6", written=46_000)

    assert context(tmp_path, before, compacted(31_000)) == (31_000, 200_000)
    assert context(tmp_path, before, compacted(31_000), after) == (46_000, 200_000)


def test_a_compaction_is_indexed_as_its_kind_and_no_words(tmp_path):
    """The index holds no UI language: the session panel words the compact event in the page's."""
    events = parse_transcript(write(tmp_path, compacted(31_000))).events
    assert [(e["kind"], e["text"]) for e in events] == [("compact", "")]


@pytest.mark.parametrize(
    ("model", "cost_state", "tokens", "window"),
    [
        ("claude-opus-5-5", None, 50_000, 1_000_000),  # Claude 5 has only the 1M window
        ("claude-haiku-5-5", None, 59_263, 1_000_000),  # Claude Code's own status line said 6 %
        ("claude-opus-4-6", "claude-opus-4-6[1m]", 50_000, 1_000_000),  # run as opus[1m]
        ("claude-sonnet-4-5-20250929", None, 250_000, 1_000_000),  # past 200k: it had 1M
        ("claude-sonnet-4-5-20250929", None, 150_000, 200_000),
        ("claude-3-7-sonnet-20250219", None, 10_000, 200_000),
    ],
)
def test_the_window_is_1m_for_claude_5_the_1m_suffix_or_a_context_past_200k(
    tmp_path, model, cost_state, tokens, window
):
    costs = {"type": "cost-state", "totalCostUSD": 1.0, "modelUsage": {cost_state: {}}} if cost_state else {}

    assert context(tmp_path, reply(model, read=tokens), costs) == (tokens, window)


def test_a_session_without_usage_has_no_window_to_show(tmp_path):
    assert context(tmp_path, user("hi", **HUMAN), compacted(1_000)) == (1_000, 0)
