"""sya: see your agents.

sya serve   local dashboard (board + office) at http://127.0.0.1:8765; a watchdog restarts it on request
sya ls      the same board as a terminal table
sya index   refresh the DuckDB index once
sya supervisor  [install] the supervisor session that watches the others
sya bridge  install | status of the VS Code bridge (send text to live sessions)
sya mcp     install | status of the MCP server that lets Claude Code sessions read the fleet

`bridge` and `supervisor` come from their features (Feature.add_cli); the rest is the host's.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

from ..core import providers
from ..core.index import Index, default_db_path
from ..core.sessions import COLUMNS, build_board
from ..features import FEATURES
from . import mcp, pairing, watchdog


def _ago(ts: str) -> str:
    try:
        delta = datetime.now(UTC) - datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h" if minutes < 48 * 60 else f"{minutes // 1440}d"


def cmd_ls(index: Index, show_all: bool) -> None:
    index.refresh()
    board = build_board(index.records(), providers.live())
    shown = {cid for cid, _ in COLUMNS} if show_all else {"needs_you", "working", "your_move", "recent"}
    for cid, title in ((col["id"], col["title"]) for col in board["columns"]):  # worded: English here
        cards = [c for c in board["cards"] if c["column"] == cid]
        if cid not in shown or not cards:
            continue
        print(f"\n{title} ({len(cards)})")
        for c in cards:
            jira = ",".join(j["key"] for j in c["jira"][:2])
            tasks = f"{c['tasks_done']}/{c['tasks_total']}" if c["tasks_total"] else ""
            wait = (c["live"] or {}).get("waiting_for") or ""
            print(
                f"  {_ago(c['last_activity']):>4}  {c['provider'][:6]:<6} {c['project'][:18]:<18} {jira:<10} "
                f"{tasks:>5}  {c['title'][:60]}{'  ⟵ ' + wait if wait else ''}"
            )


def _mcp_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp"


def _mask(token: str) -> str:
    return token[:4] + "…"


def cmd_mcp_install(port: int, dry_run: bool) -> None:
    token = mcp.tokens()["read"]
    add = [
        "claude", "mcp", "add", "--transport", "http", "-s", "user", "sya", _mcp_url(port),
        "-H", f"Authorization: Bearer {token}",
    ]  # fmt: skip
    remove = ["claude", "mcp", "remove", "-s", "user", "sya"]
    try:
        exists = subprocess.run(["claude", "mcp", "get", "sya"], capture_output=True).returncode == 0
        commands = ([remove] if exists else []) + [add]
        for cmd in commands:
            if dry_run:
                print(" ".join(cmd).replace(token, _mask(token)))
            else:
                subprocess.run(cmd, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"mcp install: {str(exc).replace(token, _mask(token))}") from exc
    if not dry_run:
        print(f"Added the sya MCP server to Claude Code (user scope): {_mcp_url(port)}")


def cmd_mcp_status(port: int) -> None:
    token = mcp.tokens()["read"]

    def rpc(rid: int, method: str, params: dict) -> dict:
        req = urllib.request.Request(
            _mcp_url(port),
            data=json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp)

    try:
        rpc(1, "initialize", {"protocolVersion": "2025-06-18"})
        listing = rpc(2, "tools/list", {})
    except (OSError, ValueError) as exc:  # URLError and HTTPError are OSErrors
        code = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else exc
        raise SystemExit(
            f"mcp status: {_mcp_url(port)} is unreachable ({code}); is `sya serve` running?"
        ) from exc
    print("\n".join(t["name"] for t in listing["result"]["tools"]))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="sya", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--db", type=Path, default=default_db_path(), help="DuckDB index file")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve", help="run the local dashboard")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--no-open", action="store_true", help="do not open a browser tab")
    p_serve.add_argument("--child", action="store_true", help=argparse.SUPPRESS)  # the watchdog's child
    p_ls = sub.add_parser("ls", help="print the board")
    p_ls.add_argument("--all", action="store_true", help="include the archive")
    sub.add_parser("index", help="refresh the index once")
    for feature in FEATURES:
        if feature.add_cli:
            feature.add_cli(sub)
    p_mcp = sub.add_parser("mcp", help="the MCP server inside `sya serve` (POST /mcp)")
    p_mcp.add_argument("action", choices=["install", "status"])
    p_mcp.add_argument("--port", type=int, default=8765)
    p_mcp.add_argument("--dry-run", action="store_true", help="install: print the commands, token masked")
    args = parser.parse_args(argv)

    if run := getattr(args, "run", None):  # a feature's subcommand
        return run(args, FEATURES)
    if args.cmd == "mcp":
        return (
            cmd_mcp_install(args.port, args.dry_run)
            if args.action == "install"
            else cmd_mcp_status(args.port)
        )
    if args.cmd == "serve" and not args.child:  # before the index: the parent must not hold the DuckDB file
        return watchdog.exec_parent(args.port, sys.argv[1:] if argv is None else argv)
    index = Index(args.db)
    if args.cmd == "index":
        t0 = time.monotonic()
        n = index.refresh()
        took = time.monotonic() - t0
        print(f"re-parsed {n} transcripts in {took:.1f}s; {len(index.records())} sessions indexed")
    elif args.cmd == "ls":
        cmd_ls(index, args.all)
    else:
        from .server import serve

        watchdog.stop_on_signals()  # SIGTERM and SIGHUP too must reach atexit: it stops the preview
        httpd = serve(index, args.port, FEATURES)
        url = f"http://127.0.0.1:{args.port}/"
        print(f"see-your-agents on {url}  (Ctrl+C to stop)")
        if not args.no_open and pairing.supported():
            httpd.app.pairing.start()  # each process starts unpaired: it opens the page with a pairing code
        elif not args.no_open:
            webbrowser.open(url)
        watchdog.exit_with_parent()
        with contextlib.suppress(KeyboardInterrupt):
            httpd.serve_forever()
        httpd.server_close()
        if httpd.app.restart_requested:
            raise SystemExit(watchdog.RESTART)


if __name__ == "__main__":  # the watchdog's child: `python -m see_your_agents.app.cli serve --child`
    main()
