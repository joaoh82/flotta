"""`flotta-request` — file a piece of work someone asked for, before starting it (FLOTTA-173).

Runs on the box, by the agent, when someone in a conversation asks it for real
work. The control plane has Jev judge it and answers whether the agent may go
ahead now or must wait for the agency. Two forms:

    flotta-request "<what is asked, in a line>" --details "<what they said>"
    flotta-request list [--json]

`--details -` reads the details from stdin. The answer is sentences for the
agent: what to do now, and what to tell the person. With the box's own
`box:peer` token, as `flotta-ask` uses.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import IO, Any

from flotta.box.pulls import PrError, _call, _where

USAGE = (
    'usage: flotta-request "<what is asked, in a line>" [--details "<what they said>"]\n'
    "       (--details - reads them from stdin)\n"
    "       flotta-request list [--json]\n"
)

Ask = Callable[..., dict[str, Any]]


def file(title: str, details: str, *, env: dict[str, str], call: Ask = _call) -> dict[str, Any]:
    base, token, box = _where(env)
    return call(
        "POST",
        f"{base}/api/boxes/{box}/requests",
        token=token,
        body={"title": title, "details": details},
    )


def mine(*, env: dict[str, str], call: Ask = _call) -> list[dict[str, Any]]:
    base, token, box = _where(env)
    answer = call("GET", f"{base}/api/boxes/{box}/requests", token=token)
    found = answer.get("requests")
    return [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []


def told(answer: dict[str, Any]) -> str:
    """What the agent is to do now, from the control plane's answer."""
    why = str(answer.get("why") or "")
    if answer.get("starts"):
        return (
            f"Approved: {why}. Go ahead with it now, in this conversation.\n"
            "If it turns out to need much more than it looked, stop and file what is left "
            "as a new request.\n"
        )
    return (
        f"Held for the agency: {why}.\n"
        "Do not start the work. Tell the person you have passed it to the agency, which "
        "decides on work like this before it starts, and that it will be picked up once "
        "they approve it. You will be sent it as a piece of work of its own if they do. "
        "You can keep helping with anything else in the meantime.\n"
    )


_STATUS = {"waiting": "waiting for the agency", "approved": "approved", "declined": "declined"}

#: How a held request's work run is going, said so the agent does not do it twice.
_WORK = {
    "pending": "pending, a run of its own waiting to start: do not start it here too",
    "running": "running, in a run of its own: it is being done, do not start it here too",
    "done": "done",
    "failed": "failed: tell the person, and do not redo it here without the agency",
    "cancelled": "cancelled, stopped on purpose: tell the person, and do not redo it here "
    "without the agency",
    "budget_blocked": "held by a budget or a pause, and not started: tell the person, and do "
    "not do it here, which spends the same machine",
}


def describe(found: list[dict[str, Any]]) -> str:
    if not found:
        return "This agent has filed no requests.\n"
    lines = ["Requests this agent filed, and how the agency answered:"]
    for r in found:
        status = _STATUS.get(str(r.get("status")), str(r.get("status")))
        if r.get("status") == "approved" and r.get("auto"):
            status = "approved on its own"
        lines.append(f"  {r.get('title')!r}: {status}")
        if r.get("note"):
            lines.append(f"    note from the agency: {r['note']}")
        work = r.get("work_status")
        if work:
            lines.append(f"    its work: {_WORK.get(str(work), str(work))}")
    return "\n".join(lines) + "\n"


def main(
    argv: list[str] | None = None,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    env: dict[str, str] | None = None,
    call: Ask | None = None,
) -> int:
    argv = sys.argv[1:] if argv is None else argv
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    env = dict(os.environ) if env is None else env
    call = call or _call

    try:
        if argv[:1] == ["list"] and argv[1:] in ([], ["--json"]):
            found = mine(env=env, call=call)
            stdout.write(json.dumps(found) + "\n" if argv[1:] else describe(found))
            return 0
        title, details, rest = None, "", list(argv)
        while rest:
            arg = rest.pop(0)
            if arg == "--details" and rest:
                value = rest.pop(0)
                details = stdin.read() if value == "-" else value
            elif arg.startswith("--details="):
                details = arg.removeprefix("--details=")
            elif title is None and not arg.startswith("--"):
                title = arg
            else:
                title = None
                break
        if not title or rest:
            stderr.write(USAGE)
            return 2
        stdout.write(told(file(title, details, env=env, call=call)))
        return 0
    except PrError as exc:
        print(
            f"flotta-request: {exc}. The request was not filed: do not start work that needs "
            "planning; tell the person the agency could not be reached and try again shortly.",
            file=stderr,
        )
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised as a console script
    raise SystemExit(main())
