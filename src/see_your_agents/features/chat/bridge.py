"""Client for the VS Code bridge extension (vscode-bridge/) and its packaging into a .vsix.

Every VS Code window runs one extension host, and each host listens on
<cache_dir>/vscode/<pid>.sock (JSON lines). A terminal is found by the pid of its shell,
so a live `claude` is matched through its parent chain.

This module is how the Claude provider's `vscode` route reaches the bridge (its `Terminals` protocol:
`shells`, `send`, `focus`): the chat's `Feature.setup` plugs it in when the dashboard starts, so core never
imports this feature.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ... import cache_dir
from ...core.providers.base import SendError

if TYPE_CHECKING:
    from ...app.feature import Feature

TIMEOUT_S = 1.5
SEND_TIMEOUT_S = 5.0  # the extension waits a moment between the paste and Enter
CODE_FALLBACK = "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
EXTENSION_FILES = ("package.json", "extension.js")

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/Content-Types">
  <Default Extension=".json" ContentType="application/json"/>
  <Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension=".vsixmanifest" ContentType="text/xml"/>
</Types>
"""

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}"/>
    <DisplayName>{display}</DisplayName>
    <Description xml:space="preserve">{description}</Description>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
</PackageManifest>
"""


class BridgeError(SendError):
    """The bridge could not do what was asked: a catalog key (chat.bridge.*), worded for whoever reads it
    (the page in its language, the terminal in English)."""


def bridge_dir() -> Path:
    return cache_dir() / "vscode"


def _ask(sock_path: Path, request: dict, timeout: float) -> dict | None:
    """One request/response; None when nothing answers (stale socket, VS Code closed)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock_path))
            s.sendall(json.dumps(request).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        reply = json.loads(data)
    except (OSError, ValueError):
        return None
    return reply if isinstance(reply, dict) else None


def list_bridges() -> list[dict]:
    """Live bridges as [{"socket": Path, "terminals": [{"name", "shellPid"}]}]; dead sockets are skipped."""
    found = []
    for sock in sorted(bridge_dir().glob("*.sock")):
        reply = _ask(sock, {"op": "list"}, TIMEOUT_S)
        if reply and reply.get("ok"):
            found.append({"socket": sock, "terminals": reply.get("terminals") or []})
    return found


def _act(op: str, shell_pid: int, **extra) -> None:
    """Try every bridge until one owns the terminal; raise BridgeError if none does."""
    request = {"op": op, "shellPid": shell_pid, **extra}
    answered = False
    for sock in sorted(bridge_dir().glob("*.sock")):
        reply = _ask(sock, request, SEND_TIMEOUT_S)
        if reply is None:
            continue
        answered = True
        if reply.get("ok"):
            return
    raise BridgeError("chat.bridge.no_terminal" if answered else "chat.bridge.no_answer")


def shells() -> frozenset[int] | None:
    """Shell pids of terminals a live bridge can type into; None when no bridge answers."""
    bridges = list_bridges()
    if not bridges:
        return None
    return frozenset(
        int(t["shellPid"]) for b in bridges for t in b["terminals"] if str(t.get("shellPid")).isdigit()
    )


def send(shell_pid: int, text: str) -> None:
    _act("send", shell_pid, text=text)


def focus(shell_pid: int) -> None:
    """Show the terminal in its window, then bring VS Code itself to the front."""
    _act("focus", shell_pid)
    subprocess.run(["open", "-a", "Visual Studio Code"], capture_output=True, check=False)


def source_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "vscode-bridge"


def build_vsix(out_dir: Path, source: Path | None = None) -> Path:
    """Package the extension into out_dir/sya-bridge-<version>.vsix (a zip with a VSIX manifest)."""
    source = source or source_dir()
    if not all((source / name).is_file() for name in EXTENSION_FILES):
        raise BridgeError("chat.bridge.no_source", path=str(source))
    meta = json.loads((source / "package.json").read_text())
    manifest = MANIFEST.format(
        name=meta["name"],
        version=meta["version"],
        publisher=meta["publisher"],
        display=meta["displayName"],
        description=meta["description"],
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    vsix = out_dir / f"{meta['name']}-{meta['version']}.vsix"
    with zipfile.ZipFile(vsix, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("extension.vsixmanifest", manifest)
        for name in EXTENSION_FILES:
            z.write(source / name, f"extension/{name}")
    return vsix


def find_code() -> str | None:
    return shutil.which("code") or (CODE_FALLBACK if Path(CODE_FALLBACK).is_file() else None)


def install() -> str:
    """Build the .vsix and install it into the user's VS Code. Only ever called from `sya bridge install`."""
    vsix = build_vsix(cache_dir())
    code = find_code()
    if code is None:
        raise BridgeError("chat.bridge.no_code", path=str(vsix))
    subprocess.run([code, "--install-extension", str(vsix), "--force"], check=True)
    return "reload the VS Code windows"


def status_lines() -> list[str]:
    """What `sya bridge status` prints: terminal output, in English."""
    bridges = list_bridges()
    if not bridges:
        return ["no live bridges (VS Code is closed or the extension isn't installed: sya bridge install)"]
    lines = []
    for b in bridges:
        lines.append(f"{b['socket'].name}: terminals: {len(b['terminals'])}")
        lines.extend(f"  shell pid {t.get('shellPid')}  {t.get('name', '')}" for t in b["terminals"])
    return lines


def add_cli(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("bridge", help="the VS Code bridge that lets the dashboard type into terminals")
    p.add_argument("action", choices=["install", "status"])
    p.set_defaults(run=run_cli)


def run_cli(args: argparse.Namespace, features: Sequence[Feature]) -> None:
    try:
        print(install() if args.action == "install" else "\n".join(status_lines()))
    except (BridgeError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"bridge {args.action}: {exc}") from exc
