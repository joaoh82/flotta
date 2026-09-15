"""Register the skills Flotta ships with the box image (FLOTTA-61).

Runs on the box, from `box_entrypoint.sh`, before `hermes serve` starts.

## Why a skill

An agent asked "which repositories can you access?" had no way to know, and
spent seven shell commands guessing. The command that answers it
(`flotta-repos`) is only useful if the agent knows it exists, and the only
thing guaranteed to be in front of it at the start of every conversation is the
system prompt Hermes renders.

Hermes lists every skill's name and description in that prompt. Two other
routes were considered and rejected:

- **`AGENTS.md` in the working directory.** Hermes reads it from the session's
  cwd, which is resolved from three settings in turn, and on a live box those
  already disagree: the process runs in `/workspace` while its sessions report
  `/root`. An instruction that appears or not depending on that is not one to
  rely on.
- **Writing into `SOUL.md` or the volume's `skills/`.** Both belong to the
  agent. A Flotta note in either would be edited, or silently kept stale by an
  agent that had changed it.

## Why an external directory

`skills.external_dirs` in `config.yaml` names directories Hermes indexes
alongside the agent's own skills, **read-only**. So the skill lives in the
image, next to the code it describes, and moves with every image build — never
copied onto the volume, never stale. An agent's own skill of the same name
takes precedence, which is Hermes's rule and a fair one.

## What it will not overwrite

The same rule as `approvals.py`: `config.yaml` is the agent's. This only adds
Flotta's directory to the list, keeping every entry already there, and leaves a
malformed `skills` block exactly as found.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

#: The skills directory inside the installed package. Hermes expects
#: `<dir>/<category>/<skill>/SKILL.md`.
#:
#: Not called `skills/`: `.dockerignore` excludes a top-level `skills/` (the
#: old orchestrator skill), and a name that only avoids that pattern by being
#: nested is one refactor away from vanishing from the image.
SKILLS_DIR = Path(__file__).resolve().parent / "hermes_skills"


def settle_external_dir(
    config: dict[str, Any] | None, directory: str
) -> tuple[dict[str, Any], bool]:
    """The config with `directory` in `skills.external_dirs`, and whether it changed.

    Pure, like `approvals.settle_timeout`: every decision about what gets
    written is here and tested against plain dictionaries.
    """
    config = dict(config or {})
    skills = config.get("skills")
    if skills is None:
        skills = {}
    if not isinstance(skills, dict):
        return config, False

    existing = skills.get("external_dirs")
    if existing is None:
        dirs: list[Any] = []
    elif isinstance(existing, str):
        # Hermes accepts a single string as well as a list; keep what it meant.
        dirs = [existing]
    elif isinstance(existing, list):
        dirs = list(existing)
    else:
        return config, False

    if directory in dirs:
        return config, False
    config["skills"] = {**skills, "external_dirs": [*dirs, directory]}
    return config, True


def apply(home: Path, directory: Path = SKILLS_DIR) -> str:
    """Register `directory` in `<home>/config.yaml`. Never raises.

    A box that cannot register a skill must still boot: the cost is an agent
    that has to find `flotta-repos` the long way, not one nobody can reach.
    """
    import yaml

    if not directory.is_dir():
        return f"skills left alone: {directory} is not in this image"

    path = home / "config.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, yaml.YAMLError) as exc:
        return f"skills left alone: could not read {path}: {exc}"
    if loaded is not None and not isinstance(loaded, dict):
        return f"skills left alone: {path} is not a mapping"

    settled, changed = settle_external_dir(loaded, str(directory))
    if not changed:
        return f"skills: {directory} already registered"
    try:
        path.write_text(yaml.safe_dump(settled, sort_keys=False), encoding="utf-8")
    except OSError as exc:
        return f"skills left alone: could not write {path}: {exc}"
    return f"skills: registered {directory} in {path}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    home = Path(args[0]) if args else Path("/data/hermes")
    print(f"[flotta-box] {apply(home)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
