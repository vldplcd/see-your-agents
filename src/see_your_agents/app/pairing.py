"""Browser pairing: a secret that tells the dashboard's own tab apart from anything else that speaks HTTP.

Origin and X-SYA keep web pages out, but any local process can forge them. What a process that only uses
the HTTP API does not have is a secret the paired tab keeps in its sessionStorage. So every mutating page POST
(the host's guard in server.do_POST; answering a permission prompt too) wants that secret, sent as the
X-SYA-Pair header. A cross-origin page cannot set that header without a preflight, which the server
does not answer.

    POST /api/pair/start    the page asks; the server makes a one-time code and opens the browser at
                            http://127.0.0.1:<port>/#pair=<code> (the code goes to that tab only)
    POST /api/pair          the tab, after the person confirms, trades the code for the secret
    POST /api/pair/forget   a paired tab unpairs every tab
    GET  /api/pair          {"paired": bool} for the tab that asks with its secret

Where things live, and why:
- The secret's hash lives in this process's memory only. While the dashboard is down, any process can take its
  port and serve the same origin; whatever it reads there belongs to a process that is gone. Every `sya serve`
  starts unpaired.
- The tab keeps the secret in sessionStorage: one tab, one origin (port included). Another tab, or a page on
  another port of 127.0.0.1, does not see it; a cookie would go to every port.
- The page does not reload itself when the server changes (app.js shows a banner), so a restart never loads
  whatever answers on the port in its place.
- 127.0.0.1 only: `localhost` may resolve to ::1, which another process can bind while the dashboard listens
  on IPv4.

The code: 32 random bytes, 2 minutes, one at a time, used up only by an exact match; a new tab at most once a
minute. Opening the browser keeps the code out of every process's argv (other users can list those): on macOS
`osascript` reads `open location` from stdin. Elsewhere there is no such way yet, so pairing is refused.

Not protected against (the residual risk): whoever takes the port while the dashboard is off and waits for the
page to be opened once in that window can plant a service worker and steal the next pairing. That takes a
process of this user (which could simply put allow-all into ~/.claude/settings.json) or of another user of the
Mac; the real fix is a port that is always held (launchd, later). Nor against a process of the same user that
drives the browser's UI or reads its memory, or a person who confirms a pairing they did not start. See
docs/ARCHITECTURE.md, "Security".
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from http import HTTPStatus

from ..i18n import Problem

CODE_TTL_S = 120.0
START_EVERY_S = 60.0  # a pairing tab at most once a minute, whoever asks
MAX_TABS = 8  # paired tabs this process remembers; the least recently used goes first
HEADER = "X-SYA-Pair"
OPENER_ENV = "SYA_PAIR_OPENER"  # a command that gets the URL on stdin instead of the browser (live checks)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def supported() -> bool:
    return bool(os.environ.get(OPENER_ENV)) or sys.platform == "darwin"


def open_browser(url: str) -> None:
    """The default browser at `url`, the URL kept out of every argv: `osascript` reads it from stdin."""
    if command := os.environ.get(OPENER_ENV):
        subprocess.run(shlex.split(command), input=url, text=True, timeout=10, check=False)
    else:
        script = f'open location "{url}"'  # the URL is [A-Za-z0-9:/._#=-] only: no quote to escape
        subprocess.run(["osascript", "-"], input=script, text=True, timeout=10, check=False)


class PairingError(Problem):
    """Pairing cannot start now (409); the host words it for the person at the page."""

    def __init__(self, key: str, /, **params: object):
        super().__init__(key, HTTPStatus.CONFLICT, **params)


class Pairing:
    def __init__(self, port: int, opener: Callable[[str], None] | None = None, clock=time.monotonic):
        self.port = port
        self.origin = f"http://127.0.0.1:{port}"
        self.opener = opener  # None: open_browser, looked up when used (tests replace it)
        self._clock = clock
        self._code: tuple[str, float] | None = None  # sha256 of the open code, when it expires
        self._last_start: float | None = None
        self._tabs: dict[str, float] = {}  # sha256 of a paired tab's secret -> its last use; memory only
        self._lock = threading.Lock()

    def any_paired(self) -> bool:
        with self._lock:
            return bool(self._tabs)

    def forget_all(self) -> None:
        with self._lock:
            self._tabs.clear()

    def new_code(self) -> str:
        """A one-time code, kept only as its hash; it replaces any code still open."""
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._code = (_sha(code), self._clock() + CODE_TTL_S)
        return code

    def start(self) -> None:
        """Open the browser at the dashboard with a fresh code; PairingError when it cannot be now."""
        if not supported():
            raise PairingError("app.pair.unsupported")
        with self._lock:
            now = self._clock()
            if self._last_start is not None and now - self._last_start < START_EVERY_S:
                left = int(START_EVERY_S - (now - self._last_start)) + 1
                raise PairingError("app.pair.too_soon", n=left)
            self._last_start = now
        (self.opener or open_browser)(f"{self.origin}/#pair={self.new_code()}")

    def redeem(self, code: object) -> str | None:
        """The secret for the open, unexpired code. Only that code uses it up; a wrong guess leaves it."""
        if not isinstance(code, str) or not code:
            return None
        with self._lock:
            if self._code is None:
                return None
            wanted, expires = self._code
            if expires <= self._clock():
                self._code = None
                return None
            if not hmac.compare_digest(wanted, _sha(code)):
                return None
            self._code = None
            secret = secrets.token_urlsafe(32)
            self._tabs[_sha(secret)] = self._clock()
            while len(self._tabs) > MAX_TABS:
                del self._tabs[min(self._tabs, key=self._tabs.__getitem__)]
        return secret

    def ours(self, headers) -> bool:
        """Sent to 127.0.0.1:<port> by its own page (Origin, when there is one, is that page)."""
        origin = headers.get("Origin")
        return headers.get("Host") == f"127.0.0.1:{self.port}" and origin in (None, self.origin)

    def paired(self, headers) -> bool:
        """A request of a paired tab: on 127.0.0.1, with the X-SYA-Pair secret this process handed out (every
        hash compared)."""
        secret = headers.get(HEADER) or ""
        if not (secret and self.ours(headers)):
            return False
        given = _sha(secret).encode()
        with self._lock:
            found = None
            for known in self._tabs:
                if hmac.compare_digest(given, known.encode()):
                    found = known
            if found:
                self._tabs[found] = self._clock()
        return found is not None
