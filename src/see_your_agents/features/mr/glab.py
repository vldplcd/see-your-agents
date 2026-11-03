"""Read-only GitLab calls through `glab api` (GET only: no fields, no method, no body) and their parsing.

One MR costs one call when it is merged or closed, two when it is open (its approvals), three when it also
has comments (its discussions, for the unresolved threads). The pipeline comes with the MR itself.

A failure is kept as words to come: a catalog key and its params (`GlabError.spec()`), which the card puts
into the reader's language (`words`) when the board is built; the cache file keeps them the same way.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

from ...core.env import clean_env
from ...i18n import Problem, t

TIMEOUT_S = 8.0
# https://<host>/<group>/<project>/-/merge_requests/<iid>; older GitLab links have no "/-"
MR_URL = re.compile(
    r"https?://([A-Za-z0-9][A-Za-z0-9.-]*(?::\d{1,5})?)/([\w.-]+(?:/[\w.-]+)*?)(?:/-)?/merge_requests/(\d{1,9})"
    r"(?:[/?#].*)?"
)
TERMINAL = {"merged", "closed"}
MAX_CALLS = 3  # per MR: the MR, its approvals, its discussions

Run = Callable[[list[str]], tuple[int, str, str]]


@dataclass(frozen=True)
class Ref:
    """One MR: the host glab talks to, the project path and the MR number."""

    host: str
    project: str
    iid: int

    @property
    def key(self) -> str:
        return f"{self.host}/{self.project}!{self.iid}"

    def argv(self, tail: str = "") -> list[str]:
        endpoint = f"projects/{quote(self.project, safe='')}/merge_requests/{self.iid}{tail}"
        return ["glab", "api", "--hostname", self.host, endpoint]


def ref_of(url: object) -> Ref | None:
    """The MR a chip links to; None for a GitHub PR or anything else."""
    m = MR_URL.fullmatch(url) if isinstance(url, str) else None
    return Ref(m[1].lower(), m[2], int(m[3])) if m else None


class GlabError(Problem):
    """A failed call, as a catalog key and params. `host`: every call to that host fails the same way for now
    (network, timeout); `login`: glab has no valid token for it."""

    def __init__(self, key: str, /, host: bool = False, login: bool = False, **params: object):
        super().__init__(key, **params)
        self.host = host or login
        self.login = login

    def spec(self) -> dict:
        """The error as the state and the cache keep it: worded later, in the reader's language."""
        return {"key": self.key, "params": self.params}


def words(spec: object, lang: str) -> str:
    """A kept error in `lang`: a {key, params} spec (a param may be one too), a list of them joined by "; ",
    or text as an older cache kept it; "" for none."""
    if isinstance(spec, list):
        return "; ".join(words(s, lang) for s in spec)
    if isinstance(spec, dict) and isinstance(spec.get("key"), str):
        params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
        worded = {k: words(v, lang) if isinstance(v, dict | list) else v for k, v in params.items()}
        return t(lang, spec["key"], **worded)
    return spec if isinstance(spec, str) else ""


def run(argv: list[str]) -> tuple[int, str, str]:
    """glab with a short timeout, no terminal and no repository around it (--hostname decides the host)."""
    p = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        stdin=subprocess.DEVNULL,
        cwd="/",
        env=clean_env(NO_COLOR="1"),
    )
    return p.returncode, p.stdout, p.stderr


def call(run_: Run, ref: Ref, tail: str = "") -> object:
    """One GET, its JSON; GlabError with a reason the drawer can show."""
    try:
        code, out, err = run_(ref.argv(tail))
    except subprocess.TimeoutExpired:
        raise GlabError("mr.err.timeout", host=True, seconds=f"{TIMEOUT_S:g}") from None
    except OSError as exc:
        raise GlabError("mr.err.no_start", host=True, reason=str(exc.strerror or exc)) from None
    if code != 0:
        raise _failure(ref, err)
    try:
        return json.loads(out)
    except ValueError:
        raise GlabError("mr.err.not_json") from None


def _failure(ref: Ref, err: str) -> GlabError:
    if "HTTP 401" in err:
        return GlabError("mr.err.login", login=True, server=ref.host)
    if "HTTP 403" in err:
        return GlabError("mr.err.forbidden")
    if "HTTP 404" in err:
        return GlabError("mr.err.not_found")
    line = next((x.strip() for x in err.splitlines() if x.strip() and x.strip() != "ERROR"), "")
    # no HTTP status at all: the request never got an answer (DNS, TLS, network)
    host = "HTTP " not in err
    return (
        GlabError("mr.err.glab", host=host, text=line[:160])
        if line
        else GlabError("mr.err.failed", host=host)
    )


def parse_mr(data: object) -> dict:
    d = data if isinstance(data, dict) else {}
    pipe = d.get("head_pipeline")
    return {
        "state": str(d.get("state") or ""),
        "draft": bool(d.get("draft") or d.get("work_in_progress")),
        "title": str(d.get("title") or "")[:200],
        "pipeline": str(pipe.get("status") or "") if isinstance(pipe, dict) else "",
        "notes": _int(d.get("user_notes_count")),
    }


def parse_approvals(data: object) -> dict:
    d = data if isinstance(data, dict) else {}
    return {
        "approved": bool(d.get("approved")),
        "approvals_left": _int(d.get("approvals_left")),
        "approvals_required": _int(d.get("approvals_required")),
    }


def unresolved(discussions: object) -> int:
    """Threads with a resolvable note still open; plain comments and system notes are not threads."""
    if not isinstance(discussions, list):
        return 0
    return sum(
        any(
            isinstance(n, dict) and n.get("resolvable") and not n.get("resolved")
            for n in d.get("notes") or []
        )
        for d in discussions
        if isinstance(d, dict)
    )


def fetch(run_: Run, ref: Ref) -> tuple[dict, int]:
    """The MR as a card shows it, and how many calls that took. Only the first call failing raises: a
    failed approvals or discussions call leaves its fields None and says why in `error`, a list of specs."""
    mr = parse_mr(call(run_, ref))
    state = {**mr, "approved": None, "approvals_left": None, "approvals_required": None, "unresolved": None}
    calls, errors = 1, []
    if mr["state"] != "opened":
        return {**state, "error": []}, calls
    try:
        calls += 1
        state.update(parse_approvals(call(run_, ref, "/approvals")))
    except GlabError as exc:
        errors.append({"key": "mr.err.approvals", "params": {"reason": exc.spec()}})
    if mr["notes"] == 0:
        state["unresolved"] = 0
    else:
        try:
            calls += 1
            state["unresolved"] = unresolved(call(run_, ref, "/discussions?per_page=100"))
        except GlabError as exc:
            errors.append({"key": "mr.err.threads", "params": {"reason": exc.spec()}})
    return {**state, "error": errors}, calls


def _int(x: object) -> int:
    return x if isinstance(x, int) and not isinstance(x, bool) else 0
