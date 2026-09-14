"""How long an agent waits for a person before a risky command is denied.

Runs on the box, from `box_entrypoint.sh`, before `hermes serve` starts.

## Why this exists (FLOTTA-60)

Hermes gates terminal commands it considers risky. When it escalates one, the
gateway asks the connected client and **waits** — for `approvals.timeout`
seconds, 300 by default, and then denies. The app now shows that question in
the conversation, so a person who is watching answers in seconds. A person who
is *not* watching — the app closed, or looking at another agent — used to cost
the agent five minutes of doing nothing, and the product cannot feel smooth
with a five-minute stall hiding in it.

So this shortens the wait and keeps the answer: unanswered is still **deny**.
Hermes's own words for it are that silence is not consent, and nothing here
changes that. It changes only how long silence takes.

## What it will not overwrite

`config.yaml` on the volume belongs to the agent and to whoever configures it.
A timeout somebody chose is left alone. The rule is "set it when nobody has":
absent, or still at Hermes's shipped default — which is how a file Hermes wrote
back in full with every default filled in is told apart from a deliberate
choice. The one case this gets wrong is a person who chose exactly 300 on
purpose, and the log line below says what was written so that is findable.

## Why not a stronger boundary

An agent has root on its own box and can edit this file, or turn approvals off
entirely. The gate is a guardrail against an agent doing something dangerous by
mistake or on a repository's say-so — it was never a boundary against the agent
itself, and this module does not pretend otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

#: How long an unanswered approval waits before it is denied.
#:
#: Sixty seconds, against Hermes's 300. Hermes chose 300 because its prompts
#: often arrive as phone notifications, and 60 once expired before a tap landed.
#: Flotta's prompt is a card in the conversation the person is already reading,
#: so a minute is ample to decide — and an agent nobody is watching gets its
#: answer in a minute instead of five.
FLOTTA_APPROVAL_TIMEOUT_S = 60

#: Hermes's shipped default. A value still equal to it was not chosen by anyone.
HERMES_DEFAULT_APPROVAL_TIMEOUT_S = 300


def settle_timeout(config: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    """The config with Flotta's approval timeout applied, and whether it changed.

    Pure: no file, no YAML. Everything that decides what gets written is here,
    so it is tested against plain dictionaries rather than through a box.

    A non-dict `approvals` block is left exactly as found. It is malformed, and
    replacing it would quietly discard whatever the person meant by it; Hermes
    will report it when it loads the file, which is the more honest place for
    that error to surface.
    """
    config = dict(config or {})
    approvals = config.get("approvals")
    if approvals is None:
        approvals = {}
    if not isinstance(approvals, dict):
        return config, False

    current = approvals.get("timeout")
    if current is not None and current != HERMES_DEFAULT_APPROVAL_TIMEOUT_S:
        return config, False

    config["approvals"] = {**approvals, "timeout": FLOTTA_APPROVAL_TIMEOUT_S}
    return config, True


def apply(home: Path) -> str:
    """Write the timeout into `<home>/config.yaml` if nobody has set one.

    Returns a line for the boot log. Never raises: a box that cannot tune its
    approval timeout must still boot, because the alternative is an agent that
    is unreachable over a five-minute wait it could have lived with.
    """
    import yaml

    path = home / "config.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, yaml.YAMLError) as exc:
        return f"approvals.timeout left alone: could not read {path}: {exc}"

    if loaded is not None and not isinstance(loaded, dict):
        return f"approvals.timeout left alone: {path} is not a mapping"

    settled, changed = settle_timeout(loaded)
    if not changed:
        existing = (settled.get("approvals") or {}).get("timeout")
        return f"approvals.timeout left alone: already {existing!r}"

    try:
        path.write_text(yaml.safe_dump(settled, sort_keys=False), encoding="utf-8")
    except OSError as exc:
        return f"approvals.timeout left alone: could not write {path}: {exc}"
    return f"approvals.timeout set to {FLOTTA_APPROVAL_TIMEOUT_S}s in {path}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    home = Path(args[0]) if args else Path("/data/hermes")
    print(f"[flotta-box] {apply(home)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
