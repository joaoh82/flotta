"""`flotta-repos` — which GitHub repositories this agent may use (FLOTTA-61).

Runs on the box, by the agent, usually because someone asked "what can you
access?".

## Why this exists

Asked exactly that, eng-r ran seven shell commands and three of them failed:
`flotta repo list` wanted a fleet store, then a signing key, and a box has
neither — correctly. Nothing on the machine knew the answer, so the agent
guessed its way towards it for most of a minute.

The answer lives on the control plane, and the box already holds a token the
control plane accepts: the `git:credential` token its credential helper trades
for GitHub credentials. This asks the same control plane, with the same token,
for the list behind that trade — and nothing more. See
`GET /api/boxes/{id}/git-credential/repos`.

## Why not `flotta repo list`

That is the operator's command: it lists *any* box's grants, with a token that
can read the whole fleet. Teaching it a second meaning on a box would make one
command behave differently depending on which machine it ran on, and the
failure the agent hit would still be one typo away. A separate name has one job.

## Output is for a model to read

Plain sentences around the list, because the reader is an agent deciding what to
do next: what it may do with these, how credentials arrive, and what to tell a
person when the repository they want is not here. `--json` for anything that
wants to parse it.

Unlike the credential helper, a failure exits non-zero and says why. Nothing
calls this on git's behalf, so there is no anonymous-clone path to protect, and
an agent needs to know the difference between "no grants" and "could not ask".
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import IO, Any

from flotta.box.git_credential import BOX_TOKEN_ENV, CONTROL_URL_ENV, DEFAULT_TIMEOUT_S


class ReposError(Exception):
    """Something a person or the agent needs to read. Never carries the token."""


def repos_url(control_url: str, box_id: str) -> str:
    return f"{control_url.rstrip('/')}/api/boxes/{box_id}/git-credential/repos"


def _get(url: str, *, token: str, timeout: float) -> dict[str, Any]:
    """One GET. stdlib, for the same reason the credential helper is."""
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = json.loads(exc.read().decode()).get("detail", "")
        raise ReposError(f"the control plane refused ({exc.code}): {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ReposError(f"cannot reach the control plane at {url}: {exc.reason}") from exc
    except (ValueError, TimeoutError) as exc:
        raise ReposError(f"bad answer from the control plane: {exc}") from exc


def fetch_repos(*, env: dict[str, str], timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Ask the control plane which repositories this box is granted."""
    control_url = (env.get(CONTROL_URL_ENV) or "").strip()
    token = (env.get(BOX_TOKEN_ENV) or "").strip()
    missing = [
        name
        for name, value in ((CONTROL_URL_ENV, control_url), (BOX_TOKEN_ENV, token))
        if not value
    ]
    if missing:
        raise ReposError(
            f"this box has no GitHub identity (${', $'.join(missing)} not set), so it "
            "has no granted repositories. Public repositories can still be cloned."
        )

    # The token names the box, not $FLOTTA_BOX_ID — see `fetch_credential`.
    from flotta.auth import peek_subject

    box_id = peek_subject(token) or ""
    if not box_id:
        raise ReposError(f"${BOX_TOKEN_ENV} does not name a box")
    payload = _get(repos_url(control_url, box_id), token=token, timeout=timeout)
    repos = payload.get("repos")
    if not isinstance(repos, list):
        raise ReposError("the control plane's answer had no repository list")
    return {
        "name": str(payload.get("name") or ""),
        "repos": [str(r) for r in repos],
    }


def describe(answer: dict[str, Any]) -> str:
    """The list, in words an agent can act on."""
    who = answer.get("name") or "This agent"
    repos = answer.get("repos") or []
    if not repos:
        return (
            f"{who} has no GitHub repositories granted.\n"
            "Public repositories can still be cloned anonymously over HTTPS. For a "
            "private one, ask the person to grant it in the Flotta app "
            "(this agent's Info panel, Repositories).\n"
        )
    lines = [f"{who} may clone, fetch and push these GitHub repositories:"]
    lines += [f"  {repo}" for repo in repos]
    lines += [
        "",
        "Use HTTPS: https://github.com/<owner>/<name>.git. Credentials are supplied "
        "per use by the git credential helper; there is no token to set or read.",
        "Any repository not listed here is refused. To add one, ask the person to "
        "grant it in the Flotta app (this agent's Info panel, Repositories). "
        "The agent cannot grant itself access.",
    ]
    return "\n".join(lines) + "\n"


def main(
    argv: list[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    env: dict[str, str] | None = None,
    fetch: Callable[..., dict[str, Any]] | None = None,
) -> int:
    argv = sys.argv[1:] if argv is None else argv
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    env = dict(os.environ) if env is None else env
    fetch = fetch or fetch_repos

    try:
        answer = fetch(env=env)
    except ReposError as exc:
        print(f"flotta-repos: {exc}", file=stderr)
        return 1

    if "--json" in argv:
        stdout.write(json.dumps(answer) + "\n")
    else:
        stdout.write(describe(answer))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a console script
    raise SystemExit(main())
