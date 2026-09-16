"""`flotta-ask` — ask another agent on this fleet a question (M7, FLOTTA-54).

Runs on the box, by the agent, when it wants a colleague's help:

    flotta-ask --list                      who can I ask, and what are they for
    flotta-ask eng-b "does the door wake a stopped box?"

The reply is printed on stdout and the command exits 0. It blocks while the
other agent thinks, which can be a minute or more — it is waking a machine and
spending a model call, and there is nothing useful to print until it answers.

## The box holds no way to reach another agent

It asks the control plane to carry the message. That is not indirection for its
own sake: the front door checks a token's *scope* and not its subject, so any
credential good enough to reach one agent reaches every agent. The box's token
carries `box:peer`, which buys the right to ask and nothing else — the control
plane checks whether it may and makes the call itself.

Every agent may ask every other by default (FLOTTA-65); a person can stop one
agent asking another. An agent cannot change that list itself — the same
shape as the git credential helper, and for the same reason: this machine's
agent has root on it.

## Failures are worth reading

Every refusal from the control plane says what to do instead, because an agent
told only "403" tries again differently three times. They arrive here unchanged
and go to stderr with a non-zero exit.
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

from flotta.box.git_credential import BOX_TOKEN_ENV, CONTROL_URL_ENV

#: Longer than the control plane's own delivery deadline, so its refusal — the
#: one with an explanation in it — arrives instead of our timeout.
DEFAULT_TIMEOUT_S = 300.0

USAGE = 'usage: flotta-ask --list | flotta-ask <agent> "<message>"'


class AskError(Exception):
    """Something the agent needs to read. Never carries the token."""


def ask_url(control_url: str, box_id: str) -> str:
    return f"{control_url.rstrip('/')}/api/boxes/{box_id}/peer/ask"


def roster_url(control_url: str, box_id: str) -> str:
    return f"{control_url.rstrip('/')}/api/boxes/{box_id}/peer/roster"


def _call(
    url: str, *, token: str, timeout: float, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One request. stdlib, for the same reason the credential helper is: an
    agent's own `pip install` must not be able to break its ability to ask for
    help."""
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = json.loads(exc.read().decode()).get("detail", "")
        raise AskError(detail or f"the control plane refused ({exc.code}): {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise AskError(f"cannot reach the control plane at {url}: {exc.reason}") from exc
    except (ValueError, TimeoutError) as exc:
        raise AskError(f"bad answer from the control plane: {exc}") from exc


def _identity(env: dict[str, str]) -> tuple[str, str, str]:
    """Control-plane URL, token, and the box id the token names."""
    control_url = (env.get(CONTROL_URL_ENV) or "").strip()
    token = (env.get(BOX_TOKEN_ENV) or "").strip()
    missing = [
        name
        for name, value in ((CONTROL_URL_ENV, control_url), (BOX_TOKEN_ENV, token))
        if not value
    ]
    if missing:
        raise AskError(
            f"this box cannot reach the fleet (${', $'.join(missing)} not set), so it "
            "cannot message other agents."
        )

    # The token names the box, not $FLOTTA_BOX_ID — see `fetch_credential`.
    from flotta.auth import peek_subject

    box_id = peek_subject(token) or ""
    if not box_id:
        raise AskError(f"${BOX_TOKEN_ENV} does not name a box")
    return control_url, token, box_id


def fetch_roster(*, env: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    """Who this agent may message."""
    control_url, token, box_id = _identity(env)
    payload = _call(roster_url(control_url, box_id), token=token, timeout=timeout)
    peers = payload.get("peers")
    if not isinstance(peers, list):
        raise AskError("the control plane's answer had no list of colleagues")
    return {"name": str(payload.get("name") or ""), "peers": peers}


def send(
    peer: str, message: str, *, env: dict[str, str], timeout: float = DEFAULT_TIMEOUT_S
) -> dict[str, Any]:
    """Carry a message to another agent and return its reply."""
    control_url, token, box_id = _identity(env)
    answer = _call(
        ask_url(control_url, box_id),
        token=token,
        timeout=timeout,
        payload={"peer": peer, "message": message},
    )
    if not isinstance(answer.get("reply"), str):
        raise AskError("the control plane's answer had no reply in it")
    return answer


def describe_roster(answer: dict[str, Any]) -> str:
    """The roster, in words an agent can act on."""
    peers = answer.get("peers") or []
    if not peers:
        return (
            "There is no other agent you can ask right now. Either you are the only "
            "agent on this fleet, or a person has stopped you asking the others — "
            "they can change that in the Flotta app (this agent's Info panel, "
            "Colleagues). You cannot change it yourself.\n"
        )
    lines = ["You can send a message to these agents:"]
    for peer in peers:
        name = str(peer.get("name") or "")
        label = peer.get("display_name") or ""
        about = peer.get("description") or ""
        said = " — ".join(part for part in (label, about) if part)
        lines.append(f"  {name}" + (f"  ({said})" if said else ""))
    lines += [
        "",
        'Ask one with: flotta-ask <agent> "<your question>"',
        "They reply once; they cannot see your conversation and cannot ask you a "
        "follow-up, so send everything they need in the one message.",
    ]
    return "\n".join(lines) + "\n"


def describe_reply(answer: dict[str, Any]) -> str:
    """One agent's answer, labelled so it is not mistaken for your own."""
    peer = answer.get("peer") or "the other agent"
    seconds = answer.get("seconds")
    took = f" (took {seconds}s)" if isinstance(seconds, int | float) else ""
    return f"{peer} replied{took}:\n\n{answer.get('reply', '')}\n"


def main(
    argv: list[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    env: dict[str, str] | None = None,
    roster: Callable[..., dict[str, Any]] | None = None,
    ask: Callable[..., dict[str, Any]] | None = None,
) -> int:
    argv = sys.argv[1:] if argv is None else argv
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    env = dict(os.environ) if env is None else env
    roster = roster or fetch_roster
    ask = ask or send

    as_json = "--json" in argv
    rest = [a for a in argv if a != "--json"]

    try:
        if not rest or rest[0] in ("--list", "-l"):
            if not rest:
                # No arguments at all is far more likely to be an agent
                # working out how the command is spelled than a mistake, so
                # answer the question it was about to ask.
                stderr.write(USAGE + "\n\n")
            answer = roster(env=env)
            stdout.write(json.dumps(answer) + "\n" if as_json else describe_roster(answer))
            return 0

        if len(rest) < 2:
            stderr.write(f"flotta-ask: no message to send.\n{USAGE}\n")
            return 2

        # Everything after the name is the message, so an unquoted question
        # reaches the colleague rather than being silently truncated to its
        # first word.
        answer = ask(rest[0], " ".join(rest[1:]), env=env)
    except AskError as exc:
        stderr.write(f"flotta-ask: {exc}\n")
        return 1

    stdout.write(json.dumps(answer) + "\n" if as_json else describe_reply(answer))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a console script
    raise SystemExit(main())
