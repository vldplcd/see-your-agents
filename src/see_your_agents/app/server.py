"""Local HTTP server: one page plus a small JSON API, both assembled from the feature registry.

Binds to 127.0.0.1 only. The host answers /, its own assets, /i18n/<lang>.js (the page's strings,
../i18n.py), /api/board, /api/session/<id>, /api/search, /api/plan, /api/health, /api/pair (browser pairing,
pairing.py) and /mcp; everything under /api/<id> goes to the feature with that id, and /<id>/<file> serves the
files that feature lists from its web/ folder. Every request is answered in the language its X-SYA-Lang names
(i18n.request_lang()), and a Problem a handler raises becomes {"error": text} in that language.
"""

from __future__ import annotations

import functools
import json
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .. import i18n
from ..core import providers
from ..core.index import Index
from ..core.providers.base import LiveSession
from ..core.sessions import build_board, build_detail, jira_base_url, stub_record
from . import mcp, watchdog
from .feature import Feature, check_registry
from .pairing import Pairing

WEB = Path(__file__).resolve().parent.parent / "web"
ASSET = re.compile(r"/([a-z0-9-]+\.(js|css))")
FEATURE_ASSET = re.compile(r"/([a-z][a-z0-9-]*)/([a-z0-9-]+\.(js|css))")
CATALOG = re.compile(r"/i18n/([a-z]{2,3}(?:-[A-Z]{2})?)\.js")
ASSET_TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}
MAX_POST_BYTES = 32 * 1024 * 1024
MCP_PATH = "/mcp"

# Every HTML page the host serves refuses to be framed (clickjacking the buttons); a plan may be framed only
# by the dashboard's own modal, and runs sandboxed: an opaque origin, none of our API, no storage.
NO_FRAMING = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"}
PLAN_HEADERS = {
    "X-Frame-Options": "SAMEORIGIN",
    "Content-Security-Policy": "sandbox allow-scripts; frame-ancestors 'self'",
}

LIVE_TTL_S = 2.0
INDEX_EVERY_S = 5.0
RESTART_DELAY_S = 0.5  # let the reply that asked for a restart reach the page first


@functools.cache
def code_version() -> str:
    """The code this process runs: the checkout's HEAD when installed from git, else the package version.
    The page reloads itself when it changes (an accepted build, a rollback)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=Path(__file__).parent, capture_output=True, text=True, timeout=5,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        out = None
    return out.stdout.strip() if out and out.returncode == 0 else mcp._version()


def page(features: Sequence[Feature], tag: str = "") -> str:
    """index.html with every feature's styles and scripts, in registry order. With a `tag` (this process:
    its version and instance), every local asset URL carries `?v=<tag>`: a copy something else served on this
    port while the dashboard was down, cached under the plain URL, is never the one loaded."""
    v = f"?v={quote(tag)}" if tag else ""
    styles = "".join(f'<link rel="stylesheet" href="{f.id}/{n}{v}">\n' for f in features for n in f.styles)
    scripts = "".join(f'<script src="{f.id}/{n}{v}"></script>\n' for f in features for n in f.scripts)
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for own in ('href="base.css"', 'src="i18n.js"', 'src="figures.js"', 'src="app.js"'):
        html = html.replace(own, f'{own[:-1]}{v}"')
    return html.replace("<!--sya:styles-->\n", styles).replace("<!--sya:scripts-->\n", scripts)


def asset(features: Sequence[Feature], path: str) -> tuple[str, bytes] | None:
    """(content type, bytes) of a page asset: the shell's own file or one a feature lists in its manifest."""
    by_id = {f.id: f for f in features}
    if shell := ASSET.fullmatch(path):
        file, kind = WEB / shell[1], shell[2]
    elif (own := FEATURE_ASSET.fullmatch(path)) and own[1] in by_id:
        file, kind = by_id[own[1]].asset(own[2]), own[3]
    else:
        return None
    return (ASSET_TYPES[kind], file.read_bytes()) if file and file.is_file() else None


def catalog_script(catalogs: i18n.Catalogs, lang: str) -> str:
    """/i18n/<lang>.js: the strings web/i18n.js writes into the page before any other script."""
    return f"window.SYA_I18N = {json.dumps(catalogs.page(lang), ensure_ascii=False)};\n"


class App:
    def __init__(self, index: Index, features: Sequence[Feature] = (), port: int = 8765):
        check_registry(features)
        self.index = index
        self.port = port  # where /mcp answers: a session a feature launched gets its MCP config from it
        self.features = tuple(features)
        self.tools = {t.name: t for f in self.features for t in f.tools}
        self.catalogs = i18n.Catalogs(i18n.folders(self.features))  # the page's strings: shell + registry
        i18n.register(self.catalogs.folders)  # and the server's t(), for a feature outside the package
        self._card_extras = [f.card_extra for f in self.features if f.card_extra]
        self._live: tuple[float, dict[str, LiveSession]] = (0.0, {})
        self._live_lock = threading.Lock()
        self._plan_cache: dict[tuple[str, float], str] = {}
        self.version = code_version()
        self.restart_requested = False
        self.shutdown: Callable[[], None] | None = None  # set by serve(): stops serve_forever
        self.pairing = Pairing(port)  # app.pairing.paired(headers): the request comes from a paired tab
        self.instance = secrets.token_hex(
            8
        )  # this process: a page that sees it change knows its pairing is gone
        for f in self.features:
            if f.setup:
                f.setup(self)

    def live(self) -> dict[str, LiveSession]:
        with self._live_lock:
            at, data = self._live
            if time.monotonic() - at > LIVE_TTL_S:
                data = providers.live()
                self._live = (time.monotonic(), data)
            return data

    def reindex_forever(self) -> None:
        while True:
            try:
                self.index.refresh()
            except Exception as exc:  # keep serving the last good index
                print(f"index refresh failed: {exc!r}")
            time.sleep(INDEX_EVERY_S)

    def request_restart(self) -> bool:
        """Ask the watchdog for a fresh process, so changed Python code loads. False when no watchdog would
        start the server again: then nothing stops."""
        if not watchdog.under_watchdog() or self.shutdown is None:
            return False
        self.restart_requested = True
        threading.Timer(RESTART_DELAY_S, self.shutdown).start()
        return True

    def board(self) -> dict:
        board = build_board(self.index.records(), self.live(), self._card_extras)
        board["version"] = self.version
        board["instance"] = self.instance
        for f in self.features:
            if f.board_extra:
                board.update(f.board_extra(self))
        return board

    def detail(self, session_id: str) -> dict | None:
        live = self.live().get(session_id)
        rec = self.index.record(session_id) or (stub_record(live) if live else None)
        if rec is None:
            return None
        base = jira_base_url(self.index.records())
        return build_detail(rec, self.index.events(session_id), live, base, self._card_extras)

    def resume_args(self, session_id: str) -> list[str]:
        """Extra flags for a one-turn resume of the session by its provider (`claude -p --resume`): the
        launch config of the feature that started it (the supervisor), so a resume never runs it with the
        user's tools and MCP servers."""
        for f in self.features:
            if f.resume_args and (args := f.resume_args(self, session_id)) is not None:
                return args
        return []

    def route_owner(self, path: str) -> Feature | None:
        return next((f for f in self.features if f.owns(path)), None)

    def plan(self, session_id: str, position: int) -> tuple[str, str] | None:
        """(content-type, body) of a plan the session recorded; never an arbitrary path."""
        rec = self.index.record(session_id)
        plans = rec.get("plans", []) if rec else []
        if not 0 <= position < len(plans):
            return None
        plan = plans[position]
        if plan.get("text"):
            return "text/plain; charset=utf-8", plan["text"]
        path = Path(plan.get("path", ""))
        if not path.is_file():
            return None
        if path.suffix == ".mdx" and (vplan := shutil.which("vplan")):
            key = (str(path), path.stat().st_mtime)
            if key not in self._plan_cache:
                out = subprocess.run(
                    [vplan, "render", str(path), "--stdout"], capture_output=True, text=True, timeout=60
                )
                if out.returncode == 0:
                    self._plan_cache[key] = out.stdout
            if key in self._plan_cache:
                return "text/html; charset=utf-8", self._plan_cache[key]
        return "text/plain; charset=utf-8", path.read_text(errors="replace")


def open_post_pattern(features: Sequence[Feature]) -> re.Pattern[str]:
    """The POSTs that skip the paired-browser check, full-matched: the pairing bootstrap and each feature's
    `open_posts`, every entry in a group of its own (check_registry refuses `|` and anchors in them too)."""
    opened = [r"/api/pair(/[a-z]+)?", *(p for f in features for p in f.open_posts)]
    return re.compile("|".join(f"(?:{p})" for p in opened))


def make_handler(app: App, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}
    # POSTs change the world (launch a session, write a group, accept a build), so each must come from the
    # paired browser (pairing.py), not just carry the page's Origin and X-SYA — any local process forges
    # those. The exceptions are the pairing bootstrap itself and routes a feature guards another way (the
    # approvals hook's HMAC-signed routes, via its `open_posts`). MCP is on /mcp with its own tokens, before
    # this guard. The census test (tests/app/test_layers.py) fails if a feature adds a POST that is neither
    # paired nor explicitly opened here.
    open_post = open_post_pattern(app.features)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet: the page polls every few seconds
            pass

        def send(
            self, status: int, ctype: str, body: str | bytes, extra: dict[str, str] | None = None
        ) -> None:
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            framing = NO_FRAMING if ctype.startswith("text/html") else {}
            for key, value in {**framing, **(extra or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, payload, status: int = HTTPStatus.OK) -> None:
            self.send(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False))

        def body_length(self, limit: int) -> int | None:
            """Content-Length within 0..limit, or None after answering: a negative length would make
            rfile.read() wait for the client to close the connection."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                self.send(HTTPStatus.BAD_REQUEST, "text/plain", "bad Content-Length")
                return None
            if length > limit:
                self.send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "text/plain", "too large")
                return None
            return length

        def mcp_request(self, method: str) -> None:
            # MCP clients are not the page: the route has its own guard (Host, Origin, bearer token).
            if (length := self.body_length(mcp.MAX_BODY_BYTES)) is None:
                return
            body = self.rfile.read(length) if length else b""
            self.send(*mcp.handle(app, method, self.headers, body, allowed_hosts, allowed_origins))

        def answer(self, handle: Callable[[], None]) -> None:
            """Answer in the language the request names; a Problem raised below is the page's error."""
            lang = i18n.lang_of(self.headers)
            with i18n.answering(lang):
                try:
                    handle()
                except i18n.Problem as exc:
                    self.send_json({"error": exc.text(lang)}, exc.status)

        def do_DELETE(self):  # noqa: N802
            if urlparse(self.path).path == MCP_PATH:
                return self.mcp_request("DELETE")
            self.send(HTTPStatus.METHOD_NOT_ALLOWED, "text/plain", "not allowed")

        def do_GET(self):  # noqa: N802 (http.server naming)
            if urlparse(self.path).path == MCP_PATH:
                return self.mcp_request("GET")
            self.answer(self.get)

        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path == MCP_PATH:
                return self.mcp_request("POST")
            self.answer(self.post)

        def get(self) -> None:
            # Transcripts are private: refuse requests addressed to another host (DNS rebinding).
            if self.headers.get("Host") not in allowed_hosts:
                return self.send(HTTPStatus.FORBIDDEN, "text/plain", "forbidden host")
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/":
                html = page(app.features, f"{app.version}.{app.instance}")
                return self.send(HTTPStatus.OK, "text/html; charset=utf-8", html)
            if found := asset(app.features, url.path):
                return self.send(HTTPStatus.OK, *found)
            if lang := CATALOG.fullmatch(url.path):
                return self.send(HTTPStatus.OK, ASSET_TYPES["js"], catalog_script(app.catalogs, lang[1]))
            if url.path == "/api/board":
                return self.send_json(app.board())
            if url.path == "/api/health":  # the watchdog's check after a restart
                return self.send_json({"ok": True, "version": app.version})
            if url.path == "/api/pair":
                return self.send_json({"paired": app.pairing.paired(self.headers)})
            if url.path.startswith("/api/session/"):
                detail = app.detail(url.path.rsplit("/", 1)[-1])
                return self.send_json(detail) if detail else self.send_json({"error": "not found"}, 404)
            if url.path == "/api/search":
                return self.send_json(app.index.search(q.get("q", "")) if q.get("q") else {})
            if url.path == "/api/plan":
                found = app.plan(q.get("session", ""), int(q.get("i", "-1")))
                if not found:
                    return self.send(404, "text/plain", "no plan")
                return self.send(HTTPStatus.OK, *found, PLAN_HEADERS)
            feature = app.route_owner(url.path)
            if feature and feature.handle_get and (reply := feature.handle_get(app, url.path, q)):
                return self.send(*reply)
            self.send(HTTPStatus.NOT_FOUND, "text/plain", "not found")

        def post(self) -> None:
            # POSTs act on sessions (send text, stop a process), so besides the Host check they must come
            # from this page: the Origin matches and the custom header forces a CORS preflight elsewhere.
            if (
                self.headers.get("Host") not in allowed_hosts
                or self.headers.get("Origin") not in allowed_origins
                or self.headers.get("X-SYA") != "1"
            ):
                return self.send(HTTPStatus.FORBIDDEN, "text/plain", "forbidden")
            if (length := self.body_length(MAX_POST_BYTES)) is None:
                return
            body = self.rfile.read(length)
            path = urlparse(self.path).path
            if path.startswith("/api/pair"):
                return self.pair(path, body)
            if not open_post.fullmatch(path) and not app.pairing.paired(self.headers):
                error = i18n.t(i18n.request_lang(), "app.pair.not_paired")
                return self.send_json({"error": error, "unpaired": True}, HTTPStatus.FORBIDDEN)
            feature = app.route_owner(path)
            if (
                feature
                and feature.handle_post
                and (reply := feature.handle_post(app, path, body, self.headers))
            ):
                return self.send(*reply)
            self.send(HTTPStatus.NOT_FOUND, "text/plain", "not found")

        def pair(self, path: str, body: bytes) -> None:
            """Browser pairing (pairing.py); the POST guard has run. Only on the 127.0.0.1 origin: the tab
            the server opens is always there, and `localhost` may be someone else's ::1."""
            if not app.pairing.ours(self.headers):
                raise i18n.Problem("app.pair.localhost", HTTPStatus.FORBIDDEN)
            if path == "/api/pair/start":
                app.pairing.start()  # PairingError: a Problem, 409
                return self.send_json({"ok": True})
            if path == "/api/pair":
                try:
                    code = json.loads(body or b"{}").get("code")
                except (ValueError, AttributeError):
                    code = None
                if (secret := app.pairing.redeem(code)) is None:
                    raise i18n.Problem("app.pair.bad_code", HTTPStatus.FORBIDDEN)
                return self.send_json({"secret": secret})  # the page keeps it in localStorage
            if path == "/api/pair/forget":
                if not app.pairing.paired(self.headers):
                    raise i18n.Problem("app.pair.not_paired", HTTPStatus.FORBIDDEN)
                app.pairing.forget_all()
                return self.send_json({"ok": True})
            self.send(HTTPStatus.NOT_FOUND, "text/plain", "not found")

    return Handler


def serve(index: Index, port: int, features: Sequence[Feature]) -> ThreadingHTTPServer:
    app = App(index, features, port)
    index.refresh()
    threading.Thread(target=app.reindex_forever, daemon=True).start()
    for f in app.features:
        if f.background:
            threading.Thread(target=f.background, args=(app,), name=f"sya-{f.id}", daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port))
    httpd.app = app  # type: ignore[attr-defined]  # the CLI reads restart_requested after serve_forever
    app.shutdown = httpd.shutdown
    return httpd
