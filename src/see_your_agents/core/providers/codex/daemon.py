"""What Codex threads wait for, from the daemon's JSON-RPC (docs/codex-cli-notes.md, «The daemon's RPC»).

A thread the shared app-server daemon hosts (a TUI is only its client) may wait on an approval or an answer.
Nothing on disk says so; only the daemon does: `Thread.status = active{activeFlags: [waitingOnApproval |
waitingOnUserInput]}`. It speaks JSON-RPC in WebSocket text frames over its control socket,
~/.codex/app-server-control/app-server-control.sock (a link into /private/tmp/codex-daemon-<uid>/).

The poll only reads: initialize, initialized, thread/loaded/list, thread/read without turns. It never sends
turn/*, thread/start or resume, and never answers the daemon's own requests (approvals among them). It runs
only while a daemon-hosted thread has an open turn on disk, at most once per POLL_S, in its own thread, and
gives up after TIMEOUT_S on any step. The protocol is experimental, so any failure turns the poll off quietly:
for RETRY_S after a transport failure (no daemon, refused, timeout), for as long as this daemon runs after a
protocol mismatch. The board then shows what the disk says, as before. The daemon is never started: without
its pid file, its process and its socket there is no poll. VS Code runs its own app-server per window: its
threads are not this daemon's and stay as the disk shows them.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import json
import math
import os
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterable
from importlib import metadata
from pathlib import Path

from ..base import LiveSession, Status
from .live import daemon_pid

POLL_S = 3.0
STALE_S = 10.0  # an older answer is not used: the poll stalled or went off
TIMEOUT_S = 1.0  # connect, handshake, each call
RETRY_S = 30.0  # quiet after a transport failure
MAX_MESSAGE = 1 << 20
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455: Sec-WebSocket-Accept = b64(sha1(key + GUID))
TEXT, CLOSE, PING, PONG = 0x1, 0x8, 0x9, 0xA
# Thread.status.activeFlags -> LiveSession.waiting_for, the words Claude's sessions use; the first one wins
WAITING_FOR = {"waitingOnApproval": "permission prompt", "waitingOnUserInput": "input needed"}


class Mismatch(Exception):
    """The daemon answered, but not in the protocol this client knows."""


class RpcError(Mismatch):
    """The daemon refused a call with a JSON-RPC error."""


def socket_path(home: Path) -> Path:
    return home / "app-server-control" / "app-server-control.sock"


def running_daemon(home: Path) -> str:
    """The daemon's pid file as written, while its process lives and its socket is there; "" otherwise."""
    pid = daemon_pid(home)
    if pid <= 0 or not socket_path(home).exists():  # exists() follows the link: a dead daemon's is dangling
        return ""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ""
    except PermissionError:  # alive, only not ours to signal
        pass
    try:
        return (home / "app-server-daemon" / "daemon.pid").read_text()
    except OSError:
        return ""


def accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def masked(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    key = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(data, "big") ^ key).to_bytes(n, "big")


def frame(payload: bytes, opcode: int = TEXT) -> bytes:
    """One final frame from the client: masked, as RFC 6455 requires of clients."""
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
    elif n < 1 << 16:
        head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
    mask = os.urandom(4)
    return head + mask + masked(payload, mask)


class Connection:
    """A WebSocket to the daemon over its unix socket. Every read waits at most `timeout`, and the whole
    talk at most `budget`: a daemon that keeps sending notifications cannot hold the poll."""

    def __init__(self, path: str, timeout: float = TIMEOUT_S, budget: float = 3 * TIMEOUT_S) -> None:
        self.timeout, self.until, self.buf = timeout, time.monotonic() + budget, b""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(timeout)
            self.sock.connect(path)
            self._handshake()
        except BaseException:
            self.sock.close()
            raise

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        with contextlib.suppress(OSError):
            self.sock.sendall(frame(struct.pack("!H", 1000), CLOSE))
        self.sock.close()

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        while b"\r\n\r\n" not in self.buf:
            if len(self.buf) > 8192:
                raise Mismatch("handshake: no end of headers")
            self._fill()
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        status, *lines = head.decode("latin-1").split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines)}
        if status.split(" ")[1:2] != ["101"] or headers.get("sec-websocket-accept") != accept(key):
            raise Mismatch(f"handshake: {status!r}")

    def _fill(self) -> None:
        left = self.until - time.monotonic()
        if left <= 0:
            raise TimeoutError("the daemon took too long")
        self.sock.settimeout(min(self.timeout, left))
        chunk = self.sock.recv(65536)
        if not chunk:
            raise EOFError("the daemon closed the connection")
        self.buf += chunk

    def _take(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str) -> None:
        self.sock.settimeout(self.timeout)
        self.sock.sendall(frame(text.encode()))

    def receive(self) -> str:
        """The next text message. A ping gets its pong; a close, binary or fragmented frame ends the talk."""
        while True:
            b0, b1 = self._take(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                (n,) = struct.unpack("!H", self._take(2))
            elif n == 127:
                (n,) = struct.unpack("!Q", self._take(8))
            if n > MAX_MESSAGE:
                raise Mismatch(f"a message of {n} bytes")
            mask = self._take(4) if b1 & 0x80 else b""
            data = self._take(n)
            data = masked(data, mask) if mask else data
            if opcode == PING:
                self.sock.sendall(frame(data, PONG))
            elif opcode == CLOSE:
                raise EOFError("the daemon closed the connection")
            elif opcode != PONG:
                if opcode != TEXT or not b0 & 0x80:
                    raise Mismatch(f"frame {b0:#04x}")
                return data.decode()


class Rpc:
    """JSON-RPC over a Connection. The daemon's notifications and its own requests are skipped, never
    answered: this client is not the one that approves anything."""

    def __init__(self, conn: Connection) -> None:
        self.conn, self.last = conn, 0

    def notify(self, method: str) -> None:
        self.conn.send(json.dumps({"jsonrpc": "2.0", "method": method}))

    def call(self, method: str, params: dict) -> dict:
        self.last += 1
        self.conn.send(json.dumps({"jsonrpc": "2.0", "id": self.last, "method": method, "params": params}))
        while True:
            msg = json.loads(self.conn.receive())
            if not isinstance(msg, dict):
                raise Mismatch(f"{method}: not an object")
            if "method" in msg or msg.get("id") != self.last:
                continue
            if "error" in msg:
                raise RpcError(f"{method}: {str(msg['error'])[:200]}")
            if not isinstance(result := msg.get("result"), dict):
                raise Mismatch(f"{method}: no result")
            return result


def client_info() -> dict:
    try:
        version = metadata.version("see-your-agents")
    except metadata.PackageNotFoundError:
        version = "0"
    return {"name": "see_your_agents", "title": "see-your-agents dashboard", "version": version}


def waits_for(status: object) -> str:
    """What a Thread.status waits for: "permission prompt", "input needed" or "". A shape this client does not
    know is a Mismatch; a new status type or flag is only not a wait."""
    if not isinstance(status, dict) or not isinstance(status.get("type"), str):
        raise Mismatch(f"status {status!r}"[:200])
    if status["type"] != "active":
        return ""
    flags = status.get("activeFlags")
    if not isinstance(flags, list):
        raise Mismatch(f"status {status!r}"[:200])
    return next((said for flag, said in WAITING_FOR.items() if flag in flags), "")


def poll(path: str, candidates: Iterable[str], timeout: float = TIMEOUT_S) -> dict[str, str]:
    """{thread id: what it waits for} among `candidates` the daemon has loaded. Raises on any failure: OSError
    or EOFError when the daemon cannot be reached, Mismatch or ValueError when it speaks another protocol."""
    with Connection(os.path.realpath(path), timeout, 3 * timeout) as conn:
        rpc = Rpc(conn)
        rpc.call("initialize", {"clientInfo": client_info(), "capabilities": None})
        rpc.notify("initialized")
        loaded = rpc.call("thread/loaded/list", {}).get("data")
        if not isinstance(loaded, list):
            raise Mismatch("thread/loaded/list: no data")
        waiting = {}
        for tid in sorted(set(candidates).intersection(loaded)):
            try:
                thread = rpc.call("thread/read", {"threadId": tid, "includeTurns": False}).get("thread")
            except RpcError:  # gone between the list and the read
                continue
            if not isinstance(thread, dict):
                raise Mismatch("thread/read: no thread")
            if said := waits_for(thread.get("status")):
                waiting[tid] = said
        return waiting


def _in_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="codex-daemon-poll", daemon=True).start()


class Waits:
    """What the daemon's threads wait for, as of the last poll; the board's own calls never wait for one."""

    def __init__(
        self,
        poll: Callable[[str, frozenset[str]], dict[str, str]] = poll,
        spawn: Callable[[Callable[[], None]], None] = _in_thread,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._poll, self._spawn, self._clock = poll, spawn, clock
        self._lock = threading.Lock()
        self._found: dict[str, str] = {}
        self._at = -math.inf  # when _found was polled
        self._running = False
        self._quiet_until = -math.inf
        self._mismatched = ""  # the daemon (its pid file) whose protocol this client does not know

    def apply(self, home: Path, sessions: dict[str, LiveSession]) -> dict[str, LiveSession]:
        """`sessions` with the daemon's waits on them: status waiting and what for, as Claude's show it."""
        busy = {tid for tid, s in sessions.items() if s.host == "daemon" and s.status == Status.BUSY}
        found = self.waiting(home, busy)
        return {
            tid: dataclasses.replace(s, status=Status.WAITING, waiting_for=found[tid]) if tid in found else s
            for tid, s in sessions.items()
        }

    def waiting(self, home: Path, candidates: set[str]) -> dict[str, str]:
        """{thread id: what it waits for} among `candidates`; starts the next poll when one is due."""
        if not candidates:
            return {}
        with self._lock:
            now, daemon, start = self._clock(), "", False
            if not self._running and now - self._at >= POLL_S and now >= self._quiet_until:
                daemon = running_daemon(home)
                start = self._running = bool(daemon) and daemon != self._mismatched
        if start:
            asked = frozenset(candidates)
            self._spawn(lambda: self._refresh(home, daemon, asked))
        with self._lock:
            if self._clock() - self._at > STALE_S:
                return {}
            return {tid: said for tid, said in self._found.items() if tid in candidates}

    def _refresh(self, home: Path, daemon: str, candidates: frozenset[str]) -> None:
        found: dict[str, str] | None = None
        mismatched = ""
        try:
            found = self._poll(str(socket_path(home)), candidates)
        except (OSError, EOFError):  # no daemon, refused, timed out: try again later
            pass
        except Exception:  # another protocol (Mismatch, bad JSON…): quiet while this daemon runs
            mismatched = daemon
        with self._lock:
            self._running = False
            if found is not None:
                self._found, self._at = found, self._clock()
                return
            self._found, self._at = {}, -math.inf
            if mismatched:
                self._mismatched = mismatched
            else:
                self._quiet_until = self._clock() + RETRY_S
