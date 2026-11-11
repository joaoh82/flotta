"""`flotta-handoff` — say this agent needs a person on its screen (FLOTTA-213).

Runs on the box, by the agent, when what it is doing on its screen needs a
person: a sign-in, a 2FA prompt, a CAPTCHA, a payment, or a site that insists
on a human. Hermes has no tool that pauses for one, so the agent says so in its
reply and ends its turn, which reaches whoever reads that conversation and, for
a run nobody is watching, nobody. This records it on the control plane instead,
where the agency's Home and the agent's card show it with **Open screen**, and
the agency is emailed.

    flotta-handoff "<what the person is to do>" [--url <the page>]

It is cleared when a person hands the screen back, or when the agency dismisses
it. During a run, the run waits for it and carries on once the screen is handed
back. With the box's own `box:peer` token, as `flotta-request` uses.

Only for an agent with a screen (`FLOTTA_SCREEN`, `flotta.box.screen`): one
without one is told so, and nothing is sent.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import IO, Any

from flotta.box import screen
from flotta.box.pulls import PrError, _call, _where

USAGE = 'usage: flotta-handoff "<what the person is to do on the screen>" [--url <the page>]\n'

#: What an agent without a screen is told, in place of a usage line.
NO_SCREEN = (
    "flotta-handoff: this agent has no screen, so there is nothing for a person to take "
    "over. Nothing was sent. Tell the person in your reply what you need instead, and "
    "never ask them for a password or a code in the conversation.\n"
)

Ask = Callable[..., dict[str, Any]]


def hand_over(
    why: str, url: str | None, *, env: dict[str, str], call: Ask = _call
) -> dict[str, Any]:
    base, token, box = _where(env)
    body: dict[str, Any] = {"why": why}
    if url:
        body["url"] = url
    return call("POST", f"{base}/api/boxes/{box}/handoffs", token=token, body=body)


def told(answer: dict[str, Any]) -> str:
    """What the agent is to do now, from the control plane's answer."""
    waits = bool(answer.get("run_waits"))
    lines = [
        "Handed over: the agency has been told you need a person on your screen, "
        "and why. It is on their Home with Open screen.",
        "Now end your turn. Say in your reply what you need the person to do on the "
        "screen, and that they should hand it back when they are done. Do not ask for a "
        "password, a code or a card number in the conversation: they type it on the "
        "screen, not to you.",
    ]
    if waits:
        lines.append(
            "This is a run: it waits for them. Once they hand the screen back you will be "
            "told to carry on, in this same conversation."
        )
    else:
        lines.append(
            "When someone hands the screen back, carry on from where you stopped when "
            "they tell you to."
        )
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

    if not screen.wanted(env):
        stderr.write(NO_SCREEN)
        return 2
    why, url, rest = None, None, list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--url" and rest:
            url = rest.pop(0)
        elif arg.startswith("--url="):
            url = arg.removeprefix("--url=")
        elif why is None and not arg.startswith("--"):
            why = arg
        else:
            why = None
            break
    if not why or not why.strip() or rest:
        stderr.write(USAGE)
        return 2
    try:
        stdout.write(told(hand_over(why, url, env=env, call=call)))
        return 0
    except PrError as exc:
        print(
            f"flotta-handoff: {exc}. The agency was not told. End your turn anyway, and say "
            "in your reply what you need a person to do on your screen. Never ask for a "
            "password or a code in the conversation.",
            file=stderr,
        )
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised as a console script
    raise SystemExit(main())
