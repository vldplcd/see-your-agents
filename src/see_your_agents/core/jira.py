"""Jira ticket keys and the Jira address in free text: prompts, titles, branch names, transcripts."""

from __future__ import annotations

import os
import re

JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
BROWSE_URL = re.compile(r"(https?://[\w.\-]+(?:/[\w.\-]+)*?)/browse/[A-Z][A-Z0-9]+-\d+")
NOT_TICKETS = {
    "UTF",
    "SHA",
    "ISO",
    "RFC",
    "HTTP",
    "TLS",
    "SSL",
    "CVE",
    "AES",
    "RSA",
    "MD",
    "UTC",
    "GMT",
    "PEP",
    "KOI",
    "CP",
    "WIN",
    "IEEE",
    "GPT",
    "PR",
    "MR",
    "COVID",
    "ARM",
    "X86",
    "AMD",
    "S3",
    "RELEASE",
    "HOTFIX",
    "FEATURE",
    "FEAT",
    "FIX",
    "BUGFIX",
    "CHORE",
    "BUMP",
    "V",
}
# Optional allowlist of Jira project keys, e.g. SYA_JIRA_PROJECTS=PROJ,OPS
JIRA_PROJECTS = {k.strip() for k in os.environ.get("SYA_JIRA_PROJECTS", "").split(",") if k.strip()}


def jira_keys_in(text: str, *, any_case: bool = False) -> list[str]:
    """Ticket-looking keys. Branch and worktree names are lowercase (proj-123-fixes): pass any_case."""
    found = JIRA_KEY.findall(text.upper() if any_case else text)
    prefixes = (lambda p: p in JIRA_PROJECTS) if JIRA_PROJECTS else (lambda p: p not in NOT_TICKETS)
    return [k for k in found if prefixes(k.split("-")[0])]
