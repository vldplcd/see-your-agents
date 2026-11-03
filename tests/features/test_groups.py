import json
import os
import stat
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from codex_rollouts import meta, turn_complete, turn_started, uid, user_item, write
from fakes import FakeApp, codex_record, fake_codex_queue, no_processes, pair, record_resume_argv

from see_your_agents import i18n
from see_your_agents.app import mcp, server
from see_your_agents.core.providers.base import LiveSession, SendRefused
from see_your_agents.core.providers.claude import courier
from see_your_agents.core.providers.claude import routes as claude_routes
from see_your_agents.core.providers.codex import queue as codex_queue
from see_your_agents.features.groups import feed, handles, post, store
from see_your_agents.features.supervisor import launch
from see_your_agents.i18n import t

SUP, A, B, W, GONE = "sup-id", "aaaa-id", "bbbb-id", "wait-id", "gone-id"
CX = uid(0)  # a Codex thread
FAR = "2999-01-01T00:00:00Z"


def record(sid, title, cwd="", **extra):
    return {
        "session_id": sid,
        "title": title,
        "cwd": cwd,
        "project": cwd or "/w/none",
        "last_activity": "2026-11-01T10:00:00Z",
        **extra,
    }


def live(sid, name, status="idle", **extra):
    return LiveSession(
        provider="claude", session_id=sid, kind="interactive", status=status, name=name, cwd="/w/p", **extra
    )


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SYA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SYA_CACHE_DIR", str(tmp_path / "cache"))
    no_processes(monkeypatch)
    monkeypatch.setattr(launch, "session_id", lambda: SUP)
    monkeypatch.setattr(claude_routes.shutil, "which", lambda n: "/usr/bin/claude")
    yield
    post.join_all()


@pytest.fixture
def work(tmp_path):
    (tmp_path / "w").mkdir()
    return str(tmp_path / "w")


@pytest.fixture
def fleet(tmp_path, work):
    records = [
        record(SUP, "Супервизор", work),
        record(A, "Fix ledger CI", work),
        record(B, "Fix ledger CI", work),  # closed, same title as A
        record(W, "Ждёт разрешения", work),
        record(GONE, "Без папки", "", transcript_missing=True),
    ]
    sessions = {
        SUP: live(SUP, "supervisor-1"),
        A: live(A, "acme-ledger-c0"),
        W: live(W, "misc-27", "waiting", waiting_for="permission"),
    }
    return FakeApp(records, sessions)


PAIRED: dict[str, dict[str, str]] = {}  # base url -> the paired browser's header; a mutating POST needs it


@pytest.fixture
def base(fleet):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(fleet, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(fleet, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    PAIRED[url] = pair(fleet, port)
    yield url
    httpd.shutdown()
    PAIRED.pop(url, None)


def request(base, path, body=None, lang=None):
    headers = {"Origin": base, "X-SYA": "1", "Content-Type": "application/json", **PAIRED.get(base, {})}
    headers |= {"X-SYA-Lang": lang} if lang else {}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def create(base, members, title="Релиз"):
    status, data = request(base, "/api/groups", {"action": "create", "title": title, "members": members})
    assert status == 200, data
    return data["group"]


class Couriers:
    """courier.deliver and the resume route, faked: what was sent, to whom, by which route."""

    def __init__(self):
        self.sent, self.resumed, self.fail = [], [], {}
        self.lock = threading.Lock()

    def deliver(self, to, text, summary, sender):
        with self.lock:
            self.sent.append({"to": to, "text": text, "summary": summary, "sender": sender})
        if to in self.fail:
            return courier.Delivery(False, error=self.fail[to])
        return courier.Delivery(True, msg_id=f"m-{to}-{len(self.sent)}")

    def resume(self, session, message):
        with self.lock:
            self.resumed.append({"session_id": session.session_id, "message": message.text})
        return "x.log"


@pytest.fixture
def couriers(monkeypatch):
    c = Couriers()
    monkeypatch.setattr(courier, "deliver", c.deliver)
    monkeypatch.setattr(claude_routes, "resume", c.resume)
    return c


def say(base, group_id, text):
    status, data = request(base, f"/api/groups/{group_id}/post", {"text": text})
    post.join_all()
    return status, data


# ---- create and validate ------------------------------------------------------------------------


def test_create_adds_the_supervisor_and_stores_the_group_privately(base, tmp_path):
    group = create(base, [A, B])
    assert group["members"] == [SUP, A, B] and group["title"] == "Релиз"
    assert group["handles"][SUP] == handles.SUPERVISOR
    stored = tmp_path / "config" / "groups.json"
    assert json.loads(stored.read_text())["groups"][0]["id"] == group["id"]
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "body",
    [
        {"title": "", "members": [A]},
        {"title": "x" * 61, "members": [A]},
        {"title": "Релиз", "members": ["nope"]},
        {"title": "Релиз", "members": "abc"},
        {"title": "Релиз", "members": [f"s{i}" for i in range(13)]},
    ],
)
def test_create_validates(base, body):
    status, data = request(base, "/api/groups", {"action": "create", **body})
    assert status == 400 and data["error"]
    assert request(base, "/api/groups")[1]["groups"] == []


def test_at_most_fifty_groups(base):
    for i in range(50):
        create(base, [A], f"g{i}")
    status, data = request(base, "/api/groups", {"action": "create", "title": "ещё", "members": [A]})
    assert status == 400 and "50" in data["error"]


def test_rename_members_and_delete(base, tmp_path):
    group = create(base, [A])
    gid = group["id"]
    assert (
        request(base, "/api/groups", {"action": "rename", "id": gid, "title": "Новое"})[1]["group"]["title"]
        == "Новое"
    )
    added = request(base, "/api/groups", {"action": "add_member", "id": gid, "session_id": B})[1]["group"]
    assert added["members"] == [SUP, A, B]
    removed = request(base, "/api/groups", {"action": "remove_member", "id": gid, "session_id": A})[1]
    assert removed["group"]["members"] == [SUP, B] and A not in removed["group"]["handles"]
    assert request(base, "/api/groups", {"action": "add_member", "id": gid, "session_id": "nope"})[0] == 400
    say(base, gid, "@все привет")
    assert (tmp_path / "config" / "groups" / f"{gid}.jsonl").exists()
    assert request(base, "/api/groups", {"action": "delete", "id": gid})[1] == {"ok": True}
    assert not (tmp_path / "config" / "groups" / f"{gid}.jsonl").exists()
    assert request(base, f"/api/groups/{gid}")[0] == 404
    assert request(base, "/api/groups", {"action": "explode"})[0] == 400


def test_the_last_member_cannot_leave(base, fleet, monkeypatch):
    monkeypatch.setattr(launch, "session_id", lambda: None)
    group = create(base, [A])
    leave = {"action": "remove_member", "id": group["id"], "session_id": A}
    status, data = request(base, "/api/groups", leave)
    assert (status, data["error"]) == (400, "A group must keep one member: delete the whole group instead")
    status, data = request(base, "/api/groups", leave, lang="ru")
    assert (status, data["error"]) == (400, "в группе должен остаться участник: удали группу целиком")


# ---- handles -----------------------------------------------------------------------------------


def test_handles_are_unique_short_and_the_supervisor_is_fixed(base):
    group = create(base, [A, B])
    h = group["handles"]
    assert h[SUP] == "supervisor" and h[A] == "fix-ledger-ci" and h[B] == "fix-ledger-ci-2"
    assert all(len(v) <= 16 for v in h.values())


def test_the_page_reads_the_supervisors_handle_in_its_language(base, fleet, couriers):
    group = create(base, [A])
    gid = group["id"]
    fleet.index._records[A]["messages_out"] = [
        {"ts": FAR, "to": "supervisor-1", "text": "готово", "msg_id": "m1", "ok": True}
    ]
    ru = request(base, f"/api/groups/{gid}", lang="ru")[1]

    assert ru["group"]["handles"][SUP] == "супервизор" and ru["members"][0]["handle"] == "супервизор"
    assert ru["feed"][-1]["to"] == "супервизор"
    assert request(base, f"/api/groups/{gid}")[1]["feed"][-1]["to"] == "supervisor"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Fix ledger CI", "fix-ledger-ci"),
        ("Разобрать падение ночного пайплайна", "разобрать"),
        ("  !!!  ", ""),
        ("UPPER_case.name", "upper-case-name"),
        ("a" * 40, "a" * 16),
    ],
)
def test_slug(text, expected):
    assert handles.slug(text) == expected


def test_pick_avoids_taken_and_reserved_words():
    assert handles.pick(["все"], set()) == "все-2" and handles.pick(["all"], set()) == "all-2"
    assert handles.pick(["Supervisor"], set()) == "supervisor-2"
    assert handles.pick(["супервизор"], set()) == "супервизор-2"
    assert handles.pick(["", "my-app"], {"my-app"}) == "my-app-2"
    assert handles.pick(["!!"], set()) == "session"
    with i18n.answering("ru"):
        assert handles.pick(["!!"], set()) == "сессия"
    assert len(handles.pick(["a" * 16], {"a" * 16})) <= 16


def test_the_reserved_words_agents_read_are_the_english_ones_the_page_offers():
    assert i18n.t("en", "groups.handle.supervisor") == handles.SUPERVISOR
    assert i18n.t("en", "groups.handle.all") == handles.EVERYONE


@pytest.mark.parametrize("lang", list(i18n.LANGS))
def test_every_language_writes_its_reserved_words_and_fallback_as_handles(lang):
    """A word a mention cannot spell (an accent, a CJK character) would be offered but never match."""
    for key in ("groups.handle.supervisor", "groups.handle.all", "groups.handle.fallback"):
        word = i18n.t(lang, key)
        assert handles.slug(word) == word and handles.mentions(f"@{word}") == [word], (lang, key, word)


# ---- mentions and the header ---------------------------------------------------------------------


def test_mention_goes_to_that_member_with_the_exact_header(base, couriers):
    group = create(base, [A, B])
    status, data = say(base, group["id"], "@fix-ledger-ci что с CI?")
    assert status == 200 and data["post"]["targets"] == [A]
    assert data["post"]["deliveries"][0]["ok"] is None  # the POST answers before the delivery finishes
    (sent,) = couriers.sent
    assert sent["to"] == "acme-ledger-c0" and sent["sender"] == "Group “Релиз”"
    assert sent["summary"] == "@fix-ledger-ci что с CI?"
    assert sent["text"] == (
        "[Group “Релиз”. You are @fix-ledger-ci. Members: @supervisor (supervisor-1), "
        "@fix-ledger-ci (acme-ledger-c0), @fix-ledger-ci-2 (not running)\n"
        "Reply in plain text in your session: the group sees your reply. "
        'To message a member directly: SendMessage(to="<name>").]\n\n'
        "@fix-ledger-ci что с CI?"
    )


def test_the_header_is_english_whatever_the_page_reads(base, couriers):
    """The header is for the agent: a Russian page gets its handles in Russian, the member does not."""
    group = create(base, [A])
    status, _ = request(base, f"/api/groups/{group['id']}/post", {"text": "@fix-ledger-ci привет"}, lang="ru")
    post.join_all()

    assert status == 200 and couriers.sent[0]["text"].startswith("[Group “Релиз”. You are @fix-ledger-ci. ")
    assert "@supervisor (supervisor-1)" in couriers.sent[0]["text"]


@pytest.mark.parametrize("everyone", ["@all", "@все", "@ALL"])
def test_everyone_gets_the_post_and_each_header_names_its_own_handle(base, couriers, everyone):
    group = create(base, [A, B])
    say(base, group["id"], f"{everyone} как дела")
    assert sorted(s["to"] for s in couriers.sent) == ["acme-ledger-c0", "supervisor-1"]
    assert [r["session_id"] for r in couriers.resumed] == [B]
    own = {s["to"]: s["text"].split(". Members")[0] for s in couriers.sent}
    assert own["supervisor-1"].endswith("You are @supervisor")
    assert couriers.resumed[0]["message"].startswith("[Group “Релиз”. You are @fix-ledger-ci-2.")


@pytest.mark.parametrize("handle", ["@supervisor", "@супервизор"])
def test_the_supervisor_is_mentioned_in_either_language(base, couriers, handle):
    group = create(base, [A])
    status, data = say(base, group["id"], f"{handle} @fix-ledger-ci глянь")
    assert status == 200 and data["post"]["targets"] == [SUP, A]


def test_no_mention_goes_to_the_supervisor(base, couriers):
    group = create(base, [A])
    say(base, group["id"], "что у нас горит?")
    assert [s["to"] for s in couriers.sent] == ["supervisor-1"]


def test_no_mention_resumes_a_stopped_supervisor_with_its_own_config(base, fleet, tmp_path, monkeypatch):
    monkeypatch.setattr(claude_routes, "_runs", {})
    spawned = record_resume_argv(monkeypatch)
    group = create(base, [A])
    del fleet.fake_live[SUP]

    status, data = say(base, group["id"], "кто-нибудь?")

    assert status == 200 and data["post"]["targets"] == [SUP]
    (argv,) = spawned.argv
    assert argv[:3] == ["/usr/bin/claude", "-p", f"--resume={SUP}"]
    assert "--strict-mcp-config" in argv and argv[argv.index("--tools") + 1] == launch.BUILTIN_TOOLS
    assert argv[-1].startswith("[Group “Релиз”. You are @supervisor.")
    assert argv[-1].endswith("\n\nкто-нибудь?")
    (d,) = store.read_log(group["id"])[0]["deliveries"]
    assert (d["route"], d["ok"]) == ("resume", True)


def test_no_mention_without_a_supervisor_session_is_409(base, couriers, monkeypatch):
    group = create(base, [A])
    monkeypatch.setattr(launch, "session_id", lambda: None)

    status, data = say(base, group["id"], "кто-нибудь?")

    assert status == 409 and "uv run sya supervisor" in data["error"]
    assert couriers.sent == couriers.resumed == [] and store.read_log(group["id"]) == []


def test_unknown_mention_is_refused_but_an_email_is_not_a_mention(base, couriers):
    group = create(base, [A])
    status, data = say(base, group["id"], "@ктото помоги")
    assert status == 400
    assert data["error"] == "No member has the handle @ктото. In the group: @supervisor, @fix-ledger-ci, @all"
    status, data = request(base, f"/api/groups/{group['id']}/post", {"text": "@ктото помоги"}, lang="ru")
    in_russian = "нет участника с таким прозвищем: @ктото. В группе: @супервизор, @fix-ledger-ci, @все"
    assert data["error"] == in_russian
    assert say(base, group["id"], "пиши на me@example.com")[0] == 200  # no mention: the supervisor


def test_empty_and_huge_text_are_refused(base, couriers):
    group = create(base, [A])
    assert say(base, group["id"], "   ")[0] == 400
    assert say(base, group["id"], "x" * 20_001)[0] == 400
    assert request(base, "/api/groups/gdeadbeef/post", {"text": "hi"})[0] == 404


# ---- routes ------------------------------------------------------------------------------------


def test_route_choice_live_closed_waiting_and_unreachable(base, couriers):
    group = create(base, [A, B, W, GONE])
    handles_ = group["handles"]
    status, data = say(
        base, group["id"], f"@{handles_[A]} @{handles_[B]} @{handles_[W]} @{handles_[GONE]} go"
    )
    assert status == 200
    by_sid = {d["session_id"]: d for d in store.read_log(group["id"])[0]["deliveries"]}
    assert (by_sid[A]["route"], by_sid[A]["ok"]) == ("peer", True) and by_sid[A]["msg_id"]
    assert (by_sid[B]["route"], by_sid[B]["ok"]) == ("resume", True)
    assert by_sid[W]["ok"] is False and "permission" in by_sid[W]["error"] and by_sid[W]["route"] == ""
    assert by_sid[GONE]["ok"] is False and by_sid[GONE]["error"]
    assert [s["to"] for s in couriers.sent] == ["acme-ledger-c0"]  # nothing went to the waiting session
    assert [r["session_id"] for r in couriers.resumed] == [B]


def test_two_live_sessions_with_one_name_are_refused(base, fleet, couriers):
    group = create(base, [A])
    fleet.fake_live[B] = live(B, "acme-ledger-c0")
    say(base, group["id"], f"@{group['handles'][A]} привет")
    (d,) = store.read_log(group["id"])[0]["deliveries"]
    assert d["ok"] is False and "Several live sessions" in d["error"] and couriers.sent == []


def test_a_failed_delivery_is_logged_with_its_error(base, couriers):
    couriers.fail["acme-ledger-c0"] = "app.provider.claude.courier.distorted"  # the courier's verdict: a key
    group = create(base, [A])
    say(base, group["id"], f"@{group['handles'][A]} привет")
    (d,) = store.read_log(group["id"])[0]["deliveries"]
    assert d["ok"] is False and d["error"] == "The courier changed the text" and d["at"]
    assert d["problem"] == {"key": "app.provider.claude.courier.distorted", "params": {}}


def boom(**kw):
    raise RuntimeError("boom")


def test_a_crashing_delivery_still_reports(base, monkeypatch):
    monkeypatch.setattr(courier, "deliver", boom)
    group = create(base, [A])
    say(base, group["id"], f"@{group['handles'][A]} привет")
    (d,) = store.read_log(group["id"])[0]["deliveries"]
    assert d["ok"] is False and d["error"] == "Delivery failed: RuntimeError('boom')"


def test_a_refusal_is_worded_when_the_feed_is_read_in_the_readers_language(base, couriers, monkeypatch):
    """The log keeps the key; a Russian page reads the Russian text, an older line keeps its own."""
    monkeypatch.setattr(courier, "deliver", boom)
    group = create(base, [A])
    say(base, group["id"], f"@{group['handles'][A]} привет")
    log = store.read_log(group["id"])
    assert log[0]["deliveries"][0]["problem"] == {
        "key": "groups.delivery_crashed",
        "params": {"detail": "RuntimeError('boom')"},
    }
    log.append({**log[0], "id": "old", "deliveries": [{"session_id": A, "ok": False, "error": "по-старому"}]})
    store._write_log(group["id"], log)

    feed_ = request(base, f"/api/groups/{group['id']}", lang="ru")[1]["feed"]
    errors = [p["deliveries"][0]["error"] for p in feed_]
    assert errors == ["сбой доставки: RuntimeError('boom')", "по-старому"]
    assert "problem" not in feed_[0]["deliveries"][0]
    english = request(base, f"/api/groups/{group['id']}")[1]["feed"][0]["deliveries"][0]["error"]
    assert english == "Delivery failed: RuntimeError('boom')"


def test_resume_start_error_is_a_failed_delivery(base, monkeypatch):
    def refuse(*a):
        raise SendRefused("app.provider.claude.reply_running")

    monkeypatch.setattr(claude_routes, "resume", refuse)
    group = create(base, [B])
    say(base, group["id"], f"@{group['handles'][B]} привет")
    (d,) = store.read_log(group["id"])[0]["deliveries"]
    assert d["ok"] is False and d["error"] == "This session’s reply is already running"
    ru = request(base, f"/api/groups/{group['id']}", lang="ru")[1]["feed"][-1]["deliveries"][0]["error"]
    assert ru == "ответ этой сессии уже выполняется"


def test_the_log_is_jsonl_with_the_post_entry(base, couriers, tmp_path):
    group = create(base, [A])
    say(base, group["id"], "раз")
    say(base, group["id"], "два")
    path = tmp_path / "config" / "groups" / f"{group['id']}.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [e["text"] for e in lines] == ["раз", "два"] and {e["kind"] for e in lines} == {"post"}
    assert lines[0]["targets"] == [SUP]
    assert set(lines[0]["deliveries"][0]) == {"session_id", "name", "route", "ok", "msg_id", "error", "at"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ---- feed --------------------------------------------------------------------------------------


def posted_at(group_id, ts):
    """Pin the stored time of the group's only post so that fake records can be placed around it."""
    log = store.read_log(group_id)
    log[0]["ts"] = ts
    store._write_log(group_id, log)


def test_feed_has_posts_and_the_reply_found_by_msg_id(fleet, base, couriers):
    group = create(base, [A])
    say(base, group["id"], f"@{group['handles'][A]} привет")
    msg_id = store.read_log(group["id"])[0]["deliveries"][0]["msg_id"]
    fleet.index._records[A]["messages_in"] = [
        {"ts": "2026-11-01T10:00:01Z", "from": "g", "text": "x", "msg_id": "other", "reply": "не то"},
        {"ts": "2026-11-01T10:00:02Z", "from": "Группа «Релиз»", "text": "x", "msg_id": msg_id,
         "reply": "Привет!", "reply_at": "2999-01-01T00:00:00Z"},
    ]  # fmt: skip
    data = request(base, f"/api/groups/{group['id']}")[1]
    kinds = [i["kind"] for i in data["feed"]]
    assert kinds == ["post", "reply"]
    reply = data["feed"][1]
    assert reply["text"] == "Привет!" and reply["handle"] == group["handles"][A] and not reply["in_progress"]
    assert reply["post_id"] == data["feed"][0]["id"]
    member = next(m for m in data["members"] if m["session_id"] == A)
    assert member["name"] == "acme-ledger-c0" and member["status"] == "idle" and not member["is_supervisor"]
    assert data["members"][0]["is_supervisor"] and data["supervisor_id"] == SUP


def test_a_reply_in_progress_is_marked(fleet, base, couriers):
    group = create(base, [A])
    say(base, group["id"], f"@{group['handles'][A]} привет")
    msg_id = store.read_log(group["id"])[0]["deliveries"][0]["msg_id"]
    fleet.index._records[A]["messages_in"] = [
        {"ts": "2026-11-01T10:00:02Z", "from": "g", "text": "x", "msg_id": msg_id, "reply": "Смотрю…",
         "reply_at": "2999-01-01T00:00:00Z"}
    ]  # fmt: skip
    fleet.fake_live[A].status = "busy"
    reply = request(base, f"/api/groups/{group['id']}")[1]["feed"][-1]
    assert reply["in_progress"] is True


@pytest.mark.parametrize(
    "header",
    [
        "[Group “Релиз”. You are @{handle}. Members: …",
        "[Группа «Релиз». Ты — @{handle}. Участники: …",  # written before the header was English
    ],
)
def test_closed_session_reply_comes_from_its_last_assistant_text(fleet, base, couriers, header):
    group = create(base, [B])
    say(base, group["id"], f"@{group['handles'][B]} привет")
    gid, handle = group["id"], group["handles"][B]
    posted_at(gid, "2026-11-01T12:00:00Z")
    rec = fleet.index._records[B]
    rec.update(last_assistant_text="Готово", last_activity="2026-11-01T12:00:09Z")
    rec["last_prompt"] = header.format(handle=handle)
    reply = [i for i in request(base, f"/api/groups/{gid}")[1]["feed"] if i["kind"] == "reply"]
    assert [r["text"] for r in reply] == ["Готово"]
    rec["last_activity"] = "2026-11-01T11:59:00Z"  # activity before the post: not an answer to it
    assert [i["kind"] for i in request(base, f"/api/groups/{gid}")[1]["feed"]] == ["post"]
    rec.update(last_activity="2026-11-01T12:00:09Z", last_prompt="совсем другой промпт")
    assert [i["kind"] for i in request(base, f"/api/groups/{gid}")[1]["feed"]] == ["post"]
    rec["last_prompt"] = header.replace("Релиз", "Другая").format(handle=handle)  # another group's header
    assert [i["kind"] for i in request(base, f"/api/groups/{gid}")[1]["feed"]] == ["post"]


def test_members_talking_to_each_other_appear_once(fleet, base, couriers):
    group = create(base, [A, B])
    gid = group["id"]
    created = group["created_at"]
    fleet.fake_live[B] = live(B, "other-name")
    sent = {
        "ts": "2999-01-01T00:00:01Z",
        "to": "other-name",
        "summary": "эй",
        "text": "эй, глянь",
        "msg_id": "m1",
        "ok": True,
    }
    fleet.index._records[A]["messages_out"] = [
        sent,
        {**sent, "msg_id": "old", "ts": "2000-01-01T00:00:00Z"},  # before the group existed
        {**sent, "msg_id": "bad", "ok": False},
        {**sent, "msg_id": "pending", "ok": None},
        {**sent, "msg_id": "out", "to": "somebody-outside"},
    ]  # fmt: skip
    fleet.index._records[B]["messages_in"] = [
        {"ts": "2999-01-01T00:00:02Z", "from": "acme-ledger-c0", "text": "эй, глянь", "msg_id": "m1"},
        {"ts": "2999-01-01T00:00:03Z", "from": "acme-ledger-c0", "text": "без результата", "msg_id": "m2"},
    ]
    items = request(base, f"/api/groups/{gid}")[1]["feed"]
    assert created < "2999"
    assert [(i["kind"], i["msg_id"]) for i in items] == [("peer", "m1"), ("peer", "m2")]
    assert items[0]["from"] == group["handles"][A] and items[0]["to"] == group["handles"][B]


def test_the_supervisor_is_known_by_its_old_name_too(fleet, base, couriers):
    """A supervisor started before its name was English is «Супервизор»: its messages are still its own."""
    group = create(base, [A])
    sent = {"ts": "2999-01-01T00:00:01Z", "text": "сводка", "ok": True}
    fleet.index._records[A]["messages_in"] = [
        {**sent, "from": "Супервизор", "msg_id": "old"},
        {**sent, "from": "Supervisor", "msg_id": "new", "ts": "2999-01-01T00:00:02Z"},
    ]

    items = request(base, f"/api/groups/{group['id']}")[1]["feed"]

    assert [(i["msg_id"], i["from"], i["to"]) for i in items] == [
        ("old", "supervisor", group["handles"][A]),
        ("new", "supervisor", group["handles"][A]),
    ]


def test_feed_limit_keeps_the_latest_items(base, couriers):
    group = create(base, [A])
    for n in range(3):
        say(base, group["id"], f"сообщение {n}")
    feed_ = request(base, f"/api/groups/{group['id']}?limit=2")[1]["feed"]
    assert [i["text"] for i in feed_] == ["сообщение 1", "сообщение 2"]


def test_list_has_members_last_activity_and_unread(fleet, base, couriers):
    group = create(base, [A])
    say(base, group["id"], f"@{group['handles'][A]} привет")
    msg_id = store.read_log(group["id"])[0]["deliveries"][0]["msg_id"]
    fleet.index._records[A]["messages_in"] = [
        {"ts": "t", "from": "g", "text": "x", "msg_id": msg_id, "reply": "ответ", "reply_at": FAR}
    ]  # fmt: skip
    (item,) = request(base, "/api/groups?since=2000-01-01T00:00:00Z")[1]["groups"]
    assert item["id"] == group["id"] and item["members"] == [SUP, A]
    assert item["unread"] == 1 and item["last_activity"] == "2999-01-01T00:00:00Z"
    assert item["activity"] == ["2999-01-01T00:00:00Z"]  # the page counts unread per group from these
    assert request(base, "/api/groups?since=2999-06-01T00:00:00Z")[1]["groups"][0]["unread"] == 0
    assert request(base, "/api/groups")[1]["groups"][0]["unread"] == 0


# ---- Codex members ------------------------------------------------------------------------------


@pytest.fixture
def codex_member(fleet):
    """A Codex thread on the board whose first turn ended, as `codex exec` from «Новая сессия»
    ("New session") leaves it."""
    turn = uid(1, "a")
    home = Path(os.environ["CODEX_HOME"])
    write(home, CX, meta(CX), turn_started(turn, 1), user_item("Codex CI", 1), turn_complete(turn, "done", 1))
    fleet.index._records[CX] = codex_record(CX)
    return CX


def test_a_codex_member_gets_the_post_by_codex_queue_and_its_answer_comes_from_the_rollout(
    fleet, base, codex_member, monkeypatch
):
    queue = fake_codex_queue(monkeypatch, answer="CI зелёный")
    group = create(base, [A, CX])
    me = group["handles"][CX]

    status, data = say(base, group["id"], f"@{me} что с CI?")

    assert status == 200 and data["post"]["targets"] == [CX]
    assert queue.message() == (
        f"[Group “Релиз”. You are @{me}. Members: @supervisor, @fix-ledger-ci, @{me}\n"
        "Reply in plain text: the group sees your reply. You cannot message the other members: "
        "put what they need to know in your reply.]\n\n"
        f"@{me} что с CI?"
    )
    (d,) = store.read_log(group["id"])[0]["deliveries"]
    assert (d["route"], d["ok"], d["msg_id"]) == ("queue", True, queue.turns[0])

    fleet.index._records[CX] = codex_record(CX)  # the index re-reads the rollout the queued turn grew
    feed_ = request(base, f"/api/groups/{group['id']}")[1]["feed"]
    replies = [(i["handle"], i["text"], i["post_id"]) for i in feed_ if i["kind"] == "reply"]
    assert replies == [(me, "CI зелёный", data["post"]["id"])]


def test_a_claude_member_learns_that_sendmessage_does_not_reach_a_codex_member(base, couriers, codex_member):
    group = create(base, [A, CX])
    say(base, group["id"], f"@{group['handles'][A]} привет")

    (sent,) = couriers.sent
    assert f"@{group['handles'][CX]} (SendMessage does not reach it)" in sent["text"]
    assert 'SendMessage(to="<name>")' in sent["text"]


@pytest.mark.parametrize(
    ("host", "status", "aborted", "reason"),
    [
        ("vscode", "idle", False, t("en", codex_queue.HELD["vscode"])),
        ("daemon", "busy", False, t("en", codex_queue.BUSY)),
        ("", "", True, t("en", codex_queue.ABORTED, sid=CX)),
    ],
)
def test_a_codex_member_codex_would_not_run_now_gets_a_failed_delivery_with_the_reason(
    fleet, base, codex_member, monkeypatch, host, status, aborted, reason
):
    queue = fake_codex_queue(monkeypatch)
    if host:
        fleet.fake_live[CX] = LiveSession(CX, "interactive", status, host=host, provider="codex")
    fleet.index._records[CX]["last_turn_aborted"] = aborted
    group = create(base, [CX])

    say(base, group["id"], f"@{group['handles'][CX]} привет")

    (d,) = store.read_log(group["id"])[0]["deliveries"]
    assert (d["ok"], d["route"], d["error"]) == (False, "", reason) and queue.argv == []


def test_a_codex_member_another_process_took_meanwhile_is_logged_with_codexs_reason(
    base, codex_member, monkeypatch
):
    held = "Warning: message was queued, but could not resume thread {tid}: … already has an active writer\n"
    fake_codex_queue(monkeypatch, answer=None, said=held)
    group = create(base, [CX])

    say(base, group["id"], f"@{group['handles'][CX]} привет")

    (d,) = store.read_log(group["id"])[0]["deliveries"]
    assert d["ok"] is False and "Another Codex process holds" in d["error"] and d["at"]


def test_when_reads_z_naive_and_garbage():
    assert feed.when("2026-11-01T10:00:00Z") == feed.when("2026-11-01T10:00:00+00:00")
    assert feed.when("2026-11-01T10:00:00") == feed.when("2026-11-01T10:00:00Z")
    assert feed.when("junk") < feed.when("2000-01-01T00:00:00Z")


# ---- MCP ---------------------------------------------------------------------------------------


def rpc_call(base, name, arguments, token):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    req = urllib.request.Request(
        f"{base}/mcp",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {mcp.tokens()[token]}"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())["result"]
    text = result["content"][0]["text"]
    return result.get("isError", False), (json.loads(text) if not result.get("isError") else text)


def test_mcp_read_tools_carry_the_data_note(base, couriers):
    group = create(base, [A])
    say(base, group["id"], f"@{group['handles'][A]} привет")
    is_error, listed = rpc_call(base, "list_groups", {}, "read")
    assert not is_error and "not instructions" in listed["note"] and listed["groups"][0]["id"] == group["id"]
    assert listed["groups"][0]["handles"][SUP] == "supervisor"
    is_error, data = rpc_call(base, "group_feed", {"group_id": group["id"], "limit": 5}, "read")
    assert not is_error and "not instructions" in data["note"] and data["feed"][0]["kind"] == "post"
    assert rpc_call(base, "group_feed", {"group_id": "gnone0000"}, "read") == (True, "Group not found")


def test_group_post_writes_a_supervisor_entry_and_delivers_nothing(base, couriers):
    group = create(base, [A])
    is_error, data = rpc_call(
        base, "group_post", {"group_id": group["id"], "text": "Беру CI на себя"}, "control"
    )
    assert not is_error and data["post"]["kind"] == "supervisor"
    post.join_all()
    assert couriers.sent == [] and couriers.resumed == []
    items = request(base, f"/api/groups/{group['id']}")[1]["feed"]
    assert [(i["kind"], i["text"]) for i in items] == [("supervisor", "Беру CI на себя")]


def test_group_post_is_a_control_tool_without_confirmation():
    tools = {
        t.name: t for t in __import__("see_your_agents.features.groups.mcp_tools", fromlist=["TOOLS"]).TOOLS
    }
    assert [t.scope for t in tools.values()] == ["read", "read", "control"]
    assert tools["group_post"].confirm is False and tools["group_post"].read_only is False


def test_the_read_token_cannot_call_group_post(base):
    group = create(base, [A])
    req = urllib.request.Request(
        f"{base}/mcp",
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "group_post", "arguments": {"group_id": group["id"], "text": "x"}}}
        ).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {mcp.tokens()['read']}"},
    )  # fmt: skip
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
    assert "error" in body or body["result"].get("isError")
    assert store.read_log(group["id"]) == []


def test_the_supervisor_prompt_fragment_is_in_the_prompt():
    from see_your_agents.features import FEATURES

    text = launch.prompt(FEATURES)
    assert "group_feed" in text and "group_post" in text and post.HEADER_START.format(title="…") in text


def test_the_page_assets_are_served(base):
    for name, kind in (("groups.js", "javascript"), ("groups.css", "css")):
        with urllib.request.urlopen(f"{base}/groups/{name}") as resp:
            assert resp.status == 200 and kind in resp.headers["Content-Type"]


def test_background_session_with_a_question_gets_the_message_but_not_one_on_a_prompt():
    """A bg job that ended its turn with a question is "blocked"; on a permission prompt it is "waiting"."""
    asked = LiveSession(
        provider="claude", session_id=A, kind="background", status="blocked", name="proj", needs="confirm: x?"
    )
    prompt = LiveSession(provider="claude", session_id=B, kind="background", status="waiting", name="other")
    fleet = SimpleNamespace(live={A: asked, B: prompt})
    assert post.plan_route(None, fleet, A) == ("peer", "")
    assert post.plan_route(None, fleet, B)[0] == ""
