"""HTTP and shared views of groups: the list, one group with its feed, create / change, post.

GET  /api/groups?since=<iso>        groups with members, last activity, the unread count since `since` and
                                    `activity` (times of the others' items), so the page can count per group
GET  /api/groups/<id>?limit=<n>     the group, its members and the feed (the last `limit` items, default 200)
POST /api/groups                    {action: create | rename | add_member | remove_member | delete, ...}
POST /api/groups/<id>/post          {text} -> the post entry, deliveries pending (they finish in threads)

A refusal is a Problem: the host answers {"error": ...} in the page's language.
"""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from typing import TYPE_CHECKING

from ...app.replies import Reply, json_reply
from ...i18n import Problem, request_lang
from . import feed, groups, post, store
from .groups import Fleet

if TYPE_CHECKING:
    from email.message import Message

    from ...app.server import App

ROUTE = "/api/groups"
ONE = re.compile(r"/api/groups/([a-z0-9]{1,16})(/post)?")
DEFAULT_LIMIT = 200


def summary(fleet: Fleet, group: dict, since: str, records) -> dict:
    items = feed.assemble(group, fleet, store.read_log(group["id"]), records)
    return {
        **groups.public(group, fleet),
        "last_activity": feed.last_ts(items, group),
        "unread": feed.count_after(items, since) if since else 0,
        "activity": [i["ts"] for i in items if i["kind"] != "post"][
            -100:
        ],  # for per-group unread in the page
    }


def listing(app: App, since: str = "") -> list[dict]:
    fleet = Fleet.of(app)
    by_id = {r["session_id"]: r for r in app.index.records()}
    return [summary(fleet, g, since, by_id.get) for g in store.load()]


def one(app: App, group_id: str, limit: int = DEFAULT_LIMIT) -> dict:
    group = groups.find(group_id)
    fleet = Fleet.of(app)
    items = feed.assemble(group, fleet, store.read_log(group_id), app.index.record)
    return {
        "group": groups.public(group, fleet),
        "members": groups.members_view(group, fleet),
        "supervisor_id": fleet.supervisor_id,
        "feed": items[-limit:],
    }


def handle_get(app: App, path: str, query: dict[str, str]) -> Reply | None:
    if path == ROUTE:
        return json_reply({"groups": listing(app, query.get("since", ""))})
    match = ONE.fullmatch(path)
    if not match or match[2]:
        return None
    limit = int(query["limit"]) if query.get("limit", "").isdigit() else DEFAULT_LIMIT
    return json_reply(one(app, match[1], max(1, limit)))


def _body(body: bytes) -> dict:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise Problem("app.bad_body")
    return data


def _act(app: App, data: dict) -> dict:
    action, gid, fleet = data.get("action"), str(data.get("id") or ""), Fleet.of(app)
    if action == "create":
        return {"group": groups.public(groups.create(fleet, data.get("title"), data.get("members")), fleet)}
    if action == "delete":
        groups.delete(gid)
        return {"ok": True}
    if action == "rename":
        group = groups.rename(gid, data.get("title"))
    elif action == "add_member":
        group = groups.add_member(fleet, gid, data.get("session_id"))
    elif action == "remove_member":
        group = groups.remove_member(gid, data.get("session_id"))
    else:
        raise Problem("groups.bad_action")
    return {"group": groups.public(group, fleet)}


def handle_post(app: App, path: str, body: bytes, headers: Message) -> Reply | None:
    if path == ROUTE:
        return json_reply(_act(app, _body(body)))
    match = ONE.fullmatch(path)
    if not match or not match[2]:
        return None
    group = groups.find(match[1])
    entry = post.post(app, group, Fleet.of(app), _body(body).get("text"))
    return json_reply({"post": feed.worded(entry, request_lang())}, HTTPStatus.OK)
