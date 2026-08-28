"""Reading `.env`, in one place.

Every `just` recipe sees `.env` because the justfile sets `dotenv-load`. A bare
`flotta` command saw none of it, and the failure was worse than "config
missing": `flotta token mint` reported

    no signing key: set $FLOTTA_SIGNING_KEY. Generate one with
    `flotta token key` ...

while the key sat in `.env` two lines from the cursor. Following that advice
mints a **new** key, which invalidates every token already deployed — so the
error actively led toward breaking a working deployment.

This module is the parser both callers share. `flotta.fly` had a copy with a
comment explaining the duplication ("`cli` pulls in typer"), which was a good
reason to duplicate and a better reason to extract: this module imports nothing
but the standard library, so the Fly scripts can use it without dragging in a
CLI framework.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DOTENV = ".env"


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse dotenv text into a mapping. Ignores what it cannot understand.

    Deliberately lenient: a malformed line in `.env` should not stop a command
    that does not need that line. The alternative — refusing to start — turns
    a stray character into an outage.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.removeprefix("export ").strip()
        if not name:
            continue
        value = value.strip().split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            values[name] = value
    return values


def read_dotenv(path: str | Path = DEFAULT_DOTENV) -> dict[str, str]:
    """Everything in a dotenv file, or an empty mapping if it is unreadable."""
    try:
        return parse_dotenv(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return {}


def read_dotenv_value(key: str, path: str | Path = DEFAULT_DOTENV) -> str | None:
    """One key from a dotenv file, or None if absent."""
    return read_dotenv(path).get(key)


def load_dotenv(path: str | Path = DEFAULT_DOTENV, env: dict[str, str] | None = None) -> list[str]:
    """Load `.env` into the environment. Returns the names it set.

    **An already-set variable always wins.** `FLOTTA_STORE=x flotta ps` must
    mean what it says, and a file quietly overriding an explicit export would
    be the kind of surprise that costs an afternoon. Same precedence `just`
    uses, so a recipe and a bare command agree about which value applies.
    """
    target = os.environ if env is None else env
    loaded: list[str] = []
    for name, value in read_dotenv(path).items():
        if name not in target:
            target[name] = value
            loaded.append(name)
    return loaded
