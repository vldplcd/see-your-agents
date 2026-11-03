"""What feature handlers hand back to the HTTP layer: (status, content type, body)."""

from __future__ import annotations

import json
from http import HTTPStatus

Reply = tuple[int, str, str | bytes]


def json_reply(payload: object, status: int = HTTPStatus.OK) -> Reply:
    return status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False)


def text_reply(text: str, status: int = HTTPStatus.OK) -> Reply:
    return status, "text/plain; charset=utf-8", text
