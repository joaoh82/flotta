"""Fly.io box configuration — the substrate that can actually keep a disk (M2).

Modal cannot stop and resume a container, so under it a box is disposable and
``HERMES_HOME`` is ephemeral. That single default deletes the reason to run
Hermes rather than a bare model call: memory, learned skills and accumulated
context all live under ``HERMES_HOME`` and all die with the container.

This module holds the configuration for the first substrate that does not have
that problem: a Fly Machine with a persistent volume mounted at ``/data``, with
``HERMES_HOME`` pointed inside it.

**No `Backend` protocol yet, deliberately.** M1 defines that interface, and the
pivot doc (§7) puts M2 first on purpose: proving durable memory on one
hand-provisioned machine is days of work, and designing an abstraction over a
capability nobody has demonstrated is how you get the wrong abstraction. This
module is plain configuration + the durable-path contract; `FlyBackend` comes
later and will consume it.

**Everything is configurable, nothing is guessed.** `flyctl` acts on whichever
org happens to be current, exactly the way `modal` acts on whichever profile is
active — and this repo already learned that lesson once, when the globally
active Modal profile was found pointing at an unrelated workspace. So the org
is pinned explicitly (`$FLOTTA_FLY_ORG`, then `.env`, then `personal`) and
`just fly-whoami` prints the resolved target before any recipe touches Fly.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

# --- defaults --------------------------------------------------------------

DEFAULT_ORG = "personal"
"""Every Fly account has a `personal` org; it is the only safe default."""

DEFAULT_APP = "flotta-box"
"""Fly app names are **globally unique across all of Fly**, not per-org, so
this default will collide for anyone who is not first. `$FLOTTA_FLY_APP`
overrides it, and `just fly-up` says so when creation fails."""

FALLBACK_REGION = "ams"
"""Used only when the nearest region cannot be detected.

Not "let Fly choose": `fly volumes create` **requires** an explicit region when
it is not attached to a TTY, so an unresolved region is a hard failure at
provision time rather than a convenient default. Detection is attempted first
(`detect_region`); this is the last resort so a recipe never dies on a
network hiccup."""

DEFAULT_VOLUME_NAME = "flotta_data"
"""Volume names are per-app and must be a valid identifier — no dashes."""

DEFAULT_VOLUME_GB = 1
DEFAULT_MOUNT_PATH = "/data"

# Matches fly/fly.toml. `create` boots a machine directly rather than through a
# deploy, so the size has to be named somewhere other than that file.
DEFAULT_VM_SIZE = "shared-cpu-1x"
DEFAULT_VM_MEMORY_MB = 1024

# The whole point of the milestone. `HERMES_HOME` relocates Hermes's *entire*
# store atomically — db, sessions, memories, skills (SEAM_NOTES Q3) — so
# putting it inside the mounted volume is the one change that makes a box able
# to remember anything.
DEFAULT_HERMES_HOME = f"{DEFAULT_MOUNT_PATH}/hermes"

ORG_ENV = "FLOTTA_FLY_ORG"
APP_ENV = "FLOTTA_FLY_APP"
REGION_ENV = "FLOTTA_FLY_REGION"
VOLUME_NAME_ENV = "FLOTTA_FLY_VOLUME_NAME"
VOLUME_GB_ENV = "FLOTTA_FLY_VOLUME_GB"
VM_SIZE_ENV = "FLOTTA_FLY_VM_SIZE"
VM_MEMORY_ENV = "FLOTTA_FLY_VM_MEMORY_MB"

DEFAULT_DOTENV = ".env"


# --- the durable-path contract ---------------------------------------------


@dataclass(frozen=True, slots=True)
class DurablePath:
    """One thing that must survive a stop/start cycle."""

    relative: str
    what: str
    #: A directory is created empty by Hermes and may legitimately stay empty
    #: until something writes to it; a file's absence is always a failure.
    is_dir: bool


# Sourced from SEAM_NOTES Q3, which traced Hermes's storage layout to file:line.
# This is the acceptance surface for M2: "memory survives" is four distinct
# claims, and asserting only the first would let the interesting ones rot.
DURABLE_PATHS: tuple[DurablePath, ...] = (
    # Measured, not assumed: after a completed headless turn on a live box,
    # state.db holds one table (`async_delegations`, zero rows). SEAM_NOTES Q3
    # describes the full sessions/messages/FTS schema, but that is written by
    # the gateway/CLI path — so this file surviving is a *file* claim today,
    # not a "your chat history is safe" claim. It becomes the latter in M3.
    DurablePath("state.db", "Hermes's state database (see note above)", is_dir=False),
    DurablePath("memories", "markdown memory written by the memory tool", is_dir=True),
    DurablePath("skills", "learned skills — the self-improvement surface", is_dir=True),
    DurablePath("sessions", "per-session transcripts", is_dir=True),
)


def durable_paths(hermes_home: str = DEFAULT_HERMES_HOME) -> tuple[str, ...]:
    """Absolute paths that must survive a stop/start cycle."""
    return tuple(f"{hermes_home.rstrip('/')}/{p.relative}" for p in DURABLE_PATHS)


# --- region detection ------------------------------------------------------


def detect_region(fetch: Callable[[], str] | None = None) -> str | None:
    """The Fly edge region nearest this machine, or None if it cannot be found.

    Every Fly edge echoes the region that served a request in a ``Fly-Region``
    header, so one request to a Fly-hosted host answers "where is nearest?"
    without a token, an API client, or a hardcoded guess.

    Total by design: a box that cannot be placed optimally is a much smaller
    problem than a provisioning recipe that dies because DNS was slow, so every
    failure yields None and the caller falls back.
    """
    if fetch is None:  # pragma: no cover - exercised live, not in tests

        def fetch() -> str:
            import urllib.request

            with urllib.request.urlopen("https://debug.fly.dev", timeout=10) as response:
                return response.read().decode("utf-8", "replace")

    try:
        body = fetch()
    except Exception:
        return None

    for line in body.splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == "fly-region":
            return _clean(value) or None
    return None


# --- configuration ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FlyConfig:
    """Immutable snapshot of which Fly target the recipes act on."""

    org: str
    app: str
    region: str | None
    volume_name: str
    volume_gb: int
    mount_path: str
    hermes_home: str
    vm_size: str
    vm_memory_mb: int

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        dotenv: str | Path = DEFAULT_DOTENV,
    ) -> FlyConfig:
        env = os.environ if env is None else env

        def pick(name: str) -> str | None:
            return _clean(env.get(name)) or read_dotenv_value(name, dotenv)

        volume_gb_raw = pick(VOLUME_GB_ENV)
        if volume_gb_raw is None:
            volume_gb = DEFAULT_VOLUME_GB
        else:
            try:
                volume_gb = int(volume_gb_raw)
            except ValueError as exc:
                raise ValueError(
                    f"{VOLUME_GB_ENV} must be an integer, got {volume_gb_raw!r}"
                ) from exc
            if volume_gb < 1:
                raise ValueError(f"{VOLUME_GB_ENV} must be >= 1, got {volume_gb}")

        vm_memory_raw = pick(VM_MEMORY_ENV)
        vm_memory_mb = int(vm_memory_raw) if vm_memory_raw else DEFAULT_VM_MEMORY_MB

        mount_path = DEFAULT_MOUNT_PATH
        return cls(
            org=pick(ORG_ENV) or DEFAULT_ORG,
            app=pick(APP_ENV) or DEFAULT_APP,
            # None here means "not configured", NOT "let Fly decide" — Fly
            # does not get a say, because `fly volumes create` refuses to run
            # without an explicit region off a TTY. `resolved_region()` turns
            # this into a concrete one.
            region=pick(REGION_ENV),
            volume_name=pick(VOLUME_NAME_ENV) or DEFAULT_VOLUME_NAME,
            volume_gb=volume_gb,
            mount_path=mount_path,
            hermes_home=f"{mount_path}/hermes",
            vm_size=pick(VM_SIZE_ENV) or DEFAULT_VM_SIZE,
            vm_memory_mb=vm_memory_mb,
        )

    @property
    def durable_paths(self) -> tuple[str, ...]:
        return durable_paths(self.hermes_home)

    def resolved_region(self, fetch: Callable[[], str] | None = None) -> str:
        """A concrete region, always. Configured, else detected, else fallback.

        `fly volumes create` refuses to run without one when it is not
        attached to a TTY, so "unset" cannot mean "decide later" — something
        has to name a region before the volume exists.
        """
        return self.region or detect_region(fetch) or FALLBACK_REGION

    def describe(self) -> str:
        """One block naming exactly what the next Fly command will act on.

        Printed by `just fly-whoami`, which gates every Fly recipe for the same
        reason `just modal-whoami` gates the Modal ones: `flyctl` acts on
        whatever org is current, and finding out afterwards is expensive.
        """
        region = self.region or "(unset — detected at provision time)"
        return "\n".join(
            (
                "Fly target for flotta recipes:",
                f"  org        {self.org}",
                f"  app        {self.app}",
                f"  region     {region}",
                f"  volume     {self.volume_name} ({self.volume_gb}GB) at {self.mount_path}",
                f"  HERMES_HOME {self.hermes_home}",
            )
        )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    return v or None


def read_dotenv_value(key: str, path: str | Path = DEFAULT_DOTENV) -> str | None:
    """Read one key from a dotenv file, or None if absent/unreadable.

    Duplicated from `cli.read_dotenv_value` rather than imported: `cli` pulls in
    typer and (transitively, via `_provision`) modal, and this module is read by
    the Fly scripts, which must not need either.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.removeprefix("export ").strip()
        if name != key:
            continue
        value = value.strip().split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value or None
    return None
