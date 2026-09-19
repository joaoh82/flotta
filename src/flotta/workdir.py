"""Where an agent clones projects, and the convention it is told about them.

`$FLOTTA_WORKDIR` is already what `box_entrypoint.sh` creates on boot, defaulting
to `/workspace`. This module is the other end of that variable: the setting a
person changes in the app, the value that travels in `BoxSpec.env` at create,
and the paragraph that lands in standing instructions so the path is not a
secret the agent has to guess.

## Why this is not `/data`

The memory volume is ~1 GB and *is* the agent. A clone or a `node_modules`
there fills it and takes the memories with it. The projects folder stays on
the rootfs. That split is load-bearing; this module refuses to catalogue a
path on `/data`.

## Why clones are lost on update (FLOTTA-56 / D15)

An image update is `machine update --image`. That swaps the rootfs, so
everything in the projects folder vanishes. Option 1, recorded as D15: accept
the loss, tell the agent to clone if the folder is empty, and say so in the
Info panel. A second volume is the designed home for multi-day uncommitted
work (M6's workspace tier) and is not this ticket.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The entrypoint's default, the image's `ENV`, and the catalogue's default.
#: One name, because a drift between them is a box whose setting does nothing.
DEFAULT_WORKDIR = "/workspace"

ENV_KEY = "FLOTTA_WORKDIR"


def validate_workdir(value: str) -> str:
    """A cleaned absolute path, or a refusal.

    Empty means "no override" — the same as every other setting. A path on
    `/data` is refused rather than stored, because storing it would be a way
    to point every new agent's builds at the memory volume.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if "\x00" in text:
        raise ValueError("Projects folder cannot contain a NUL byte")
    if not text.startswith("/") or text.startswith("//"):
        raise ValueError("Projects folder must be an absolute path, like /workspace")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    parts = text.split("/")
    # `"/workspace".split("/")` is `["", "workspace"]` — the leading empty is
    # the root. Anything else empty is `//` in the middle.
    if any(part == "" for part in parts[1:]):
        raise ValueError("Projects folder cannot contain empty path segments")
    if any(part in (".", "..") for part in parts[1:]):
        raise ValueError("Projects folder cannot contain . or ..")
    if text == "/data" or text.startswith("/data/"):
        raise ValueError(
            "Projects folder cannot be on the memory volume (/data). "
            "A clone or node_modules there would fill it and take the agent's "
            "memory with it."
        )
    if text == "/":
        raise ValueError("Projects folder cannot be the filesystem root")
    return text


def resolve_workdir(store: Any, env: Mapping[str, str] | None = None) -> str:
    """What a new agent is created with: stored setting, else environment, else default.

    An unreadable stored value falls back rather than refusing the create —
    the same rule as `resolve_box_resources`. A bad number in the volume field
    must not cost someone an agent, and neither must a hand-edited path.
    """
    if env is None:
        from flotta.settings import layered

        source: Mapping[str, str] = layered(store)
    else:
        source = env
    raw = (source.get(ENV_KEY) or "").strip()
    if not raw:
        return DEFAULT_WORKDIR
    try:
        return validate_workdir(raw) or DEFAULT_WORKDIR
    except ValueError:
        return DEFAULT_WORKDIR


def workdir_from_env(env: Mapping[str, str]) -> str:
    """What is in force on a box: the env var, else the entrypoint default."""
    raw = (env.get(ENV_KEY) or "").strip()
    if not raw:
        return DEFAULT_WORKDIR
    try:
        return validate_workdir(raw) or DEFAULT_WORKDIR
    except ValueError:
        return DEFAULT_WORKDIR


def projects_convention(workdir: str) -> str:
    """The standing-instruction paragraph that makes a path a projects folder.

    A path the agent does not know about is just a directory. This is what it
    is told: check before cloning, sync `main` first, and re-clone after an
    update without being asked — because the folder does not survive one.
    """
    return (
        f"Your projects live in {workdir}. Before starting work on a repository, "
        f"check whether it is already cloned there. If it is, fetch and sync "
        f"`main` (or `master`) first, then do the work on a branch. If it is not, "
        f"clone it there. {workdir} is on the machine's disk, not the memory "
        f"volume at /data, and is wiped when the agent is updated — re-clone "
        f"without asking. Never put clones or build output on /data."
    )


def with_projects_convention(
    instructions: str | None, workdir: str, *, max_len: int | None = None
) -> str | None:
    """User-written instructions with the projects convention appended.

    Returns `None` when there is nothing to seed: an empty field still means
    "Hermes's default identity", not "replace that identity with a path". The
    skill (`flotta-projects`) tells an agent that has no SOUL.md. Appending
    here is for the agent that *does* have standing instructions, so the
    convention is in the same file as the rest of what it was told.

    If the merged text would not fit, the user's words win and the skill
    carries the convention. Truncating their persona to make room for ours
    would be the worse lie.
    """
    text = (instructions or "").strip() or None
    if text is None:
        return None
    if _already_has_convention(text, workdir):
        return text
    merged = f"{text}\n\n{projects_convention(workdir)}"
    if max_len is not None and len(merged) > max_len:
        return text
    return merged


def _already_has_convention(text: str, workdir: str) -> bool:
    lower = text.lower()
    return workdir in text and "projects live in" in lower
