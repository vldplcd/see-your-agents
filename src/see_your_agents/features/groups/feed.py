"""The feed of a group, assembled from its log and the members' index records.

Items, sorted by time (each has `key`, `ts`, `kind`):
  post        the user's text with its deliveries (per-target status)
  supervisor  an entry the supervisor wrote with group_post; never delivered
  reply       a member's answer to a post. A delivery with a msg_id (Claude's courier, Codex's queue): the
              record's entry under that id, in `messages_in` (a peer message) or `queued_turns` (a turn the
              program's queue started); its `reply` is the session's latest text after the message, so it
              grows while the turn runs. Resume delivery: the record's `last_assistant_text`, when
              `last_prompt` starts with the group header and `last_activity` is at or after the post; only the
              member's latest such post gets it.
  peer        a message between two members after the group was created: `messages_out` with ok and a
              member's name in `to`, plus matching `messages_in` that the sender's record lacks (by msg_id)

Handles and refusals are worded in the reader's language (request_lang(): the page's, English over MCP).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from ...i18n import request_lang, t
from . import groups
from .groups import Fleet
from .post import HEADER_START

Records = Callable[[str], "dict | None"]
# The header's start before agent texts were English: logs and transcripts keep it, so old answers still show.
OLD_HEADER_START = "[Группа «{title}»"


def when(ts: str) -> datetime:
    """An ISO time as an instant; unreadable text sorts first."""
    try:
        at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _reply_item(post: dict, group: dict, fleet: Fleet, sid: str, text: str, ts: str) -> dict:
    live = fleet.live.get(sid)
    return {
        "key": f"reply:{post['id']}:{sid}",
        "kind": "reply",
        "ts": ts,
        "post_id": post["id"],
        "session_id": sid,
        "handle": groups.handle_of(group, fleet, sid, request_lang()),
        "text": text,
        "in_progress": bool(live and live.status == "busy"),
    }


def _replies_by_id(post: dict, group: dict, fleet: Fleet, records: Records) -> list[dict]:
    items = []
    for d in post.get("deliveries", []):
        if not (d.get("ok") and d.get("msg_id")):
            continue
        rec = records(d["session_id"]) or {}
        inbox = [*(rec.get("messages_in") or []), *(rec.get("queued_turns") or [])]
        msg = next((m for m in inbox if m.get("msg_id") == d["msg_id"]), None)
        if msg and msg.get("reply"):
            items.append(
                _reply_item(post, group, fleet, d["session_id"], msg["reply"], msg.get("reply_at", ""))
            )
    return items


def _resumed_replies(posts: list[dict], group: dict, fleet: Fleet, records: Records) -> list[dict]:
    """Closed sessions: the record's last assistant text answers the member's newest resumed post."""
    latest: dict[str, dict] = {}
    for post in posts:  # posts are in time order, so the last one wins
        for d in post.get("deliveries", []):
            if d.get("ok") and d.get("route") == "resume":
                latest[d["session_id"]] = post
    items = []
    for sid, post in latest.items():
        rec = records(sid) or {}
        mine = tuple(start.format(title=group["title"]) for start in (HEADER_START, OLD_HEADER_START))
        asked_by_group = (rec.get("last_prompt") or "").startswith(mine)
        answered = when(rec.get("last_activity", "")) >= when(post["ts"])
        if asked_by_group and answered and rec.get("last_assistant_text"):
            items.append(
                _reply_item(post, group, fleet, sid, rec["last_assistant_text"], rec["last_activity"])
            )
    return items


def _peer_items(group: dict, fleet: Fleet, records: Records) -> list[dict]:
    names = {fleet.name(sid): sid for sid in group["members"] if fleet.name(sid)}
    if fleet.supervisor_id in group["members"]:  # by any name it had: older transcripts carry the old one
        names.update(dict.fromkeys(fleet.supervisor_names, fleet.supervisor_id))
    since, lang = when(group["created_at"]), request_lang()
    seen: set[str] = set()
    items = []

    def add(msg: dict, sender: str, receiver: str) -> None:
        mid = msg.get("msg_id") or ""
        if (mid and mid in seen) or when(msg.get("ts", "")) < since:
            return
        seen.add(mid)
        items.append(
            {
                "key": f"peer:{mid or msg.get('ts')}:{sender}",
                "kind": "peer",
                "ts": msg["ts"],
                "msg_id": mid,
                "from": groups.handle_of(group, fleet, sender, lang),
                "to": groups.handle_of(group, fleet, receiver, lang),
                "text": msg.get("text") or msg.get("summary") or "",
            }
        )

    for sid in group["members"]:
        for m in (records(sid) or {}).get("messages_out") or []:
            if m.get("ok") is True and names.get(m.get("to")) not in (None, sid):
                add(m, sid, names[m["to"]])
    for sid in group["members"]:  # received, but missing on the sender's side (not recorded yet or no result)
        for m in (records(sid) or {}).get("messages_in") or []:
            sender = names.get(m.get("from"))
            if sender and sender != sid:
                add(m, sender, sid)
    return items


def worded(entry: dict, lang: str) -> dict:
    """A log entry with each refusal kept as a key (post._result) in `lang`; older lines keep their text."""
    if not any("problem" in d for d in entry.get("deliveries", [])):
        return entry
    deliveries = []
    for d in entry["deliveries"]:
        out, problem = {k: v for k, v in d.items() if k != "problem"}, d.get("problem")
        if isinstance(problem, dict) and isinstance(problem.get("key"), str):
            params = problem.get("params")
            out["error"] = t(lang, problem["key"], **(params if isinstance(params, dict) else {}))
        deliveries.append(out)
    return {**entry, "deliveries": deliveries}


def assemble(group: dict, fleet: Fleet, log: list[dict], records: Records) -> list[dict]:
    feed, lang = [], request_lang()
    posts = [e for e in log if e.get("kind") == "post"]
    for e in log:
        if e.get("kind") in ("post", "supervisor"):
            feed.append({**worded(e, lang), "key": f"{e['kind']}:{e['id']}"})
    for entry in posts:
        feed += _replies_by_id(entry, group, fleet, records)
    feed += _resumed_replies(posts, group, fleet, records)
    feed += _peer_items(group, fleet, records)
    feed.sort(key=lambda item: when(item["ts"]))
    return feed


def last_ts(feed: list[dict], group: dict) -> str:
    return feed[-1]["ts"] if feed else group["created_at"]


def count_after(feed: list[dict], since: str) -> int:
    """Items newer than `since` that the user did not write themselves: the unread badge."""
    cut = when(since)
    return sum(item["kind"] != "post" and when(item["ts"]) > cut for item in feed)
