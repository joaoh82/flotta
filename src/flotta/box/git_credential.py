"""The box's git credential helper — how an agent pushes without holding a key.

Part 1 (#49) built the control plane's half: per-box repository grants and
``POST /api/boxes/{id}/git-credential``. This is the half that runs *on the
box*, and it is the reason the design is worth the machinery.

## Why a helper and not a token in the environment

The obvious way to let a box clone private code is a PAT as a Fly secret,
exactly like ``OPENROUTER_API_KEY`` today. Two observations from the live box
on 2026-08-30 rule it out as a default:

- **The agent has root and uses it.** Handed a build that failed on a missing
  dependency, it ran ``apt install libpq-dev`` unprompted. Correct behaviour,
  and proof it will act on instructions that originated in a repository.
- **A secret in the environment is readable by the thing being injected.**
  ``echo $GH_TOKEN`` is one turn away, and a GitHub token with push access is
  worth considerably more than a model key.

So the box holds no GitHub credential at rest. It holds a *Flotta* token scoped
to ``git:credential`` and nothing else, and trades it — per repository, per
invocation — for a GitHub credential it never writes down.

**What this does not buy, stated plainly.** The agent can still *invoke* this
helper; it must, or it could not push. What it cannot do is read a GitHub token
out of its own environment, use one for a repository it was not granted, or
keep using one after the grant is withdrawn. That is a smaller claim than "the
agent cannot get a token" and it is the true one.

## The protocol

git speaks a line protocol to credential helpers::

    git-credential-flotta get   <<<  protocol=https
                                     host=github.com
                                     path=owner/repo.git

and reads ``username=``/``password=`` back on stdout. ``store`` and ``erase``
are the caching verbs; we accept and ignore them, because there is nothing to
cache — a credential that is fetched per use is one that revocation reaches.

``path`` arrives only because the box sets ``credential.useHttpPath=true``.
Without it git asks for "a credential for github.com" and per-repository grants
would have nothing to key on.

## Failure is silent, on purpose

Every failure path exits 0 having printed no credential. git then behaves as if
no helper existed: an anonymous clone of a public repo still works, and a
private one fails with git's own authentication error rather than ours. Exiting
non-zero would turn "this box has no GitHub identity configured" into a hard
error on every public clone, which is a worse default for a machine that is
useful without one.

The *diagnosis* still has to reach a human, so the reason goes to stderr, which
git passes through. Never the token.
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

#: Where the control plane lives, and the token to present to it. Both are set
#: on the machine when the box is created; neither is in the image.
CONTROL_URL_ENV = "FLOTTA_CONTROL_URL"
BOX_TOKEN_ENV = "FLOTTA_BOX_TOKEN"
#: Informational only — `printenv` on a box should say which box it is. The
#: helper reads the id out of the **token**, because two sources can disagree
#: and one cannot. See `fetch_credential`.
BOX_ID_ENV = "FLOTTA_BOX_ID"

#: GitHub only, for now. A helper that answered for every host would be asked
#: for credentials it cannot supply on every private registry the agent
#: touches, and each of those is a control-plane round trip to reach a 422.
SUPPORTED_HOST = "github.com"

#: Short. This runs in front of every fetch and push, so a control plane that
#: is down should cost the agent a few seconds, not a minute.
DEFAULT_TIMEOUT_S = 10.0


class HelperError(Exception):
    """Something is wrong that a human needs to read. Never carries a secret."""


class NotOurs(HelperError):
    """The request is for a host this helper does not answer for.

    Its own class rather than a flag on `HelperError` because it is the one
    failure that must stay *silent*: git asks every configured helper about
    every host, so a box that also talks to a private package registry would
    otherwise print a line of ours on each of those requests.
    """


def parse_request(stream: IO[str]) -> dict[str, str]:
    """git's ``key=value`` block → a dict. A blank line ends it.

    Unknown keys are kept rather than rejected: git has added fields to this
    protocol before (``wwwauth[]``, ``oauth_refresh_token``) and a helper that
    fails on one it does not recognise breaks on a git upgrade.
    """
    fields: dict[str, str] = {}
    for raw in stream:
        line = raw.rstrip("\n")
        if not line:
            break
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value
    return fields


def repo_from_request(fields: dict[str, str]) -> str:
    """The repository this request is about, as ``owner/name``.

    Raises `HelperError` when the request is not one we can answer — a
    non-GitHub host, or a path that does not name a repository. Both are
    ordinary, not exceptional: git asks every configured helper about every
    host.
    """
    host = fields.get("host", "").strip().lower()
    if host != SUPPORTED_HOST:
        raise NotOurs(f"not a {SUPPORTED_HOST} request (host={host or 'unset'})")

    path = fields.get("path", "").strip().strip("/")
    if not path:
        raise HelperError(
            "no repository path in the request — the box needs "
            "`git config --global credential.useHttpPath true`"
        )

    # `.git` and any trailing segments off; the control plane normalises again,
    # but sending it something recognisable turns a 422 into a match.
    parts = [p for p in path.removesuffix(".git").split("/") if p]
    if len(parts) < 2:
        raise HelperError(f"{path!r} does not name a repository as owner/name")
    return f"{parts[0]}/{parts[1]}".lower()


def credential_url(control_url: str, box_id: str) -> str:
    """The endpoint part 1 built, for this box."""
    return f"{control_url.rstrip('/')}/api/boxes/{box_id}/git-credential"


def _post(url: str, *, token: str, repo: str, timeout: float) -> dict[str, Any]:
    """One POST, with the errors a human can act on spelled out.

    stdlib rather than httpx: this runs in front of every git operation, and
    ``import httpx`` costs more than the request does. It is also one less
    thing that has to be installed for an agent's own ``pip install`` to be
    unable to break its box's ability to fetch code.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps({"repo": repo}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = json.loads(exc.read().decode()).get("detail", "")
        # The control plane's 403 already names the grant command, so pass it
        # through rather than paraphrasing it into something less useful.
        raise HelperError(f"control plane refused ({exc.code}): {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise HelperError(f"cannot reach the control plane at {url}: {exc.reason}") from exc
    except (ValueError, TimeoutError) as exc:
        raise HelperError(f"bad answer from the control plane: {exc}") from exc


def fetch_credential(
    repo: str,
    *,
    env: dict[str, str],
    timeout: float = DEFAULT_TIMEOUT_S,
) -> tuple[str, str]:
    """Trade this box's Flotta token for a GitHub credential. ``(user, secret)``."""
    control_url = (env.get(CONTROL_URL_ENV) or "").strip()
    token = (env.get(BOX_TOKEN_ENV) or "").strip()

    missing = [
        name
        for name, value in ((CONTROL_URL_ENV, control_url), (BOX_TOKEN_ENV, token))
        if not value
    ]
    if missing:
        raise HelperError(
            f"this box has no GitHub identity: ${', $'.join(missing)} not set. "
            "Configure it with `just box-identity <box>`."
        )

    # **The token names the box.** Not `$FLOTTA_BOX_ID`, which is informational
    # only — a second source that can disagree with the first, and did: adopting
    # an existing machine rotates its secrets but not its environment, so a box
    # held a token for `b-d82e...` while its environment still said `b-2a94...`.
    # Every request would have addressed one box with the other's token and been
    # refused by the check that makes box tokens safe — while the fleet recorded
    # the identity as successfully minted. One source cannot drift from itself.
    from flotta.auth import peek_subject

    box_id = peek_subject(token) or ""
    if not box_id:
        raise HelperError(
            f"${BOX_TOKEN_ENV} does not name a box, so there is nothing to ask "
            "about. Re-issue it with `just box-identity <box>`."
        )

    payload = _post(credential_url(control_url, box_id), token=token, repo=repo, timeout=timeout)
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "").strip()
    if not password:
        raise HelperError("the control plane returned no credential")
    return username or "x-access-token", password


def format_credential(username: str, password: str) -> str:
    """git's answer format. Values are newline-terminated and never quoted."""
    return f"username={username}\npassword={password}\n"


def main(
    argv: list[str] | None = None,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    env: dict[str, str] | None = None,
    fetch: Callable[..., tuple[str, str]] | None = None,
) -> int:
    """Entry point. Always 0 for ``get`` — see the module docstring."""
    argv = sys.argv[1:] if argv is None else argv
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    env = dict(os.environ) if env is None else env
    fetch = fetch or fetch_credential

    operation = argv[0] if argv else ""
    if operation != "get":
        # `store` and `erase` are the cache verbs. Nothing is cached, so both
        # are correctly implemented as doing nothing. An unknown verb is also
        # a no-op rather than an error, for the same reason `parse_request`
        # tolerates unknown keys.
        return 0

    try:
        repo = repo_from_request(parse_request(stdin))
    except NotOurs:
        # Silent: this is the normal case, not a problem. See `NotOurs`.
        return 0
    except HelperError as exc:
        print(f"flotta: {exc}", file=stderr)
        return 0

    try:
        username, password = fetch(repo, env=env)
    except HelperError as exc:
        print(f"flotta: no credential for {repo}: {exc}", file=stderr)
        return 0

    stdout.write(format_credential(username, password))
    stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
