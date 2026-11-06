"""Write an agent's rules into its `config.yaml` (FLOTTA-124).

Runs on the box, and is the one module of `flotta.box` that does not come with
the image: `provision.set_rules` sends this file's source over `exec` and runs
it with the box's Python, so a rule reaches an agent on the image it already
has. That is why it imports nothing of Flotta's, only `yaml`, which every box
has because Hermes does.

**What it touches.** The keys `flotta.rules.OWNED_KEYS` names, and nothing
else: each is set to what it is given, or removed for `None`, so a rule taken
out is gone rather than left behind. `command_allowlist`, which Hermes writes
when someone answers "always", and everything else in the file stay as found.

**Written whole, then moved.** Hermes reads this file on every command, so a
half-written one would be a broken agent for as long as it took. A rename
replaces it in one step. The staged file is this run's own, and runs on one
box take a lock on the file next to it, so two saves at once land one after
the other rather than one renaming the other's half-written file.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def merge(config: dict[str, Any] | None, changes: list[list[Any]]) -> dict[str, Any]:
    """`config` with `changes` applied, each `[section, key, value]`. Pure.

    A section that isn't a mapping is someone's mistake. Replacing it would
    lose what they meant, and leaving it would leave a rule out, so it fails.
    """
    merged = dict(config or {})
    for section, key, value in changes:
        block = merged.get(section)
        if block is None:
            block = {}
        if not isinstance(block, dict):
            raise ValueError(f"{section} in config.yaml is not a mapping")
        block = dict(block)
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
        if block:
            merged[section] = block
        else:
            merged.pop(section, None)
    return merged


def apply(home: Path, changes: list[list[Any]]) -> str:
    import yaml

    path = home / "config.yaml"
    with open(home / ".config.yaml.flotta-lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError(f"{path} is not a mapping")
        merged = merge(loaded, changes)
        fd, staged = tempfile.mkstemp(dir=home, prefix=".config.yaml.", suffix=".flotta")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True))
            os.replace(staged, path)
        except BaseException:
            Path(staged).unlink(missing_ok=True)
            raise
    return f"rules written to {path}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: rules.py <hermes home> <base64 json changes>", file=sys.stderr)
        return 2
    changes = json.loads(base64.b64decode(args[1]).decode("utf-8"))
    try:
        print(f"[flotta-box] {apply(Path(args[0]), changes)}")
    except (OSError, ValueError) as exc:
        print(f"[flotta-box] rules not written: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
