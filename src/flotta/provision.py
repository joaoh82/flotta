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

import math
import os
import secrets as secrets_module
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from flotta.backend import (
    Backend,
    BackendError,
    BoxSpec,
    UnknownBackendError,
)
from flotta.backend import backend_for as _backend_for
from flotta.backend import pause as _pause
from flotta.store import (
    Box,
    FleetStore,
    Task,
    UnknownEntityError,
    is_terminal,
)

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


def resolve_cost_rate(
    explicit: float | None = None, env: dict[str, str] | None = None
) -> float | None:
    """Dollars per container-second, or None when the operator has not set one."""
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


def resolve_max_concurrent(
    explicit: int | None = None, env: dict[str, str] | None = None
) -> int | None:
    """How many tasks may be live at once: explicit -> env -> default.

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


#: The box's dashboard user. Constant, because the door logs in as it — the
#: username was never the secret, the password is.
BOX_AUTH_USER = "flotta"


def fleet_secrets(env: dict[str, str] | None = None) -> tuple[dict[str, str], list[str]]:
    """What every box in this fleet needs to boot, and what is missing.

    Returns `(secrets, missing)`. Distinct from `build_identity`, which is what
    makes a box *itself*: these are the same for every agent, and until M8.3
    they were never injected at all — one Fly app held the whole fleet and
    `just fly-auth` / `just fly-secrets` had set them on it once, by hand.

    One app per agent removed that accident, and the first box created through
    the API crash-looped:

        box_entrypoint.sh: HERMES_DASHBOARD_BASIC_AUTH_USERNAME: set it with:
        just fly-auth
        Main child exited normally with code: 1

    **The password is fleet-wide and must stay so.** The door logs into every
    box on the caller's behalf using its own copy; a password generated per box
    would lock the door out of the agent it just created. The session *secret*
    is per-app state and is generated fresh, because nothing else needs to know
    it.
    """
    source = os.environ if env is None else env
    secrets: dict[str, str] = {}
    missing: list[str] = []

    password = (source.get("FLOTTA_BOX_PASSWORD") or "").strip()
    if password:
        secrets["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"] = BOX_AUTH_USER
        secrets["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] = password
        secrets["HERMES_DASHBOARD_BASIC_AUTH_SECRET"] = secrets_module.token_urlsafe(48)
    else:
        # Without it the entrypoint refuses to serve and the machine restarts
        # forever, which is a far worse failure than being told now.
        missing.append("FLOTTA_BOX_PASSWORD")

    model = (source.get("FLOTTA_MODEL") or "").strip()
    base_url = (source.get("FLOTTA_MODEL_BASE_URL") or "").strip()
    api_key = (source.get("FLOTTA_API_KEY") or "").strip()
    if model and base_url and api_key:
        secrets["FLOTTA_MODEL"] = model
        secrets["FLOTTA_MODEL_BASE_URL"] = base_url
        secrets["FLOTTA_API_KEY"] = api_key

        # **Two consumers, two vocabularies.** `flotta.box.run` reads the
        # FLOTTA_* names; `hermes serve` — the surface the app actually talks
        # to — resolves a provider through Hermes's own config and ignores
        # them, so a box with only FLOTTA_* answers every turn with "No
        # inference provider configured". The native name depends on the
        # endpoint, so it is derived rather than guessed.
        if "openrouter" in base_url.lower():
            secrets["OPENROUTER_API_KEY"] = api_key
        else:
            secrets["OPENAI_API_KEY"] = api_key
            secrets["OPENAI_BASE_URL"] = base_url
    else:
        missing.append("FLOTTA_MODEL / FLOTTA_MODEL_BASE_URL / FLOTTA_API_KEY")

    return secrets, missing


def build_identity(
    box_id: str,
    box_name: str,
    *,
    ttl_s: int = BOX_TOKEN_TTL_S,
    env: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """A box's own identity, split by what may be read off the machine.

    Returns `(env, secrets)`. The split is the point:

    - **env** — `FLOTTA_BOX_ID`, `FLOTTA_BOX_NAME`, and where to reach the
      control plane and what domain to sign commits under. All of it appears in
      `fly machine status`, and none of it is a secret.
    - **secrets** — `FLOTTA_BOX_TOKEN`, and nothing else. It authenticates as
      the box, so it must never be in the machine's configuration.

    Everything is omitted rather than guessed. A box with no token still boots
    and still commits under its own name; it just cannot fetch a GitHub
    credential, which is the same state every box was in before this existed.

    Shared with `flotta token box` so the block that command prints is exactly
    what creation injects. Two builders would drift, and the drift would be
    invisible until a rotated identity behaved differently from a fresh one.
    """
    source = os.environ if env is None else env
    from flotta.auth import SCOPE_GIT_CREDENTIAL, AuthError, box_subject, mint

    box_env = {"FLOTTA_BOX_ID": box_id, "FLOTTA_BOX_NAME": box_name}

    control_url = (source.get("FLOTTA_CONTROL_URL") or "").strip()
    if control_url:
        box_env["FLOTTA_CONTROL_URL"] = control_url

    email_domain = (
        source.get("FLOTTA_GIT_EMAIL_DOMAIN") or source.get("FLOTTA_DOMAIN") or ""
    ).strip()
    if email_domain:
        box_env["FLOTTA_GIT_EMAIL_DOMAIN"] = email_domain

    try:
        token = mint(
            subject=box_subject(box_id),
            scopes={SCOPE_GIT_CREDENTIAL},
            ttl_s=ttl_s,
            env=source,
        )
    except AuthError:
        # No signing key is a legitimate state — it is the whole loopback
        # development path, where `create_app` admits every request. Refusing
        # to create a box because auth is unconfigured would make identity a
        # prerequisite for having a fleet at all.
        return box_env, {}

    return box_env, {"FLOTTA_BOX_TOKEN": token}


def create_box(
    name: str,
    *,
    store: FleetStore,
    backend: Backend | None = None,
    spec: BoxSpec | None = None,
) -> dict[str, Any]:
    """Provision a **persistent** box and record it. Returns ``{box_id, endpoint}``.

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
    impl = backend or _backend_for("fly://")  # default substrate for persistent boxes

    # `create` is idempotent over the *machine* — it adopts `machines[0]` rather
    # than adding a second — but the store is not, so a second `create_box`
    # under a different name would mint a second row pointing at the same
    # endpoint. Two rows, one machine: `stop_box` on either would then lie
    # about the other. Unreachable through today's CLI, which is exactly why it
    # is worth closing before `flotta create` exists to reach it.
    probe = _peek_endpoint(impl, name)
    if probe:
        for existing in store.list_boxes():
            if existing.endpoint == probe and not is_terminal("box", existing.status):
                raise BoxOccupied(
                    f"box {existing.id} ({existing.name}) already occupies {probe}. "
                    "One machine hosts one box; destroy it with `flotta kill` "
                    "before creating another, or point FLOTTA_FLY_APP elsewhere."
                )

    box = store.create_box(name)
    store.add_event("box", box.id, "provisioning", {"name": name, "backend": impl.scheme})

    # Identity travels with the machine rather than arriving afterwards. The
    # ordering already works: `store.create_box` produced the id above, so
    # `box:<id>` can be signed before anything exists to sign for. Doing it in
    # a second command — which is what `just box-identity` was — is wrong for a
    # caller that has no shell: `POST /api/boxes` is one request, and M8's
    # "create Agent B" is one button.
    base = spec or BoxSpec(name=name)
    identity_env, identity_secrets = build_identity(box.id, name)

    # What every box needs, as opposed to what makes this one itself. Missing
    # values are recorded rather than raised: a box with no provider key still
    # boots and can be fixed, and refusing to create would strand the operator
    # with no agent and no obvious way to get one.
    fleet, missing_fleet = fleet_secrets()
    spec = replace(
        base,
        env={**identity_env, **base.env},
        secrets={**identity_secrets, **fleet, **base.secrets},
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

    # An adopted machine did not exist when its secrets were written, because
    # it existed before `create` ran at all. On Fly that is the *normal* path,
    # not an edge: the only way to release an image is `fly deploy`, which
    # creates a machine while doing it — so `create` always has one to adopt,
    # and the whole create-time injection above would never have fired in
    # production. Found by comparing timestamps on the fleet's first box: its
    # machine was five days older than its row.
    identity_error: str | None = None
    if handle.adopted and identity_secrets:
        try:
            impl.apply_secrets(handle.endpoint, identity_secrets)
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
    rate = resolve_cost_rate(cost_per_second)

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
        if started is None:
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
    rate = resolve_cost_rate(cost_per_second)

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
        return {"box_id": box_id, "status": "torn_down", "already_torn_down": True}

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
        store.update_task_status(task.id, "failed")
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
    return {
        "box_id": box_id,
        "status": "torn_down",
        "already_torn_down": False,
        "cancelled": cancelled,
        "cancel_error": cancel_error,
        "failed_tasks": failed_tasks,
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


def resolve_idle_after(explicit: float | None = None, env: dict[str, str] | None = None) -> float:
    """`--idle-after` → `$FLOTTA_IDLE_AFTER_S` → 30 minutes. 0 disables."""
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
    threshold = resolve_idle_after(idle_after_s)
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
