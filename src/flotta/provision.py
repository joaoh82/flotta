"""Provisioning — create boxes, watch their tasks, tear them down.

**Where each half runs (OQ2 / decision D10).** Two reasons pick a watcher over
self-reporting, and the second is the one that lasts:

1. Under D8 the v0.1 store is a plain local SQLite file, which a container
   cannot reach. This is a consequence of that deferral, *not* of the design —
   D3 still points at Turso, and a Turso Cloud store would be reachable from a
   container, so this reason expires when Turso lands.
2. A machine that dies mid-task — OOM, preemption, container kill — writes
   nothing at all. A task that owned its own status would strand in
   ``running`` forever. The watcher owns the verdict precisely because it
   outlives the machine, and that stays true under Turso.

So the module splits in two:

- ``run_worker`` runs **inside Modal**. It does the work and touches no store.
  This is the only piece `modal deploy` publishes, and it is deliberately
  **not** renamed: it is Tier 3, the stateless one-shot, and calling it a box
  would be wrong in the other direction. Its successor name is `run_shard`,
  which belongs to M8, not here.
- ``spawn_box`` / ``watch_task`` / ``stop_box`` / ``start_box`` /
  ``teardown_box`` run **locally**, next to the store file, and are its only
  writers. The CLI (M4) and the dashboard (M5) call these.

**The pivot, as it lands here.** v0.1's `spawn_worker` created one row that was
simultaneously a machine and a task. It is now two: a **box** that owns the
endpoint and the machine lifecycle, and a **task** that owns the verdict. Under
Modal the box is still disposable — Modal cannot stop and resume a container —
so `stop_box`/`start_box` are store-side only until M1's `Backend` protocol
gives them something real to call. That asymmetry is the point: it documents in
code why Modal cannot be the primary substrate.

**Lifecycle and the events it writes.**

    spawn_box()      box:  provisioning ──running──> running   (+ endpoint)
                     task: pending ──spawned──> running
    watch_task()     task: running ──completed──> done
                     task: running ──failed/timed_out──> failed
    stop_box()       box:  running ──stopped──> stopped        (idempotent)
    start_box()      box:  stopped ──running──> running        (idempotent)
    teardown_box()   box:  any ──torn_down──> torn_down        (idempotent)
                     its live tasks ──> failed

The recorded ``endpoint`` is the Modal function-call handle
(``modal://flotta-provision/run_worker/<fc_id>``), not an HTTP URL: under this
backend a box is one-shot, so the call id *is* the address you can later
re-attach to, cancel, or fetch results from. When M1 lands a persistent
backend, this column becomes a real hostname without changing the schema.

Import discipline matches `worker/server.py`: `modal` is imported lazily inside
the adapter functions, so the pure store-writing logic here is unit-testable
with fakes and the base `flotta` package keeps no hard Modal dependency.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import secrets as secrets_module
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from flotta import budgets as budget_rules
from flotta.auth import DEFAULT_CLIENT, DEFAULT_ORG, box_password
from flotta.backend import (
    Backend,
    BackendError,
    BoxSpec,
    NotSupported,
    SizeRefused,
    UnknownBackendError,
    check_memory_mb,
    default_scheme,
)
from flotta.backend import backend_for as _backend_for
from flotta.backend import pause as _pause
from flotta.github import GITHUB_TOKEN_ENV
from flotta.rules import Rules, config_changes, rules_from_json
from flotta.runs import RUN_DEADLINE_S
from flotta.store import (
    INSTRUCTIONS_CHANGED,
    Box,
    FleetStore,
    InvalidTransitionError,
    Task,
    UnknownEntityError,
    box_label,
    is_terminal,
    reaches_fleet_github,
    validate_box_meta,
    validate_box_name,
    validate_tenant_id,
)

_log = logging.getLogger("flotta.provision")

# How long a task may run before it is considered overdue. Lived in
# `worker/config.py` — a container's watchdog — until the shard tier was cut.
# It is a *task* deadline, so it belongs beside the task machinery.
DEFAULT_TIMEOUT_S = 900

# The ceiling `timeout_s` may be raised to. Was Modal's per-function hard cap;
# kept as a sanity bound on a deadline nobody should set to a week.
MAX_TIMEOUT_S = 3600

# Live-task cap. v0.1 capped *workers*, when a worker was both the machine and
# the work. Box existence is uncapped now — the whole fleet arithmetic assumes
# tens of them — so the guard moved to the thing that actually burns CPU: a
# task in a live state. Default 1 keeps the money behaviour v0.1 had; raising
# it is an explicit opt-in to territory nothing has tested. 0 means unlimited,
# for anyone who means it.
DEFAULT_MAX_CONCURRENT = 1
MAX_CONCURRENT_ENV = "FLOTTA_MAX_CONCURRENT"

# Cost estimation (OQ3, decided in M7.3). Modal's billing API was investigated
# and **cannot** attribute cost to a single task: `Workspace.billing.report()`
# returns line items keyed by *App* id at daily or hourly resolution, every
# task shares the one `flotta-provision` app, and neither `Function.spawn`
# nor `with_options` accepts a per-call tag. A 12-second task is simply not
# separable from that.
#
# So the estimate is `wall-clock seconds × a rate the operator sets themselves`
# (see `billable_seconds` — *not* the task's `duration_s`, which omits image
# pull and container boot and prices a dry run at zero). Unset
# by default, in which case `cost_estimate` stays NULL and every surface renders
# an em dash — a blank is better than a number derived from a rate nobody chose.
# It covers **container time only**; model tokens are a separate bill Flotta
# never sees.
COST_PER_SECOND_ENV = "FLOTTA_COST_PER_SECOND"


def _default_backend() -> Backend:
    """Where a new box goes: `$FLOTTA_BACKEND`, else Fly.

    Through `_backend_for` rather than `flotta.backend.default_backend`, so the
    one seam every test patches to stand in for a substrate is still the one.
    """
    return _backend_for(f"{default_scheme()}://")


def resolve_cost_rate(
    explicit: float | None = None, env: Mapping[str, str] | None = None
) -> float | None:
    """Dollars per container-second, or None when the operator has not set one.

    `env` is a mapping rather than the process environment so a caller with a
    store can pass `settings.layered(store)` — which is how a value set in the
    app wins over one set at deploy time. Every resolver here takes it for the
    same reason.
    """
    import os

    env = os.environ if env is None else env

    if explicit is None:
        raw = (env.get(COST_PER_SECOND_ENV) or "").strip()
        if not raw:
            return None
        try:
            explicit = float(raw)
        except ValueError as exc:
            raise ValueError(f"{COST_PER_SECOND_ENV} must be a number, got {raw!r}") from exc

    if not math.isfinite(explicit):
        raise ValueError(f"{COST_PER_SECOND_ENV} must be a finite number, got {explicit}")
    if explicit < 0:
        raise ValueError(f"{COST_PER_SECOND_ENV} must be >= 0, got {explicit}")
    return explicit or None


def billable_seconds(task: Task, now: datetime | None = None) -> float | None:
    """How long the task ran, as observed from here.

    Measured on the **task**, not the box: a box spans months across many
    tasks, so wall time from its `created_at` would price a machine's whole
    life against one piece of work. This is also why `cost_estimate` lives on
    `tasks` — the formula and the column have to agree about what they mean.

    **Not** the task's `duration_s`. That measures time *inside* `run_worker`
    and excludes image pull and container boot, so it understates what Modal
    bills — a dry run reports `0.0` while its container demonstrably ran. Wall
    time from `started_at` is the better proxy: it spans launch to verdict.

    It errs slightly high, because it also includes the local round-trip, and
    that is the direction to err in for a cost estimate.
    """
    started = _parse_ts(task.started_at)
    if started is None:
        # Never ran — either still `pending`, or failed straight out of it.
        # Time spent waiting on a stopped box is not billable: nothing was
        # running to bill. Returning None keeps the estimate blank rather than
        # charging for the wait.
        return None
    end = _parse_ts(task.finished_at) or now or datetime.now(UTC)
    return max(0.0, (end - started).total_seconds())


def estimate_cost(seconds: Any, rate: float | None) -> float | None:
    """`seconds × rate`, or None when either is missing or unusable.

    Total by design: an unusable duration yields no estimate rather than an
    exception, because failing to price a task must never fail recording it.
    """
    if rate is None or not isinstance(seconds, int | float) or isinstance(seconds, bool):
        return None
    if seconds < 0:
        return None
    return round(float(seconds) * rate, 6)


# Grace beyond a task's own timeout before `reconcile` calls it stranded.
# The container hard-exits at its timeout, so this only has to cover the lag
# between that exit and the local process noticing.
DEFAULT_GRACE_S = 60


def _settings_env(store: FleetStore) -> Mapping[str, str] | None:
    """`settings.layered(store)`, or the plain environment if that fails.

    Deliberately forgiving. These resolvers run inside the reconcile loop and
    on every create, and a settings read that raised — an old store, a database
    hiccup — would take down the sweep that keeps the fleet honest. Falling
    back to the environment is exactly the behaviour before this table existed,
    so the failure mode is "your override is ignored", not "the fleet stops".
    """
    try:
        from flotta.settings import layered

        return layered(store)
    except Exception:  # noqa: BLE001 - see above; never worth ending a sweep
        return None


def resolve_max_concurrent(
    explicit: int | None = None, env: Mapping[str, str] | None = None
) -> int | None:
    """How many tasks may be live at once: explicit -> fleet setting -> env -> default.

    Returns ``None`` for *unlimited*, which is what ``0`` requests. Anyone
    setting that has said so deliberately; the default of 1 is what protects
    everyone who has not thought about it.
    """
    import os

    env = os.environ if env is None else env

    if explicit is None:
        raw = (env.get(MAX_CONCURRENT_ENV) or "").strip()
        if raw:
            try:
                explicit = int(raw)
            except ValueError as exc:
                raise ValueError(f"{MAX_CONCURRENT_ENV} must be an integer, got {raw!r}") from exc
        else:
            explicit = DEFAULT_MAX_CONCURRENT

    if explicit < 0:
        raise ValueError(f"{MAX_CONCURRENT_ENV} must be >= 0, got {explicit}")
    return None if explicit == 0 else explicit


# Provider config reaches the container as a Modal Secret, never as a plain
# function argument (that would land in call logs).
class ProvisionError(Exception):
    """Base error for provisioning operations."""


class BoxOccupied(ProvisionError):
    """Another live box already claims the machine this one would adopt.

    A distinct type because it is the one create failure that is a *conflict*
    about fleet state rather than something going wrong — the caller can act on
    it (pick another app, tear the other box down), and an API needs to say 409
    here and something else everywhere else. Branching on an exception type
    rather than parsing a message keeps that from rotting.
    """


class BoxNotRunning(ProvisionError):
    """The machine was created but is not up. **A box exists.**

    Carries the ids because that is the whole point: the row is recorded as
    `stopped` and the machine is sitting there costing disk, so a caller that
    only learns "it failed" cannot start it *or* destroy it. That is how you
    get an orphan machine billing against an account nobody is looking at.
    """

    def __init__(self, message: str, *, box_id: str, endpoint: str | None) -> None:
        super().__init__(message)
        self.box_id = box_id
        self.endpoint = endpoint


class TaskTimeout(ProvisionError):
    """The task did not produce a result before the watch deadline.

    Adapters translate Modal's own timeout errors into this, so `watch_task`
    never has to import or catch a Modal exception type.
    """


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a store timestamp, tolerating anything unexpected."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def classify_result(result: Any) -> tuple[str, str, dict[str, Any]]:
    """Map a task result onto ``(status, event_type, payload)``.

    Pure and total: any shape of input yields a verdict, because leaving a
    task stuck in `running` because its result was malformed is strictly
    worse than recording a failure.
    """
    if not isinstance(result, dict):
        return "failed", "failed", {"error": f"malformed task result: {result!r}"}

    if result.get("completed"):
        payload = {
            "final_response": result.get("final_response"),
            "api_calls": result.get("api_calls"),
            "task_id": result.get("task_id"),
            "duration_s": result.get("duration_s"),
            "dry_run": bool(result.get("dry_run")),
        }
        return "done", "completed", payload

    error = result.get("error") or "task reported failure without an error message"
    if result.get("timed_out"):
        return "failed", "timed_out", {"error": error, "duration_s": result.get("duration_s")}
    return "failed", "failed", {"error": error, "duration_s": result.get("duration_s")}


# -- Modal adapters (lazy; swapped for fakes in tests) ----------------------

Launcher = Callable[..., str]
Waiter = Callable[[str, float | None], Any]
Canceller = Callable[[str], None]


def _resolve_backend(box: Box, backend: Backend | None) -> Backend:
    """The backend that owns this box, from its stored endpoint.

    Routing on the endpoint scheme rather than a `boxes.backend` column keeps
    one fact in one place — see `flotta.backend.backend_for`. An explicit
    `backend` argument overrides it, which is how the tests stay hermetic.
    """
    if backend is not None:
        return backend
    try:
        return _backend_for(box.endpoint)
    except UnknownBackendError as exc:
        if not box.endpoint:
            raise ProvisionError(
                f"box {box.id} has no endpoint, so there is no substrate to act on. "
                "A box that was never launched can only be torn down."
            ) from exc
        raise ProvisionError(
            f"box {box.id} lives on a substrate this build does not know how to "
            f"drive ({box.endpoint!r}): {exc}"
        ) from exc


def _require_box(store: FleetStore, box_id: str) -> Box:
    box = store.get_box(box_id)
    if box is None:
        raise UnknownEntityError(f"no box with id {box_id!r}")
    return box


def _require_task(store: FleetStore, task_id: str) -> Task:
    task = store.get_task(task_id)
    if task is None:
        raise UnknownEntityError(f"no task with id {task_id!r}")
    return task


#: A box token's default lifetime. Longer than a person's (30d) on purpose: a
#: box is unattended, so an expiry is a capability that stops working while
#: nobody is watching, and the failure surfaces days later as a clone that
#: cannot authenticate. The token is also the narrowest in the system —
#: `git:credential` only, confined to one box — so a longer life buys less
#: for an attacker than it costs in silent breakage.
#:
#: Rotation is still the answer, not a longer number: `just box-identity`.
BOX_TOKEN_TTL_S = 90 * 24 * 3600

#: How long the token an agent calls models with lasts: years, unlike its identity.
#: Nothing renews it, and when it expires the agent can no longer think. It carries
#: only `model:call`, and the gateway refuses a destroyed agent, so a long life does
#: not reach far: at worst, spend metered against the agent it names.
MODEL_TOKEN_TTL_S = 5 * 365 * 24 * 3600

GATEWAY_URL_ENV = "FLOTTA_GATEWAY_URL"

#: A move onto the gateway, or back off it (FLOTTA-90).
GATEWAY_JOINED = "gateway_joined"
GATEWAY_LEFT = "gateway_left"
GATEWAY_MOVE_FAILED = "gateway_move_failed"


#: The box's dashboard user. Constant, because the door logs in as it — the
#: username was never the secret, the password is.
BOX_AUTH_USER = "flotta"


def gateway_url(env: Mapping[str, str] | None = None) -> str | None:
    """The base URL Hermes appends `/chat/completions` to, if there is a gateway.

    **This variable is the switch.** With it unset, boxes call the provider directly,
    exactly as before. Environment only, like the endpoint: it decides where a token
    goes, so it is not a setting that can be changed over HTTP.
    """
    source = os.environ if env is None else env
    url = (source.get(GATEWAY_URL_ENV) or "").strip().rstrip("/")
    if not url:
        return None
    return url if url.endswith("/v1") else f"{url}/v1"


def gateway_secrets(
    box_id: str,
    model: str,
    *,
    env: Mapping[str, str],
    org: str | None = None,
    client: str | None = None,
) -> dict[str, str]:
    """What a box needs to call models through the gateway. Raises `AuthError`.

    Its own token where a provider's key would be, and **the fleet's key nowhere**.
    The same three names as ever, so an image from before this works too: only the
    `providers:` entry in `flotta.box.inference` needs a newer one.
    """
    from flotta.auth import SCOPE_MODEL_CALL, box_subject, mint
    from flotta.inference import is_openrouter, provider_secrets

    url = gateway_url(env)
    assert url is not None
    token = mint(
        subject=box_subject(box_id),
        scopes={SCOPE_MODEL_CALL},
        ttl_s=MODEL_TOKEN_TTL_S,
        env=dict(env),
        **_tenant_claims(org, client),
    )
    secrets = provider_secrets(model, url, token)
    if is_openrouter((env.get("FLOTTA_MODEL_BASE_URL") or "").strip()):
        secrets["FLOTTA_GATEWAY_UPSTREAM"] = "openrouter"
    return secrets


def _tenant_claims(org: str | None, client: str | None) -> dict[str, str]:
    """A token's tenancy, absent for the default org's default client (M11.5)."""
    if (org or DEFAULT_ORG, client or DEFAULT_CLIENT) == (DEFAULT_ORG, DEFAULT_CLIENT):
        return {}
    return {"org": org or DEFAULT_ORG, "client": client or DEFAULT_CLIENT}


def fleet_secrets(
    env: dict[str, str] | None = None,
    *,
    box_id: str,
    org: str | None = None,
    client: str | None = None,
) -> tuple[dict[str, str], list[str]]:
    """What every box in this fleet needs to boot, and what is missing.

    Returns `(secrets, missing)`. Distinct from `build_identity`, which is what
    makes a box *itself*: these come from the fleet's configuration, and until M8.3
    they were never injected at all — one Fly app held the whole fleet and
    `just fly-auth` / `just fly-secrets` had set them on it once, by hand.

    One app per agent removed that accident, and the first box created through
    the API crash-looped:

        box_entrypoint.sh: HERMES_DASHBOARD_BASIC_AUTH_USERNAME: set it with:
        just fly-auth
        Main child exited normally with code: 1

    **The password is the box's own, derived from the fleet's** (FLOTTA-96,
    `flotta.auth.box_password`). The door logs into every box on the caller's
    behalf, so a password generated at random per box would lock it out of the
    agent it just created; the fleet's own on every box let any agent log in to
    any other. Derived is both: the door works it out, and it opens one box.
    `box_id` is required so that no caller can forget it and hand out the
    fleet's. The session *secret* is per-app state and is generated fresh,
    because nothing else needs to know it.
    """
    source = os.environ if env is None else env
    secrets: dict[str, str] = {}
    missing: list[str] = []

    password = (source.get("FLOTTA_BOX_PASSWORD") or "").strip()
    if password:
        secrets["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"] = BOX_AUTH_USER
        secrets["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] = box_password(password, box_id)
        secrets["HERMES_DASHBOARD_BASIC_AUTH_SECRET"] = secrets_module.token_urlsafe(48)
    else:
        # Without it the entrypoint refuses to serve and the machine restarts
        # forever, which is a far worse failure than being told now.
        missing.append("FLOTTA_BOX_PASSWORD")

    model = (source.get("FLOTTA_MODEL") or "").strip()
    base_url = (source.get("FLOTTA_MODEL_BASE_URL") or "").strip()
    api_key = (source.get("FLOTTA_API_KEY") or "").strip()
    if gateway_url(source):
        from flotta.auth import AuthError

        if not model:
            missing.append("FLOTTA_MODEL")
            return secrets, missing
        try:
            secrets.update(gateway_secrets(box_id, model, env=source, org=org, client=client))
        except AuthError:
            # Without a token, the agent would boot with no way to call a model.
            missing.append("FLOTTA_SIGNING_KEY (the gateway needs a token for this agent)")
    elif model and base_url and api_key:
        # **Two consumers, two vocabularies.** `flotta.box.run` reads the
        # FLOTTA_* names; `hermes serve` — the surface the app talks to —
        # reads neither them nor, on this path, `OPENAI_BASE_URL`. It reads
        # `config.yaml`, which the box's boot step (`flotta.box.inference`)
        # writes from these values. A box with only FLOTTA_* and no boot step
        # runs Hermes's own default model, which is how every agent ran
        # "the configured model" by coincidence until FLOTTA-39.
        #
        # One derivation for every path — create, a model change, the boot
        # step. See `flotta.inference`, including the key leak the old
        # `OPENAI_*` arrangement allowed.
        from flotta.inference import provider_secrets

        secrets.update(provider_secrets(model, base_url, api_key))
    else:
        missing.append("FLOTTA_MODEL / FLOTTA_MODEL_BASE_URL / FLOTTA_API_KEY")

    return secrets, missing


def build_identity(
    box_id: str,
    box_name: str,
    *,
    org: str | None = None,
    client: str | None = None,
    instructions: str | None = None,
    ttl_s: int = BOX_TOKEN_TTL_S,
    env: dict[str, str] | None = None,
    author: CommitAuthor | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """A box's own identity, split by what may be read off the machine.

    Returns `(env, secrets)`. The split is the point:

    - **env** — `FLOTTA_BOX_ID`, `FLOTTA_BOX_NAME`, and where to reach the
      control plane and what domain to sign commits under. All of it appears in
      `fly machine status`, and none of it is a secret. With `author`, also
      whom the box's commits are *authored* as (FLOTTA-105): the box stays the
      committer, which the entrypoint sets from the name and domain.
    - **secrets** — `FLOTTA_BOX_TOKEN`, and nothing else. It authenticates as
      the box, so it must never be in the machine's configuration.

    Everything is omitted rather than guessed. A box with no token still boots
    and still commits under its own name; it just cannot fetch a GitHub
    credential, which is the same state every box was in before this existed.

    Shared with `flotta token box` so the block that command prints is exactly
    what creation injects. Two builders would drift, and the drift would be
    invisible until a rotated identity behaved differently from a fresh one.

    The token carries the box's tenancy (M11.5) when it is not the default
    org's default client, so every check that compares a token's org with a
    box's sees this box as its own. A default box's token is unchanged, byte
    for byte: an absent claim already means the default org.
    """
    source = os.environ if env is None else env
    from flotta.auth import SCOPE_BOX_PEER, SCOPE_GIT_CREDENTIAL, AuthError, box_subject, mint
    from flotta.workdir import DEFAULT_WORKDIR
    from flotta.workdir import ENV_KEY as WORKDIR_ENV

    box_env = {"FLOTTA_BOX_ID": box_id, "FLOTTA_BOX_NAME": box_name}

    # Standing instructions ride along as env, not as a secret: the agent is
    # meant to read them, and nothing about "you review backend PRs" needs
    # hiding from `machine status`. The entrypoint writes them to
    # `$HERMES_HOME/SOUL.md` exactly once — the volume copy is the agent's
    # after that — so this is the seed, and the store row is its record.
    if instructions:
        box_env["FLOTTA_INSTRUCTIONS"] = instructions

    # Projects folder. Always set, including the default: the entrypoint
    # already falls back to `/workspace`, but a missing key makes the Info
    # panel guess, and a guessed path is the same class of lie as a field
    # that decides nothing. `create_box` overwrites this with the fleet
    # setting; this default is for callers of `build_identity` that are not
    # that path (`flotta token box`).
    workdir = (source.get(WORKDIR_ENV) or "").strip() or DEFAULT_WORKDIR
    box_env[WORKDIR_ENV] = workdir

    control_url = (source.get("FLOTTA_CONTROL_URL") or "").strip()
    if control_url:
        box_env["FLOTTA_CONTROL_URL"] = control_url

    email_domain = (
        source.get("FLOTTA_GIT_EMAIL_DOMAIN") or source.get("FLOTTA_DOMAIN") or ""
    ).strip()
    if email_domain:
        box_env["FLOTTA_GIT_EMAIL_DOMAIN"] = email_domain

    # Omitted rather than guessed, like the rest: with no author the
    # entrypoint leaves `author.*` unset and the box commits as itself.
    if author is not None:
        box_env[GIT_AUTHOR_NAME_ENV] = author.name
        box_env[GIT_AUTHOR_EMAIL_ENV] = author.email

    try:
        token = mint(
            subject=box_subject(box_id),
            scopes={SCOPE_GIT_CREDENTIAL, SCOPE_BOX_PEER},
            ttl_s=ttl_s,
            env=source,
            **_tenant_claims(org, client),
        )
    except AuthError:
        # No signing key is a legitimate state — it is the whole loopback
        # development path, where `create_app` admits every request. Refusing
        # to create a box because auth is unconfigured would make identity a
        # prerequisite for having a fleet at all.
        return box_env, {}

    return box_env, {"FLOTTA_BOX_TOKEN": token}


# -- whom a box's commits are authored as (FLOTTA-105) ---------------------------
#
# A box commits as itself — `<box>@<domain>`, an address that resolves to no
# GitHub account on purpose — and that is where Vercel stops it: before
# creating a deployment it checks that the commit *author* is a member of the
# team, with no setting to turn that off, and `developer--…@flotta.dev` is
# nobody. Proven live on 2026-11-02: PR #118 on a client's Vercel-connected
# repository was refused, then built the moment the author was the person who
# had connected the account, with the committer still the box.
#
# So the author is a person and the committer is the box. The person is
# whoever connected the GitHub account the box's client uses — GitHub vouched
# for them at connect time, and `<id>+<login>@users.noreply` is the address
# GitHub issues for the account — or, for the operator's own agents on
# `$FLOTTA_GITHUB_TOKEN`, whose token that is.
#
# One author per box, not per repository, and that is a limit worth knowing:
# the credential is picked by the repository's owner (`github_installation_for`),
# the author is not. The nearest connection decides — the client's own account
# if it connected one, else its org's, else the fleet token — and a box with
# repositories from two accounts commits to both as the one person. A
# connection that names nobody (made before migration 9) is not passed over
# for somebody further out: a client's commits do not carry its agency's or
# the operator's name because its own account predates this. It names nobody
# until it is connected again, and the GitHub page says so. (`includeIf
# "hasconfig:remote.*.url:…"` could set an author per owner if it ever has to.)
#
# Set when the machine is made, and rewritten by `refresh_author` when the
# connection it came from changes (FLOTTA task 108).

GIT_AUTHOR_NAME_ENV = "FLOTTA_GIT_AUTHOR_NAME"
GIT_AUTHOR_EMAIL_ENV = "FLOTTA_GIT_AUTHOR_EMAIL"


@dataclass(frozen=True, slots=True)
class CommitAuthor:
    """Who a box's commits are authored as, and where that came from."""

    name: str
    email: str
    #: The GitHub login the address resolves to.
    login: str
    #: `github_app` (a connected account) or `fleet_token` (the operator's PAT).
    source: str


#: `/user` answers for the fleet token, by token, so creating agents does not
#: ask GitHub the same question every time. Only answers are kept: a blip is
#: asked again next time rather than remembered.
_FLEET_TOKEN_OWNERS: dict[str, Any] = {}


def commit_author_for(
    store: FleetStore, org: str, client: str, env: Mapping[str, str]
) -> CommitAuthor | None:
    """Whom a box in `org`'s `client` should author its commits as, or None.

    None means the nearest GitHub connection names nobody, or there is none
    and the fleet token is either not theirs to use or names nobody; the box
    then commits as itself, as every box did before this.
    """
    connections = store.github_installations_for(org, client)
    if connections:
        # The client's own connections if it has any, else the org's: never
        # a wider tenant's name on a narrower one's commits.
        nearest = connections[0].client
        for installation in connections:
            if installation.client != nearest:
                break
            person = installation.person
            if person is not None:
                return CommitAuthor(
                    person.commit_name, person.commit_email, person.login, "github_app"
                )
        return None
    if not reaches_fleet_github(org):
        return None
    token = (env.get(GITHUB_TOKEN_ENV) or "").strip()
    if not token:
        return None
    from flotta.github import token_owner  # at call time, so a test can stand in for it

    person = _FLEET_TOKEN_OWNERS.get(token)
    if person is None:
        person = token_owner(token)
        if person is None:
            return None
        _FLEET_TOKEN_OWNERS[token] = person
    return CommitAuthor(person.commit_name, person.commit_email, person.login, "fleet_token")


#: The events a commit-author refresh writes (FLOTTA task 108). The success
#: event is the one `create_box` writes, so one event type answers "whose
#: name is on this agent's commits" wherever it was decided.
COMMIT_AUTHOR = "commit_author"
COMMIT_AUTHOR_REFRESH_FAILED = "commit_author_refresh_failed"


def refresh_author(
    box_id: str,
    *,
    store: FleetStore,
    backend: Backend | None = None,
    reason: str = "requested",
) -> dict[str, Any]:
    """Re-decide whom an existing box's commits are authored as, and apply it.

    `create_box` decides the author once and writes it into the machine env,
    so a reconnect as somebody else, a disconnect, or an account from before
    migration 9 being connected again reached only boxes created afterwards
    (FLOTTA task 108). This runs the same rule, `commit_author_for`, against
    the box as it is now, and rewrites the two env keys when they differ from
    what the machine carries.

    **Nobody is an empty value, not an absent key.** `flyctl machine update
    --env` sets keys and has no way to remove one, and the entrypoint reads an
    empty `FLOTTA_GIT_AUTHOR_EMAIL` as none (`${...:-}`), so writing "" takes
    a person's name off a box exactly as a key that was never set leaves it.

    Costs a restart of a running box, which is inherent: the entrypoint reads
    its environment once, at boot. An asleep box stays asleep and boots with
    the new name, which is `apply_env`'s promise. Nothing is rewritten when
    the machine already says what the rule decides, so a reconnect as the
    same person restarts nobody. A machine that cannot be read counts as
    stale, for the reason `repair_connection` gives: a restart it did not
    need costs a minute, a name it should have lost costs every commit after.
    """
    from flotta.settings import layered

    box = _require_box(store, box_id)
    if is_terminal("box", box.status):
        raise ProvisionError(f"box {box_id} is {box.status!r}; there is no machine to update.")
    if box.status == "provisioning" or not box.endpoint:
        raise ProvisionError(
            f"box {box_id} is still being created and is named as part of that; "
            "wait for it to settle."
        )
    author = commit_author_for(store, box.org, box.client, layered(store))
    wanted = {
        GIT_AUTHOR_NAME_ENV: author.name if author is not None else "",
        GIT_AUTHOR_EMAIL_ENV: author.email if author is not None else "",
    }
    impl = _resolve_backend(box, backend)
    try:
        info = impl.inspect(box.endpoint)
    except (BackendError, NotSupported, NotImplementedError, AttributeError):
        info = None
    current = (
        None
        if info is None
        else {
            GIT_AUTHOR_NAME_ENV: getattr(info, "git_author_name", None) or "",
            GIT_AUTHOR_EMAIL_ENV: getattr(info, "git_author_email", None) or "",
        }
    )
    result = {
        "box_id": box.id,
        "name": box.name,
        "login": author.login if author is not None else None,
        "email": author.email if author is not None else None,
        "source": author.source if author is not None else None,
        "changed": current != wanted,
    }
    if current == wanted:
        return result
    applier = getattr(impl, "apply_env", None)
    try:
        if applier is None:
            raise NotSupported(f"{type(impl).__name__} cannot rewrite a machine's environment")
        applier(box.endpoint, wanted)
    except (BackendError, NotSupported) as exc:
        store.add_event(
            "box",
            box.id,
            COMMIT_AUTHOR_REFRESH_FAILED,
            {"error": str(exc)[:400], "login": result["login"], "reason": reason},
        )
        raise UpgradeFailed(
            f"could not change whose name is on {box.name}'s commits: {exc}. "
            "Its machine is unchanged."
        ) from exc
    store.add_event(
        "box",
        box.id,
        COMMIT_AUTHOR,
        {
            "login": result["login"],
            "email": result["email"],
            "source": result["source"],
            "reason": reason,
            "was": (current or {}).get(GIT_AUTHOR_EMAIL_ENV) or None,
        },
    )
    return result


def refresh_authors_in(
    store: FleetStore,
    org: str,
    client: str | None = None,
    *,
    reason: str,
    backend: Backend | None = None,
) -> list[dict[str, Any]]:
    """`refresh_author` for every settled box a connection could name.

    What a connect, a reconnect or a disconnect runs afterwards: the org's
    boxes for an org-wide account, one client's for its own. Each box decides
    for itself, so a client with its own account is reached by an org-wide
    change and finds nothing to rewrite. One box's failure is its own
    timeline's, and the next box is still refreshed.
    """
    import logging

    done: list[dict[str, Any]] = []
    for box in store.list_boxes_in(org, client):
        if is_terminal("box", box.status) or box.status == "provisioning" or not box.endpoint:
            continue
        try:
            done.append(refresh_author(box.id, store=store, backend=backend, reason=reason))
        except (ProvisionError, UpgradeFailed) as exc:
            # The failure event is already on the box's timeline.
            logging.getLogger(__name__).warning("refreshing %s's author failed: %s", box.name, exc)
            done.append({"box_id": box.id, "name": box.name, "error": str(exc)})
    return done


#: How long a box may sit in `provisioning` before it is presumed abandoned.
#: Generous: a cold app, a volume and a boot is minutes on a bad day, and
#: tearing down a provision that was merely slow would be worse than waiting.
STALE_PROVISION_S = 20 * 60


def reconcile_boxes(
    store: FleetStore,
    *,
    backend_for: Callable[[str], Backend] | None = None,
    now: datetime | None = None,
    stale_after_s: int = STALE_PROVISION_S,
) -> list[dict[str, Any]]:
    """Make box rows agree with the substrate. Returns what it changed.

    `reconcile` owns **tasks**; this owns **boxes**, and it exists because two
    real failures had no owner:

    **A provision that never finished.** The endpoint is written only once
    `create` returns, so a control plane that dies mid-create leaves a row at
    `provisioning` with `endpoint = None` — and a real machine that no Flotta
    verb can reach, because every one of them addresses a box by its endpoint.
    `DELETE` on such a row answered `200 torn_down` and destroyed nothing,
    which is the orphan-machine-billing scenario `FlyBackend.create` already
    warns about, arriving through a door nobody had checked. So: ask the
    backend whether a machine exists under that name, record the endpoint if it
    does, and close the row either way.

    **A row that says `running` about a machine that is not.** `create_box`
    observes `started`, which is true for the second before an entrypoint that
    cannot boot exits. The row then claims a healthy agent while the machine
    crash-loops and stops.

    Nothing here *creates*. A sweeper that could provision would race the
    caller that is already doing it; this only closes and corrects.
    """
    resolve = backend_for or _backend_for
    moment = now or datetime.now(UTC)
    changed: list[dict[str, Any]] = []

    for box in store.list_boxes():
        if box.status == "provisioning":
            created = _parse_ts(box.created_at)
            # An unparseable timestamp is not evidence of abandonment. Leaving
            # the row alone is the safe reading — the alternative is tearing
            # down a live provision over a formatting problem.
            if created is None or (moment - created).total_seconds() < stale_after_s:
                continue
            changed.append(_close_stranded_provision(store, box, resolve))
        elif box.status == "running" and box.endpoint:
            drift = _correct_running(store, box, resolve)
            if drift:
                changed.append(drift)

    return changed


def _close_stranded_provision(
    store: FleetStore, box: Box, resolve: Callable[[str], Backend]
) -> dict[str, Any]:
    """Tear down a provision nobody finished, machine included."""
    detail: dict[str, Any] = {"box_id": box.id, "name": box.name, "action": "stranded_provision"}
    endpoint = box.endpoint

    if endpoint is None:
        # The machine may exist under the box's name even though the row never
        # learned its address. Finding it is the difference between a tidy row
        # and a machine billing forever.
        try:
            endpoint = _peek_endpoint(resolve(f"{default_scheme()}://"), box.name)
        except Exception as exc:  # noqa: BLE001 - a probe must never end a sweep
            detail["probe_error"] = f"{type(exc).__name__}: {exc}"

    if endpoint:
        detail["endpoint"] = endpoint
        try:
            resolve(endpoint).destroy(endpoint)
            detail["destroyed"] = True
        except Exception as exc:  # noqa: BLE001
            # Recorded, not raised: the row still has to be closed, and the
            # endpoint is now written down so a human can finish the job.
            detail["destroyed"] = False
            detail["error"] = f"{type(exc).__name__}: {exc}"

    store.add_event(
        "box", box.id, "torn_down", {"reason": "provisioning never completed", **detail}
    )
    store.update_box_status(box.id, "torn_down", endpoint=endpoint)
    return detail


def _correct_running(
    store: FleetStore, box: Box, resolve: Callable[[str], Backend]
) -> dict[str, Any] | None:
    """A row claiming `running` about a machine that is not."""
    try:
        observed = resolve(box.endpoint).state(box.endpoint)
    except Exception:  # noqa: BLE001 - an unreadable substrate is not a verdict
        return None

    if observed in ("started", "starting", "replacing", "unknown"):
        return None

    # `gone` means the machine was destroyed outside Flotta; anything else
    # settled means it is simply not up. Neither is `running`.
    status = "torn_down" if observed == "gone" else "stopped"
    detail = {"reason": f"substrate says {observed!r}"}
    # When it really went down, for `flotta.metering`: a control plane that was
    # down corrects this late, and shouldn't bill for the hours it wasn't looking.
    with contextlib.suppress(Exception):
        down_at = resolve(box.endpoint).inspect(box.endpoint).last_down_at
        if down_at:
            detail["machine_down_at"] = down_at
    store.add_event("box", box.id, status, detail)
    store.update_box_status(box.id, status)
    return {"box_id": box.id, "name": box.name, "action": "corrected", "observed": observed}


def reserve_box(
    name: str,
    *,
    store: FleetStore,
    backend: Backend,
    org: str = DEFAULT_ORG,
    client: str = DEFAULT_CLIENT,
    display_name: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
    model: str | None = None,
) -> Box:
    """Write the row, and nothing else. Returns immediately.

    Split out of `create_box` because provisioning takes *minutes* — an app, a
    volume, a machine, a boot — and was modelled as one synchronous call. Behind
    an HTTP request that means the proxy gives up first:

        POST /api/boxes -> 502 Application failed to respond

    while the work carries on. The caller is told it failed; a machine appears
    anyway. Reserving the row separately lets the API answer at once with
    something real to poll.

    `name` is what the agent is called in its client; the row gets its
    address (`flotta.address`), which is what everything after this uses.
    """
    # The projects convention is part of what the agent is told, not a
    # separate file: a path it does not know about is not a projects folder.
    # Appended here so the store record matches the seed, on both the
    # synchronous create and the 202 path that provisions later.
    from flotta.store import INSTRUCTIONS_MAX
    from flotta.workdir import resolve_workdir, with_projects_convention

    instructions = with_projects_convention(
        instructions, resolve_workdir(store), max_len=INSTRUCTIONS_MAX
    )

    # Validated before the row exists, so a description that cannot be stored
    # refuses without spending the name — the same reason the name itself is
    # checked at the top of `store.create_box`.
    validate_box_meta(display_name, description, instructions)
    if model is not None:
        model = allowed_model(model, store)
    box = store.create_box(name, org=org, client=client)
    store.add_event("box", box.id, "provisioning", {"name": box.name, "backend": backend.scheme})
    if display_name or description or instructions:
        store.set_box_meta(
            box.id,
            display_name=display_name,
            description=description,
            instructions=instructions,
        )
    if model is not None:
        # In the store rather than carried in memory: on the 202 path this row
        # is provisioned later, on a thread, and `create_box` reads it back —
        # the same reason instructions live in `box_meta`.
        store.set_box_model(box.id, model)
    return box


def resolve_box_resources(
    store: FleetStore,
    *,
    volume_gb: int | None = None,
    region: str | None = None,
    memory_mb: int | None = None,
) -> tuple[int | None, str | None, int | None]:
    """What this agent gets: what was asked for, else the fleet's default.

    Returns `(volume_gb, region, memory_mb)`, any of which may be None —
    meaning "the backend decides", which for Fly is `FlyConfig`. Three levels,
    and the last one is deliberately *not* resolved here: `FlyConfig` is the
    substrate's config, and reaching into it from `provision` would put Fly in
    a module that routes across substrates.

    This is the whole per-agent seam, and it turned out not to need one.
    `BoxSpec` already carries `region` and `volume_gb` as portable fields, and
    `FlyBackend.create` already prefers them over its own config — so a value
    reaches a machine by travelling in the spec, exactly as `image` and
    `secrets` do. No registry change, no new protocol verb, and nothing
    provider-specific in the shared shape.

    **The machine's size is memory (FLOTTA-157), and that was the decision
    this used to defer.** Memory is the portable field: every substrate gives a
    VM some megabytes, while `vm_size` is a Fly preset (`shared-cpu-1x`) that
    means nothing to a Firecracker pool. So an agent is sized by `memory_mb`,
    one of `flotta.backend.MEMORY_SIZES_MB`, and the backend derives the CPUs — on Fly a
    shared CPU holds at most 2 GB, so 4 GB is `--vm-cpus 2`. The CPU kind and
    preset stay fleet-wide. Its fleet default has no settings field yet, so it
    is the backend's (`$FLOTTA_FLY_VM_MEMORY_MB`, else `DEFAULT_MEMORY_MB`), and
    an existing agent changes size through `resize_box`, not here.
    """
    from flotta.settings import layered

    env = layered(store)

    if volume_gb is None:
        raw = (env.get("FLOTTA_FLY_VOLUME_GB") or "").strip()
        if raw:
            try:
                volume_gb = int(raw)
            except ValueError:
                # The fleet default being unreadable must not stop a create:
                # the backend still has its own, and refusing here would strand
                # someone with no agent over a typo in a settings field.
                volume_gb = None

    if region is None:
        region = (env.get("FLOTTA_FLY_REGION") or "").strip() or None

    if memory_mb is not None:
        memory_mb = _checked_size(memory_mb)

    return volume_gb, region, memory_mb


def create_box(
    name: str,
    *,
    store: FleetStore,
    backend: Backend | None = None,
    spec: BoxSpec | None = None,
    box: Box | None = None,
    org: str = DEFAULT_ORG,
    client: str = DEFAULT_CLIENT,
    volume_gb: int | None = None,
    region: str | None = None,
    memory_mb: int | None = None,
    display_name: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Provision a **persistent** box and record it. Returns ``{box_id, endpoint}``.

    `name` is what the agent is called in `org`'s `client`. From the moment it
    is validated the box is known by its **address** instead — the row's name,
    the Fly app, the occupancy probe and the identity all use it — because the
    address is what is unique: two orgs' `support` agents are two apps.

    The verb v0.1 had no use for. `spawn_box` creates a machine *for a task* and
    throws it away; this creates a machine you **have**, which tasks then visit.
    Under Modal the distinction was meaningless — every container was disposable
    — which is why `create_box` arrives with the first substrate that can keep a
    disk.

    The row is written before the provision so a failed create still leaves
    something that explains itself, exactly as `spawn_box` does. A box whose
    provision failed is closed rather than left in `provisioning` forever: there
    is no machine behind it, so nothing can ever move it forward.
    """
    # First, before a substrate is even resolved. `_peek_endpoint` shells out,
    # so validating only in the store would spend a `flyctl` round trip on a
    # name that can never work — and would make refusing a typo depend on
    # having a working Fly credential, which is the wrong dependency for
    # "that name has a capital letter in it".
    #
    # The store validates again. That is the guarantee; this is the shortcut.
    # A reserved row already has its address; anything else is composed here,
    # before the probe below names a Fly app after it.
    if box is not None:
        name = box.name
    else:
        short = validate_box_name(name)
        # Cleaned once, here, and passed on cleaned: the store strips the ids
        # too, and everything named before the row exists has to agree with it.
        org = validate_tenant_id(org, kind="org")
        client = validate_tenant_id(client, kind="client")
        name = box_label(short, org=org, client=client)
    if model is not None:
        # Same reasoning as the name: a model id that can never work is refused
        # before a substrate round trip, not after a machine exists. So is one
        # that is not on the fleet's list (FLOTTA-85).
        model = allowed_model(model, store)
    if memory_mb is not None:
        # And a size, for the same reason: a number nothing has priced costs
        # nothing to refuse now and a machine to refuse later.
        memory_mb = _checked_size(memory_mb)

    # A pending org's agent is refused before a substrate is resolved, let alone
    # probed. The store refuses the row too (`FleetStore.create_box`), and that
    # is the guarantee; this keeps a sign-up from costing even a `flyctl` call.
    # An org that does not exist is left to the store, which names the client.
    if box is None and store.get_org(org) is not None:
        store.require_active_org(org)

    impl = backend or _default_backend()

    # `create` is idempotent over the *machine* — it adopts `machines[0]` rather
    # than adding a second — but the store is not, so a second `create_box`
    # under a different name would mint a second row pointing at the same
    # endpoint. Two rows, one machine: `stop_box` on either would then lie
    # about the other. Unreachable through today's CLI, which is exactly why it
    # is worth closing before `flotta create` exists to reach it.
    probe = None if box is not None else _peek_endpoint(impl, name)
    if probe:
        for existing in store.list_boxes():
            if existing.endpoint == probe and not is_terminal("box", existing.status):
                raise BoxOccupied(
                    f"box {existing.id} ({existing.name}) already occupies {probe}. "
                    "One machine hosts one box; destroy it with `flotta kill` "
                    "before creating another, or point FLOTTA_FLY_APP elsewhere."
                )

    # A caller that already reserved the row passes it in — that is how the
    # API answers immediately and provisions afterwards, without the row being
    # created twice or the name being taken between the two halves.
    if box is None:
        box = reserve_box(
            short,
            store=store,
            backend=impl,
            org=org,
            client=client,
            display_name=display_name,
            description=description,
            instructions=instructions,
            model=model,
        )

    # Identity travels with the machine rather than arriving afterwards. The
    # ordering already works: `store.create_box` produced the id above, so
    # `box:<id>` can be signed before anything exists to sign for. Doing it in
    # a second command — which is what `just box-identity` was — is wrong for a
    # caller that has no shell: `POST /api/boxes` is one request, and M8's
    # "create Agent B" is one button.
    # Named by address whatever the caller's spec said: the Fly app is named
    # after it, and a tenant box's spec carrying its short name would put it
    # in another org's app.
    base = replace(spec, name=name) if spec else BoxSpec(name=name)

    # The image every agent is born on, resolved the same way an upgrade
    # resolves it — `$FLOTTA_FLY_IMAGE`, else the newest release on the build
    # app.
    #
    # Without this, `create` and `upgrade` disagree about what "the fleet
    # image" means. `FlyBackend.create` falls back to `self.config.image` and
    # then to *the box's own app's* release history, which with one app per
    # agent is a brand-new app with no releases — so unsetting the variable
    # would leave upgrades working and creation refusing, which is worse than
    # either behaviour on its own.
    #
    # An explicit `spec.image` still wins: a caller naming an image means it.
    # A backend that boots from no image (`local://`) is not given one, and the
    # lookup is skipped rather than ignored: it asks Fly.
    if not base.image and getattr(impl, "takes_image", True):
        fleet_image, _ = resolve_fleet_image()
        if fleet_image:
            base = replace(base, image=fleet_image)

    # From the store, not the argument: on the 202 path the row (and its
    # meta) was reserved by the API before this ran on a thread, and the
    # argument is None. One source, both paths.
    meta = store.meta_for_box(box.id)

    # Through the settings layer, not the raw environment: a projects folder
    # (or a model) set in the app's Fleet settings has to reach the machine
    # being created, or the field decides nothing.
    from flotta.settings import layered
    from flotta.workdir import ENV_KEY as WORKDIR_ENV
    from flotta.workdir import resolve_workdir

    source = layered(store)
    author = commit_author_for(store, box.org, box.client, source)
    identity_env, identity_secrets = build_identity(
        box.id,
        name,
        org=box.org,
        client=box.client,
        instructions=meta.instructions if meta else None,
        env=source,
        author=author,
    )
    if author is not None:
        # On the timeline, so "whose name is on this agent's commits" is
        # answered where the agent is, not by reading a machine's env.
        store.add_event(
            "box",
            box.id,
            "commit_author",
            {"login": author.login, "email": author.email, "source": author.source},
        )
    # Always, including the default. `build_identity` already puts a value
    # in, but it does not validate; this is the cleaned path the setting
    # actually means, and the one the Info panel will read back off the
    # machine.
    identity_env = {**identity_env, WORKDIR_ENV: resolve_workdir(store, source)}

    # What this agent gets, as opposed to what every agent gets. Travels in the
    # spec because that is what the portable fields are for — see
    # `resolve_box_resources`.
    resolved_volume, resolved_region, resolved_memory = resolve_box_resources(
        store, volume_gb=volume_gb, region=region, memory_mb=memory_mb
    )

    # What every box needs, as opposed to what makes this one itself. Missing
    # values are recorded rather than raised: a box with no provider key still
    # boots and can be fixed, and refusing to create would strand the operator
    # with no agent and no obvious way to get one.
    fleet, missing_fleet = fleet_secrets(source, box_id=box.id, org=box.org, client=box.client)
    # This agent's own model, if it has one — from the store for the reason
    # `meta` is. Only the model is overridden: the endpoint and key stay the
    # fleet's (see the `box_models` table).
    own_model = store.model_for_box(box.id)
    if own_model and "FLOTTA_MODEL" in fleet:
        fleet["FLOTTA_MODEL"] = own_model
    if "FLOTTA_MODEL" in fleet:
        store.add_event(
            "box",
            box.id,
            MODEL_SET,
            {
                "model": fleet["FLOTTA_MODEL"],
                "source": "agent" if own_model else "fleet",
                "reason": "created",
            },
        )
    # A caller's own spec wins over both, which is the escape hatch a test or a
    # one-off create needs — and the reason `base` is merged last rather than
    # first.
    spec = replace(
        base,
        # A caller's own spec still wins, the same rule as `env` and `secrets`
        # below: a test or a one-off create that named a size meant it.
        volume_gb=base.volume_gb if base.volume_gb is not None else resolved_volume,
        region=base.region or resolved_region,
        memory_mb=base.memory_mb if base.memory_mb is not None else resolved_memory,
        env={**identity_env, **base.env},
        secrets={**identity_secrets, **fleet, **base.secrets},
    )

    if spec.volume_gb is not None or spec.region is not None or spec.memory_mb is not None:
        # What makes this agent different from the fleet default, recorded
        # before the machine exists so it survives a provision that fails.
        # An event rather than a column: this is an immutable fact about one
        # create, which is the case events are actually good at — and the store
        # still has no way to add a column to an existing fleet.
        store.add_event(
            "box",
            box.id,
            "resources",
            {"volume_gb": spec.volume_gb, "region": spec.region, "memory_mb": spec.memory_mb},
        )

    try:
        handle = impl.create(spec)
    except Exception as exc:
        # Deliberately broad. This caught only `BackendError`, which is the
        # failure a backend *means* to raise — but a flyctl timeout, an OSError,
        # or a plain bug in a backend is not that, and any of them left the row
        # at `provisioning` forever with a machine possibly already created and
        # billing. M0's review found the same shape (`subprocess.TimeoutExpired`
        # escaping) and it matters more now: `POST /api/boxes` puts this behind
        # a network call, so an unexpected error would strand a row per request.
        #
        # `Exception`, not `BaseException`: a Ctrl-C should not be recorded as
        # a teardown.
        detail = f"create failed: {type(exc).__name__}: {exc}"
        store.add_event("box", box.id, "torn_down", {"reason": detail})
        store.update_box_status(box.id, "torn_down")
        raise ProvisionError(detail) from exc

    # The size the machine has, on the timeline, so the box payload can say it
    # without asking the substrate on every read (`box_sizes`). What the backend
    # reports rather than what was asked for: an adopted machine keeps its own.
    given_memory = getattr(handle, "memory_mb", None)
    if given_memory is None and not handle.adopted:
        given_memory = spec.memory_mb
    if given_memory is not None:
        store.add_event("box", box.id, SIZE_SET, {"memory_mb": given_memory, "reason": "created"})
    if handle.adopted and spec.memory_mb is not None and given_memory != spec.memory_mb:
        # `resources` above said what was asked, before the machine existed.
        # Adopting changes nothing about a machine, so say plainly that the
        # size asked for is not the size it has; a resize applies it.
        store.add_event(
            "box",
            box.id,
            "size_not_applied",
            {
                "requested_memory_mb": spec.memory_mb,
                "memory_mb": given_memory,
                "reason": "adopted a machine that already existed; resize it to apply",
            },
        )

    # An adopted machine did not exist when its secrets were written, because
    # it existed before `create` ran at all. On Fly that is the *normal* path,
    # not an edge: the only way to release an image is `fly deploy`, which
    # creates a machine while doing it — so `create` always has one to adopt,
    # and the whole create-time injection above would never have fired in
    # production. Found by comparing timestamps on the fleet's first box: its
    # machine was five days older than its row.
    identity_error: str | None = None

    # **Everything the machine needs, not just what makes it itself.**
    # This applied `identity_secrets` alone, so an adopted machine got its
    # token and none of the credentials it needs to *serve* — and adoption is
    # the normal path on Fly, as the comment below says. A single-app fleet
    # would still have crash-looped on `HERMES_DASHBOARD_BASIC_AUTH_*` with the
    # control plane holding the password all along, and `missing_fleet` would
    # have been empty, so the new diagnostic could not have seen it: a worse
    # failure with less observability than having nothing configured.
    #
    # The condition was also wrong in the same way — `and identity_secrets`
    # skipped the fleet's secrets entirely when there was no signing key.
    adopted_secrets = {**fleet, **identity_secrets}
    if handle.adopted and adopted_secrets:
        try:
            impl.apply_secrets(handle.endpoint, adopted_secrets)
        except BackendError as exc:
            # Not fatal. A box without an identity still boots, still talks,
            # and still commits under its own name — it just cannot fetch a
            # git credential. Tearing down a working machine over that would
            # trade a small missing capability for a destroyed one.
            #
            # Recorded in a variable rather than by clearing `identity_secrets`,
            # which is what this did first: that made the failure indistinguishable
            # from having no signing key, and the box was left carrying an event
            # saying "no signing key configured" when the key was fine. A false
            # diagnosis is worse than none — it sends someone to check the one
            # thing that is not broken.
            identity_error = f"apply_secrets failed: {exc}"

    # Recorded as an event rather than a column: the store has no migration
    # path, so a new column would not appear on an existing fleet. An event
    # does, it is already polymorphic, and `flotta logs <box>` shows it — which
    # is how an operator sees an identity approaching expiry before a clone
    # fails for a reason nothing on the box can explain.
    # Said plainly, and *before* the identity event, because a box missing
    # these does not merely lack a capability — it will not boot at all, and
    # the only symptom is a machine that restarts until Fly gives up.
    if missing_fleet:
        store.add_event(
            "box",
            box.id,
            "fleet_secrets_missing",
            {
                "missing": missing_fleet,
                "reason": "this box cannot serve without them; set them wherever "
                "the control plane runs and recreate it",
            },
        )

    if identity_error:
        store.add_event("box", box.id, "identity_skipped", {"reason": identity_error})
    elif identity_secrets:
        from flotta.auth import verify

        store.add_event(
            "box",
            box.id,
            "identity_minted",
            {"expires_at": verify(identity_secrets["FLOTTA_BOX_TOKEN"]).expires_at},
        )
    else:
        store.add_event(
            "box",
            box.id,
            "identity_skipped",
            {"reason": "no signing key configured; this box cannot fetch git credentials"},
        )

    # `Backend.create` may return before the box is running — the protocol says
    # so, and on a Firecracker pool that will be the normal case. Writing
    # `running` here without asking would reintroduce exactly the lie this
    # milestone closed for `stop_box`: a row claiming a machine is up while it
    # is asleep. So start it, then believe the substrate rather than the plan.
    started_error: str | None = None
    try:
        impl.start(handle.endpoint)
    except BackendError as exc:
        started_error = f"{type(exc).__name__}: {exc}"

    # If `start` returned cleanly, that is evidence the box is up; an
    # unreadable `state()` must not overturn it.
    observed = _observed_state(
        impl, handle.endpoint, assume="stopped" if started_error else "started"
    )
    if observed == "started":
        store.update_box_status(box.id, "running", endpoint=handle.endpoint)
        store.add_event(
            "box", box.id, "running", {"endpoint": handle.endpoint, "machine_id": handle.id}
        )
        return {
            "box_id": box.id,
            "endpoint": handle.endpoint,
            "machine_id": handle.id,
            "status": "running",
        }

    # The machine exists but is not up. `stopped` is the true statement, and it
    # is recoverable — `flotta start` retries — where `torn_down` would discard
    # a machine that is sitting there costing disk.
    detail = started_error or f"machine is {observed!r} after create"
    store.update_box_status(box.id, "stopped", endpoint=handle.endpoint)
    store.add_event(
        "box",
        box.id,
        "stopped",
        {"endpoint": handle.endpoint, "machine_id": handle.id, "reason": detail},
    )
    raise BoxNotRunning(
        f"box {box.id} was created ({handle.endpoint}) but is not running: {detail}. "
        f"It is recorded as stopped; `flotta start {box.id}` will retry.",
        box_id=box.id,
        endpoint=handle.endpoint,
    )


def _peek_endpoint(impl: Backend, box_name: str | None = None) -> str | None:
    """The endpoint `create` would adopt, without creating anything.

    Best-effort and side-effect-free: used only to notice that a machine is
    already spoken for. A backend that cannot answer simply yields None and the
    duplicate check is skipped, because refusing to create on the strength of a
    failed probe would be worse than the duplicate it guards against.
    """
    peek = getattr(impl, "existing_endpoint", None)
    if peek is None:
        return None
    try:
        # Older backends answer for the fleet, newer ones per box. Try the
        # specific question first and fall back rather than requiring every
        # implementation to change at once.
        try:
            return peek(box_name)
        except TypeError:
            return peek()
    except BackendError:
        return None


def _observed_state(impl: Backend, endpoint: str, *, assume: str) -> str:
    """What the substrate says, falling back to what we already know.

    `assume` is what the caller has independent evidence for — a `start()` that
    returned without raising is real evidence the box is up. An earlier version
    caught bare `Exception` and returned `"unknown"`, which meant a flaky
    `state()` *after a successful start* recorded the box as `stopped` while
    the machine was running: the same row-disagrees-with-reality bug this
    milestone exists to remove, pointing the other way.

    Only `BackendError` is tolerated — a substrate that cannot answer. Anything
    else is a bug here and should surface.
    """
    try:
        return impl.state(endpoint)
    except BackendError:
        return assume


def stop_box(
    box_id: str,
    *,
    store: FleetStore,
    reason: str = "idle",
    backend: Backend | None = None,
    prefer_suspend: bool = True,
) -> dict[str, Any]:
    """Put a box to sleep — disk retained, CPU released. Idempotent.

    **Real infrastructure since M1.** Under M0 this only wrote a row; the
    container kept running and kept billing while `count_active_boxes()`
    reported zero. It now asks the backend to actually release the CPU, and
    the row is written *after* that succeeds.

    Prefers `suspend` (a Firecracker memory snapshot) and falls back to a cold
    `stop` where the substrate refuses. Measured on a real box, suspend is not
    the *faster* of the two — cold stop reaches `started` in ~0.31s against
    suspend's ~0.43s. What suspend buys is what the VM's uptime counter shows:
    it keeps its memory (44.4s -> 53.5s) where a cold stop resets it (72.1s ->
    6.7s). That is worth nothing today, when a box runs `sleep infinity`, and
    is worth everything once a box runs Hermes as a service — a cold start
    means re-importing the agent before it can think.

    The store still records only ``stopped``. How the CPU was released is a
    substrate detail and lands in the event payload as ``method``; promoting it
    to a fleet status would leak Fly's vocabulary into a state machine that has
    to describe Hetzner and Modal too.
    """
    box = _require_box(store, box_id)
    if box.status == "stopped":
        return {"box_id": box_id, "status": "stopped", "already_stopped": True}

    # Legality before any side effect — an illegal transition must not leave a
    # `stopped` event describing something that never happened.
    if box.status != "running":
        raise ProvisionError(f"box {box_id} is {box.status!r}; only a running box can be stopped")

    # Refuse while work is in flight. Suspending mid-task is a coherent thing to
    # want and is not this milestone: the task's watcher is a *local* process
    # holding a Modal call handle, and freezing the box underneath it would
    # strand that watcher. M6's workspace tier is where mid-task suspend
    # becomes meaningful.
    live = [t.id for t in store.list_tasks(box_id=box_id) if not is_terminal("task", t.status)]
    if live:
        raise ProvisionError(
            f"box {box_id} has {len(live)} task(s) still running ({', '.join(live)}); "
            "stopping would not stop the spend, only the bookkeeping. "
            "Wait for them, or use `flotta kill` to cancel and destroy the box."
        )

    impl = _resolve_backend(box, backend)
    try:
        method = _pause(impl, box.endpoint or box_id, prefer_suspend=prefer_suspend)
    except BackendError as exc:
        raise ProvisionError(f"could not stop box {box_id}: {exc}") from exc

    store.add_event(
        "box",
        box_id,
        "stopped",
        {"reason": reason, "previous_status": box.status, "method": method},
    )
    store.update_box_status(box_id, "stopped")
    return {
        "box_id": box_id,
        "status": "stopped",
        "already_stopped": False,
        "method": method,
    }


class UpgradeFailed(ProvisionError):
    """The substrate refused or broke while re-imaging a box.

    Distinct from a plain `ProvisionError` here, which means the caller asked
    for something this box cannot do — terminal, mid-provision, no image named.
    Those are actionable by the caller; this one is not, and conflating them
    would answer a flyctl outage with "you asked for the wrong thing".
    """


#: Where Hermes keeps an agent's identity. Matches `HERMES_HOME` in
#: `fly/Dockerfile`; a box that ever moves it would need this to follow.
SOUL_PATH = "/data/hermes/SOUL.md"


def set_instructions(
    box_id: str,
    instructions: str | None,
    *,
    store: FleetStore,
    backend: Backend | None = None,
) -> dict[str, Any]:
    """Rewrite an agent's standing instructions on its own volume.

    **Why this cannot be a store write.** The row in `box_meta` is a record;
    the copy an agent reads is `SOUL.md` on its volume, written once by the
    entrypoint and never touched again. Changing the record alone would make
    the app describe a persona the agent is not running — the exact class of
    lie FLOTTA-29 spent a milestone removing.

    **Why the change is not live until a conversation restarts.** Hermes
    renders a system prompt once, when a session starts, and keeps it for that
    session's life (FLOTTA-58). So this writes the file and records *when*; it
    is the app, holding the conversation, that starts the fresh one. Nothing
    here restarts the machine: a restart would not help either, because the
    frozen prompt lives in `state.db` on the same volume.

    The content is **base64 in transit**. `exec` runs through `flyctl ssh`,
    where multi-line input is mangled and every shell metacharacter in a
    person's prose is a way to run something else on a machine whose agent has
    root. Base64 is alphabet-only, so the command is a fixed shape no matter
    what was typed.

    Clearing the instructions removes the file rather than writing an empty
    one: Hermes seeds its own default when `SOUL.md` is absent, and an empty
    file would leave an agent with no identity at all.
    """
    import base64

    box = store.get_box(box_id) or store.get_box_by_name(box_id)
    if box is None:
        raise ProvisionError(f"no box {box_id!r}")
    # `stopped` is allowed on purpose: an agent is asleep most of the time, and
    # `exec` starts the machine itself. Only a torn-down box has no volume to
    # write to — refusing a sleeping one would refuse the normal case. A paused
    # one is another matter, since that start would wake it.
    _refuse_paused(box)
    if is_terminal("box", box.status):
        raise ProvisionError(f"box {box.name} is {box.status}, so there is no volume to write to")

    _, _, cleaned = validate_box_meta(None, None, instructions)
    impl = _resolve_backend(box, backend)

    if cleaned is None:
        command = f"rm -f {SOUL_PATH}"
    else:
        blob = base64.b64encode(cleaned.encode("utf-8")).decode("ascii")
        # `-d` on GNU coreutils and busybox alike; the box is Ubuntu.
        command = f"printf %s {blob} | base64 -d > {SOUL_PATH}"

    # **The endpoint, not the box id.** Every backend verb is addressed by
    # endpoint; `FlyBackend._addr` treats anything without a `fly://` scheme as
    # a bare *machine id* in the fleet's default app. Passing `box.id` sent it
    # hunting for a machine called `b-…` in the wrong app, which is not an
    # error — it is ninety seconds of polling for a machine that will never
    # appear, then a 500. The fake backend ignores this argument, so the whole
    # suite was green while the live call could not work.
    if not box.endpoint:
        raise ProvisionError(f"box {box.name} has no endpoint, so there is no machine to write to")
    result = impl.exec(box.endpoint, command)
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout or "").strip() or "no output"
        store.add_event("box", box.id, "instructions_write_failed", {"detail": detail[:500]})
        raise ProvisionError(f"could not write {SOUL_PATH} on {box.name}: {detail}")

    current = store.meta_for_box(box.id)
    meta = store.set_box_meta(
        box.id,
        display_name=current.display_name if current else None,
        description=current.description if current else None,
        instructions=cleaned,
    )
    # Written **after** the file, never before: an event saying the persona
    # changed, on a box where the write failed, is a lie in the audit trail
    # that outlives the traceback.
    store.add_event(
        "box",
        box.id,
        INSTRUCTIONS_CHANGED,
        {"chars": len(cleaned) if cleaned else 0, "cleared": cleaned is None},
    )
    return {
        "box_id": box.id,
        "instructions": meta.instructions,
        "changed_at": store.instructions_changed_at(box.id),
    }


#: The box's Python: Hermes's, which has `yaml`, as `box_entrypoint.sh` uses.
BOX_PYTHON = "/opt/hermes-venv/bin/python3"
HERMES_HOME = "/data/hermes"
#: The event that says an agent's rules were written to its `config.yaml`.
RULES_CHANGED = "rules_changed"
#: One save at a time per box, from the write on the machine to the record of
#: it, so the store can't say one save while the machine holds another. Per
#: process: the control plane is one. The box's own lock keeps the file whole
#: whatever happens here.
_RULES_LOCKS: dict[str, threading.Lock] = {}
_RULES_LOCKS_GUARD = threading.Lock()


def _rules_lock(box_id: str) -> threading.Lock:
    with _RULES_LOCKS_GUARD:
        return _RULES_LOCKS.setdefault(box_id, threading.Lock())


def _writable_box(box_id: str, store: FleetStore) -> Box:
    """A box whose `config.yaml` can be written now, or `ProvisionError` saying why not."""
    box = store.get_box(box_id) or store.get_box_by_name(box_id)
    if box is None:
        raise ProvisionError(f"no box {box_id!r}")
    _refuse_paused(box)
    if is_terminal("box", box.status):
        raise ProvisionError(f"box {box.name} is {box.status}, so there is no volume to write to")
    if not box.endpoint:
        raise ProvisionError(f"box {box.name} has no endpoint, so there is no machine to write to")
    return box


def _config_command(changes: list[list[Any]]) -> str:
    """Run `flotta.box.rules` on the box with `changes`: the writer travels with them."""
    import base64
    import json
    from pathlib import Path

    from flotta.box import rules as box_rules

    script = base64.b64encode(Path(box_rules.__file__).read_bytes()).decode("ascii")
    payload = base64.b64encode(json.dumps(changes).encode("utf-8")).decode("ascii")
    return f"printf %s {script} | base64 -d | {BOX_PYTHON} - {HERMES_HOME} {payload}"


def set_rules(
    box_id: str,
    rules: Rules,
    *,
    store: FleetStore,
    backend: Backend | None = None,
    by: str | None = None,
) -> dict[str, Any]:
    """Write an agent's rules into its `config.yaml`, then record them (FLOTTA-124).

    **The writer travels with the rules.** `flotta.box.rules` is sent as source,
    base64 like everything else on this path (`set_instructions` says why), and
    run with the box's own Python. An agent made before this module existed
    gets its rules without a new image.

    **When they apply** is `flotta.rules`'s: the next command for what is
    enforced, the next conversation for the rest. As with instructions, nothing
    restarts. Asleep is fine, since `exec` starts the machine; paused is not.
    """
    import json

    box = _writable_box(box_id, store)
    impl = _resolve_backend(box, backend)

    with _rules_lock(box.id):
        result = impl.exec(box.endpoint, _config_command(config_changes(rules).to_json()))
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout or "").strip() or "no output"
            store.add_event("box", box.id, "rules_write_failed", {"detail": detail[:500]})
            raise ProvisionError(f"could not write the rules on {box.name}: {detail}")
        store.set_box_rules(box.id, None if rules.empty else json.dumps(rules.to_json()))
    # After the write, as with instructions: the audit trail says what landed.
    store.add_event(
        "box",
        box.id,
        RULES_CHANGED,
        {
            "always": len(rules.always),
            "never": len(rules.never),
            "careful": len(rules.careful),
            "denied_commands": len(rules.denied_commands),
            "disabled_tools": list(rules.disabled_tools),
            "private_conversations": rules.private_conversations,
            "by": by,
        },
    )
    return rules_of(box.id, store=store)


def rules_of(box_id: str, *, store: FleetStore) -> dict[str, Any]:
    """An agent's rules as the API answers them, with when they were last written."""
    stored = store.box_rules(box_id)
    rules = rules_from_json(stored[0] if stored else None)
    return {"box_id": box_id, "rules": rules.to_json(), "changed_at": stored[1] if stored else None}


#: The event that says an agent's MCP grants were written to its `config.yaml`.
MCP_GRANTS_CHANGED = "mcp_grants_changed"

#: In the command that asks a box's Hermes to reload its MCP servers, and nowhere else.
MCP_RELOAD_MARK = "--flotta-mcp-reload"


def _mcp_reload_command() -> str:
    """Run `flotta.box.mcp_reload` on the box: the script travels with the command."""
    import base64
    from pathlib import Path

    from flotta.box import mcp_reload

    script = base64.b64encode(Path(mcp_reload.__file__).read_bytes()).decode("ascii")
    return f"printf %s {script} | base64 -d | {BOX_PYTHON} - {MCP_RELOAD_MARK}"


def _reload_mcp(box: Box, store: FleetStore, backend: Backend | None) -> bool | None:
    """Ask a running agent's Hermes to drop and rediscover its MCP servers. Best effort.

    `None` when nothing was asked: an agent that isn't running reads its config
    when it boots, so it isn't woken for this, and a substrate with no shell has
    no Hermes to ask. The outcome is an event either
    way, and never fails the grant, which is already in force at the gateway.
    """
    if box.status != "running" or not box.endpoint:
        return None
    try:
        result = _resolve_backend(box, backend).exec(box.endpoint, _mcp_reload_command())
    except NotSupported:
        # A substrate with no shell, like `just local`'s stand-in: it has no Hermes
        # to reload, so nothing was asked, rather than a reload that failed.
        return None
    except Exception as exc:  # noqa: BLE001 - best effort, recorded
        detail: str | None = str(exc) or type(exc).__name__
    else:
        detail = (
            None
            if result.exit_code == 0
            else ((result.stderr or result.stdout or "").strip() or "no output")
        )
    if detail is None:
        store.add_event("box", box.id, "mcp_reloaded", {})
        return True
    store.add_event("box", box.id, "mcp_reload_failed", {"detail": detail[:500]})
    return False


def set_mcp_grants(
    box_id: str,
    grants: tuple[Any, ...],
    *,
    store: FleetStore,
    backend: Backend | None = None,
    by: str | None = None,
) -> dict[str, Any]:
    """Write an agent's MCP grants into its `config.yaml`, then record them (FLOTTA-125).

    Through the FLOTTA-124 writer, under the same lock as the rules, since it
    is the same file. Each grant is a `flotta-<server>` entry pointing at the
    gateway with a fresh `mcp:call` token for this box (`flotta.mcp` says why
    it is written out rather than referred to).

    **Recorded first, written second: the opposite of the rules.** The record
    is what the gateway enforces, so it is the grant; the config is only how
    the agent finds the server. Recorded first, a grant taken away stops on the
    next call even if the machine can't be written, and the answer says the
    config is behind (`written: false`) rather than pretending nothing changed.
    A new server appears in the agent's next conversation; one taken away stays
    in an open conversation's tool list until Hermes reloads (`reload.mcp`, or
    a new conversation), and the gateway refuses it meanwhile.
    """
    import json

    from flotta.auth import SCOPE_MCP_CALL, AuthError, box_subject, mint
    from flotta.mcp import catalogue_of, config_changes, mcp_base
    from flotta.settings import layered

    box = store.get_box(box_id) or store.get_box_by_name(box_id)
    if box is None:
        raise ProvisionError(f"no box {box_id!r}")
    source = layered(store)
    base = mcp_base(source)
    if base is None:
        raise ProvisionError(
            f"there is no gateway for MCP servers to go through. Set ${GATEWAY_URL_ENV} "
            "where the control plane runs."
        )
    try:
        token = mint(
            subject=box_subject(box.id),
            scopes={SCOPE_MCP_CALL},
            ttl_s=MODEL_TOKEN_TTL_S,
            env=dict(source),
            **_tenant_claims(box.org, box.client),
        )
    except AuthError as exc:
        raise ProvisionError(
            "no signing key is configured, so there is no token to give this agent. Set "
            "$FLOTTA_SIGNING_KEY where the control plane runs."
        ) from exc
    previous = [g.server for g in store.box_mcp_grants(box.id)]
    servers, _ = catalogue_of(store)
    changes = config_changes(grants, servers, base=base, token=token, previous=previous)
    detail = None
    try:
        _writable_box(box.id, store)
    except ProvisionError as exc:
        detail = str(exc)
    with _rules_lock(box.id):
        store.set_box_mcp_grants(
            box.id,
            [
                (g.server, json.dumps(list(g.tools)) if g.tools is not None else None, g.ask_first)
                for g in grants
            ],
        )
        store.add_event(
            "box", box.id, MCP_GRANTS_CHANGED, {"servers": [g.to_json() for g in grants], "by": by}
        )
        if detail is None:
            assert box.endpoint is not None
            try:
                result = _resolve_backend(box, backend).exec(box.endpoint, _config_command(changes))
            except Exception as exc:  # noqa: BLE001 - the grant stands; the config is behind
                detail = str(exc) or type(exc).__name__
            else:
                if result.exit_code != 0:
                    detail = (result.stderr or result.stdout or "").strip() or "no output"
        if detail is not None:
            store.add_event("box", box.id, "mcp_grants_write_failed", {"detail": detail[:500]})
    reloaded = _reload_mcp(box, store, backend) if detail is None else None
    return {
        **mcp_grants_of(box.id, store=store),
        "written": detail is None,
        "detail": detail,
        "reloaded": reloaded,
    }


def rewrite_mcp_configs(
    servers: Iterable[str],
    *,
    store: FleetStore,
    backend: Backend | None = None,
    by: str | None = None,
) -> dict[str, bool]:
    """Write again the config of every agent granted any of `servers`, after the
    catalogue changed (FLOTTA-125). Each `{box: written}`.

    The grants stay as they are: a server taken off the catalogue is refused at
    the gateway at once, its grant shows as going away, and writing the config
    again takes its entry out and reloads the agent, the same as removing the
    grant. A server whose tools changed gets its new `tools.include`.
    """
    from flotta.mcp import grant_from_row

    out: dict[str, bool] = {}
    for box_id in store.boxes_granted_mcp(list(servers)):
        box = store.get_box(box_id)
        if box is None or is_terminal("box", box.status):
            continue
        grants = tuple(
            grant_from_row(r.server, r.tools, r.ask_first) for r in store.box_mcp_grants(box_id)
        )
        try:
            written = set_mcp_grants(box_id, grants, store=store, backend=backend, by=by)
        except ProvisionError as exc:
            _log.warning("the MCP config of %s was not rewritten: %s", box_id, exc)
            out[box_id] = False
            continue
        out[box_id] = bool(written["written"])
    return out


def mcp_grants_of(box_id: str, *, store: FleetStore) -> dict[str, Any]:
    """An agent's grants as the API answers them."""
    from flotta.mcp import grant_from_row

    rows = store.box_mcp_grants(box_id)
    return {
        "box_id": box_id,
        "servers": [grant_from_row(r.server, r.tools, r.ask_first).to_json() for r in rows],
        "changed_at": max((r.updated_at for r in rows), default=None),
    }


def upgrade_box(
    box_id: str,
    *,
    store: FleetStore,
    image: str | None = None,
    backend: Backend | None = None,
    reason: str = "requested",
) -> dict[str, Any]:
    """Move a live agent onto a new image, keeping everything it remembers.

    The gap this closes: `HERMES_REF` pins what a box image is built from, and
    `just hermes-bump` moves it — but a machine runs the image it was created
    with, so every existing agent stays on whatever Hermes was pinned the day
    it was made, and the distance grows with every bump.

    Destroy-and-recreate is not the workaround. `teardown_box` removes the
    volume, and the volume is the agent: `/data/hermes` holds its memories, its
    skills and its conversation history. The M2 note that "a box's memory
    outlives its machine" was true when destroy was machine-only; it has not
    described `teardown_box` since.

    **An image must be named.** Explicitly, or by `$FLOTTA_FLY_IMAGE` — never
    inferred from the box's own app. With one app per agent, "the app's last
    release" *is* what this machine already runs, so guessing would re-image a
    box to itself and report an upgrade. Refusing is the honest failure.

    Never automatic. Upgrading an agent with months of memory is strictly
    riskier than building a fresh one — `hermes-bump`'s own comment says
    bumping is not mechanical — so this is a verb somebody calls, like
    destroying.
    """
    box = _require_box(store, box_id)
    if is_terminal("box", box.status):
        raise ProvisionError(
            f"box {box_id} is {box.status!r}; there is no machine left to re-image."
        )
    if box.status == "provisioning":
        raise ProvisionError(
            f"box {box_id} is still provisioning. Upgrading mid-create would race the "
            "thread building it; wait for it to settle."
        )

    target_image = (image or "").strip() or _fleet_image()
    if not target_image:
        raise ProvisionError(
            "no image to upgrade to. Pass one explicitly or set $FLOTTA_FLY_IMAGE — "
            "this deliberately does not fall back to the box's own app, because with "
            "one app per agent that is the image it already runs."
        )

    impl = _resolve_backend(box, backend)
    target = box.endpoint or box_id
    before = _peek_image(impl, target)
    if before and _same_image(before, target_image):
        return {
            "box_id": box_id,
            "image": target_image,
            "already_current": True,
            "previous_image": before,
        }

    try:
        impl.reimage(target, target_image)
    except NotSupported as exc:
        raise ProvisionError(f"this substrate cannot re-image a box: {exc}") from exc
    except BackendError as exc:
        # The row is untouched, so the agent is exactly as it was. That is the
        # acceptance criterion that matters: a failed upgrade must not leave a
        # half-migrated box.
        #
        # `UpgradeFailed`, not a bare `ProvisionError`: the substrate broke,
        # which is not the same as the caller asking for something impossible.
        # The API answers the first with 502 and the second with 409, matching
        # `POST /api/boxes`, and the CLI exits 1 rather than 2.
        #
        # The previous image travels in the message because it is the only way
        # back. Without it an operator who wants to undo this has to go and ask
        # the substrate what the box used to run, which is exactly what they
        # cannot do while it is broken.
        failure = (
            f"could not upgrade box {box_id}: {exc}. It still runs "
            f"{before or '(unknown)'}; nothing was changed."
        )
        # Recorded, not only raised. A caller that is still holding the
        # exception can read it; a caller that is not — the app, which asked
        # for this in the background and went back to polling — has nowhere
        # else to learn the upgrade failed, and an upgrade that silently does
        # nothing is indistinguishable from one that has not finished.
        store.add_event(
            "box",
            box_id,
            "upgrade_failed",
            {"reason": reason, "from": before, "to": target_image, "error": str(exc)},
        )
        raise UpgradeFailed(failure) from exc

    after = _peek_image(impl, target)
    store.add_event(
        "box",
        box_id,
        "reimaged",
        {"reason": reason, "from": before, "to": after or target_image},
    )
    return {
        "box_id": box_id,
        "image": after or target_image,
        "previous_image": before,
        "already_current": False,
    }


#: The event a rotation writes. The token is never in it — only when it ends
#: and what it may do.
IDENTITY_ROTATED = "identity_rotated"


def rotate_identity(
    box_id: str,
    *,
    store: FleetStore,
    backend: Backend | None = None,
    ttl_s: int = BOX_TOKEN_TTL_S,
    reason: str = "requested",
) -> dict[str, Any]:
    """Give an existing agent a fresh identity token, on **its own** machine.

    For an identity that is expiring, a signing key that was rotated, or a
    token that predates a scope the agent now needs — M7's `box:peer` was the
    first, and every agent made before it had to be re-minted by hand.

    **The target is the box's endpoint, never configuration.** The recipe this
    replaces took the app from `$FLOTTA_FLY_APP`, which predates one app per
    agent: on the live fleet that is the image-build app, so it would have put
    eng-g's token on the wrong app and left eng-g unchanged. The endpoint is
    the one place that says where an agent actually lives.

    **Only the token, and the box's own password.** `build_identity` also
    produces the machine's id, name, control-plane URL and commit-email domain,
    which were set when the machine was created — re-writing them from whatever
    this process happens to have would be a way to change them by accident.
    They *can* go stale: re-registering after the Railway move left every
    agent calling the old control plane under its old id. Fixing that is a
    deliberate act with its own check, `repair_connection` (FLOTTA-98), which
    compares the machine with this control plane first. The password cannot change that way: it is
    derived from the box's id (FLOTTA-96), and writing it is how an agent made
    before that stops holding the fleet's. Skipped when the control plane has
    no `$FLOTTA_BOX_PASSWORD`, rather than writing a blank one.

    **A sleeping agent stays asleep.** `apply_secrets` deploys without
    `--stage`, which flyctl runs as a restart-only update; its
    `shouldSkipLaunch` skips starting any machine that was not already
    started (flyctl v0.4.102, `internal/command/deploy/machines_launchinput.go`).
    A running agent restarts, which is inherent: the entrypoint reads its
    environment once, at boot.

    The old token stays valid until it expires. Tokens are revoked only by
    rotating the signing key; this does not pretend otherwise.
    """
    from flotta.auth import AuthError, verify
    from flotta.settings import layered

    box = _require_box(store, box_id)
    if is_terminal("box", box.status):
        raise ProvisionError(f"box {box_id} is {box.status!r}; there is no machine to update.")
    if box.status == "provisioning" or not box.endpoint:
        raise ProvisionError(
            f"box {box_id} is still being created and receives its identity as part of "
            "that; wait for it to settle."
        )

    _, secrets = build_identity(box.id, box.name, org=box.org, client=box.client, ttl_s=ttl_s)
    token = secrets.get("FLOTTA_BOX_TOKEN")
    if not token:
        raise ProvisionError(
            "no signing key is configured, so there is no identity to issue. Set "
            "$FLOTTA_SIGNING_KEY where the control plane runs."
        )
    try:
        claims = verify(token)
    except AuthError as exc:  # pragma: no cover - we just minted it with this key
        raise ProvisionError(f"the new token does not verify: {exc}") from exc

    rewritten = {"FLOTTA_BOX_TOKEN": token}
    fleet_password = (layered(store).get("FLOTTA_BOX_PASSWORD") or "").strip()
    if fleet_password:
        rewritten["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] = box_password(fleet_password, box.id)

    impl = _resolve_backend(box, backend)
    try:
        impl.apply_secrets(box.endpoint, rewritten)
    except BackendError as exc:
        store.add_event(
            "box",
            box.id,
            "identity_rotation_failed",
            {"reason": reason, "error": str(exc)[:400]},
        )
        raise UpgradeFailed(
            f"could not give box {box.name} a new identity: {exc}. Its current one is unchanged."
        ) from exc

    result = {
        "box_id": box.id,
        "name": box.name,
        "expires_at": claims.expires_at,
        "scopes": sorted(claims.scopes),
        # Whether the box now holds its own password rather than the fleet's:
        # the timeline is how to tell which agents a FLOTTA-96 rollout reached.
        "own_password": "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD" in rewritten,
    }
    store.add_event(
        "box",
        box.id,
        IDENTITY_ROTATED,
        {
            "reason": reason,
            "expires_at": claims.expires_at,
            "scopes": result["scopes"],
            "own_password": result["own_password"],
        },
    )
    return result


#: The events a connection repair writes (FLOTTA-98).
CONNECTION_REPAIRED = "connection_repaired"
CONNECTION_REPAIR_FAILED = "connection_repair_failed"


def _same_url(a: str | None, b: str | None) -> bool:
    return (a or "").strip().rstrip("/") == (b or "").strip().rstrip("/")


def connection_of(box: Any, info: Any, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Whether `box`'s machine calls this control plane, as itself (FLOTTA-98).

    Every box-side call — a git credential, `flotta-repos`, `flotta-ask` —
    goes to the `FLOTTA_CONTROL_URL` the machine booted with. After the move
    off Railway every agent kept the old address and its pre-move
    `FLOTTA_BOX_ID`, so each of those calls reached a dead host and was
    answered 404; the agent said, correctly, that it had no access, and
    nothing on any screen said why.

    Pure: `info` is the `MachineInfo` the caller already read. `ok` is
    `None` when there is nothing to compare — no machine reading, or a
    control plane that does not know its own address — which is not the
    same as fine and must not be rendered as fine.
    """
    source = os.environ if env is None else env
    wanted = (source.get("FLOTTA_CONTROL_URL") or "").strip()
    has = getattr(info, "control_url", None) if info is not None else None
    has_id = getattr(info, "env_box_id", None) if info is not None else None
    out: dict[str, Any] = {
        "ok": None,
        "control_url": has,
        "wanted_control_url": wanted or None,
        "box_id": has_id,
        "problems": [],
    }
    if info is None or getattr(info, "state", None) == "gone":
        return out
    if not wanted:
        out["problems"].append(
            "this control plane does not know its own address: set $FLOTTA_CONTROL_URL "
            "where it runs, so agents can be pointed at it"
        )
        return out
    problems: list[str] = out["problems"]
    if not has:
        problems.append(
            "it has no control-plane address, so it cannot fetch a git credential, "
            "list its repositories or ask a colleague"
        )
    elif not _same_url(has, wanted):
        problems.append(
            f"it calls {has}, not this control plane ({wanted}), so its git credentials, "
            "repository list and colleagues all fail"
        )
    if has_id and has_id != box.id:
        problems.append(f"it was told it is {has_id}; this agent is {box.id}")
    out["ok"] = not problems
    return out


def repair_connection(
    box_id: str,
    *,
    store: FleetStore,
    backend: Backend | None = None,
) -> dict[str, Any]:
    """Point an agent's machine at this control plane, as itself (FLOTTA-98).

    Rewrites whichever of `FLOTTA_CONTROL_URL`, `FLOTTA_BOX_ID` and
    `FLOTTA_BOX_NAME` disagree with this control plane's record, then issues
    a fresh token with `rotate_identity`, so the token names the box this
    store knows too. `rotate_identity` alone deliberately leaves the
    environment alone, on the reasoning that it "has not changed"; a
    re-registration after a move is exactly when it has.

    The commit-email domain is not touched: changing it changes what the
    agent commits as, and nothing about a move makes the old one wrong.

    **Refuses rather than guesses** when this control plane has no
    `$FLOTTA_CONTROL_URL`: writing a blank, or an address inferred from a
    request, would trade one dead host for another.

    A running agent restarts — twice, when its environment was stale: once
    for the environment and once for the token. An asleep one stays asleep
    and takes both when it next wakes.
    """
    box = _require_box(store, box_id)
    if is_terminal("box", box.status):
        raise ProvisionError(f"box {box_id} is {box.status!r}; there is no machine to repair.")
    if box.status == "provisioning" or not box.endpoint:
        raise ProvisionError(
            f"box {box_id} is still being created and is connected as part of that; "
            "wait for it to settle."
        )
    wanted = (os.environ.get("FLOTTA_CONTROL_URL") or "").strip()
    if not wanted:
        raise ProvisionError(
            "this control plane does not know its own address. Set $FLOTTA_CONTROL_URL "
            "where it runs, then repair again."
        )
    from flotta.auth import resolve_signing_key

    if not resolve_signing_key():
        # Checked before the machine is touched: the token step would refuse
        # after the environment had already been rewritten and restarted.
        raise ProvisionError(
            "no signing key is configured, so there is no identity to issue. Set "
            "$FLOTTA_SIGNING_KEY where the control plane runs, then repair again."
        )

    impl = _resolve_backend(box, backend)
    try:
        info = impl.inspect(box.endpoint)
    except (BackendError, NotSupported, NotImplementedError, AttributeError):
        info = None
    current = {
        "FLOTTA_CONTROL_URL": getattr(info, "control_url", None),
        "FLOTTA_BOX_ID": getattr(info, "env_box_id", None),
    }
    wanted_env = {
        "FLOTTA_CONTROL_URL": wanted,
        "FLOTTA_BOX_ID": box.id,
        "FLOTTA_BOX_NAME": box.name,
    }
    if info is None:
        # Unreadable counts as stale: rewriting a value that was already right
        # costs a restart, leaving a wrong one costs every box-side call.
        changed = dict(wanted_env)
    else:
        changed = {k: wanted_env[k] for k, v in current.items() if not _same_url(v, wanted_env[k])}
        if changed:
            # Not readable from the machine, and free in the same update.
            changed["FLOTTA_BOX_NAME"] = box.name
    if changed:
        applier = getattr(impl, "apply_env", None)
        try:
            if applier is None:
                raise NotSupported(f"{type(impl).__name__} cannot rewrite a machine's environment")
            applier(box.endpoint, changed)
        except (BackendError, NotSupported) as exc:
            store.add_event(
                "box", box.id, CONNECTION_REPAIR_FAILED, {"error": str(exc)[:400], "step": "env"}
            )
            raise UpgradeFailed(
                f"could not point {box.name} at this control plane: {exc}. "
                "Its machine is unchanged."
            ) from exc

    try:
        identity = rotate_identity(box.id, store=store, backend=impl, reason="connection repair")
    except (UpgradeFailed, ProvisionError) as exc:
        # The environment step has already happened by now, so say what it
        # rewrote: "unchanged" in the token step's error is about the token.
        store.add_event(
            "box",
            box.id,
            CONNECTION_REPAIR_FAILED,
            {"error": str(exc)[:400], "step": "token", "rewrote": sorted(changed)},
        )
        raise

    result = {
        "box_id": box.id,
        "name": box.name,
        "control_url": wanted,
        "rewrote": sorted(changed),
        "expires_at": identity["expires_at"],
    }
    store.add_event(
        "box",
        box.id,
        CONNECTION_REPAIRED,
        {"control_url": wanted, "rewrote": result["rewrote"], "was": current["FLOTTA_CONTROL_URL"]},
    )
    return result


def movable_box(box_id: str, store: FleetStore) -> tuple[Box, str]:
    """A box with a settled machine, and the model that machine already runs.

    Not `fleet_model`: a new default reaches no existing agent (FLOTTA-86), and a
    move changes the provider only. Resolved in the order the fleet API reports it
    (`control.app._box_dict`), so the two agree, except that this never falls back
    to today's default: writing a guess would be the model change it must not make.

    Public because the control plane asks before answering `202`: after that, a
    refusal would reach only a log.
    """
    box = _require_box(store, box_id)
    if is_terminal("box", box.status):
        raise ProvisionError(f"box {box_id} is {box.status!r}; there is no machine to update.")
    # Removing a secret on Fly seems to start a stopped machine (FLOTTA-112).
    _refuse_paused(box)
    if box.status == "provisioning" or not box.endpoint:
        raise ProvisionError(f"box {box_id} is still being created; wait for it to settle.")
    # Both write `FLOTTA_MODEL`, so a move landing last would undo the change while
    # the store recorded it.
    under_way = model_changes(store, [box.id]).get(box.id)
    if under_way and under_way["state"] == "pending":
        raise ProvisionError(
            f"{box.name} is switching to {under_way['model']}; wait for that to finish."
        )
    model = (
        store.model_for_box(box.id)
        or store.models_given([box.id]).get(box.id)
        or model_given_by_default()
    )
    if not model:
        raise ProvisionError(
            f"nothing records which model {box.name} runs; set its model, then move it."
        )
    return box, model


def _rewrite_provider(
    box: Box,
    impl: Backend,
    store: FleetStore,
    *,
    write: dict[str, str],
    remove: list[str],
    write_failed: str,
    remove_failed: str,
) -> None:
    """Write, then remove, in that order.

    The other order would leave an agent on OpenRouter with no key to call it with.
    A running agent restarts for each. A sleeping one can wake for it: `eng-d` did on
    2 Nov 2026, and idle sleep put it back. `rotate_identity`'s write alone does not, so
    the removal is the likely cause.
    """
    try:
        impl.apply_secrets(box.endpoint, write)
    except BackendError as exc:
        store.add_event(
            "box", box.id, GATEWAY_MOVE_FAILED, {"step": "write", "error": str(exc)[:400]}
        )
        raise UpgradeFailed(f"{write_failed}: {exc}") from exc
    remover = getattr(impl, "remove_secrets", None)
    try:
        if remover is None:
            raise NotSupported(f"{type(impl).__name__} cannot remove a machine's secrets")
        remover(box.endpoint, remove)
    except (BackendError, NotSupported) as exc:
        store.add_event(
            "box", box.id, GATEWAY_MOVE_FAILED, {"step": "remove", "error": str(exc)[:400]}
        )
        raise UpgradeFailed(f"{remove_failed}: {exc}") from exc


def join_gateway(
    box_id: str,
    *,
    store: FleetStore,
    backend: Backend | None = None,
) -> dict[str, Any]:
    """Move an existing agent onto the gateway, and take the fleet's key off it.

    One agent at a time, because Hermes behaves differently off OpenRouter's URL
    (`inference.gateway_provider`), and that has to be measured on one before the
    rest. `leave_gateway` is the way back. Synchronous: the control plane runs it on
    a thread, as `set_model`.
    """
    from flotta.auth import AuthError
    from flotta.settings import layered

    source = layered(store)
    if not gateway_url(source):
        raise ProvisionError(
            f"there is no gateway to move to. Set ${GATEWAY_URL_ENV} where the control plane runs."
        )
    box, model = movable_box(box_id, store)
    try:
        secrets = gateway_secrets(box.id, model, env=source, org=box.org, client=box.client)
    except AuthError as exc:
        raise ProvisionError(
            "no signing key is configured, so there is no token to give this agent. Set "
            "$FLOTTA_SIGNING_KEY where the control plane runs."
        ) from exc
    _rewrite_provider(
        box,
        _resolve_backend(box, backend),
        store,
        write=secrets,
        remove=["OPENROUTER_API_KEY"],
        write_failed=f"could not move {box.name}, which still calls its provider directly",
        remove_failed=f"{box.name} now calls the gateway, and still holds the provider's key",
    )
    via = secrets["FLOTTA_MODEL_BASE_URL"]
    store.add_event("box", box.id, GATEWAY_JOINED, {"via": via, "model": model})
    return {"box_id": box.id, "name": box.name, "model": model, "via": via}


def leave_gateway(
    box_id: str,
    *,
    store: FleetStore,
    backend: Backend | None = None,
) -> dict[str, Any]:
    """Put an agent back on the provider directly: the way back from `join_gateway`.

    Needs the control plane to still hold the key, so it has to outlive the last
    agent's move.
    """
    from flotta.inference import provider_secrets
    from flotta.settings import layered

    source = layered(store)
    base_url = (source.get("FLOTTA_MODEL_BASE_URL") or "").strip()
    api_key = (source.get("FLOTTA_API_KEY") or "").strip()
    box, model = movable_box(box_id, store)
    if not (base_url and api_key):
        raise ProvisionError(
            "there is no provider to go back to. Set $FLOTTA_MODEL_BASE_URL and "
            "$FLOTTA_API_KEY where the control plane runs."
        )
    _rewrite_provider(
        box,
        _resolve_backend(box, backend),
        store,
        write=provider_secrets(model, base_url, api_key),
        remove=["FLOTTA_GATEWAY_UPSTREAM"],
        write_failed=f"could not move {box.name}, which still calls the gateway",
        remove_failed=f"{box.name} calls its provider directly, with a stale gateway setting",
    )
    store.add_event("box", box.id, GATEWAY_LEFT, {"via": base_url, "model": model})
    return {"box_id": box.id, "name": box.name, "model": model, "via": base_url}


#: The event a model change writes.
MODEL_SET = "model_set"
#: A model change accepted and under way on a thread (FLOTTA-99), and one that
#: could not be written to the machine. `MODEL_SET` is the success.
MODEL_CHANGE_STARTED = "model_change_started"
MODEL_CHANGE_FAILED = "model_change_failed"
#: How long a started change counts as under way with no outcome recorded.
#: A change takes a restart, a minute or two; one older than this was cut off
#: by the control plane itself restarting, and saying "restarting" for ever
#: would be the window lying the other way.
MODEL_CHANGE_STALE_S = 10 * 60


def model_given_by_default(env: Mapping[str, str] | None = None) -> str | None:
    """What an agent with no `model_set` record was given: the environment's
    `FLOTTA_MODEL`, not the stored setting (FLOTTA-99).

    Such an agent predates FLOTTA-39, which first recorded the model at
    creation, and the settings table (FLOTTA-41) came after that, so the
    environment is the only default it can have been made with. Falling back
    to the stored default instead claimed it ran whatever the operator chose
    *later* — which is how "an earlier fleet default" never appeared.
    """
    env = os.environ if env is None else env
    return (env.get("FLOTTA_MODEL") or "").strip() or None


def model_change_of(event: Any, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Where an agent's latest model change stands, from its latest
    `model_change_started` / `model_change_failed` / `model_set` event.

    `{"state": "pending", ...}` while one is under way, `{"state": "failed",
    ...}` when the last one could not be applied, and None when there is
    nothing to say: no change, a finished one, or a start too old to still be
    running (`MODEL_CHANGE_STALE_S`).
    """
    if event is None or event.type == MODEL_SET:
        return None
    payload = event.payload or {}
    out = {"model": payload.get("model"), "at": event.ts}
    if event.type == MODEL_CHANGE_FAILED:
        return {**out, "state": "failed", "detail": payload.get("error")}
    if event.type != MODEL_CHANGE_STARTED:
        return None
    try:
        started = datetime.fromisoformat(event.ts)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    if ((now or datetime.now(UTC)) - started).total_seconds() > MODEL_CHANGE_STALE_S:
        return None
    return {**out, "state": "pending"}


def model_changes(store: FleetStore, box_ids: list[str]) -> dict[str, dict[str, Any]]:
    """`model_change_of` for each of `box_ids` that has something to say."""
    latest = store.latest_box_events(
        box_ids, (MODEL_SET, MODEL_CHANGE_STARTED, MODEL_CHANGE_FAILED)
    )
    found = {box_id: model_change_of(event) for box_id, event in latest.items()}
    return {box_id: change for box_id, change in found.items() if change}


def fleet_model(
    store: FleetStore | None = None, env: Mapping[str, str] | None = None
) -> str | None:
    """The model an agent without its own runs, or None if the fleet has none."""
    from flotta.settings import layered

    source = layered(store, env) if store is not None else (os.environ if env is None else env)
    return (source.get("FLOTTA_MODEL") or "").strip() or None


def allowed_models(
    store: FleetStore | None = None, env: Mapping[str, str] | None = None
) -> list[str]:
    """The models an agent may be given (FLOTTA-85): the fleet's default first,
    then `FLOTTA_MODELS`, each once.

    The operator's list, because every agent calls its model with the fleet's
    one key: whoever picks the model, the operator pays for it. Empty when the
    fleet has neither, and then no agent can be given a model of its own.
    """
    from flotta.settings import layered

    source = layered(store, env) if store is not None else (os.environ if env is None else env)
    listed = (source.get("FLOTTA_MODELS") or "").replace("\n", ",").split(",")
    models = [m for m in [fleet_model(store, env), *(p.strip() for p in listed)] if m]
    return list(dict.fromkeys(models))


def models_for_box(
    box_id: str, store: FleetStore, env: Mapping[str, str] | None = None
) -> list[str]:
    """The models a call from this agent may name (FLOTTA-106): the fleet's, and
    whatever it already runs, since taking a model off the list changes nobody on it.

    Compared exactly, so `:online` or any other variant has to be listed itself.

    Not `model_given_by_default`: `env` is the gateway's, not the control plane's that
    such an agent was made with, and a `FLOTTA_MODEL` left on it would be a model the
    fleet page can't remove. So an agent with no `model_set` record, on a default
    that has since left the list, is refused until it is given a model.
    """
    runs = [store.model_for_box(box_id), store.models_given([box_id]).get(box_id)]
    return list(dict.fromkeys([*allowed_models(store, env), *(m for m in runs if m)]))


def allowed_model(model: str, store: FleetStore) -> str:
    """`model` validated and on the fleet's list, or `InvalidModel`.

    Here rather than only in the API so that every path meets it: the routes,
    `flotta create --model`, and any later caller of `set_model`. Called before
    a substrate is touched, so a refused model costs nothing.
    """
    from flotta.inference import InvalidModel, validate_model

    model = validate_model(model)
    allowed = allowed_models(store)
    if model not in allowed:
        listed = ", ".join(allowed) or "none: the fleet has no models set"
        raise InvalidModel(
            f"{model} is not one of the fleet's models ({listed}). "
            "The fleet's operator chooses which models agents may use."
        )
    return model


def _model_plan(box_id: str, model: str | None, store: FleetStore) -> tuple[Any, str | None, str]:
    """The box, the model it is to have of its own (None: the fleet's), and
    the model its machine will be given — or the refusal, before anything is
    touched."""
    wanted = None if model is None else allowed_model(model, store)
    box = _require_box(store, box_id)
    if is_terminal("box", box.status):
        raise ProvisionError(f"box {box_id} is {box.status!r}; there is no machine to change.")
    if box.status == "provisioning" or not box.endpoint:
        raise ProvisionError(
            f"box {box_id} is still being created. Its model is set as part of that; "
            "wait for it to settle."
        )
    effective = wanted or fleet_model(store)
    if not effective:
        raise ProvisionError(
            "this fleet has no model configured (FLOTTA_MODEL on the control plane), "
            "so there is no default to fall back to."
        )
    return box, wanted, effective


def begin_model_change(box_id: str, model: str | None, *, store: FleetStore) -> dict[str, Any]:
    """Accept a model change that `set_model` will then make on a thread
    (FLOTTA-99), or refuse it now.

    The change itself waits for Fly to restart a running agent, which is
    longer than the web app gives a call: answering after it is how a change
    that worked arrived as "the control plane did not answer". So every
    refusal a caller can act on happens here, and `model_change_started`
    tells the window a restart is coming — which is also what lets an open
    conversation reconnect by itself instead of showing an error.

    One change at a time per agent: a second started while the first is
    under way would race it to the machine, and whichever `secrets import`
    landed last would win regardless of which the store recorded.
    """
    box, wanted, effective = _model_plan(box_id, model, store)
    under_way = model_changes(store, [box.id]).get(box.id)
    if under_way and under_way["state"] == "pending":
        raise ProvisionError(
            f"{box.name} is already switching to {under_way['model']}; wait for that to finish."
        )
    source = "agent" if wanted else "fleet"
    store.add_event("box", box.id, MODEL_CHANGE_STARTED, {"model": effective, "source": source})
    return {"box_id": box.id, "name": box.name, "model": effective, "source": source}


def set_model(
    box_id: str,
    model: str | None,
    *,
    store: FleetStore,
    backend: Backend | None = None,
) -> dict[str, Any]:
    """Change the model an agent runs. None means "the fleet default".

    Writes `FLOTTA_MODEL` to the agent's own machine, the same way
    `rotate_identity` writes its token: a running agent restarts and its boot
    step rewrites Hermes's `config.yaml`; a sleeping one stays asleep and does
    so when it next wakes. The endpoint and key are untouched — they were set
    at creation and stay the fleet's.

    **Substrate first, row second.** If the machine cannot be updated, the
    store still says what the agent really runs, and `model_change_failed`
    says why — which also ends a change `begin_model_change` started.

    Synchronous: the control plane runs it on a thread (FLOTTA-99).
    """
    box, wanted, effective = _model_plan(box_id, model, store)
    default = fleet_model(store)

    impl = _resolve_backend(box, backend)
    try:
        impl.apply_secrets(box.endpoint, {"FLOTTA_MODEL": effective})
    except BackendError as exc:
        store.add_event(
            "box", box.id, MODEL_CHANGE_FAILED, {"model": effective, "error": str(exc)[:400]}
        )
        raise UpgradeFailed(
            f"could not change {box.name}'s model: {exc}. It still runs "
            f"{store.model_for_box(box.id) or default}."
        ) from exc

    store.set_box_model(box.id, wanted)
    source = "agent" if wanted else "fleet"
    store.add_event(
        "box", box.id, MODEL_SET, {"model": effective, "source": source, "reason": "changed"}
    )
    return {"box_id": box.id, "name": box.name, "model": effective, "source": source}


#: An agent's size recorded (FLOTTA-157): what it was created with, or a
#: resize that reached the machine. The payload's `memory_mb` is the latest.
SIZE_SET = "size_set"
#: A resize accepted and under way on a thread, and one the substrate refused.
#: The same three-event shape as a model change, for the same window.
SIZE_CHANGE_STARTED = "size_change_started"
SIZE_CHANGE_FAILED = "size_change_failed"
#: How long a started resize counts as under way with no outcome recorded:
#: `MODEL_CHANGE_STALE_S`'s reasoning, and a resize is the same one restart.
SIZE_CHANGE_STALE_S = MODEL_CHANGE_STALE_S


class InvalidSize(ProvisionError, ValueError):
    """A memory size that is not on `MEMORY_SIZES_MB`."""


def _checked_size(memory_mb: object) -> int:
    try:
        return check_memory_mb(memory_mb)
    except ValueError as exc:
        raise InvalidSize(str(exc)) from exc


def _memory_of_label(label: str | None) -> int | None:
    """2048 from `machine:shared-1x-2048`; None from anything else."""
    tail = (label or "").rpartition("-")[2]
    return int(tail) if tail.isdigit() else None


def _instant_of(text: str | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text or "")
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def box_sizes(store: FleetStore, box_ids: list[str]) -> dict[str, int]:
    """Each agent's memory in MB, as far as the store knows, without asking Fly.

    The newer of two records. The latest `size_set`, written at creation and
    by every resize that landed; and what the hourly metering sweep last read
    off the machine, stamped with the hour it closed. The sweep is up to an
    hour behind a resize made here, and the only one to see a resize made
    anywhere else (`fly machine update`) — so trusting the event for ever left
    such an agent showing a size it no longer had, and answering
    `already_current` to the resize that would have put it back.

    Not `inspect` per request: it is a `flyctl` subprocess, and the fleet list
    would be one per agent. Absent when neither says, which the window shows
    as unknown rather than guessed.
    """
    found: dict[str, tuple[datetime | None, int]] = {}
    for box_id, event in store.latest_box_events(box_ids, (SIZE_SET,)).items():
        value = (event.payload or {}).get("memory_mb")
        if isinstance(value, int) and not isinstance(value, bool):
            found[box_id] = (_instant_of(event.ts), value)
    for box_id, (label, read_at) in store.machine_labels(box_ids).items():
        memory = _memory_of_label(label)
        if memory is None:
            continue
        when = _instant_of(read_at)
        recorded = found.get(box_id)
        if recorded is None or (when is not None and (recorded[0] is None or when > recorded[0])):
            found[box_id] = (when, memory)
    return {box_id: memory for box_id, (_, memory) in found.items()}


def size_change_of(event: Any, *, now: datetime | None = None) -> dict[str, Any] | None:
    """`{"memory_mb", "since"}` while a resize is under way, else None.

    Only the pending state, unlike `model_change_of`: a resize that failed
    leaves the machine the size it was, which `memory_mb` already says, and
    the timeline's `size_change_failed` says why.
    """
    if event is None or event.type != SIZE_CHANGE_STARTED:
        return None
    try:
        started = datetime.fromisoformat(event.ts)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    if ((now or datetime.now(UTC)) - started).total_seconds() > SIZE_CHANGE_STALE_S:
        return None
    return {"memory_mb": (event.payload or {}).get("memory_mb"), "since": event.ts}


def size_changes(store: FleetStore, box_ids: list[str]) -> dict[str, dict[str, Any]]:
    """`size_change_of` for each of `box_ids` with a resize under way."""
    latest = store.latest_box_events(box_ids, (SIZE_SET, SIZE_CHANGE_STARTED, SIZE_CHANGE_FAILED))
    found = {box_id: size_change_of(event) for box_id, event in latest.items()}
    return {box_id: change for box_id, change in found.items() if change}


def _size_plan(box_id: str, memory_mb: object, store: FleetStore) -> tuple[Box, int, int | None]:
    """The box, the size it is to have, and the size it has (None: unknown) —
    or the refusal, before anything is touched."""
    wanted = _checked_size(memory_mb)
    box = _require_box(store, box_id)
    if is_terminal("box", box.status):
        raise ProvisionError(f"box {box_id} is {box.status!r}; there is no machine to resize.")
    if box.status == "provisioning" or not box.endpoint:
        raise ProvisionError(
            f"box {box_id} is still being created; wait for it to settle before resizing it."
        )
    return box, wanted, box_sizes(store, [box.id]).get(box.id)


class ResizeNotSupported(ProvisionError):
    """This agent's substrate cannot change a machine's size (a local stand-in)."""


def begin_resize(
    box_id: str,
    memory_mb: object,
    *,
    store: FleetStore,
    backend: Backend | None = None,
) -> dict[str, Any]:
    """Accept a resize that `resize_box` will then make on a thread, or refuse
    it now (FLOTTA-157).

    `begin_model_change`'s shape, for its reason: Fly restarts a running agent
    to resize it, which outlasts the web app's ten seconds, so every refusal a
    caller can act on happens here and `size_change_started` tells the window
    a restart is coming. An agent already that size answers
    `already_current` and records nothing — there is no restart to warn of.
    """
    box, wanted, current = _size_plan(box_id, memory_mb, store)
    out = {"box_id": box.id, "name": box.name, "memory_mb": wanted}
    # The substrate's own refusals, asked before anything is recorded: one that
    # cannot resize at all, or a machine that cannot have this size (a Fly
    # performance CPU needs 2 GB). Either answered on the thread would show
    # "resizing" for something that was never going to happen.
    try:
        _resolve_backend(box, backend).check_resize(box.endpoint, wanted)
    except NotSupported as exc:
        raise ResizeNotSupported(f"{box.name} cannot be resized: {exc}") from exc
    except SizeRefused as exc:
        raise InvalidSize(f"{box.name} cannot have {wanted} MB: {exc}") from exc
    except BackendError as exc:
        raise ProvisionError(f"could not check whether {box.name} can be resized: {exc}") from exc
    if size_changes(store, [box.id]).get(box.id):
        raise ProvisionError(f"{box.name} is already being resized; wait for that to finish.")
    if current == wanted:
        return {**out, "already_current": True}
    store.add_event("box", box.id, SIZE_CHANGE_STARTED, {"memory_mb": wanted, "from": current})
    return out


def resize_box(
    box_id: str,
    memory_mb: object,
    *,
    store: FleetStore,
    backend: Backend | None = None,
) -> dict[str, Any]:
    """Give an agent's machine `memory_mb` of memory, keeping its volume.

    **Substrate first, row second**, as `set_model` is: a machine Fly refused
    to resize is still the size it was, so `size_set` is written only once
    `Backend.resize` returns, and `size_change_failed` otherwise — which also
    ends a resize `begin_resize` started. A running agent restarts; an asleep
    one stays asleep and wakes at its new size.

    Metering follows by itself: the hourly sweep reads the machine's size from
    the substrate (`flotta.metering`), so hours after the next sweep are priced
    at the new size. Synchronous: the control plane runs it on a thread.
    """
    box, wanted, current = _size_plan(box_id, memory_mb, store)
    impl = _resolve_backend(box, backend)
    try:
        impl.resize(box.endpoint, wanted)
    except BackendError as exc:
        store.add_event(
            "box", box.id, SIZE_CHANGE_FAILED, {"memory_mb": wanted, "error": str(exc)[:400]}
        )
        still = f"{current} MB" if current else "the size it was"
        raise UpgradeFailed(f"could not resize {box.name}: {exc}. It still has {still}.") from exc
    store.add_event(
        "box", box.id, SIZE_SET, {"memory_mb": wanted, "from": current, "reason": "resized"}
    )
    return {"box_id": box.id, "name": box.name, "memory_mb": wanted}


#: How long a resolved release image is reused. The lookup is a `flyctl`
#: subprocess and the answer changes only when somebody builds, so a minute of
#: staleness costs nothing and saves a subprocess per Info panel opened.
_RELEASE_CACHE_S = 60.0
_release_cache: tuple[float, str, str | None] | None = None


def resolve_fleet_image(
    env: Mapping[str, str] | None = None,
    *,
    lookup: Callable[[str], str | None] | None = None,
    now: float | None = None,
) -> tuple[str | None, str]:
    """The image an upgrade moves onto, and **where that answer came from**.

    Two sources, in this order:

    1. `$FLOTTA_FLY_IMAGE`, when it is set. Explicit configuration wins, the
       same way a stored setting wins over the environment — reversing that
       would mean quietly ignoring something a person deliberately wrote down.
    2. Otherwise the newest **complete** release on the fleet's build app,
       which is the app `just fly-up` deploys into. This is the answer that
       cannot go stale: building a new image *is* updating it.

    The source travels with the value because the first option is a standing
    trap. It is a deployment variable naming an image in another Fly app, and
    it has gone stale twice — most recently still naming an app that had been
    deleted, which made the window offer to *downgrade* the one agent that had
    been upgraded. Reporting "this came from the environment, and the newest
    build is something else" is what makes that diagnosable from the app rather
    than from a Railway dashboard.

    **The app must be configured, not defaulted.** `FlyConfig.app` falls back
    to `flotta-box`, which is a name somebody else may own — Fly app names are
    globally unique. Resolving a release from a guessed app would mean reading
    a stranger's registry and handing the fleet whatever they last built. So
    this reads `$FLOTTA_FLY_APP` directly and refuses to guess.
    """
    from flotta.fly import APP_ENV, IMAGE_ENV

    source = os.environ if env is None else env

    configured = (source.get(IMAGE_ENV) or "").strip()
    if configured:
        return configured, "env"

    app = (source.get(APP_ENV) or "").strip()
    if not app:
        return None, "none"

    found = _newest_release_image(app, lookup=lookup, now=now)
    return (found, "release") if found else (None, "none")


def _newest_release_image(
    app: str,
    *,
    lookup: Callable[[str], str | None] | None = None,
    now: float | None = None,
) -> str | None:
    """The build app's newest complete release, cached briefly.

    Never raises: this feeds a panel and an upgrade's default, and a `flyctl`
    that is having a bad minute should cost "we do not know" rather than an
    exception on a read somebody asked for.
    """
    global _release_cache

    moment = time.monotonic() if now is None else now
    if (
        _release_cache is not None
        and _release_cache[1] == app
        and moment - _release_cache[0] < _RELEASE_CACHE_S
    ):
        return _release_cache[2]

    reader = lookup
    if reader is None:

        def reader(name: str) -> str | None:
            impl = _backend_for("fly://")
            current = getattr(impl, "current_image", None)
            # A substrate with no notion of "the app's last release" is not a
            # failure; it just has no answer to this question.
            return current(name) if callable(current) else None

    try:
        found = reader(app)
    except Exception:  # noqa: BLE001 - see the docstring
        return None

    found = (found or "").strip() or None
    _release_cache = (moment, app, found)
    return found


def _fleet_image(env: Mapping[str, str] | None = None) -> str | None:
    """The image this fleet builds — never from a box's own app.

    Kept as the one-value form because every caller that only needs the image
    should not have to unpack a source it does not use.
    """
    return resolve_fleet_image(env)[0]


def _same_image(a: str, b: str) -> bool:
    """Whether two OCI references name the same image.

    Not string equality, because the two sides are written differently and a
    live machine proved it. Fly reports what a machine runs as

        registry.fly.io/little-stream-574:deployment-01M1HY4…@sha256:715985…

    — tag **and** digest — while `--image` and `$FLOTTA_FLY_IMAGE` are written
    without the digest. Compared as strings those never match, so the
    "already on this image" short-circuit never fires and every no-op upgrade
    restarts the agent. That is the third time this comparison has been wrong
    in the same way, and the first time against a real machine's output rather
    than a fixture.

    Digests win when both sides have one: that is the exact identity, and two
    different digests are two different images whatever their tags say. With
    only one digest there is nothing to compare it to, so the `name:tag` half
    decides — safe here because Fly's deployment tags are unique per release
    and never move. A registry where a tag *is* re-pointed would make this
    read a moved tag as unchanged; passing the digest explicitly is the way to
    be sure, and is why the digest branch exists at all.
    """
    left_a, _, digest_a = a.strip().partition("@")
    left_b, _, digest_b = b.strip().partition("@")
    if digest_a and digest_b:
        return digest_a == digest_b
    return left_a == left_b


def _peek_image(impl: Backend, target: str) -> str | None:
    """What the substrate says this box runs, or None if it will not say.

    Optional capability, read through `getattr` the same way `_peek_endpoint`
    reads `existing_endpoint`: a backend that cannot answer should cost the
    event a field, not fail the upgrade.
    """
    reader = getattr(impl, "image_of", None)
    if reader is None:
        return None
    try:
        return reader(target)
    except Exception:  # noqa: BLE001 - a probe must never fail the operation
        return None


def start_box(
    box_id: str,
    *,
    store: FleetStore,
    reason: str = "requested",
    backend: Backend | None = None,
) -> dict[str, Any]:
    """Wake a stopped box back to ``running``. Idempotent.

    The other half of the pivot's central transition, and real infrastructure
    since M1. `wake` and `create` stay distinct on purpose (§M7): conflating
    them is how you end up with forty half-remembered agents instead of forty
    agents — which is also why only a `stopped` box may start.

    Resumes from a memory snapshot when `stop_box` took one, so a box that was
    suspended comes back with its working state rather than cold.
    """
    box = _require_box(store, box_id)
    if box.status == "running":
        return {"box_id": box_id, "status": "running", "already_running": True}

    if box.status != "stopped":
        raise ProvisionError(
            f"box {box_id} is {box.status!r}; only a stopped box can be started. "
            "Starting is waking an existing box, never creating one."
        )

    impl = _resolve_backend(box, backend)
    target = box.endpoint or box_id
    try:
        impl.start(target)
    except BackendError as exc:
        raise ProvisionError(f"could not start box {box_id}: {exc}") from exc

    # Verified, not assumed — `create_box` believes the substrate rather than
    # the plan and this must not be laxer than its sibling. A `start` that
    # returns while the machine is still down would otherwise leave a row
    # claiming `running`, which is the bug this milestone closed for `stop`.
    observed = _observed_state(impl, target, assume="started")
    if observed not in ("started", "unknown"):
        raise ProvisionError(
            f"box {box_id} did not come up: the substrate reports {observed!r}. "
            "The row is unchanged; try again."
        )

    store.add_event("box", box_id, "running", {"reason": reason, "previous_status": box.status})
    store.update_box_status(box_id, "running")
    return {"box_id": box_id, "status": "running", "already_running": False}


class BoxPaused(ProvisionError):
    """A budget has paused this box, and nothing may wake it (FLOTTA-91)."""


class BudgetHolds(ProvisionError):
    """A budget still keeps this box paused, so it can't be unpaused yet."""


def _refuse_paused(box: Box) -> None:
    """For anything that would start a paused box's machine as a side effect."""
    if box.status == "paused":
        raise BoxPaused(f"{box.name} is paused by budget, so it was not woken.")


def wake_box(
    box_id: str,
    *,
    store: FleetStore,
    backend: Backend | None = None,
    reason: str = "addressed",
) -> dict[str, Any]:
    """Ensure a box is up so it can be addressed. Idempotent.

    **A box is meant to be asleep most of the time** — that is the entire cost
    argument (§8.4: an idle fleet costs about what a fleet of disks costs). So
    anything that addresses a box has to be willing to wake it. §M7 says the
    same thing from the other end: "delegation wakes a stopped box; it does not
    create one".

    Distinct from `start_box`, which is the *operator's* verb and refuses
    anything that is not `stopped` — asking to start a mid-provision box is a
    mistake worth reporting. This is the *addressing* path: it accepts a box
    that is already `running` as well as one that is `stopped`, and reconciles a
    row that disagrees with the substrate. That last case happens for real: Fly
    can stop a machine on its own during a host drain, leaving the store saying
    `running` while nothing is listening.

    It is **not** laxer than `start_box` about the two things that matter:

    - Legality is checked **before** any substrate call. Starting a machine and
      *then* refusing the wake would leave it running with the row still saying
      `torn_down` — the exact store/reality disagreement this whole milestone
      exists to remove.
    - A start is **verified** before the row moves. A `start()` that returns
      while the machine is still coming up would otherwise write `running` and
      hand the caller straight into the "host was not found in DNS" failure
      this function was added to prevent.
    """
    box = _require_box(store, box_id)

    # Legality first, before anything can happen to the machine.
    _refuse_paused(box)
    if box.status not in ("running", "stopped"):
        raise ProvisionError(
            f"box {box_id} is {box.status!r}; only a running or stopped box can be addressed"
        )

    impl = _resolve_backend(box, backend)
    target = box.endpoint or box_id
    observed = _observed_state(impl, target, assume="unknown")

    # Only an *observed* non-started state counts as asleep. When `state()`
    # cannot answer we start anyway (it is idempotent) but do not claim to have
    # woken anything — "woke it" should be a fact, not a guess.
    was_asleep = observed not in ("started", "unknown")

    if observed != "started":
        try:
            impl.start(target)
        except BackendError as exc:
            raise ProvisionError(f"could not wake box {box_id}: {exc}") from exc

        after = _observed_state(impl, target, assume="started")
        if after not in ("started", "unknown"):
            raise ProvisionError(
                f"box {box_id} did not come up: the substrate reports {after!r}. "
                "The row is unchanged; try again."
            )

    if box.status == "stopped":
        store.add_event("box", box_id, "running", {"reason": reason, "previous_status": "stopped"})
        store.update_box_status(box_id, "running")

    return {
        "box_id": box_id,
        "status": "running",
        "was_asleep": was_asleep,
        "observed_before": observed,
    }


def watch_task(
    task_id: str,
    *,
    store: FleetStore,
    timeout_s: float | None = None,
    waiter: Waiter | None = None,
    cost_per_second: float | None = None,
) -> dict[str, Any]:
    """Await a task's result and record the terminal status it implies.

    The watcher, unchanged in spirit from v0.1 and moved down a tier: it now
    resolves a **task**, not a machine. Blocks until the container returns, the
    deadline passes, or Modal reports the call gone; every one of those
    outcomes writes a terminal state, so no task is left stranded in `running`.
    """
    # Resolved up front, deliberately. A bad rate must never surface *after* a
    # result is in hand: raising there would leave a `completed` event recorded
    # against a row still marked `running`, and `reconcile` — which resolves the
    # same way — could not rescue it either. Failing here costs nothing and says
    # exactly what is wrong.
    rate = resolve_cost_rate(cost_per_second, _settings_env(store))

    # No default waiter. Modal's `FunctionCall.get` used to be one, and cutting
    # the shard tier removed the only thing that could hand back a verdict.
    # A task's producer — and therefore its waiter — arrives with the workspace
    # tier (M6). Until then this is dormant infrastructure, and refusing is the
    # honest answer: inventing a verdict for work nothing observed is the exact
    # failure `reconcile` exists to prevent.
    if waiter is None:
        raise ProvisionError(
            "no waiter: nothing produces task results yet. The Modal path was "
            "cut with the shard tier and the workspace tier (M6) has not landed. "
            "Pass `waiter=` explicitly to drive a task you are producing yourself."
        )
    wait = waiter
    task = _require_task(store, task_id)

    if is_terminal("task", task.status):
        return {"task_id": task_id, "status": task.status, "already_terminal": True}

    box = _require_box(store, task.box_id)
    call_id = box.endpoint
    if call_id is None:
        payload = {"error": f"box has no endpoint to watch (endpoint={box.endpoint!r})"}
        store.add_event("task", task_id, "failed", payload)
        # Unpriced on purpose — no endpoint means nothing was ever launched.
        store.update_task_status(task_id, "failed")
        return {"task_id": task_id, "status": "failed", "error": payload["error"]}

    try:
        result: Any = wait(call_id, timeout_s)
    except TaskTimeout as exc:
        payload = {"error": f"watch deadline exceeded: {exc}"}
        store.add_event("task", task_id, "timed_out", payload)
        # The most expensive outcome there is — the container ran to its full
        # deadline — so this is the last branch that should go unpriced.
        store.update_task_status(
            task_id, "failed", cost_estimate=estimate_cost(billable_seconds(task), rate)
        )
        return {"task_id": task_id, "status": "failed", "timed_out": True, **payload}
    except Exception as exc:
        payload = {"error": f"task call failed: {type(exc).__name__}: {exc}"}
        store.add_event("task", task_id, "failed", payload)
        # The call existed, so a container almost certainly ran; price it.
        store.update_task_status(
            task_id, "failed", cost_estimate=estimate_cost(billable_seconds(task), rate)
        )
        return {"task_id": task_id, "status": "failed", **payload}

    status, event_type, payload = classify_result(result)
    store.add_event("task", task_id, event_type, payload)
    # Priced from the row as it stands *before* the write: it has no
    # `finished_at` yet, so `billable_seconds` measures to now — the same
    # instant `update_task_status` is about to stamp. One write, no second
    # transition (and `done -> done` would rightly be rejected anyway).
    cost = estimate_cost(billable_seconds(task), rate)
    store.update_task_status(task_id, status, result=payload, cost_estimate=cost)
    return {
        "task_id": task_id,
        "status": status,
        "event": event_type,
        "result": result,
        "cost_estimate": cost,
    }


def task_deadline_s(store: FleetStore, task_id: str) -> int:
    """The `timeout_s` a task was spawned with, from its `spawned` event.

    Falls back to the default when the event is missing or malformed — an
    unreadable payload should not make a stranded task un-reconcilable.
    """
    for event in store.get_events("task", task_id):
        if event.type == "spawned" and isinstance(event.payload, dict):
            value = event.payload.get("timeout_s")
            if isinstance(value, int) and value > 0:
                return value
    return DEFAULT_TIMEOUT_S


def overdue_tasks(
    store: FleetStore, *, now: datetime | None = None, grace_s: int = DEFAULT_GRACE_S
) -> list[tuple[Task, float]]:
    """Live tasks past their own deadline, with how far past, oldest first.

    "Past its deadline" is the task's own `timeout_s` plus a grace period —
    the container hard-exits at that timeout, so anything still `running` well
    beyond it has stopped without anyone recording the outcome.

    Not runs, which share the table: a run's deadline is `RUN_DEADLINE_S`,
    and `reap_runs` closes those.
    """
    now = now or datetime.now(UTC)
    overdue: list[tuple[Task, float]] = []
    for task in store.list_tasks():
        if is_terminal("task", task.status):
            continue
        # A task with no `started_at` is `pending` — waiting for its box, not
        # stranded. Measuring its age from `created_at` would reconcile it to
        # `failed` after `timeout_s + grace` purely for having waited, which is
        # the opposite of what a sleeping fleet is meant to allow.
        started = _parse_ts(task.started_at)
        if started is None or store.get_run(task.id) is not None:
            continue
        age = (now - started).total_seconds()
        limit = task_deadline_s(store, task.id) + grace_s
        if age > limit:
            overdue.append((task, age - limit))
    return sorted(overdue, key=lambda pair: pair[1], reverse=True)


def reconcile(
    store: FleetStore,
    *,
    waiter: Waiter | None = None,
    now: datetime | None = None,
    grace_s: int = DEFAULT_GRACE_S,
    fetch_timeout_s: float = 10.0,
    cost_per_second: float | None = None,
) -> list[dict[str, Any]]:
    """Resolve tasks stranded in a live state past their deadline.

    Why this exists: under D10 the store is written only by local code, so a
    task spawned without `--wait` and never watched sits at `running` forever
    once its container dies. One was found in the wild at 138 hours.

    Recovery is attempted first, and it is usually possible — a Modal call's
    result outlives the container by a long way (measured: a 9-hour-old result
    was still retrievable). So a stranded task's *answer* is normally
    recoverable, not merely losable, and this records it properly.

    When the result cannot be fetched the task is marked `failed` with an
    explicit reason. It is never marked `completed` — inventing a success for
    work nobody observed is exactly the lie the watcher design exists to avoid.

    Note this reconciles **tasks**, not boxes. A stranded task is a verdict
    nobody recorded; a box outliving its tasks is not a fault, it is the
    product. Box-level reconciliation against real backend state is M4's
    control-plane loop.
    """
    # Same reason as `watch_task`: resolved before any work, so a typo'd rate
    # cannot block the very recovery this function exists to perform.
    rate = resolve_cost_rate(cost_per_second, _settings_env(store))

    # Optional here, unlike `watch_task`. Reconciling is mostly *closing* rows
    # nothing will ever report on, and that works with no waiter at all — which
    # is the state of the world until M6. A waiter, when passed, lets a result
    # still be recovered instead of the row merely being closed.
    wait = waiter
    outcomes: list[dict[str, Any]] = []

    for task, over_by in overdue_tasks(store, now=now, grace_s=grace_s):
        box = store.get_box(task.box_id)
        call_id = box.endpoint if box else None
        if call_id is None or wait is None:
            payload = {
                "error": "stranded with no way to recover a result; never reported one",
                "overdue_by_s": round(over_by, 1),
                "reconciled": True,
            }
            store.add_event("task", task.id, "failed", payload)
            # Unpriced on purpose — no endpoint means no container ran.
            store.update_task_status(task.id, "failed")
            outcomes.append({"task_id": task.id, "status": "failed", "recovered": False})
            continue

        try:
            result: Any = wait(call_id, fetch_timeout_s)
        except Exception as exc:
            payload = {
                "error": (
                    f"stranded past its deadline and the result could not be fetched: "
                    f"{type(exc).__name__}: {exc}"
                ),
                "overdue_by_s": round(over_by, 1),
                "reconciled": True,
            }
            store.add_event("task", task.id, "failed", payload)
            # The call existed and ran past its deadline — the container was
            # billed even though its result is unreachable.
            store.update_task_status(
                task.id, "failed", cost_estimate=estimate_cost(billable_seconds(task), rate)
            )
            outcomes.append({"task_id": task.id, "status": "failed", "recovered": False})
            continue

        status, event_type, payload = classify_result(result)
        payload = {**payload, "reconciled": True, "overdue_by_s": round(over_by, 1)}
        store.add_event("task", task.id, event_type, payload)
        cost = estimate_cost(billable_seconds(task), rate)
        store.update_task_status(task.id, status, result=payload, cost_estimate=cost)
        outcomes.append(
            {
                "task_id": task.id,
                "status": status,
                "event": event_type,
                "recovered": status == "done",
                "result": result,
            }
        )

    outcomes.extend(reap_runs(store, now=now, grace_s=grace_s))
    return outcomes


def reap_runs(
    store: FleetStore, *, now: datetime | None = None, grace_s: int = DEFAULT_GRACE_S
) -> list[dict[str, Any]]:
    """Close runs still `running` past `RUN_DEADLINE_S` as `failed`, `stranded`.

    Whoever delivers a run ends it, and gives up well inside the deadline. A
    run still going after it lost its deliverer, most likely to a restart of
    the control plane mid-delivery, and nothing else will ever end it (D10).
    A queued run is not stranded: it is waiting for its box.
    """
    now = now or datetime.now(UTC)
    outcomes: list[dict[str, Any]] = []
    for task in store.list_tasks(status="running"):
        run = store.get_run(task.id)
        started = _parse_ts(run.started_at) if run else None
        if run is None or started is None:
            continue
        over_by = (now - started).total_seconds() - (RUN_DEADLINE_S + grace_s)
        if over_by <= 0:
            continue
        try:
            store.finish_run(run.id, "failed", error_class="stranded", now=now)
        except InvalidTransitionError:
            continue  # its deliverer ended it between the read and the write
        store.add_event(
            "task",
            run.id,
            "failed",
            {"error": "stranded past its deadline", "overdue_by_s": round(over_by, 1)},
        )
        outcomes.append({"task_id": run.id, "status": "failed", "recovered": False})
    return outcomes


def teardown_box(
    box_id: str,
    *,
    store: FleetStore,
    reason: str = "requested",
    canceller: Canceller | None = None,
    backend: Backend | None = None,
) -> dict[str, Any]:
    """Destroy a box, resolving anything still running on it. Idempotent.

    Calling this on an already torn-down box is a no-op that returns cleanly —
    the store's transition table makes `torn_down` terminal, so a second
    attempt would otherwise raise. Cancellation is best-effort: a container
    that already exited cannot be cancelled, and that must not stop the row
    from closing.

    Its live tasks are marked **failed**, not torn down. Tasks have no
    `torn_down` state by design: work that was interrupted did not happen, and
    a verdict that says so is worth more than one that shrugs.
    """
    box = _require_box(store, box_id)

    if box.status == "torn_down":
        # Idempotent, and *convergent*: a box torn down before names were
        # released still holds one, and returning early left it that way
        # forever — a second teardown could not help, so freeing the name of an
        # already-dead agent needed direct access to the fleet database.
        #
        # Releasing here means the rule applies to rows that predate it, which
        # is the difference between a fix and a fix for new boxes only.
        return {
            "box_id": box_id,
            "status": "torn_down",
            "already_torn_down": True,
            "name_released": store.release_name(box_id),
        }

    # Destruction goes through the backend that owns the box: a Fly box loses
    # its machine *and* its volume. `canceller` survives as a pure injection
    # seam — it was the Modal cancel path, and keeping it lets a caller (and
    # the tests) drive destruction without a backend.
    cancelled = False
    cancel_error: str | None = None
    if canceller is not None:
        if box.endpoint is not None:
            try:
                canceller(box.endpoint)
                cancelled = True
            except Exception as exc:
                cancel_error = f"{type(exc).__name__}: {exc}"
    elif box.endpoint:
        try:
            _resolve_backend(box, backend).destroy(box.endpoint)
            cancelled = True
        except (BackendError, ProvisionError) as exc:
            # Best effort, same as a Modal cancel: a machine that is already
            # gone must not stop the row from closing.
            cancel_error = f"{type(exc).__name__}: {exc}"

    failed_tasks: list[str] = []
    for task in store.list_tasks(box_id=box_id):
        if is_terminal("task", task.status):
            continue
        store.add_event(
            "task", task.id, "failed", {"error": f"box torn down ({reason})", "reconciled": False}
        )
        if store.get_run(task.id) is None:
            store.update_task_status(task.id, "failed")
        else:
            # A queued run never started, so it was cancelled, not failed.
            ending = "failed" if task.status == "running" else "cancelled"
            store.finish_run(task.id, ending, error_class="torn_down")
        failed_tasks.append(task.id)

    for workspace in store.list_workspaces(box_id=box_id):
        if workspace.status == "torn_down":
            continue
        store.add_event("workspace", workspace.id, "torn_down", {"reason": f"box {reason}"})
        store.update_workspace_status(workspace.id, "torn_down")

    store.add_event(
        "box",
        box_id,
        "torn_down",
        {
            "reason": reason,
            "cancelled": cancelled,
            "cancel_error": cancel_error,
            "previous_status": box.status,
            "failed_tasks": failed_tasks,
        },
    )
    store.update_box_status(box_id, "torn_down")

    # The agent is gone; its *address* should not be held hostage by the row
    # that records it. Renaming frees `eng-a` for a fresh agent while leaving
    # the history intact — events are keyed by id, not by name.
    released = store.release_name(box_id)

    return {
        "box_id": box_id,
        "status": "torn_down",
        "already_torn_down": False,
        "cancelled": cancelled,
        "cancel_error": cancel_error,
        "failed_tasks": failed_tasks,
        "name_released": released,
    }


# -- idle sleep -------------------------------------------------------------

#: How long a box may sit with nothing happening before it is suspended.
#: Thirty minutes is a guess at the shape of a conversation with an agent —
#: long enough that stepping away for coffee does not cost a cold start, short
#: enough that a box forgotten on a Friday is not billed all weekend.
DEFAULT_IDLE_AFTER_S = 30 * 60
IDLE_AFTER_ENV = "FLOTTA_IDLE_AFTER_S"

#: The event a box's activity is recorded as. Activity lives in the event log
#: rather than a `boxes.last_active_at` column for one blunt reason: the store
#: has no migration machinery — the schema is `CREATE TABLE IF NOT EXISTS` and
#: nothing else — so adding a column would silently break every existing fleet.
#: M0 could sidestep that by refusing pre-M0 files outright; this cannot.
ADDRESSED_EVENT = "addressed"


def resolve_idle_after(
    explicit: float | None = None, env: Mapping[str, str] | None = None
) -> float:
    """`--idle-after` → the fleet setting → `$FLOTTA_IDLE_AFTER_S` → 30 minutes.

    0 disables. The middle step is what `settings.layered` inserts: an explicit
    argument still wins, because a caller who named a number meant it.
    """
    if explicit is not None:
        return float(explicit)
    env = os.environ if env is None else env
    raw = (env.get(IDLE_AFTER_ENV) or "").strip()
    if not raw:
        return float(DEFAULT_IDLE_AFTER_S)
    try:
        return max(0.0, float(raw))
    except ValueError as exc:
        raise ProvisionError(f"{IDLE_AFTER_ENV}={raw!r} is not a number") from exc


def last_activity_at(store: FleetStore, box_id: str) -> datetime | None:
    """When anything last happened to a box, from its timeline.

    The timeline spans the box *and its tasks*, which is what makes this a
    usable idleness signal rather than a box-status one: a box quietly driving
    a task is not idle, and its task's events say so.
    """
    newest: datetime | None = None
    for event in store.get_box_timeline(box_id):
        stamped = _parse_ts(event.ts)
        if stamped is not None and (newest is None or stamped > newest):
            newest = stamped
    return newest


def idle_boxes(
    store: FleetStore,
    *,
    now: datetime | None = None,
    idle_after_s: float | None = None,
) -> list[tuple[Box, float]]:
    """Boxes that are running, unoccupied, and have been quiet long enough.

    Returns `(box, idle_seconds)` so a caller can log *why* rather than just
    what — an operator asking "why did my agent go to sleep" deserves a number.
    """
    threshold = resolve_idle_after(idle_after_s, _settings_env(store))
    if threshold <= 0:
        return []  # explicitly disabled
    current = now or datetime.now(UTC)

    # One query, not one per box: a live task means occupied, whatever the
    # event log says, and a box mid-task must never be suspended.
    occupied = {t.box_id for t in store.list_tasks() if not is_terminal("task", t.status)}

    idle: list[tuple[Box, float]] = []
    for box in store.list_boxes():
        if box.status != "running" or box.id in occupied:
            continue
        seen = last_activity_at(store, box.id)
        if seen is None:
            # No timeline at all is not evidence of idleness — it is evidence
            # of a box this code does not understand. Leave it alone.
            continue
        quiet_for = (current - seen).total_seconds()
        if quiet_for >= threshold:
            idle.append((box, quiet_for))
    return idle


def _release_paused(store: FleetStore, box: Box, impl: Backend, reason: str) -> str:
    """Out of `paused`, by `stopped`, and to `running` only if the machine is up."""
    observed = _observed_state(impl, box.endpoint or box.id, assume="stopped")
    store.add_event("box", box.id, "budget_released", {"reason": reason})
    store.update_box_status(box.id, "stopped")
    if observed != "started":
        return "stopped"
    # Or the idle sweep, which only looks at `running`, would never put it to sleep.
    store.add_event("box", box.id, "running", {"reason": reason, "previous_status": "stopped"})
    store.update_box_status(box.id, "running")
    return "running"


def unpause_box(
    box_id: str,
    *,
    store: FleetStore,
    backend: Backend | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """An owner's way out of `paused`. See `flotta.budgets`. Wakes nothing."""
    box = _require_box(store, box_id)
    if box.status != "paused":
        return {"box_id": box.id, "status": box.status, "already_unpaused": True}
    current = now or datetime.now(UTC)
    held = budget_rules.holding(store, box, current)
    if held:
        raise BudgetHolds(
            f"{held[0].sentence()} Raise the limit or remove the budget to unpause {box.name}."
        )
    status = _release_paused(store, box, _resolve_backend(box, backend), "unpaused")
    return {"box_id": box.id, "status": status, "already_unpaused": False}


def _tell_thresholds(store: FleetStore, found: budget_rules.Standing, boxes: list[Box]) -> None:
    """Each threshold once a period, on the client and on every box it covers."""
    crossed = []
    if found.soft_reached:
        crossed.append("budget_soft_limit")
    if found.reached:
        crossed.append("budget_limit")
    for kind in crossed:
        payload = {"budget": found.details()}
        if store.record_budget_event(found.budget, kind, period_start=found.since, payload=payload):
            for box in boxes:
                if budget_rules.counts_against(found.budget, box):
                    store.add_event("box", box.id, kind, payload)


def _settle_paused_machine(
    store: FleetStore,
    box: Box,
    impl: Backend,
    held: list[budget_rules.Standing],
    *,
    current: datetime,
    idle_after: float,
) -> None:
    """A kill stops the machine now. A pause lets it sleep once it is quiet."""
    target = box.endpoint or box.id
    top = held[0]
    if top.budget.action == "kill":
        if _observed_state(impl, target, assume="unknown") in ("stopped", "suspended", "gone"):
            return
        impl.stop(target)
        store.add_event("box", box.id, "budget_killed", {"budget": top.details()})
        return
    seen = last_activity_at(store, box.id)
    if idle_after <= 0 or seen is None or (current - seen).total_seconds() < idle_after:
        return
    # A live task means occupied, as in `idle_boxes`, however quiet the timeline is:
    # suspending under it strands the watcher holding its call. Tried again next sweep.
    if any(not is_terminal("task", t.status) for t in store.list_tasks(box_id=box.id)):
        return
    if _observed_state(impl, target, assume="unknown") != "started":
        return
    method = _pause(impl, target, prefer_suspend=True)
    store.add_event(
        "box", box.id, "budget_machine_slept", {"budget": top.details(), "method": method}
    )


def enforce_budgets(
    store: FleetStore,
    *,
    backend: Backend | None = None,
    now: datetime | None = None,
    idle_after_s: float | None = None,
) -> list[dict[str, Any]]:
    """Act on every budget: the sweep half of `flotta.budgets`.

    The gateway has already been refusing by the time this runs. What this adds is
    what only something that can reach a machine can do, plus the events. One box
    the substrate refuses doesn't stop the rest, and comes round again next sweep.
    The row moves before the machine does, the reverse of `stop_box`, so a
    substrate that is failing can't leave a box anyone may still wake.
    """
    current = now or datetime.now(UTC)
    idle_after = resolve_idle_after(idle_after_s, _settings_env(store))
    standings = [budget_rules.standing(store, b, current) for b in store.all_budgets()]
    boxes = [b for b in store.list_boxes() if b.status in ("running", "stopped", "paused")]
    for found in standings:
        _tell_thresholds(store, found, boxes)

    outcomes: list[dict[str, Any]] = []
    for box in boxes:
        covering = [s for s in standings if budget_rules.counts_against(s.budget, box)]
        held = sorted((s for s in covering if s.stops), key=lambda s: s.budget.action != "kill")
        try:
            if held:
                if box.status != "paused":
                    store.add_event(
                        "box",
                        box.id,
                        "budget_paused",
                        {"budget": held[0].details(), "previous_status": box.status},
                    )
                    store.update_box_status(box.id, "paused")
                    outcomes.append({"box_id": box.id, "paused": True})
                _settle_paused_machine(
                    store,
                    box,
                    _resolve_backend(box, backend),
                    held,
                    current=current,
                    idle_after=idle_after,
                )
            elif box.status == "paused" and not any(
                s.unpriced and s.budget.action in budget_rules.STOPPING_ACTIONS for s in covering
            ):
                _release_paused(store, box, _resolve_backend(box, backend), "budget")
                outcomes.append({"box_id": box.id, "released": True})
        except (ProvisionError, BackendError) as exc:
            _log.warning("budget action on %s failed: %s", box.name, exc)
            outcomes.append({"box_id": box.id, "error": str(exc)})
    return outcomes


def sleep_idle_boxes(
    store: FleetStore,
    *,
    backend: Backend | None = None,
    now: datetime | None = None,
    idle_after_s: float | None = None,
    prefer_suspend: bool = True,
) -> list[dict[str, Any]]:
    """Suspend boxes nobody is using. The other half of the cost argument.

    Until this existed, "an idle fleet costs about what a fleet of disks costs"
    was **theoretical**: nothing suspended anything, `flotta stop` was manual,
    and a created box billed CPU until someone remembered it.

    Deliberately *not* folded into `reconcile`. That sweep resolves stranded
    *tasks* and its failure mode is a row that lies; this one spends money and
    its failure mode is an agent that went to sleep mid-thought. Different
    concerns, different blast radius, separate functions — and the loop can run
    one without the other.

    **Suspend, not stop**, where the substrate offers it. M1 measured the
    difference: suspend restores RAM (uptime 44.4s → 53.5s across a cycle)
    where a cold stop does not (72.1s → 6.7s). That was worth nothing when PID
    1 was `sleep infinity`; it is worth a great deal now that it is a Hermes
    that takes seconds to import itself.
    """
    outcomes: list[dict[str, Any]] = []
    for box, quiet_for in idle_boxes(store, now=now, idle_after_s=idle_after_s):
        detail = {"idle_s": round(quiet_for, 1), "reason": "idle"}
        try:
            result = stop_box(box.id, store=store, backend=backend, prefer_suspend=prefer_suspend)
        except (ProvisionError, BackendError) as exc:
            # One box refusing to sleep must not stop the sweep. A machine Fly
            # is having trouble with is exactly the one still costing money.
            outcomes.append({**detail, "box_id": box.id, "slept": False, "error": str(exc)})
            continue
        outcomes.append({**detail, "box_id": box.id, "slept": True, "method": result.get("method")})
    return outcomes
