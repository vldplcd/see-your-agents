"""The todo list: storage and validation, Jira links, the header counts, the MCP tools, the morning button."""

import json
import stat
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from fakes import FakeApp, no_processes, pair, record_resume_argv

from see_your_agents import config_dir
from see_your_agents.app import mcp, server
from see_your_agents.core.providers.base import LiveSession
from see_your_agents.core.providers.claude import courier
from see_your_agents.core.providers.claude import routes as claude_routes
from see_your_agents.core.providers.claude.transcript import REPLY_NOTE
from see_your_agents.features import FEATURES, todo
from see_your_agents.features.supervisor import launch

SUPERVISOR = "sup-1"
RECORDS = [
    {"session_id": "s1", "title": "Чиним CI", "cwd": "/w/p", "last_activity": "2026-11-01T00:00:00Z"},
    {"session_id": SUPERVISOR, "title": "Супервизор", "cwd": "/w/s", "last_activity": "2026-11-01T00:00:00Z"},
]
LIVE = {
    SUPERVISOR: LiveSession(
        provider="claude", session_id=SUPERVISOR, kind="interactive", status="idle", name="supervisor-x"
    )
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SYA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SYA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("SYA_JIRA_URL", "https://jira.example.com/")
    no_processes(monkeypatch)
    todo.todo._morning.update(state="idle", error="", at="")


PAIRED: dict[str, dict[str, str]] = {}  # base url -> the paired browser's header; a mutating POST needs it


def serve(app, tmp_path=None):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(app, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    PAIRED[url] = pair(app, port)
    return httpd, url


@pytest.fixture
def base(tmp_path):
    httpd, url = serve(FakeApp(RECORDS, LIVE))
    yield url
    httpd.shutdown()


def fetch(url, payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def post(base, body, path="/api/todo", lang=None):
    page = {"Origin": base, "X-SYA": "1", "Content-Type": "application/json", **PAIRED.get(base, {})}
    return fetch(f"{base}{path}", body, {**page, **({"X-SYA-Lang": lang} if lang else {})})


def add(base, text, **extra):
    return post(base, {"action": "add", "text": text, **extra})[1]["items"][-1]


def test_add_update_delete_and_clear_done(base):
    first = add(base, "Написать ревью")
    second = add(base, "Позвонить", urgent=True)

    assert first["urgent"] is False and first["done"] is False and first["source"] == "you"
    assert second["urgent"] is True
    status, body = post(base, {"action": "update", "id": first["id"], "done": True, "text": " Ревью готово "})
    done = next(i for i in body["items"] if i["id"] == first["id"])
    assert status == 200 and done["done"] and done["text"] == "Ревью готово" and done["done_at"]
    assert (body["urgent"], body["open"]) == (1, 1)

    post(base, {"action": "update", "id": first["id"], "done": False})
    assert fetch(f"{base}/api/todo")[1]["items"][0]["done_at"] is None
    post(base, {"action": "update", "id": first["id"], "done": True})
    assert [i["id"] for i in post(base, {"action": "clear_done"})[1]["items"]] == [second["id"]]
    assert post(base, {"action": "delete", "id": second["id"]})[1]["items"] == []


def test_a_bang_makes_the_item_urgent_and_is_stripped(base):
    item = add(base, "  ! Срочно позвонить")

    assert item["urgent"] is True and item["text"] == "Срочно позвонить"
    assert post(base, {"action": "add", "text": "!  "})[0] == 400


def test_the_file_is_private_and_written_atomically(base):
    add(base, "Раз")

    path = config_dir() / "todo.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())[0]["text"] == "Раз"
    assert [p.name for p in path.parent.iterdir() if p.name.startswith(".todo-")] == []


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"action": "add", "text": ""}, 400),
        ({"action": "add", "text": "x" * 501}, 400),
        ({"action": "add", "text": 5}, 400),
        ({"action": "add", "text": "ok", "urgent": "yes"}, 400),
        ({"action": "update", "id": "nope", "done": True}, 404),
        ({"action": "delete", "id": "nope"}, 404),
        ({"action": "update"}, 404),
        ({"action": "explode"}, 400),
        ({}, 400),
    ],
)
def test_bad_requests_are_refused_and_change_nothing(base, body, status):
    keep = add(base, "Остаётся")

    assert post(base, body)[0] == status
    assert fetch(f"{base}/api/todo")[1]["items"][0]["id"] == keep["id"]


def test_update_needs_a_field_and_valid_values(base):
    item = add(base, "Дело")

    for bad in ({"text": ""}, {"urgent": 1}, {"done": "x"}, {"session_id": 5}, {}):
        assert post(base, {"action": "update", "id": item["id"], **bad})[0] == 400


def test_the_list_is_capped(base):
    for n in range(todo.todo.MAX_ITEMS):
        todo.todo.apply({"action": "add", "text": f"дело {n}"})

    status, body = post(base, {"action": "add", "text": "лишнее"})
    assert (status, body["error"]) == (400, "The list already has 300 to-dos: clear the done ones")
    status, body = post(base, {"action": "add", "text": "лишнее"}, lang="ru")
    assert (status, body["error"]) == (400, "в списке уже 300 дел: очисти сделанные")


def test_the_post_keeps_the_page_guard(base):
    req = urllib.request.Request(
        f"{base}/api/todo", json.dumps({"action": "clear_done"}).encode(), headers={"Origin": base}
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 403


def test_jira_keys_become_links_and_the_session_gets_its_title(base):
    item = add(base, "Проверить PROJ-123 и proj-9 и PROJ-123 снова", session_id="s1")

    assert item["jira"] == [{"key": "PROJ-123", "url": "https://jira.example.com/browse/PROJ-123"}]
    assert item["session_title"] == "Чиним CI"
    assert "jira" not in json.loads((config_dir() / "todo.json").read_text())[0]
    assert "session_title" not in add(base, "Без заголовка", session_id="unknown")


def test_update_attaches_a_started_session_and_an_empty_id_detaches_it(base):
    """«Запустить» ("Launch") on an item attaches the session it started through this update."""
    item = add(base, "PROJ-123 починить CI")

    linked = post(base, {"action": "update", "id": item["id"], "session_id": "s1"})[1]["items"][0]
    assert linked["session_id"] == "s1" and linked["session_title"] == "Чиним CI"
    unlinked = post(base, {"action": "update", "id": item["id"], "session_id": ""})[1]["items"][0]
    assert "session_id" not in unlinked and "session_title" not in unlinked


def test_without_a_base_url_the_key_stays_plain(base, monkeypatch):
    monkeypatch.delenv("SYA_JIRA_URL")

    assert add(base, "Глянуть PROJ-77")["jira"] == [{"key": "PROJ-77", "url": ""}]


def test_the_board_carries_the_counts(tmp_path):
    app = FakeApp(RECORDS, LIVE)
    assert app.board()["todo"] == {"urgent": 0, "open": 0}

    for body in ({"text": "a", "urgent": True}, {"text": "b"}, {"text": "c", "urgent": True}):
        todo.todo.apply({"action": "add", **body})
    items, _ = todo.todo.apply({"action": "update", "id": todo.todo._load()[2]["id"], "done": True})

    assert app.board()["todo"] == {"urgent": 1, "open": 2}
    assert len(items) == 3


# ---- MCP --------------------------------------------------------------------------------------


def rpc_call(url, name, arguments, scope):
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    headers = {"Authorization": f"Bearer {mcp.tokens()[scope]}", "Content-Type": "application/json"}
    result = fetch(f"{url}/mcp", request, headers)[1]["result"]
    text = result["content"][0]["text"]
    return result["isError"], json.loads(text) if not result["isError"] else text


def test_the_supervisor_adds_updates_and_lists_over_mcp(base):
    err, added = rpc_call(
        base, "todo_add", {"text": "Свериться по PROJ-5", "urgent": True, "session_id": "s1"}, "control"
    )
    assert not err and added["urgent"] == 1 and added["open"] == 1
    rpc_call(base, "todo_add", {"text": "Потом"}, "control")

    err, listing = rpc_call(base, "todo_list", {}, "read")
    first = listing["open"][0]
    assert first["source"] == "supervisor" and first["urgent"] and first["jira_keys"] == ["PROJ-5"]
    assert first["session_id"] == "s1" and first["id"] == added["id"]

    assert not rpc_call(base, "todo_update", {"id": added["id"], "done": True}, "control")[0]
    err, listing = rpc_call(base, "todo_list", {}, "read")
    assert [i["text"] for i in listing["open"]] == ["Потом"]
    assert [i["text"] for i in listing["done_last_24h"]] == ["Свериться по PROJ-5"]


def test_todo_list_drops_old_done_items(base):
    old = add(base, "Давно")
    post(base, {"action": "update", "id": old["id"], "done": True})
    path = config_dir() / "todo.json"
    items = json.loads(path.read_text())
    items[0]["done_at"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(items))

    assert rpc_call(base, "todo_list", {}, "read")[1] == {"open": [], "done_last_24h": []}


def test_tool_errors_and_scopes(base):
    missing = rpc_call(base, "todo_update", {"id": "nope", "done": True}, "control")
    assert missing == (True, "id: no such to-do")  # the supervisor reads tool errors in English
    assert rpc_call(base, "todo_add", {"text": ""}, "control")[0] is True
    assert rpc_call(base, "todo_add", {"text": "x" * 501}, "control")[0] is True
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "todo_add", "arguments": {"text": "a"}},
    }
    headers = {"Authorization": f"Bearer {mcp.tokens()['read']}", "Content-Type": "application/json"}
    assert "error" in fetch(f"{base}/mcp", request, headers)[1]  # control tool, read token


def test_the_tools_need_no_confirmation_and_the_prompt_explains_them():
    perms = launch.permissions(FEATURES)

    assert {"mcp__sya__todo_list", "mcp__sya_control__todo_add", "mcp__sya_control__todo_update"} <= set(
        perms["allow"]
    )
    prompt = launch.prompt(FEATURES)
    assert f'"{todo.todo.MORNING_SENDER}"' in prompt and "Morning review" in prompt and "todo_add" in prompt


# ---- morning review ---------------------------------------------------------------------------


def wait_for(cond):
    for _ in range(100):
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_morning_is_refused_without_a_supervisor_session(tmp_path):
    httpd, url = serve(FakeApp(RECORDS, {}))
    try:
        status, body = post(url, {"session_id": SUPERVISOR}, "/api/todo/morning")
        assert status == 409 and body["error"] == "The supervisor isn’t running: uv run sya supervisor"
        status, body = post(url, {"session_id": SUPERVISOR}, "/api/todo/morning", lang="ru")
        assert status == 409 and body["error"] == "Супервизор не запущен: uv run sya supervisor"
        assert post(url, {}, "/api/todo/morning")[0] == 409
    finally:
        httpd.shutdown()


@pytest.fixture
def stopped_supervisor(tmp_path, monkeypatch):
    """The supervisor has a transcript and a folder but no process; a resume turn records its argv only."""
    monkeypatch.setattr(launch, "session_id", lambda: SUPERVISOR)
    spawned = record_resume_argv(monkeypatch)
    httpd, url = serve(FakeApp([{**r, "cwd": str(tmp_path)} for r in RECORDS], {}))
    yield url, spawned
    httpd.shutdown()
    claude_routes._runs.clear()


def test_morning_resumes_a_stopped_supervisor_with_its_own_config(stopped_supervisor):
    url, spawned = stopped_supervisor

    status, body = post(url, {"session_id": SUPERVISOR}, "/api/todo/morning", lang="ru")

    assert (status, body) == (202, {"state": "sent"})
    assert fetch(f"{url}/api/todo")[1]["morning"]["state"] == "sent"
    (argv,) = spawned.argv
    assert argv[:3] == ["/usr/bin/claude", "-p", f"--resume={SUPERVISOR}"]
    assert argv[-2:] == ["--", todo.todo.morning_text("ru")]
    assert "--strict-mcp-config" in argv and argv[argv.index("--tools") + 1] == launch.BUILTIN_TOOLS


def test_the_morning_request_is_english_and_names_the_pages_language():
    """The supervisor reads English, and answers in the language the person reads the dashboard in."""
    assert todo.todo.morning_text("ru").startswith("Morning review. The user pressed the button")
    assert todo.todo.morning_text("ru").endswith("dashboard language is ru: answer in that language.")
    assert todo.todo.morning_text("en").isascii() and todo.todo.MORNING_SUMMARY == "Morning review"


def test_morning_never_resumes_a_session_that_is_not_the_supervisor(stopped_supervisor):
    url, spawned = stopped_supervisor

    status, body = post(url, {"session_id": "s1"}, "/api/todo/morning")

    assert (status, body["error"]) == (409, todo.todo.not_running().text()) and spawned.argv == []


def test_morning_without_claude_points_to_the_terminal(stopped_supervisor, monkeypatch):
    url, spawned = stopped_supervisor
    which = claude_routes.shutil.which
    monkeypatch.setattr(claude_routes.shutil, "which", lambda n: None if n == "claude" else which(n))

    status, body = post(url, {"session_id": SUPERVISOR}, "/api/todo/morning")

    assert status == 409 and spawned.argv == []
    assert "uv run sya supervisor" in body["error"] and "claude command wasn’t found" in body["error"]


def test_morning_goes_to_the_live_name_through_the_courier(base, monkeypatch):
    calls = []
    monkeypatch.setattr(
        courier, "deliver", lambda **kw: calls.append(kw) or courier.Delivery(ok=True, msg_id="m1")
    )

    status, body = post(base, {"session_id": SUPERVISOR}, "/api/todo/morning")

    assert status == 202 and body == {"state": "sending"}
    wait_for(lambda: fetch(f"{base}/api/todo")[1]["morning"]["state"] == "sent")
    assert calls == [
        {
            "to": "supervisor-x",
            "text": REPLY_NOTE + todo.todo.morning_text("en"),
            "summary": "Morning review",
            "sender": claude_routes.SENDER,  # the chat's sender: the courier adds REPLY_NOTE only for it
        }
    ]
    assert "post_digest" in calls[0]["text"] and "todo_add" in calls[0]["text"]


def test_a_failed_delivery_is_reported_by_the_next_poll(base, monkeypatch):
    distorted = courier.Delivery(ok=False, error="app.provider.claude.courier.distorted")
    monkeypatch.setattr(courier, "deliver", lambda **kw: distorted)

    assert post(base, {"session_id": SUPERVISOR}, "/api/todo/morning")[0] == 202
    wait_for(lambda: fetch(f"{base}/api/todo")[1]["morning"]["state"] == "error")
    assert fetch(f"{base}/api/todo")[1]["morning"]["error"] == "The courier changed the text"
    in_russian = fetch(f"{base}/api/todo", headers={"X-SYA-Lang": "ru"})[1]["morning"]["error"]
    assert in_russian == "курьер исказил текст"
