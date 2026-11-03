import json
from pathlib import Path

from see_your_agents.features.tools.toolbox import (
    build_catalog,
    parse_frontmatter,
    redact_args,
    redact_command,
    safe_url,
    worded,
)

SECRETS = [
    "sk-ant-api03-Zx9Qw8Er7Ty6Ui5Op4As3Df2",
    "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345",
    "hunter2-pass-value",
    "s3cr3tHeaderValue",
    "userinfo-password",
    "q=querysecret99",
    "eyJhbGciOiJIUzI1NiJ9.payload.sig",
    "9f8e7d6c5b4a39281706f5e4d3c2b1a0",
    "env-secret-value",
]


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def make_home(tmp_path: Path) -> tuple[Path, Path, Path]:
    home, project = tmp_path / "claude", tmp_path / "proj"
    write(
        home / "skills" / "lint" / "SKILL.md",
        "---\nname: lint-fix\ndescription: >\n  Fix lint\n  errors fast.\n---\nbody",
    )
    write(
        home / "agents" / "critic.md",
        '---\nname: critic\ndescription: "Reviews: designs"\nmodel: sonnet\ntools: Read, Grep\n---\n',
    )
    write(home / "commands" / "ship.md", "---\ndescription: Ship it\n---\n")
    plugin = home / "plugins" / "cache" / "mk" / "demo" / "1.0.0"
    write(
        plugin / ".claude-plugin" / "plugin.json", json.dumps({"name": "demo", "description": "Demo plugin"})
    )
    write(plugin / "skills" / "plan" / "SKILL.md", "---\nname: plan\ndescription: Plan things\n---\n")
    write(plugin / "agents" / "scout.md", "---\ndescription: Scouts\n---\n")
    write(
        plugin / ".mcp.json",
        json.dumps({"mcpServers": {"docs": {"type": "http", "url": "https://docs.example.com/mcp"}}}),
    )
    write(
        home / "plugins" / "installed_plugins.json",
        json.dumps(
            {
                "version": 2,
                "plugins": {"demo@mk": [{"scope": "user", "installPath": str(plugin), "version": "1.0.0"}]},
            }
        ),
    )
    write(
        home / "settings.json",
        json.dumps(
            {
                "enabledPlugins": {"demo@mk": True},
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {"type": "command", "command": f"check.sh --api-key {SECRETS[2]} --verbose"}
                            ],
                        }
                    ]
                },
            }
        ),
    )
    write(
        project / ".claude" / "skills" / "local-skill" / "SKILL.md",
        "---\nname: local-skill\ndescription: Project only\n---\n",
    )
    write(
        project / ".mcp.json",
        json.dumps(
            {
                "mcpServers": {
                    "pg": {"command": "/usr/bin/npx", "args": ["-y", "pg-mcp", f"--password={SECRETS[2]}"]}
                }
            }
        ),
    )
    claude_json = write(
        tmp_path / ".claude.json",
        json.dumps(
            {
                "mcpServers": {
                    "tracker": {
                        "type": "stdio",
                        "command": "/opt/bin/uvx",
                        "args": [
                            "tracker-mcp",
                            "--token",
                            SECRETS[0],
                            "--base-url",
                            f"https://u:{SECRETS[4]}@t.example.com/api?{SECRETS[5]}",
                            SECRETS[7],
                        ],
                        "env": {"TRACKER_TOKEN": SECRETS[8], "API_KEY": SECRETS[1]},
                    },
                    "hosted": {
                        "type": "http",
                        "url": f"https://user:{SECRETS[4]}@mcp.example.com:8443/v1?{SECRETS[5]}#frag",
                        "headers": {"Authorization": f"Bearer {SECRETS[6]}", "X-Key": SECRETS[3]},
                    },
                },
                "projects": {
                    str(project): {
                        "mcpServers": {
                            "scratch": {
                                "command": "node",
                                "args": ["srv.js", f"Bearer {SECRETS[0]}"],
                                "env": {"X": SECRETS[8]},
                            }
                        }
                    }
                },
            }
        ),
    )
    return home, claude_json, project


def record(sid, title, cwd, usage, last="2026-10-30T10:00:00Z"):
    return {"session_id": sid, "title": title, "cwd": cwd, "last_activity": last, "tool_usage": usage}


def by_name(items):
    return {it["name"]: it for it in items}


def test_frontmatter_scalars_blocks_and_lists():
    text = (
        "---\nname: x\ndescription: |\n  line one\n  line two\nother: 'it''s'\n"
        "tools:\n  - Read\n  - Grep\n---\nbody: not me"
    )
    meta = parse_frontmatter(text)
    assert meta == {
        "name": "x",
        "description": "line one\nline two",
        "other": "it's",
        "tools": ["Read", "Grep"],
    }
    assert parse_frontmatter("no frontmatter") == {}


def test_catalog_attributes_plugin_items_and_counts_contents(tmp_path):
    home, claude_json, project = make_home(tmp_path)
    cat = build_catalog([record("s1", "Work", str(project), {})], home=home, claude_json=claude_json)

    skills = by_name(cat["skills"])
    assert (
        skills["lint-fix"]["description"] == "Fix lint errors fast."
        and skills["lint-fix"]["source"] == "user"
    )
    assert skills["plan"]["plugin"] == "demo" and skills["plan"]["source"] == "plugin"
    assert skills["local-skill"]["source"] == "project" and skills["local-skill"]["project"] == "proj"
    critic = by_name(cat["agents"])["critic"]
    assert (critic["model"], critic["tools"]) == ("sonnet", ["Read", "Grep"])
    assert by_name(cat["agents"])["scout"]["plugin"] == "demo"
    plugin = cat["plugins"][0]
    assert (plugin["name"], plugin["marketplace"], plugin["enabled"], plugin["version"]) == (
        "demo",
        "mk",
        True,
        "1.0.0",
    )
    assert plugin["counts"] == {"skills": 1, "agents": 1, "commands": 0, "mcp": 1}
    mcp = {(m["source"], m["name"]) for m in cat["mcp"]}
    assert {
        ("user", "tracker"),
        ("user", "hosted"),
        ("local", "scratch"),
        ("project", "pg"),
        ("plugin", "docs"),
    } <= mcp


def test_usage_is_aggregated_and_unmatched_usage_becomes_builtin_or_connector(tmp_path):
    home, claude_json, project = make_home(tmp_path)
    records = [
        record(
            "a",
            "Alpha",
            str(project),
            {
                "skill:lint-fix": [3, "2026-10-30T10:00:00Z"],
                "skill:demo:plan": [1, "2026-10-29T10:00:00Z"],
                "agent:Explore": [2, "2026-10-30T11:00:00Z"],
                "mcp:tracker": [4, "2026-10-30T12:00:00Z"],
                "mcp_tool:tracker/get": [4, "2026-10-30T12:00:00Z"],
                "mcp:claude_ai_Slack_Team": [1, "2026-10-28T00:00:00Z"],
            },
        ),
        record(
            "b",
            "Beta",
            str(project),
            {"skill:lint-fix": [5, "2026-10-31T10:00:00Z"], "skill:simplify": [2, "2026-10-31T10:00:00Z"]},
        ),
    ]
    cat = build_catalog(records, home=home, claude_json=claude_json)

    lint = by_name(cat["skills"])["lint-fix"]["usage"]
    assert lint["count"] == 8 and lint["last"] == "2026-10-31T10:00:00Z"
    assert [s["session_id"] for s in lint["sessions"]] == ["b", "a"]
    assert by_name(cat["skills"])["plan"]["usage"]["count"] == 1
    assert cat["skills"][0]["name"] == "lint-fix"  # sorted by usage
    simplify = by_name(cat["skills"])["simplify"]
    assert simplify["source"] == "builtin" and simplify["usage"]["count"] == 2
    assert by_name(cat["agents"])["Explore"]["usage"]["count"] == 2
    assert by_name(cat["agents"])["Plan"]["usage"]["count"] == 0  # built-in, never called
    assert by_name(cat["mcp"])["Slack Team"]["source"] == "connector"
    tracker = next(m for m in cat["mcp"] if m["name"] == "tracker")
    assert tracker["usage"]["count"] == 4 and tracker["tools"] == [{"name": "get", "count": 4}]
    assert by_name(cat["mcp"])["hosted"]["usage"]["count"] == 0
    assert cat["plugins"][0]["usage"]["count"] == 1

    # the cache keeps the built-in notes as keys; each reply words them in the reader's language
    en, ru = worded(cat, "en"), worded(cat, "ru")
    slack = by_name(en["mcp"])["Slack Team"]
    assert slack["description"] == "A claude.ai connector (connected in the account, not in a config)."
    assert "note" not in slack and by_name(cat["mcp"])["Slack Team"]["description"] == ""
    assert (
        by_name(ru["agents"])["Plan"]["description"]
        == "Встроенный субагент-архитектор: готовит план реализации."
    )
    assert by_name(ru["skills"])["lint-fix"] == by_name(cat["skills"])["lint-fix"]  # its own text stays


def test_no_secret_reaches_the_api_response(tmp_path):
    home, claude_json, project = make_home(tmp_path)
    cat = build_catalog([record("s1", "Work", str(project), {})], home=home, claude_json=claude_json)
    blob = json.dumps(cat, ensure_ascii=False)

    for secret in SECRETS:
        assert secret not in blob
    tracker = next(m for m in cat["mcp"] if m["name"] == "tracker")
    assert tracker["command"] == "uvx" and tracker["env_names"] == ["API_KEY", "TRACKER_TOKEN"]
    assert tracker["args"][:3] == ["tracker-mcp", "--token", "•••"]
    hosted = next(m for m in cat["mcp"] if m["name"] == "hosted")
    assert hosted["url"] == "https://mcp.example.com:8443/v1" and hosted["transport"] == "http"
    hook = cat["hooks"][0]
    assert (hook["event"], hook["matcher"], hook["command"]) == (
        "PreToolUse",
        "Bash",
        "check.sh --api-key ••• --verbose",
    )


def test_redaction_helpers():
    assert redact_args(["--api-key=abc", "--name", "x", "--secret", "v", "run"]) == [
        "--api-key=•••",
        "--name",
        "x",
        "--secret",
        "•••",
        "run",
    ]
    assert redact_args(["@scope/server-fs", "/Users/me/some/long/path/here", "pg-mcp-server-long-name"]) == [
        "@scope/server-fs",
        "/Users/me/some/long/path/here",
        "pg-mcp-server-long-name",
    ]
    assert redact_args(["DB_PASSWORD=x", "Authorization: Bearer abc123"])[0] == "DB_PASSWORD=•••"
    assert safe_url("https://a:b@h.io/p?x=1") == "https://h.io/p"
    assert safe_url("not a url") == "•••"
    long = redact_command("echo " + "word " * 100)
    assert len(long) == 160 and long.endswith("…")
