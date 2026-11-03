"""Files the user attached to a message in the chat: cache_dir()/uploads/<session_id>/. A resume turn of that
session may read them."""

from pathlib import Path

from .. import cache_dir


def uploads_root() -> Path:
    return cache_dir() / "uploads"


def session_uploads(session_id: str) -> Path:
    return uploads_root() / session_id
