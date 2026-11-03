"""Who said hello to the agents from the page. In memory: a real feature keeps its file in config_dir()."""

from __future__ import annotations

import argparse
import json
import threading
from http import HTTPStatus
from typing import TYPE_CHECKING

from see_your_agents.app.mcp import Tool
from see_your_agents.app.replies import Reply, json_reply
from see_your_agents.i18n import Problem

if TYPE_CHECKING:
    from email.message import Message

    from see_your_agents.app.server import App
    from see_your_agents.core.providers.base import LiveSession

ROUTE = "/api/hello"
MAX_NAME = 40
_lock = threading.Lock()
names: list[str] = []


def handle_get(app: App, path: str, query: dict[str, str]) -> Reply | None:
    if path != ROUTE:
        return None  # not ours: the host answers 404
    with _lock:
        return json_reply({"names": list(names)})


def handle_post(app: App, path: str, body: bytes, headers: Message) -> Reply | None:
    if path != ROUTE:
        return None
    try:
        name = json.loads(body).get("name")
    except (ValueError, AttributeError):  # not JSON, or not an object
        name = None
    if not isinstance(name, str) or not 0 < len(name.strip()) <= MAX_NAME:
        raise Problem("hello.bad_name", HTTPStatus.BAD_REQUEST, max=MAX_NAME)  # worded by the host
    with _lock:
        names.append(name.strip())
        return json_reply({"names": list(names)})


def card_fields(rec: dict, live: LiveSession | None) -> dict:
    """Every board card (and session detail) learns how many words its title has."""
    return {"hello_words": len((rec.get("title") or "").split())}


def _names(app: App, args: dict) -> dict:
    with _lock:
        return {"names": list(names)}


# Text an agent reads is English and never a catalog string (docs/i18n.md).
TOOL = Tool("hello_names", "read", "Who said hello to the agents on the dashboard page.", {}, (), _names)


def add_cli(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("hello", help="say hello")
    p.set_defaults(run=lambda args, features: print("Hello"))
