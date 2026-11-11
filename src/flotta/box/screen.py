"""Whether this agent has a screen: Hermes's Bot Desktop on, or the tools off.

Runs on the box, from `box_entrypoint.sh`, before `hermes serve` starts, as
`python -m flotta.box.screen "$HERMES_HOME"`. The switch is `FLOTTA_SCREEN`,
which the control plane sets per agent (FLOTTA-208): `1` is a screen, and
anything else, including unset, is none.

## Why the off state has work to do

The box image carries the whole desktop (FLOTTA-207): Xvnc and Xfce, Chromium,
agent-browser and cua-driver. Hermes offers a tool whenever its availability
check passes, and with those binaries present `computer_use` and every
`browser_*` tool pass it, on every agent. Before that image no agent had a
browser. So an agent without a screen has both toolsets put in
`agent.disabled_toolsets`, `browser.headed: false` and
`bot_desktop.auto_start: false`. That is the state of every agent that exists,
and of every agent created without a screen.

## On

`bot_desktop.auto_start: true`, `bot_desktop.placement: gateway` and
`browser.headed: true`, which Screen.1 measured on a staging box: the first
browser call starts the screen, and Chromium draws on it. The toolsets this
module disabled are enabled again. The browser profile stays where Hermes puts
it, `$HERMES_HOME/bot-desktop/browser-profile` on the volume, and the image's
`AGENT_BROWSER_ARGS` keeps Chromium's cache on the rootfs; `flotta.box.doctor`
warns when the volume fills.

## What it will not overwrite

`config.yaml` belongs to the agent and to whoever configures it, so this module
remembers what it wrote, in `MARKER` beside the config. A key it may set is one
that is absent, still at Hermes's shipped default, or still at the value this
module last wrote. Anything else was somebody's choice and is left alone. The
same goes for `disabled_toolsets`: this module removes only the entries it
added, so a toolset an operator disabled stays disabled when a screen is turned
on.

The one case this gets wrong is approvals' case: somebody who set a key to
exactly Hermes's default on purpose. Hermes may write the file back in full,
defaults included, and a default written by Hermes cannot be told apart from one
typed by a person.

An agent has root on its own box, so none of this is a boundary against the
agent. It is the switch, and it never raises: a box that cannot tune its screen
must still boot.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

#: Where this module records what it wrote, under `$HERMES_HOME`.
MARKER = ".flotta-screen.json"

#: The env var the control plane sets. `ON_VALUES` is a screen; anything else is none.
ENV_KEY = "FLOTTA_SCREEN"
ON_VALUES = frozenset({"1", "true", "on", "yes"})

#: The toolsets an agent without a screen must not be offered (Hermes's
#: `toolsets.py`: `computer_use`, and `browser` for every `browser_*` tool).
SCREEN_TOOLSETS: tuple[str, ...] = ("computer_use", "browser")

#: Hermes's shipped defaults for the keys this module writes
#: (`hermes_cli/config_defaults.py`, v0.22.5). A value still equal to one was not
#: chosen by anyone.
HERMES_DEFAULTS: dict[str, Any] = {
    "bot_desktop.auto_start": False,
    "bot_desktop.placement": "auto",
    "browser.headed": False,
}

#: What a screen is, and what no screen is. A key in `ON` and not in `OFF`
#: (the placement) is taken back out when the screen goes, if it is still ours.
ON: dict[str, Any] = {
    "bot_desktop.auto_start": True,
    "bot_desktop.placement": "gateway",
    "browser.headed": True,
}
OFF: dict[str, Any] = {
    "bot_desktop.auto_start": False,
    "browser.headed": False,
}

_MISSING = object()


def wanted(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    """Whether this agent is to have a screen, from `FLOTTA_SCREEN`."""
    source = os.environ if env is None else env
    return (source.get(ENV_KEY) or "").strip().lower() in ON_VALUES


def _get(config: dict[str, Any], dotted: str) -> Any:
    section, key = dotted.split(".", 1)
    block = config.get(section)
    if not isinstance(block, dict):
        return _MISSING
    return block.get(key, _MISSING)


def _block(config: dict[str, Any], section: str) -> dict[str, Any] | None:
    """`config[section]` as a dict to write into, or None if it is something else."""
    block = config.get(section)
    if block is None:
        block = {}
        config[section] = block
    return block if isinstance(block, dict) else None


def _toolsets(raw: Any) -> list[str] | None:
    """`agent.disabled_toolsets` as a list, the way Hermes reads it (a list or
    a comma-separated string); None for anything else, which is left alone."""
    if raw is None or raw is _MISSING:
        return []
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",") if p.strip()]
    if isinstance(raw, list) and all(isinstance(p, str) for p in raw):
        return list(raw)
    return None


def settle(
    config: dict[str, Any] | None, marker: dict[str, Any] | None, on: bool
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """The config and the marker with the screen `on` or off, and what changed.

    Pure: no file, no YAML, so everything that decides what gets written is
    tested against plain dictionaries. `marker` is what this module wrote last
    time: `{"values": {dotted key: value}, "disabled": [toolset, ...]}`.
    """
    config = {k: (dict(v) if isinstance(v, dict) else v) for k, v in (config or {}).items()}
    ours: dict[str, Any] = dict((marker or {}).get("values") or {})
    added: list[str] = [t for t in (marker or {}).get("disabled") or [] if isinstance(t, str)]
    notes: list[str] = []

    def free(dotted: str, current: Any) -> bool:
        return (
            current is _MISSING
            or current == HERMES_DEFAULTS.get(dotted, _MISSING)
            or (dotted in ours and current == ours[dotted])
        )

    target = ON if on else OFF
    for dotted, value in target.items():
        current = _get(config, dotted)
        if not free(dotted, current):
            # Somebody chose it after (or without) this module: theirs now.
            ours.pop(dotted, None)
            notes.append(f"{dotted} left at {current!r}: somebody chose it")
            continue
        section, key = dotted.split(".", 1)
        block = _block(config, section)
        if block is None:
            notes.append(f"{dotted} left alone: {section} is not a mapping")
            continue
        if current != value:
            block[key] = value
            notes.append(f"{dotted}: {value!r}")
        ours[dotted] = value

    # A key a screen needs and no screen does not: back out, if still ours.
    for dotted in [k for k in ON if k not in target]:
        current = _get(config, dotted)
        if dotted in ours and current is not _MISSING and current == ours[dotted]:
            section, key = dotted.split(".", 1)
            del config[section][key]
            notes.append(f"{dotted} removed")
        ours.pop(dotted, None)

    agent = config.get("agent")
    listed = _toolsets(_get(config, "agent.disabled_toolsets"))
    if listed is None or (agent is not None and not isinstance(agent, dict)):
        notes.append("agent.disabled_toolsets left alone: not a list")
    else:
        before = list(listed)
        if on:
            listed = [t for t in listed if t not in added]
            kept = [t for t in SCREEN_TOOLSETS if t in listed]
            if kept:
                notes.append(f"{', '.join(kept)} still disabled: somebody chose it")
            added = []
        else:
            for name in SCREEN_TOOLSETS:
                if name in listed:
                    continue
                if name in added:
                    # This module put it there and somebody has taken it out:
                    # their choice, kept until a screen comes and goes again.
                    notes.append(f"{name} left enabled: somebody chose it")
                    continue
                listed.append(name)
                added.append(name)
            # Kept even for an entry somebody took out, which is how the next
            # boot knows not to put it back.
            added = list(dict.fromkeys(added))
        if listed != before:
            block = _block(config, "agent")
            assert block is not None  # checked above
            block["disabled_toolsets"] = listed
            notes.append(f"agent.disabled_toolsets: {listed}")

    return config, {"values": ours, "disabled": added}, notes


def apply(home: Path, env: dict[str, str] | None = None) -> str:
    """Write the screen switch into `<home>/config.yaml`. A line for the boot log.

    Never raises: an agent that cannot tune its screen must still boot.
    """
    try:
        return _apply(home, env)
    except Exception as exc:  # noqa: BLE001 - the boot must go on
        return f"screen left alone: {type(exc).__name__}: {exc}"


def _apply(home: Path, env: dict[str, str] | None) -> str:
    import yaml

    on = wanted(env)
    path = home / "config.yaml"
    marker_path = home / MARKER
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, yaml.YAMLError) as exc:
        return f"screen left alone: could not read {path}: {exc}"
    if loaded is not None and not isinstance(loaded, dict):
        return f"screen left alone: {path} is not a mapping"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(marker, dict):
            marker = {}
    except (OSError, ValueError):
        marker = {}

    settled, new_marker, notes = settle(loaded, marker, on)
    state = "on" if on else "off"
    changed = settled != (loaded or {})
    try:
        # The marker first: if the config write then fails, the marker claims
        # values that are not there, and a value that is not there is free
        # anyway. The other order would leave this module's own values looking
        # like somebody's choice, and the switch stuck.
        if new_marker != marker:
            marker_path.write_text(json.dumps(new_marker, indent=2) + "\n", encoding="utf-8")
        if changed:
            path.write_text(yaml.safe_dump(settled, sort_keys=False), encoding="utf-8")
    except OSError as exc:
        return f"screen {state}, left alone: could not write: {exc}"
    detail = "; ".join(notes) if notes else "already so"
    return f"screen {state}: {detail}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    home = Path(args[0]) if args else Path("/data/hermes")
    print(f"[flotta-box] {apply(home)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
