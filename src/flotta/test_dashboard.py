"""What the dashboard promises about itself, held where `just check` sees it.

The dashboard has no authentication and a button that destroys boxes, so the
one thing standing between it and anyone else is where it listens. Its
README and its startup banner both said "localhost only" while `next dev`
listened on every interface, which put that button on the LAN. Nothing ran
the claim, so nothing caught it.

And it is a Next.js server, so which Next it runs is a security fact, not a
preference: 16.2.12 carried critical advisories (remote code execution in
image optimisation among them) that 16.4.3 fixes.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[2] / "dashboard"
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
#: The first release clear of the advisories 16.2.12 carried (flotta issue 4).
SAFE_NEXT = (16, 4, 3)


def _scripts() -> dict[str, str]:
    return json.loads((DASHBOARD / "package.json").read_text())["scripts"]


@pytest.mark.parametrize("script", ["dev", "start"])
def test_the_dashboard_listens_on_this_machine_only(script):
    argv = shlex.split(_scripts()[script])
    assert argv[:2] == ["next", script], argv
    hosts = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("-H", "--hostname")]
    assert hosts and hosts[-1] in LOOPBACK, (
        f"`npm run {script}` is {_scripts()[script]!r}: without a loopback --hostname, Next "
        "listens on every interface, and anyone who can reach the port can destroy any box"
    )


def _version(text: str) -> tuple[int, ...]:
    found = re.match(r"^(\d+)\.(\d+)\.(\d+)$", text)
    assert found, f"not an exact version: {text!r}"
    return tuple(int(n) for n in found.groups())


def test_the_dashboard_runs_a_next_without_the_known_critical_advisories():
    lock = json.loads((DASHBOARD / "package-lock.json").read_text())
    installed = lock["packages"]["node_modules/next"]["version"]
    assert _version(installed) >= SAFE_NEXT, f"next {installed} is older than 16.4.3"
    declared = json.loads((DASHBOARD / "package.json").read_text())["dependencies"]["next"]
    # Exact, so `npm install` on another machine cannot move it.
    assert _version(declared) >= SAFE_NEXT, declared
