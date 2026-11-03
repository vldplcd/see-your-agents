"""The group model: who is in a group, their handles, and the actions that change a group.

A group is {id, title, members: [session_id], handles: {session_id: handle}, created_at}. Handles are chosen
when a member joins and kept, so a renamed session does not change what the user types. The supervisor's
session comes from `supervisor_id` in /api/board (data from the supervisor feature, not an import).
Refusals are Problems: the page reads them in its language, an agent (MCP) in English.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING

from ...core import providers
from ...core.providers.base import Cap, LiveSession
from ...i18n import Problem, request_lang, t
from . import handles, store

if TYPE_CHECKING:
    from ...app.server import App

MAX_TITLE = 60
MAX_MEMBERS = 12
MAX_GROUPS = 50


@dataclass
class Fleet:
    """The sessions a request works with, read once from the app."""

    cards: dict[str, dict]
    live: dict[str, LiveSession]
    supervisor_id: str | None
    supervisor_names: tuple[str, ...] = ()  # every name it answers to, the old one included (/api/board)

    @classmethod
    def of(cls, app: App) -> Fleet:
        board = app.board()
        cards = {c["session_id"]: c for c in board["cards"]}
        names = tuple(board.get("supervisor_names") or ())
        return cls(cards, app.live(), board.get("supervisor_id"), names)

    def name(self, session_id: str) -> str:
        """The address for SendMessage; empty for a session that is not running."""
        live = self.live.get(session_id)
        return live.name if live else ""

    def title(self, session_id: str) -> str:
        return (self.cards.get(session_id) or {}).get("title") or t(request_lang(), "groups.unknown_session")

    def messages_peers(self, session_id: str) -> bool:
        """Whether the session's program lets it message other sessions (Cap.PEER: SendMessage)."""
        live = self.live.get(session_id)
        pid = (self.cards.get(session_id) or {}).get("provider") or (live.provider if live else "")
        try:
            return Cap.PEER in providers.by_id(pid).caps
        except KeyError:
            return False


def find(group_id: str) -> dict:
    group = store.get(group_id) if store.GROUP_ID.fullmatch(group_id) else None
    if group is None:
        raise Problem("groups.not_found", HTTPStatus.NOT_FOUND)
    return group


def handle_of(group: dict, fleet: Fleet, session_id: str, lang: str | None = None) -> str:
    """The member's @handle. The supervisor's is a word: in `lang` for the page, in English for an agent."""
    if session_id == fleet.supervisor_id:
        return t(lang, "groups.handle.supervisor") if lang else handles.SUPERVISOR
    return (group.get("handles") or {}).get(session_id) or session_id[:8]


def public(group: dict, fleet: Fleet) -> dict:
    lang = request_lang()
    return {
        "id": group["id"],
        "title": group["title"],
        "members": list(group["members"]),
        "handles": {sid: handle_of(group, fleet, sid, lang) for sid in group["members"]},
        "created_at": group["created_at"],
    }


def members_view(group: dict, fleet: Fleet) -> list[dict]:
    out, lang = [], request_lang()
    for sid in group["members"]:
        card = fleet.cards.get(sid) or {}
        live = fleet.live.get(sid)
        out.append(
            {
                "session_id": sid,
                "handle": handle_of(group, fleet, sid, lang),
                "name": fleet.name(sid),
                "title": fleet.title(sid),
                "project": card.get("project", ""),
                "column": card.get("column", ""),
                "status": live.status if live else "",
                "waiting_for": live.waiting_for if live else "",
                "is_supervisor": sid == fleet.supervisor_id,
            }
        )
    return out


# ---- actions -----------------------------------------------------------------------------------


def clean_title(raw: object) -> str:
    title = " ".join(raw.split()) if isinstance(raw, str) else ""
    if not 1 <= len(title) <= MAX_TITLE:
        raise Problem("groups.bad_title", max=MAX_TITLE)
    return title


def _known(fleet: Fleet, session_id: object) -> str:
    if not isinstance(session_id, str) or session_id not in fleet.cards:
        raise Problem("groups.unknown_member", session=repr(session_id))
    return session_id


def _handle_for(fleet: Fleet, session_id: str, taken: set[str]) -> str:
    if session_id == fleet.supervisor_id:
        return handles.SUPERVISOR
    card = fleet.cards[session_id]
    return handles.pick([card.get("title", ""), card.get("project", ""), fleet.name(session_id)], taken)


def create(fleet: Fleet, title: object, members: object) -> dict:
    title = clean_title(title)
    if not isinstance(members, list):
        raise Problem("groups.bad_members")
    ids: list[str] = []
    if fleet.supervisor_id in fleet.cards:
        ids.append(fleet.supervisor_id)
    ids += [m for m in dict.fromkeys(_known(fleet, m) for m in members) if m not in ids]
    if not ids:
        raise Problem("groups.no_members")
    if len(ids) > MAX_MEMBERS:
        raise Problem("groups.too_many_members", n=MAX_MEMBERS)
    with store.LOCK:
        if len(store.load()) >= MAX_GROUPS:
            raise Problem("groups.too_many_groups", n=MAX_GROUPS)
        taken: set[str] = set()
        assigned = {}
        for sid in ids:
            assigned[sid] = _handle_for(fleet, sid, taken)
            taken.add(assigned[sid])
        group = {
            "id": store.new_id(),
            "title": title,
            "members": ids,
            "handles": assigned,
            "created_at": store.now(),
        }
        store.add(group)
    return group


def rename(group_id: str, title: object) -> dict:
    new = clean_title(title)
    return _changed(group_id, lambda g: g.update(title=new))


def add_member(fleet: Fleet, group_id: str, session_id: object) -> dict:
    sid = _known(fleet, session_id)

    def change(group: dict) -> None:
        if sid in group["members"]:
            return
        if len(group["members"]) >= MAX_MEMBERS:
            raise Problem("groups.too_many_members", n=MAX_MEMBERS)
        group["members"].append(sid)
        taken = {h for s, h in group.get("handles", {}).items() if s in group["members"]}
        group.setdefault("handles", {})[sid] = _handle_for(fleet, sid, taken)

    return _changed(group_id, change)


def remove_member(group_id: str, session_id: object) -> dict:
    def change(group: dict) -> None:
        if session_id not in group["members"]:
            raise Problem("groups.not_a_member")
        if len(group["members"]) == 1:
            raise Problem("groups.last_member")
        group["members"].remove(session_id)
        group.get("handles", {}).pop(session_id, None)

    return _changed(group_id, change)


def delete(group_id: str) -> None:
    if not store.GROUP_ID.fullmatch(group_id) or not store.remove(group_id):
        raise Problem("groups.not_found", HTTPStatus.NOT_FOUND)


def _changed(group_id: str, change) -> dict:
    """Run `change` on the stored group; a Problem it raises leaves the group as it was."""
    find(group_id)
    group = store.update(group_id, change)
    if group is None:
        raise Problem("groups.not_found", HTTPStatus.NOT_FOUND)
    return group
