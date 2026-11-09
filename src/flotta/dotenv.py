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

import argparse
import os
import re
import shlex
import sys
from pathlib import Path

DEFAULT_DOTENV = ".env"

#: Which file to read instead of `.env` (M14.8). `just staging <recipe>` sets it
#: to `.env.staging`, and every reader here then sees that file and never
#: `.env`, so no production value fills a gap in staging's settings: a
#: staging command missing a value fails, rather than borrowing production's.
DOTENV_ENV = "FLOTTA_DOTENV"


def dotenv_path() -> str:
    """The dotenv file this process reads: `$FLOTTA_DOTENV`, else `.env`."""
    return os.environ.get(DOTENV_ENV, "").strip() or DEFAULT_DOTENV


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


def read_dotenv(path: str | Path | None = None) -> dict[str, str]:
    """Everything in a dotenv file, or an empty mapping if it is unreadable."""
    try:
        return parse_dotenv(Path(path or dotenv_path()).read_text(encoding="utf-8"))
    except OSError:
        return {}


def read_dotenv_value(key: str, path: str | Path | None = None) -> str | None:
    """One key from a dotenv file, or None if absent."""
    return read_dotenv(path).get(key)


def load_dotenv(path: str | Path | None = None, env: dict[str, str] | None = None) -> list[str]:
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


_SHELL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def shell_exports(values: dict[str, str]) -> str:
    """`values` as `export NAME='value'` lines a shell can `eval` (M14.8).

    For `just staging`, which used to `.` the file: that runs it as a script,
    so `FLOTTA_X=$(cmd)` executed `cmd` and a `$` inside a secret aborted
    under `set -u`. This is the parse `flotta` itself uses, quoted, so a value
    is only ever a value. A name a shell cannot hold is left out.
    """
    return "".join(
        f"export {name}={shlex.quote(value)}\n"
        for name, value in values.items()
        if _SHELL_NAME.match(name)
    )


def main(argv: list[str] | None = None) -> int:
    """`python -m flotta.dotenv exports FILE [--require NAME=VALUE]`."""
    parser = argparse.ArgumentParser(prog="python -m flotta.dotenv")
    commands = parser.add_subparsers(dest="command", required=True)
    exports = commands.add_parser("exports", help="print FILE as shell exports")
    exports.add_argument("file")
    exports.add_argument("--require", action="append", default=[], metavar="NAME=VALUE")
    args = parser.parse_args(argv)

    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read {args.file}: {exc.strerror}", file=sys.stderr)
        return 1
    values = parse_dotenv(text)
    for required in args.require:
        name, _, value = required.partition("=")
        if values.get(name) != value:
            print(f"{args.file} does not say {name}={value}; refusing to run", file=sys.stderr)
            return 1
    print(shell_exports(values), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
