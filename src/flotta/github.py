"""Asking GitHub whether the fleet's token can reach a repository.

The one question the control plane needs GitHub to answer, and it exists
because of a gap discovered by using the app: **a Flotta grant can only narrow
what the fleet token reaches, never widen it.**

`box_repos` decides which repositories Flotta will hand a credential for.
GitHub decides what that credential can then do. Granting a repository the
token has no access to produces a grant that looks correct everywhere in
Flotta and fails much later, on a machine, as GitHub's
`Write access to repository not granted` — a message that sounds like a
permissions problem on the repository during a clone and sends the reader to
the wrong settings page entirely.

So the check happens where the token lives, at the moment somebody asks for
the grant.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

#: GitHub's REST root. Not configurable: Enterprise would need more than a host
#: swap (auth, API version), and a half-supported option is worse than none.
API_ROOT = "https://api.github.com"

#: Bounded tightly. This sits in front of a button, and the fallback when it
#: times out is to allow the grant — so a slow answer costs the person a
#: moment, never the ability to configure their fleet.
DEFAULT_TIMEOUT_S = 5.0

Verdict = Literal["reachable", "denied", "unknown"]


@dataclass(frozen=True, slots=True)
class Reach:
    """What GitHub said, and why it matters.

    Three outcomes rather than a bool, because "no" and "could not ask" must
    not collapse. Refusing a grant on a network blip would turn a GitHub
    hiccup into "you cannot configure your fleet", which is a worse failure
    than the one this check exists to prevent.
    """

    verdict: Verdict
    detail: str = ""

    @property
    def refuses(self) -> bool:
        return self.verdict == "denied"


def repo_reachable(
    repo: str,
    *,
    token: str | None,
    fetch: Callable[..., Any] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Reach:
    """Can the fleet's token see `owner/name`?

    `repo` must already be normalised — this asks about a path, and a URL or a
    `.git` suffix would ask about a repository that does not exist.

    **404 is a denial, not an absence.** GitHub answers 404 rather than 403 for
    a private repository a token cannot see, deliberately, so that tokens
    cannot be used to enumerate private repositories. The two cases are
    indistinguishable from here and the advice is the same either way, so they
    share a message that does not claim to know which it was.

    Anything else — a 5xx, a timeout, a connection failure, no token to ask
    with — is `unknown`, and the caller allows the grant. `fetch` is injected
    so the suite never reaches the network.
    """
    if not (token or "").strip():
        # Nothing to ask with. Public repositories still work without any
        # credential, so this is not a reason to refuse a grant.
        return Reach("unknown", f"no fleet token configured, so {repo} was not checked")

    caller = fetch or _httpx_get
    try:
        status = caller(
            f"{API_ROOT}/repos/{repo}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout_s=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 - any transport failure is "could not ask"
        return Reach("unknown", f"could not reach GitHub to check {repo}: {exc}")

    if status == 200:
        return Reach("reachable")
    if status in (403, 404):
        return Reach(
            "denied",
            f"the fleet's GitHub token has no access to {repo}. A grant can only "
            f"narrow what that token reaches, never widen it — so this one would "
            f"be stored and then fail on the agent. Add {repo} to the token's "
            f"scope on GitHub, or use a token that covers it.",
        )
    return Reach("unknown", f"GitHub answered {status} when asked about {repo}")


def _httpx_get(url: str, *, headers: dict[str, str], timeout_s: float) -> int:
    """The status code, and nothing else.

    Narrow on purpose: this module has no business holding a response body
    that was fetched with the fleet's credential, and a status code is the
    whole answer.
    """
    import httpx

    return httpx.get(url, headers=headers, timeout=timeout_s).status_code
