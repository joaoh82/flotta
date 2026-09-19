"""`flotta-workdir` — where this agent clones projects (FLOTTA-56).

Runs on the box, by the agent, because "where do I put this repo?" is a
question it used to guess, and a guessed path on `/data` is how a clone takes
the agent's memory with it.

The folder is on the machine's disk, not the memory volume. An image update
replaces that disk, so everything here is lost. The output says so, because
an agent that does not know that will report a checkout as surviving an
update — which is the claim this command exists to stop anyone building on.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from typing import IO, Any

from flotta.workdir import workdir_from_env


def describe(env: Mapping[str, str]) -> dict[str, Any]:
    """The path in force, and the lifetime that is the whole point of asking."""
    path = workdir_from_env(env)
    return {
        "workdir": path,
        "survives_update": False,
        "survives_nap": True,
        "on_memory_volume": False,
    }


def render(info: Mapping[str, Any]) -> str:
    path = info["workdir"]
    return (
        f"Projects folder: {path}\n"
        "\n"
        "Clones, builds and checkouts live here — on the machine's disk, not "
        "on /data (the memory volume). A nap keeps this folder; an image update "
        "replaces the disk, so everything here is lost. If a project is not in "
        "this folder, clone it. If it is, fetch and sync main (or master) before "
        "starting work. Do not ask; just do it.\n"
        "\n"
        "Never put node_modules, clones or build output on /data.\n"
    )


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: IO[str] | None = None,
) -> int:
    args = sys.argv[1:] if argv is None else argv
    out = stdout or sys.stdout
    info = describe(os.environ if env is None else env)
    if args and args[0] == "--json":
        out.write(json.dumps(info) + "\n")
        return 0
    if args:
        out.write("usage: flotta-workdir [--json]\n")
        return 2
    out.write(render(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
