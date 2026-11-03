"""Catalog of what agents can use: skills, subagents, MCP servers, plugins, hooks, with usage counts.

Route: GET /api/tools. Everything is read from disk on request and cached
for CACHE_TTL_S; usage comes from the per-session `tool_usage` the transcript parser records. The cache
holds the built-in items' notes as catalog keys (`note`); each reply words them in the request's language.

~/.claude.json and MCP configs hold tokens. Nothing from env, headers, URL userinfo/query or secret-looking
arguments leaves this module: only names, transports, hosts and redacted argument lists (see redact_args).
"""

from __future__ import annotations

import json
import math
import re
import shlex
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ...app.replies import Reply, json_reply
from ...core.providers.claude.paths import claude_home, claude_json
from ...i18n import request_lang, t

if TYPE_CHECKING:
    from ...app.server import App

CACHE_TTL_S = 30.0
MAX_PROJECTS = 50
HOOK_COMMAND_LIMIT = 160
DESCRIPTION_LIMIT = 700
MASK = "•••"

BUILTIN_AGENTS = {  # name -> the catalog key of its description
    "general-purpose": "tools.builtin.general_purpose",
    "Explore": "tools.builtin.explore",
    "Plan": "tools.builtin.plan",
    "statusline-setup": "tools.builtin.statusline_setup",
    "claude-code-guide": "tools.builtin.claude_code_guide",
}
BUILTIN_NOTE = "tools.builtin.note"
CONNECTOR_NOTE = "tools.builtin.connector"


# --- small parsers ---------------------------------------------------------------------------


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


BLOCK_HEADER = re.compile(r"[|>][+-]?\d*")
KEY_LINE = re.compile(r"^([A-Za-z_][\w-]*)\s*:(?:\s+(.*)|$)")


def parse_frontmatter(text: str) -> dict:
    """The `---` block of a markdown file: `key: value`, quoted values, `|`/`>` blocks, `- item` lists."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), len(lines))
    out: dict = {}
    i = 1
    while i < end:
        m = KEY_LINE.match(lines[i])
        i += 1
        if not m:
            continue
        key, value = m.group(1), (m.group(2) or "").strip()
        indented = []
        while i < end and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
            indented.append(lines[i])
            i += 1
        body = [ln.strip() for ln in indented]
        if BLOCK_HEADER.fullmatch(value):
            joined = (" " if value[0] == ">" else "\n").join(ln for ln in body if ln)
            out[key] = joined.strip()
        elif not value and any(ln.startswith("- ") for ln in body):
            out[key] = [_unquote(ln[2:]) for ln in body if ln.startswith("- ")]
        else:
            out[key] = " ".join([_unquote(value), *[ln for ln in body if ln]]).strip()
    return out


def _words(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [w.strip().strip("'\"") for w in str(value or "").strip("[]").split(",") if w.strip()]


def _description(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= DESCRIPTION_LIMIT else text[: DESCRIPTION_LIMIT - 1] + "…"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --- redaction -------------------------------------------------------------------------------

SECRET_NAME = re.compile(r"key|token|secret|passw|passwd|auth|credential|cookie|bearer|signature", re.I)
SECRET_PREFIX = re.compile(
    r"^(sk-|sk_|pk_|ghp_|gho_|ghs_|github_pat_|glpat-|xox[abprs]-|AKIA|ASIA|eyJ|ya29\.|AIza)"
)
SCHEME_VALUE = re.compile(r"\b(Bearer|Basic|Token)\s+\S+", re.I)
URL_IN_TEXT = re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.I)
TOKENISH = re.compile(r"[A-Za-z0-9_\-+=/.]{20,}")


def _entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def looks_secret(value: str) -> bool:
    """A long opaque string: known token prefix, or high-entropy mix of letters and digits."""
    if SECRET_PREFIX.match(value):
        return True
    if not TOKENISH.fullmatch(value) or value.startswith(("/", "~", ".", "@")) or value.count("/") > 1:
        return False
    mixed = any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
    return mixed and _entropy(value) > 3.4


def safe_url(url: str) -> str:
    """scheme://host[:port]/path with no userinfo, query or fragment; opaque path segments masked."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return MASK
    if not parts.scheme or not host:
        return MASK
    path = "/".join(MASK if looks_secret(seg) else seg for seg in parts.path.split("/"))
    return f"{parts.scheme}://{host}{port}{path}"


def redact_value(value: str) -> str:
    """One argument value: URLs reduced, auth schemes and opaque tokens masked."""
    if looks_secret(value):
        return MASK
    value = URL_IN_TEXT.sub(lambda m: safe_url(m.group(0)), value)
    value = SCHEME_VALUE.sub(lambda m: f"{m.group(1)} {MASK}", value)
    return value


def redact_args(args: list[str]) -> list[str]:
    """Replace values of flags/assignments named like key, token, secret, password with •••."""
    out: list[str] = []
    mask_next = False
    for arg in map(str, args):
        if mask_next and not arg.startswith("-"):
            out.append(MASK)
            mask_next = False
            continue
        mask_next = False
        name, eq, value = arg.partition("=")
        if eq and SECRET_NAME.search(name) and (name.startswith("-") or name.replace("_", "").isalnum()):
            out.append(f"{name}={MASK}")
        elif arg.startswith("-") and not eq and SECRET_NAME.search(arg):
            out.append(arg)
            mask_next = True
        elif eq and re.fullmatch(r"\w+", name):
            out.append(f"{name}={redact_value(value)}")
        else:
            out.append(redact_value(arg))
    return out


def redact_command(command: str, limit: int = HOOK_COMMAND_LIMIT) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    text = " ".join(redact_args(tokens))
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- catalog sources -------------------------------------------------------------------------


def _item(kind: str, name: str, source: str, description: str = "", **extra) -> dict:
    owner = extra.get("plugin") or extra.get("project") or ""
    usage_keys = extra.pop("usage_keys", [])
    return {
        "id": f"{kind}:{source}:{owner}:{name}",
        "name": name,
        "kind": kind,
        "source": source,
        "description": _description(description),
        **extra,
        "_keys": usage_keys,
    }


def _markdown_items(kind: str, files: list[Path], source: str, usage_prefix: str, **owner) -> list[dict]:
    items = []
    for path in files:
        meta = parse_frontmatter(_read_text(path))
        name = str(meta.get("name") or (path.parent.name if path.name == "SKILL.md" else path.stem))
        extra = {"path": str(path), **owner}
        if kind == "agent":
            extra.update(model=str(meta.get("model") or ""), tools=_words(meta.get("tools")))
        if kind == "command" and meta.get("argument-hint"):
            extra["argument_hint"] = str(meta["argument-hint"])
        plugin = owner.get("plugin")
        keys = [f"{usage_prefix}:{name}"] + ([f"{usage_prefix}:{plugin}:{name}"] if plugin else [])
        items.append(_item(kind, name, source, meta.get("description") or "", usage_keys=keys, **extra))
    return items


def _skill_files(root: Path) -> list[Path]:
    return sorted(root.glob("*/SKILL.md"))


def _md_files(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("*.md") if p.is_file())


def _dir_items(base: Path, source: str, **owner) -> dict[str, list[dict]]:
    """skills, agents, commands under one `.claude`-like directory (user home, project, plugin root)."""
    return {
        "skills": _markdown_items("skill", _skill_files(base / "skills"), source, "skill", **owner),
        "agents": _markdown_items("agent", _md_files(base / "agents"), source, "agent", **owner),
        "commands": _markdown_items("command", _md_files(base / "commands"), source, "command", **owner),
    }


def _mcp_item(
    name: str, cfg: object, source: str, path: Path, usage_name: str | None = None, **owner
) -> dict | None:
    if not isinstance(cfg, dict):
        return None
    command = str(cfg.get("command") or "")
    args = [str(a) for a in cfg.get("args") or []]
    if command:  # some configs put the whole command line into `command`
        try:
            head, *rest = shlex.split(command)
        except ValueError:
            head, rest = command, []
        command, args = Path(head).name, rest + args
    url = str(cfg.get("url") or "")
    transport = str(cfg.get("type") or cfg.get("transport") or ("http" if url and not command else "stdio"))
    safe = safe_url(url) if url else ""
    env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    headers = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else {}
    usage_name = usage_name or name
    return _item(
        "mcp",
        name,
        source,
        "",
        path=str(path),
        transport=transport,
        command=command,
        args=redact_args(args),
        url=safe,
        host=urlsplit(safe).netloc if safe and safe != MASK else "",
        env_names=sorted(map(str, env)),
        header_names=sorted(map(str, headers)),
        usage_keys=[f"mcp:{usage_name}"],
        mcp_name=usage_name,
        **owner,
    )


def _mcp_servers(data: dict) -> dict:
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def _plugin_mcp(root: Path, plugin: str) -> list[dict]:
    configs: dict = {}
    mcp_json = _read_json(root / ".mcp.json")
    configs.update(_mcp_servers(mcp_json) or {k: v for k, v in mcp_json.items() if isinstance(v, dict)})
    for rel in ("plugin.json", ".claude-plugin/plugin.json"):
        configs.update(_mcp_servers(_read_json(root / rel)))
    path = root / ".mcp.json"
    return [
        it
        for name, cfg in configs.items()
        if (it := _mcp_item(name, cfg, "plugin", path, f"plugin_{plugin}_{name}", plugin=plugin))
    ]


def _hook_items(data: dict, source: str, path: Path, **owner) -> list[dict]:
    hooks = data.get("hooks")
    items = []
    for event, groups in (hooks if isinstance(hooks, dict) else {}).items():
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            matcher = str(group.get("matcher") or "")
            for hook in group.get("hooks") or []:
                if not isinstance(hook, dict):
                    continue
                command = redact_command(str(hook.get("command") or hook.get("prompt") or ""))
                item = _item(
                    "hook", f"{event} {matcher}".strip(), source, "", path=str(path), event=str(event),
                    matcher=matcher, command=command, hook_type=str(hook.get("type") or "command"), **owner,
                )  # fmt: skip
                item["id"] += f":{len(items)}"
                items.append(item)
    return items


def _plugins(home: Path, enabled: dict) -> list[tuple[dict, Path]]:
    raw = _read_json(home / "plugins" / "installed_plugins.json").get("plugins")
    found = []
    for key, entry in (raw if isinstance(raw, dict) else {}).items():
        installs = entry if isinstance(entry, list) else [entry]
        install = next((i for i in installs if isinstance(i, dict) and i.get("scope") == "user"), None)
        install = install or next((i for i in installs if isinstance(i, dict)), None)
        if not install or not install.get("installPath"):
            continue
        name, _, marketplace = str(key).partition("@")
        root = Path(install["installPath"])
        meta = _read_json(root / ".claude-plugin" / "plugin.json") or _read_json(root / "plugin.json")
        info = {
            "id": f"plugin:plugin::{key}",
            "name": name,
            "kind": "plugin",
            "source": "plugin",
            "plugin": name,
            "marketplace": marketplace,
            "version": str(install.get("version") or meta.get("version") or ""),
            "enabled": bool(enabled.get(key)),
            "description": _description(meta.get("description")),
            "path": str(root),
            "scope": str(install.get("scope") or ""),
            "_keys": [],
        }
        found.append((info, root))
    return found


def scan_disk(home: Path, claude_json: Path, projects: list[Path]) -> dict[str, list[dict]]:
    """Every catalog entry that exists on disk, without usage."""
    out: dict[str, list[dict]] = {k: [] for k in ("skills", "agents", "commands", "mcp", "plugins", "hooks")}

    def add(found: dict[str, list[dict]]) -> None:
        for kind, items in found.items():
            out[kind].extend(items)

    settings = _read_json(home / "settings.json")
    add(_dir_items(home, "user"))
    for name in ("settings.json", "settings.local.json"):
        out["hooks"] += _hook_items(_read_json(home / name), "user", home / name)

    enabled = settings.get("enabledPlugins") if isinstance(settings.get("enabledPlugins"), dict) else {}
    for info, root in _plugins(home, enabled):
        found = _dir_items(root, "plugin", plugin=info["name"])
        found["mcp"] = _plugin_mcp(root, info["name"])
        hooks_file = root / "hooks" / "hooks.json"
        found["hooks"] = _hook_items(_read_json(hooks_file), "plugin", hooks_file, plugin=info["name"])
        for items in found.values():
            for it in items:
                it["enabled"] = info["enabled"]
        info["counts"] = {k: len(found[k]) for k in ("skills", "agents", "commands", "mcp")}
        add(found)
        out["plugins"].append(info)

    config = _read_json(claude_json)
    for name, cfg in _mcp_servers(config).items():
        if it := _mcp_item(name, cfg, "user", claude_json):
            out["mcp"].append(it)
    config_projects = config.get("projects") if isinstance(config.get("projects"), dict) else {}
    for project, pcfg in config_projects.items():
        for name, cfg in _mcp_servers(pcfg if isinstance(pcfg, dict) else {}).items():
            if it := _mcp_item(
                name, cfg, "local", claude_json, project=Path(project).name, project_path=project
            ):
                out["mcp"].append(it)

    for project in projects:
        if project / ".claude" == home:
            continue
        owner = {"project": project.name, "project_path": str(project)}
        add(_dir_items(project / ".claude", "project", **owner))
        mcp_file = project / ".mcp.json"
        for name, cfg in _mcp_servers(_read_json(mcp_file)).items():
            if it := _mcp_item(name, cfg, "project", mcp_file, **owner):
                out["mcp"].append(it)
        settings_file = project / ".claude" / "settings.json"
        out["hooks"] += _hook_items(_read_json(settings_file), "project", settings_file, **owner)

    for kind in ("skills", "agents", "commands", "mcp", "hooks"):
        out[kind] = _dedupe_projects(out[kind])
    return out


def _dedupe_projects(items: list[dict]) -> list[dict]:
    """The same project file seen from several checkouts/worktrees is one entry with a count."""
    seen: dict[tuple, dict] = {}
    out = []
    for it in items:
        if it["source"] not in ("project", "local"):
            out.append(it)
            continue
        sig = (
            it["kind"],
            it["source"],
            it["name"],
            it["description"],
            it.get("command"),
            it.get("url"),
            it.get("matcher"),
        )
        first = seen.get(sig)
        if first is None:
            seen[sig] = it
            out.append(it)
        else:
            first["also_in"] = first.get("also_in", 0) + 1
    return out


def project_dirs(records: list[dict], config_projects: list[str]) -> list[Path]:
    """Existing working directories of recent sessions, newest first, capped."""
    ranked = sorted(records, key=lambda r: r.get("last_activity") or "", reverse=True)
    seen: dict[str, None] = {}
    for rec in ranked:
        for cwd in (rec.get("cwd"), rec.get("project")):
            if cwd:
                seen.setdefault(cwd)
    for cwd in config_projects:
        seen.setdefault(cwd)
    found = []
    for cwd in seen:
        path = Path(cwd)
        if path.is_dir():
            found.append(path)
        if len(found) >= MAX_PROJECTS:
            break
    return found


# --- usage -----------------------------------------------------------------------------------


def collect_usage(records: list[dict]) -> dict[str, dict]:
    """key -> {count, last, sessions: {session_id: [title, count]}} over all sessions."""
    usage: dict[str, dict] = {}
    for rec in records:
        for key, (count, last) in (rec.get("tool_usage") or {}).items():
            entry = usage.setdefault(key, {"count": 0, "last": "", "sessions": {}})
            entry["count"] += count
            entry["last"] = max(entry["last"], last or "")
            entry["sessions"][rec["session_id"]] = [rec.get("title") or rec["session_id"][:8], count]
    return usage


def usage_of(usage: dict[str, dict], keys: list[str]) -> dict:
    count, last, sessions = 0, "", {}
    for key in keys:
        if entry := usage.get(key):
            count += entry["count"]
            last = max(last, entry["last"])
            for sid, (title, n) in entry["sessions"].items():
                sessions[sid] = [title, sessions.get(sid, ["", 0])[1] + n]
    top = sorted(sessions.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
    return {
        "count": count,
        "last": last or None,
        "sessions": [{"session_id": sid, "title": t, "count": n} for sid, (t, n) in top],
    }


def _leftover_items(usage: dict[str, dict], claimed: set[str]) -> dict[str, list[dict]]:
    """Usage with nothing on disk behind it: built-in agents and skills, claude.ai connectors."""
    out: dict[str, list[dict]] = {"skills": [], "agents": [], "commands": [], "mcp": []}
    kinds = {"skill": "skills", "agent": "agents", "command": "commands", "mcp": "mcp"}
    for key in sorted(usage):
        prefix, _, name = key.partition(":")
        if prefix not in kinds or key in claimed:
            continue
        kind, source, note = prefix, "builtin", BUILTIN_NOTE
        if prefix == "agent":
            note = BUILTIN_AGENTS.get(name, BUILTIN_NOTE)
        if prefix == "mcp" and name.startswith("claude_ai_"):
            source, note = "connector", CONNECTOR_NOTE
            name = name.removeprefix("claude_ai_").replace("_", " ")
        extra = {"mcp_name": key[4:]} if prefix == "mcp" else {}
        out[kinds[prefix]].append(_item(kind, name, source, note=note, usage_keys=[key], **extra))
    # Built-in agents are worth listing even when unused, so "never called" is visible too.
    for name, note in BUILTIN_AGENTS.items():
        if f"agent:{name}" not in usage:
            out["agents"].append(_item("agent", name, "builtin", note=note, usage_keys=[f"agent:{name}"]))
    return out


def _mcp_tools(usage: dict[str, dict], server: str) -> list[dict]:
    prefix = f"mcp_tool:{server}/"
    tools = [
        {"name": k.removeprefix(prefix), "count": v["count"]}
        for k, v in usage.items()
        if k.startswith(prefix)
    ]
    return sorted(tools, key=lambda tool: tool["count"], reverse=True)[:8]


def build_catalog(records: list[dict], *, home: Path, claude_json: Path, now: datetime | None = None) -> dict:
    config = _read_json(claude_json)
    config_projects = [p for p in (config.get("projects") or {}) if isinstance(p, str)]
    catalog = scan_disk(home, claude_json, project_dirs(records, config_projects))
    usage = collect_usage(records)
    claimed = {k for items in catalog.values() for it in items for k in it["_keys"]}
    for kind, items in _leftover_items(usage, claimed).items():
        catalog[kind].extend(items)
    for kind, items in catalog.items():
        for it in items:
            keys = it.pop("_keys")
            if kind != "plugins":
                it["usage"] = usage_of(usage, keys) if keys else {"count": 0, "last": None, "sessions": []}
            if kind == "mcp":
                it["tools"] = _mcp_tools(usage, it.get("mcp_name", it["name"]))
            if kind == "plugins":  # a plugin's usage is the sum over what it ships
                own = [
                    x
                    for k in ("skills", "agents", "commands", "mcp")
                    for x in catalog[k]
                    if x.get("plugin") == it["name"]
                ]
                used = [x["usage"] for x in own]
                last = max((u["last"] or "" for u in used), default="")
                it["usage"] = {"count": sum(u["count"] for u in used), "last": last or None, "sessions": []}
        items.sort(key=lambda it: (-(it.get("usage") or {}).get("count", 0), it["name"].lower()))
    return {"generated_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"), **catalog}


_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def cached_catalog(app: App) -> dict:
    key = (str(claude_home()), str(claude_json()))
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
            return hit[1]
    catalog = build_catalog(app.index.records(), home=claude_home(), claude_json=claude_json())
    with _cache_lock:
        _cache[key] = (time.monotonic(), catalog)
    return catalog


def worded(catalog: dict, lang: str) -> dict:
    """The catalog for a reader of `lang`: a built-in item's `note` key becomes its description."""

    def word(item: dict) -> dict:
        if "note" not in item:
            return item
        return {**{k: v for k, v in item.items() if k != "note"}, "description": t(lang, item["note"])}

    return {kind: [word(i) for i in v] if isinstance(v, list) else v for kind, v in catalog.items()}


def handle_get(app: App, path: str, query: dict[str, str]) -> Reply | None:
    if path != "/api/tools":
        return None
    return json_reply(worded(cached_catalog(app), request_lang()))
