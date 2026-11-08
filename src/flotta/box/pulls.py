"""`flotta-pr` — this agent's pull requests, as the agency sees them (FLOTTA-171).

Runs on the box. Three things, with the box's own `git:credential` token:

- `flotta-pr report <url>` tells the agency about a pull request this agent
  opened. The box's `gh` runs it after every `gh pr create` that succeeds, so the
  agent only runs it by hand for a PR opened some other way (`gh api`, a GitHub
  tool). The control plane reads the PR from GitHub itself; this only says which.
- `flotta-pr may-merge` exits 0 if the agency lets this agent merge its own pull
  requests, 1 if not, and 2 if it could not ask. The box's `gh` runs it before
  `gh pr merge`, and refuses unless it says 0: a merge nobody could check is not
  one to make.
- `flotta-pr list` prints this agent's pull requests and where each stands.

Output is for a model to read, as `flotta-repos`'s is.
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

USAGE = (
    "usage: flotta-pr report <pull request url>\n"
    "       flotta-pr may-merge\n"
    "       flotta-pr list [--json]\n"
)


class PrError(Exception):
    """Something a person or the agent needs to read. Never carries the token."""


def _call(
    method: str, url: str, *, token: str, body: Any = None, timeout: float = DEFAULT_TIMEOUT_S
) -> dict[str, Any]:
    """One request. stdlib, for the same reason the credential helper is."""
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = json.loads(exc.read().decode()).get("detail", "")
        raise PrError(f"the control plane refused ({exc.code}): {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise PrError(f"cannot reach the control plane: {exc.reason}") from exc
    except (ValueError, TimeoutError) as exc:
        raise PrError(f"bad answer from the control plane: {exc}") from exc


def _where(env: dict[str, str]) -> tuple[str, str, str]:
    """The control plane, this box's token, and the box it names."""
    control_url = (env.get(CONTROL_URL_ENV) or "").strip().rstrip("/")
    token = (env.get(BOX_TOKEN_ENV) or "").strip()
    if not control_url or not token:
        raise PrError(
            "this box has no GitHub identity, so the agency cannot be asked about pull requests"
        )
    from flotta.auth import peek_subject

    box_id = peek_subject(token) or ""
    if not box_id:
        raise PrError(f"${BOX_TOKEN_ENV} does not name a box")
    return control_url, token, box_id


Ask = Callable[..., dict[str, Any]]


def report(url: str, *, env: dict[str, str], call: Ask = _call) -> dict[str, Any]:
    base, token, box = _where(env)
    return call("POST", f"{base}/api/boxes/{box}/pull-requests", token=token, body={"url": url})


def policy(*, env: dict[str, str], call: Ask = _call) -> dict[str, Any]:
    base, token, box = _where(env)
    return call("GET", f"{base}/api/boxes/{box}/pr-policy", token=token)


def pulls(*, env: dict[str, str], call: Ask = _call) -> list[dict[str, Any]]:
    base, token, box = _where(env)
    answer = call("GET", f"{base}/api/boxes/{box}/pull-requests", token=token)
    found = answer.get("pull_requests")
    return [p for p in found if isinstance(p, dict)] if isinstance(found, list) else []


_REVIEW = {
    "approved": "approved",
    "changes_requested": "changes requested",
    "commented": "commented on",
    None: "not reviewed yet",
}


def describe(found: list[dict[str, Any]]) -> str:
    if not found:
        return "This agent has no pull requests the agency knows of.\n"
    lines = ["This agent's pull requests, as the agency sees them:"]
    for pr in found:
        state = pr.get("state")
        if state == "open":
            where = _REVIEW.get(pr.get("review_state"), "reviewed")
            if pr.get("following") == "needs_person":
                where += "; its reviews now wait for a person"
        else:
            where = str(state)
        lines.append(f"  {pr.get('repo')}#{pr.get('number')} {pr.get('title')!r}: {where}")
        lines.append(f"    {pr.get('url')}")
    lines += [
        "",
        "New review feedback on an open one is sent to you as its own piece of work: "
        "you do not need to go and check for it.",
    ]
    return "\n".join(lines) + "\n"


def main(
    argv: list[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    env: dict[str, str] | None = None,
    call: Ask | None = None,
) -> int:
    argv = sys.argv[1:] if argv is None else argv
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    env = dict(os.environ) if env is None else env
    call = call or _call
    command, rest = (argv[0], argv[1:]) if argv else ("", [])

    try:
        if command == "report" and len(rest) == 1:
            answer = report(rest[0], env=env, call=call)
            pr = answer.get("pull_request") or {}
            name = f"{pr.get('repo')}#{pr.get('number')}"
            if answer.get("created"):
                stdout.write(f"Reported {name} to the agency.\n")
            else:
                stdout.write(f"The agency already knew of {name}.\n")
            return 0
        if command == "may-merge" and not rest:
            if policy(env=env, call=call).get("may_merge") is True:
                stdout.write("This agent may merge its own pull requests.\n")
                return 0
            stdout.write(
                "This agent may not merge its own pull requests: the agency merges them. "
                "Leave it open and say it is ready.\n"
            )
            return 1
        if command == "list" and rest in ([], ["--json"]):
            found = pulls(env=env, call=call)
            stdout.write(json.dumps(found) + "\n" if rest else describe(found))
            return 0
    except PrError as exc:
        print(f"flotta-pr: {exc}", file=stderr)
        return 2
    stderr.write(USAGE)
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised as a console script
    raise SystemExit(main())
