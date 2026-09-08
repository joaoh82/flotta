"""Fleet settings a person can change, and where they win over the environment.

**Flotta is used through the desktop app.** How long an agent stays awake, how
often the loop sweeps, what a container-second costs — all of it was a process
environment variable, which means it could only be changed by whoever deploys
the control plane, with a restart. That is the wrong owner and the wrong place
for anything the product is meant to be configured with.

So: a stored value wins over the environment, the environment remains the
fallback, and nothing that already works stops working.

## Why the keys are environment-variable names

Because it makes the precedence *mechanical*. Every consumer already reads
`env.get("FLOTTA_IDLE_AFTER_S")` and takes an injectable mapping; handing them
`layered(store)` makes them store-aware without changing a line of their logic
or reimplementing a fallback chain six times. What a person sees in the app is
`Setting.label`; this name never reaches a screen.

## Why there is an allowlist

`layered` shadows environment lookups. Without `SETTINGS` bounding what may be
written, this table would be a way to override **any** variable the control
plane reads — `FLOTTA_SIGNING_KEY`, `FLOTTA_GITHUB_TOKEN`, `FLOTTA_BOX_PASSWORD`
— through an authenticated HTTP call. That is not a hypothetical; it is the
obvious consequence of a generic key/value settings endpoint, and the allowlist
is the whole defence.

The rule the catalogue encodes: **configuration is settable, credentials are
not.** A credential reaches the control plane the way it always has, and the
app can never read one back — which is the property that made this a desktop
app rather than a web page.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

Kind = Literal["seconds", "int", "money", "text"]

#: Names that must never appear in the catalogue. See `Setting.__post_init__`.
_CREDENTIAL_WORDS = ("KEY", "TOKEN", "PASSWORD", "SECRET", "CREDENTIAL")


@dataclass(frozen=True)
class Setting:
    """One thing a person may change, and enough to render it without guessing."""

    #: The environment variable this overrides, and its key in the store.
    key: str
    #: What the app calls it. The key is an implementation detail; this is not.
    label: str
    #: One sentence of why you would touch it, shown under the field.
    help: str
    kind: Kind
    #: What happens with nothing stored and nothing in the environment. Rendered
    #: as the placeholder, so an empty field is never a mystery.
    default: str

    def __post_init__(self) -> None:
        """Refuse to catalogue anything that looks like a credential.

        There is a test that asserts this too, and a test is the wrong place
        for it on its own: `layered` shadows environment lookups and the API
        writes whatever the catalogue admits, so one careless entry arms both
        at once. Raising here means the module will not import — the failure
        cannot be skipped, and it happens at the point of the mistake.
        """
        if any(word in self.key.upper() for word in _CREDENTIAL_WORDS):
            raise ValueError(
                f"{self.key!r} looks like a credential. Fleet settings are "
                "configuration only: `layered` shadows the environment for "
                "anything catalogued here, so admitting a secret would let it "
                "be overridden over HTTP."
            )


#: Every setting the fleet exposes. Adding one here is what makes it settable —
#: there is deliberately no way to write a key that is not in this list.
SETTINGS: tuple[Setting, ...] = (
    Setting(
        key="FLOTTA_IDLE_AFTER_S",
        label="Sleep agents after",
        help=(
            "Seconds of quiet before an agent suspends itself. It keeps its "
            "disk and its memory and wakes when addressed. 0 disables sleeping, "
            "which means paying for CPU nobody is using."
        ),
        kind="seconds",
        default="1800",
    ),
    Setting(
        key="FLOTTA_RECONCILE_INTERVAL_S",
        label="Sweep every",
        help=(
            "How often the control plane reconciles the fleet: closing stranded "
            "provisions, correcting rows that disagree with the substrate, and "
            "sleeping idle agents."
        ),
        kind="seconds",
        default="60",
    ),
)

# Deliberately **not** catalogued yet, and both for the same reason:
# `FLOTTA_MAX_CONCURRENT` and `FLOTTA_COST_PER_SECOND`.
#
# **Nothing in production creates a task.** The only `create_task(` calls in
# this package are `asyncio.create_task`; the producer arrives with the
# workspace tier (M6). So the concurrency cap gates nothing and the cost rate
# prices nothing — verified against the live fleet, which holds five agents and
# zero tasks.
#
# A field that changes nothing is the same class of lie FLOTTA-29 spent a
# milestone removing from this app, and it would be a strange one to reintroduce
# in the change whose whole premise is that the window tells the truth.
#
# The two are not equally far away, which is worth knowing when they come back:
# `resolve_cost_rate` is already called by `watch_task` and `reconcile`, so the
# rate starts working the moment tasks exist. `resolve_max_concurrent` has no
# caller at all and needs the gate built. Add each here in the change that gives
# it something to do.
#
# One consequence: only `seconds` is catalogued today, so `validate`'s `int` and
# `money` branches are unreachable until one of these returns. They are the
# validation vocabulary rather than product surface, so they stay — an unused
# enum member misleads nobody, which is exactly what separates it from a field
# in a window.
BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}


class UnknownSettingError(ValueError):
    """Raised for a key that is not in the catalogue.

    Its own type because the API answers it with a 422 rather than a 500, and
    because "you may not set that" is a different sentence from "that value is
    not a number".
    """


def validate(key: str, value: str) -> str:
    """A setting's value, cleaned, or a refusal explaining why it cannot be one.

    Checked here rather than only where it is consumed, because a value that
    cannot be parsed is stored *once* and then breaks the fleet on every read
    afterwards — including, for the sweep interval, at control-plane startup,
    where the failure looks like a crash loop rather than a bad number.
    """
    setting = BY_KEY.get(key)
    if setting is None:
        raise UnknownSettingError(
            f"{key!r} is not a fleet setting. Settable: {', '.join(sorted(BY_KEY))}"
        )

    text = (value or "").strip()
    if not text:
        # Empty means "no override" everywhere, which is how a field is
        # cleared. `clear_setting` is what the caller should reach for, but a
        # blank submitted from a form has to mean the same thing.
        return ""

    if setting.kind in ("seconds", "money"):
        try:
            number = float(text)
        except ValueError as exc:
            raise ValueError(f"{setting.label} must be a number, got {text!r}") from exc
        # `float("inf")` and `float("nan")` parse, and neither is `< 0`.
        # Stored as the sweep interval, `inf` becomes `await sleep(inf)` and the
        # reconcile loop never ticks again — and clearing the field afterwards
        # cannot wake a sleep that never ends, so the fleet stops reconciling
        # until someone restarts the process. `nan` compares false against
        # everything and silently becomes the floor, i.e. a busy loop.
        if not math.isfinite(number):
            raise ValueError(f"{setting.label} must be a real number, got {text!r}")
        if number < 0:
            raise ValueError(f"{setting.label} cannot be negative, got {text!r}")
    elif setting.kind == "int":
        try:
            number = int(text)
        except ValueError as exc:
            raise ValueError(f"{setting.label} must be a whole number, got {text!r}") from exc
        if number < 0:
            raise ValueError(f"{setting.label} cannot be negative, got {text!r}")
    return text


class _Layered(Mapping[str, str]):
    """The environment, with stored settings in front of it.

    A `Mapping` rather than a merged `dict` so the environment is read at
    lookup time: the control plane is long-running, and a snapshot taken at
    startup would go stale in exactly the situation this exists to serve.

    Only catalogue keys are shadowed. A lookup for anything else — a signing
    key, a provider credential, a Fly token — goes straight to the environment,
    whatever the table happens to contain. That is belt *and* braces: the API
    already refuses to write those keys, and this refuses to read them back
    even if a row appeared some other way.
    """

    def __init__(self, stored: Mapping[str, str], env: Mapping[str, str]) -> None:
        self._stored = {k: v for k, v in stored.items() if k in BY_KEY and v.strip()}
        self._env = env

    def __getitem__(self, key: str) -> str:
        if key in self._stored:
            return self._stored[key]
        return self._env[key]

    def __iter__(self) -> Any:
        seen = set(self._stored)
        yield from self._stored
        for key in self._env:
            if key not in seen:
                yield key

    def __len__(self) -> int:
        return len({*self._stored, *self._env})

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<layered settings: {sorted(self._stored)} over environment>"


def layered(store: Any, env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """What every consumer should read instead of `os.environ`.

    Pass it as the `env` argument that `resolve_idle_after`, `resolve_cost_rate`
    and `resolve_max_concurrent` already accept, or to `FlyConfig.from_env`.
    """
    return _Layered(store.all_settings(), os.environ if env is None else env)


def describe(store: Any, env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """The catalogue with current values, for the app to render.

    Each entry says where its value came from — `store`, `env` or `default` —
    because "why is my fleet not using the number I set" is otherwise
    unanswerable from the outside, and the answer is usually that a deployment
    variable is still in play.
    """
    source_env = os.environ if env is None else env
    stored = store.all_settings()
    out: list[dict[str, Any]] = []
    for setting in SETTINGS:
        stored_value = (stored.get(setting.key) or "").strip()
        env_value = (source_env.get(setting.key) or "").strip()
        if stored_value:
            value, source = stored_value, "store"
        elif env_value:
            value, source = env_value, "env"
        else:
            value, source = setting.default, "default"
        out.append(
            {
                "key": setting.key,
                "label": setting.label,
                "help": setting.help,
                "kind": setting.kind,
                "default": setting.default,
                "value": value,
                "source": source,
            }
        )
    return out
