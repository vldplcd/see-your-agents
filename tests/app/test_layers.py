"""The import rules of docs/ARCHITECTURE.md.

Core imports neither app nor features; the host imports no feature (the CLI aside: it hands the registry
over); features import each other only through the exceptions listed there.
"""

import ast
from pathlib import Path

import see_your_agents
from see_your_agents.features import FEATURES

PACKAGE = Path(see_your_agents.__file__).parent
# feature -> features it may import, each one documented in docs/ARCHITECTURE.md
FEATURE_EXCEPTIONS = {"supervisor": {"chat"}, "todo": {"chat"}}


def imported(path: Path) -> set[str]:
    """Absolute dotted names of the see_your_agents modules a file imports, type-checking imports included."""
    package = path.relative_to(PACKAGE.parent).parts[:-1]  # a relative import counts from the file's package
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            base = ".".join(package[: len(package) - node.level + 1]) if node.level else ""
            module = ".".join(p for p in (base, node.module or "") if p)
            names = [f"{module}.{a.name}" for a in node.names] if node.level and not node.module else [module]
            found.update(names)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
    return {name for name in found if name.startswith("see_your_agents")}


def sources(folder: str) -> list[Path]:
    return sorted((PACKAGE / folder).rglob("*.py"))


def test_core_imports_neither_the_host_nor_features():
    for path in sources("core"):
        bad = {m for m in imported(path) if m.startswith(("see_your_agents.app", "see_your_agents.features"))}
        assert not bad, f"{path.name} imports {bad}"


def test_the_host_imports_no_feature_except_the_cli():
    for path in sources("app"):
        if path.name == "cli.py":
            continue
        bad = {m for m in imported(path) if m.startswith("see_your_agents.features")}
        assert not bad, f"{path.name} imports {bad}"


def test_features_import_each_other_only_through_documented_exceptions():
    for folder in sorted(p for p in (PACKAGE / "features").iterdir() if (p / "__init__.py").is_file()):
        allowed = {folder.name, *FEATURE_EXCEPTIONS.get(folder.name, ())}
        for path in sorted(folder.rglob("*.py")):
            others = {
                m.split(".")[2]
                for m in imported(path)
                if m.startswith("see_your_agents.features.") and m.split(".")[2] not in allowed
            }
            assert not others, f"features/{folder.name}/{path.name} imports features {others}"


# ---- the POST pairing census -------------------------------------------------------------------
# Every mutating POST needs the paired browser; the host (server.do_POST) enforces this for everything but
# an explicit open allowlist. These two sets pin that allowlist: a new feature with a POST handler, or a new
# `open_posts` entry, changes a set here and fails the test until someone classifies the route. A route left
# off `OPEN_POSTS` is paired by default — the safe direction.
FEATURES_WITH_POST = {"approvals", "awake", "builder", "chat", "groups", "launch", "looks", "todo"}
# Paths that do NOT need the paired browser, guarded another way. The host also opens /api/pair* (bootstrap).
OPEN_POSTS = {
    "/api/approvals/hello",  # the hook says hello, signed with the hook token
    "/api/approvals/ask",  # the hook registers a prompt, signed
    r"/api/approvals/[A-Za-z0-9_-]{16,64}/wait",  # the hook's long poll, signed
    r"/api/approvals/[A-Za-z0-9_-]{16,64}/withdraw",  # the hook leaves, signed
}


def test_every_mutating_post_needs_the_paired_browser_or_is_explicitly_open():
    with_post = {f.id for f in FEATURES if f.handle_post}
    assert with_post == FEATURES_WITH_POST, (
        "a feature's POST handler changed; a new one is paired by default — add it here, and to OPEN_POSTS "
        "only if it is guarded another way (like the approvals hook's signed routes)"
    )
    opened = {p for f in FEATURES for p in f.open_posts}
    assert opened == OPEN_POSTS, "open_posts changed: an unpaired POST must be justified here"


# The only files outside core/providers that may name the Claude package, each one documented in
# docs/ARCHITECTURE.md and docs/providers.md.
CLAUDE_BY_DESIGN = {
    "features/chat/__init__.py",  # the chat's setup wires the bridge and the host into Claude's routes
    "features/supervisor/launch.py",  # the supervisor is a Claude Code session; start_agent starts one
    "features/builder/agent.py",  # so is the builder
    "features/tools/toolbox.py",  # Tools is a catalog of Claude Code's own configuration
    "features/approvals/install.py",  # `sya hooks install` writes the hook into Claude Code's settings
}


def test_a_provider_package_is_named_only_by_the_registry_and_documented_files():
    """Everything else asks the session's provider or the registry: a new provider needs no if-branches."""
    for path in sources("app") + sources("core") + sources("features"):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel.startswith("core/providers/") or rel in CLAUDE_BY_DESIGN:
            continue
        named = {m for m in imported(path) if m.startswith("see_your_agents.core.providers.claude")}
        assert not named, f"{rel} imports {named}"
