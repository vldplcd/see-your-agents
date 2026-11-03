"""The Codex daemon's control socket for tests: the frames a probe recorded from codex 0.166.0 (redacted), and
a fake daemon that answers with them. Nothing here talks to a real daemon: the socket lives in a temp folder.

Recorded on 2026-11-03 (docs/notes/codex-cli-notes.md, «The daemon's RPC»): the handshake reply, initialize,
thread/loaded/list, thread/read of an idle thread, `active` with no flags while a turn ran, and the two
notifications the daemon pushed unasked. `waitingOnApproval` and `waitingOnUserInput` are from the protocol's
own schema (`codex app-server generate-json-schema`): no probe could put a daemon thread there without a TUI.
"""

import base64
import contextlib
import hashlib
import json
import os
import shutil
import socket
import struct
import tempfile
import threading
from pathlib import Path

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

HANDSHAKE_REPLY = (
    "HTTP/1.1 101 Switching Protocols\r\nconnection: Upgrade\r\nupgrade: websocket\r\n"
    "sec-websocket-accept: {accept}\r\nx-codex-websocket-max-unfragmented-message-bytes: 16777216\r\n\r\n"
)
INITIALIZE = {
    "userAgent": "codex-tui/0.166.0 (Mac OS 26.4.0; arm64) vscode/1.130.0 (see_your_agents; 0)",
    "codexHome": "/Users/someone/.codex",
    "platformFamily": "unix",
    "platformOs": "macos",
}
NOTIFICATIONS = [
    {
        "method": "remoteControl/status/changed",
        "params": {
            "status": "disabled",
            "serverName": "host.local",
            "installationId": "00000000-0000-4000-8000-000000000000",
            "environmentId": None,
        },
        "emittedAtMs": 1793710812987,
    },
    {"method": "account/updated", "params": {"authMode": "chatgpt", "planType": "plus"}, "emittedAtMs": 1},
]
# The daemon's own request to the client that runs a turn (schema: CommandExecutionRequestApprovalParams):
# a poll must never answer it.
APPROVAL_REQUEST = {
    "id": 1,  # the same id as the poll's first call: still not its answer
    "method": "item/commandExecution/requestApproval",
    "params": {"threadId": "t", "turnId": "u", "itemId": "i", "command": "touch x", "cwd": "/w"},
}
IDLE = {"type": "idle"}
ACTIVE = {"type": "active", "activeFlags": []}
NOT_LOADED = {"type": "notLoaded"}
APPROVAL = {"type": "active", "activeFlags": ["waitingOnApproval"]}  # schema
USER_INPUT = {"type": "active", "activeFlags": ["waitingOnUserInput"]}  # schema


def thread(tid: str, status: dict) -> dict:
    """thread/read's result, without turns, as 0.166.0 gave it for a thread `codex exec` started."""
    cwd = "/work/app"
    return {
        "thread": {
            "id": tid,
            "environments": [{"environmentId": "local", "cwd": cwd, "runtimeWorkspaceRoots": [cwd]}],
            "extra": None,
            "sessionId": tid,
            "forkedFromId": None,
            "parentThreadId": None,
            "preview": "Reply with the single word OK.",
            "ephemeral": False,
            "section": None,
            "sectionEnteredAt": None,
            "projectId": None,
            "historyMode": "paginated",
            "modelProvider": "openai",
            "model": "gpt-6-luna",
            "reasoningEffort": "medium",
            "createdAt": 1793711014,
            "updatedAt": 1793711060,
            "recencyAt": 1793711057,
            "status": status,
            "path": f"/Users/someone/.codex/sessions/2026/11/03/rollout-2026-11-03T16-03-34-{tid}.jsonl",
            "cwd": cwd,
            "cliVersion": "0.166.0",
            "originator": "codex_exec",
            "source": "exec",
            "canAcceptDirectInput": True,
            "threadSource": "user",
            "agentNickname": None,
            "agentRole": None,
            "gitInfo": None,
            "name": None,
            "daybreakEnabled": None,
            "turns": [],
        },
        "readState": {"firstUnread": {"type": "turn", "turnId": tid}, "revision": "r"},
    }


def accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def server_frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
    """A frame from the server: never masked."""
    n = len(payload)
    b0 = (0x80 if fin else 0) | opcode
    if n < 126:
        return struct.pack("!BB", b0, n) + payload
    if n < 1 << 16:
        return struct.pack("!BBH", b0, 126, n) + payload
    return struct.pack("!BBQ", b0, 127, n) + payload


def text(msg: dict) -> bytes:
    return server_frame(json.dumps(msg).encode())


def read_client_frame(conn: socket.socket) -> tuple[int, bytes, bool]:
    """(opcode, unmasked payload, was masked) of the client's next frame."""

    def exact(n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    b0, b1 = exact(2)
    n = b1 & 0x7F
    if n == 126:
        (n,) = struct.unpack("!H", exact(2))
    elif n == 127:
        (n,) = struct.unpack("!Q", exact(8))
    mask = exact(4) if b1 & 0x80 else b""
    data = exact(n)
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return b0 & 0x0F, data, bool(mask)


class FakeDaemon:
    """A daemon on a unix socket in a short temp folder, linked from CODEX_HOME the way Codex links its own,
    with a daemon.pid that names this (live) process.

    `statuses` — loaded threads and their Thread.status (None: thread/read answers an error).
    `mode`: "ok"; "ping" (a ping before every answer); "silent" (never answers the handshake); "mute"
    (handshake, then no answers); "refuse" (the socket file is there, nobody listens); "bad_handshake" (400);
    "bad_accept"; "bad_json"; "binary"; "fragmented". `chatter` goes out before every answer. `received`
    keeps what clients sent: (opcode, message or payload, masked)."""

    def __init__(self, home: Path, statuses: dict[str, object], mode: str = "ok", chatter=()):
        self.statuses, self.mode, self.chatter = statuses, mode, list(chatter)
        self.received: list[tuple[int, object, bool]] = []
        self.connections = 0
        self.folder = tempfile.mkdtemp(prefix="sya-")  # AF_UNIX paths are short: CODEX_HOME's is too long
        self.path = os.path.join(self.folder, "d.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        if mode == "refuse":
            self.server.close()  # the file stays, connecting is refused
        else:
            self.server.listen(4)
            self.server.settimeout(0.2)
        self.stopped = threading.Event()
        self.closed = threading.Event()  # a client sent its close frame
        self.thread = threading.Thread(target=self._serve, daemon=True)
        if mode != "refuse":
            self.thread.start()
        link = home / "app-server-control" / "app-server-control.sock"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(self.path)
        pid_file = home / "app-server-daemon" / "daemon.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(json.dumps({"pid": os.getpid(), "processStartTime": "Tue Nov  3 10:42:24 2026"}))

    def close(self) -> None:
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(2)
        with contextlib.suppress(OSError):
            self.server.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def sent(self) -> list[dict]:
        """The JSON-RPC messages clients sent, in order."""
        return [m for op, m, _ in self.received if op == 0x1 and isinstance(m, dict)]

    def methods(self) -> list[str]:
        return [m.get("method", "") for m in self.sent()]

    def messages(self, method: str) -> list[dict]:
        return [m for m in self.sent() if m.get("method") == method]

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                conn, _ = self.server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            with conn:
                conn.settimeout(2)
                with contextlib.suppress(OSError, EOFError):
                    self._talk(conn)

    def _talk(self, conn: socket.socket) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(1)
        if self.mode == "silent":
            self.stopped.wait(3)
            return
        key = next(
            line.split(":", 1)[1].strip()
            for line in head.decode().split("\r\n")
            if line.lower().startswith("sec-websocket-key")
        )
        if self.mode == "bad_handshake":
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\ncontent-length: 0\r\n\r\n")
            return
        answer = "nope" if self.mode == "bad_accept" else accept(key)
        conn.sendall(HANDSHAKE_REPLY.format(accept=answer).encode())
        while True:
            opcode, data, was_masked = read_client_frame(conn)
            msg = json.loads(data) if opcode == 0x1 else data
            self.received.append((opcode, msg, was_masked))
            if opcode == 0x8:
                self.closed.set()
                return
            if opcode != 0x1 or "id" not in msg:
                continue
            if self.mode == "mute":
                continue
            for extra in self.chatter:
                conn.sendall(text(extra))
            if self.mode == "bad_json":
                conn.sendall(server_frame(b"{not json"))
            elif self.mode == "binary":
                conn.sendall(server_frame(b"\x00\x01", opcode=0x2))
            elif self.mode == "fragmented":
                conn.sendall(server_frame(b'{"id": ', fin=False))
            else:
                if self.mode == "ping":
                    conn.sendall(server_frame(b"hi", opcode=0x9))
                conn.sendall(text({"id": msg["id"], **self._answer(msg)}))

    def _answer(self, msg: dict) -> dict:
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            return {"result": INITIALIZE}
        if method == "thread/loaded/list":
            return {"result": {"data": list(self.statuses), "nextCursor": None}}
        if method == "thread/read" and self.statuses.get(params.get("threadId")) is not None:
            return {"result": thread(params["threadId"], self.statuses[params["threadId"]])}
        return {"error": {"code": -32600, "message": f"unknown {method}"}}
