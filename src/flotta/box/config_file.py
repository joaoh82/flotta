"""How a boot step changes the agent's `config.yaml` (FLOTTA-253).

Runs on the box. `box_entrypoint.sh` runs four steps over this file before
Hermes starts (`approvals`, `skills`, `inference`, `screen`), and the control
plane writes it too, over `exec`, with `flotta.box.rules` (an agent's rules and
its MCP servers). Each of those reads the file, changes its own keys and saves
the whole of it, so two at once need to take turns, and until this module the
boot steps did not: they saved with a plain write and took no lock.

**Two things went wrong that way, both found by reading (review of flotta #262).**
A boot step that read the file before a control-plane write and saved after it
put its older copy back, without the MCP servers just written. And a plain write
empties the file before it fills it, so a control-plane write that read it in
that instant merged onto nothing and renamed its own file over the path: the
boot step's write then went to a file nobody reads. For `inference` that is an
agent up without its model.

**So: the lock `rules.py` takes, and a save by rename.** `locked` holds
`LOCK_NAME` for the whole read, change and save. `save` writes a staged file
and renames it, so the path never holds half a file. `rules.py` travels over
`exec` and can import nothing of Flotta's, so it has its own copy of both; a
test keeps the two lock names the same.

**A boot must go on.** A step that cannot take the lock, because the folder
cannot be written or because something has held the lock for `WAIT_S`, goes on
without it, as every step did before: a model set without the lock is better
than no model. `locked` says which it was.

**What this does not cover.** Hermes writes this file itself, when someone
answers "always" to a command, and takes no lock of ours.

A saved file is readable by its owner only, as one `rules.py` saved always
was: it can hold the model's key and each MCP server's token.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

#: The lock file beside `config.yaml`. The name `flotta.box.rules` takes too.
LOCK_NAME = ".config.yaml.flotta-lock"

#: How long a boot step waits for the lock before going on without it. A
#: control-plane write holds it for a moment; this is for one that never ends.
WAIT_S = 30.0


@contextlib.contextmanager
def locked(
    home: Path, *, wait_s: float | None = None, sleep=time.sleep, clock=time.monotonic
) -> Iterator[bool]:
    """Hold the config's lock for the block: yields whether it is held.

    False when it could not be taken, and the block runs anyway.
    """
    wait_s = WAIT_S if wait_s is None else wait_s
    try:
        lock = open(home / LOCK_NAME, "a")  # noqa: SIM115 - closed below, after the block
    except OSError:
        yield False
        return
    try:
        held, deadline = False, clock() + wait_s
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if clock() >= deadline:
                    break
                sleep(0.1)
        yield held
    finally:
        lock.close()  # which lets the lock go


def save(path: Path, config: dict[str, Any]) -> None:
    """Replace `path` with `config` in one step. `OSError` when it cannot."""
    import yaml

    fd, staged = tempfile.mkstemp(dir=path.parent, prefix=".config.yaml.", suffix=".flotta")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(yaml.safe_dump(config, sort_keys=False))
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise
