"""Provisioning — spawn workers, watch them finish, tear them down.

**Where each half runs (OQ2 / decision D10).** Two reasons pick a watcher over
worker self-reporting, and the second is the one that lasts:

1. Under D8 the v0.1 store is a plain local SQLite file, which a container
   cannot reach. This is a consequence of that deferral, *not* of the design —
   D3 still points at Turso, and a Turso Cloud store would be reachable from a
   container, so this reason expires when Turso lands.
2. A worker that dies mid-task — OOM, preemption, container kill — writes
   nothing at all. A worker that owned its own status would strand in
   ``running`` forever. The watcher owns the verdict precisely because it
   outlives the worker, and that stays true under Turso.

So the module splits in two:

- ``run_worker`` runs **inside Modal**. It does the work and touches no store.
  This is the only piece `modal deploy` publishes.
- ``spawn_worker`` / ``watch_worker`` / ``teardown`` run **locally**, next to
  the store file, and are its only writers. The CLI (M4), the dashboard (M5)
  and the orchestrator skill (M6) all call these.

So the worker never writes fleet state; it only ever *returns* a result, which
the local watcher translates into a status change. A worker that dies without
returning still resolves, because the watcher — not the worker — owns the
verdict.

**Lifecycle and the events it writes.**

    spawn_worker()   provisioning ──spawned──> running   (+ endpoint)
    watch_worker()   running ──completed──> done
                     running ──failed/timed_out──> failed
    teardown()       any ──torn_down──> torn_down        (idempotent)

The recorded ``endpoint`` is the Modal function-call handle
(``modal://flotta-provision/run_worker/<fc_id>``), not an HTTP URL: v0.1
workers are one-shot, so the call id *is* the address you can later re-attach
to, cancel, or fetch results from. When M6 needs the orchestrator to dial a
*live* worker over MCP, serve-mode plus a `modal.forward` tunnel turns this
column into a real URL without changing the schema.

Import discipline matches `worker/server.py`: `modal` is imported lazily inside
the adapter functions, so the pure store-writing logic here is unit-testable
with fakes and the base `flotta` package keeps no hard Modal dependency.
"""

from __future__ import annotations

import math
import pathlib
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import modal

# Prime sys.path so `flotta.*` resolves when this file is run as a Modal
# entrypoint (`modal deploy src/flotta/provision.py` — src/ is not otherwise on
# sys.path). Defensive: in-container Modal copies this file to /root/provision.py
# where those parents do not exist, and the package arrives via the image mount.
# Duplicated in worker/modal_app.py — it cannot be factored into a helper module,
# because importing that helper is the very thing it exists to make possible.
_HERE = pathlib.Path(__file__).resolve()
_SRC = _HERE.parents[1] if len(_HERE.parents) > 1 else None
if _SRC is not None and (_SRC / "flotta" / "worker").is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from flotta.store import TERMINAL as _TERMINAL  # noqa: E402  the canonical set
from flotta.store import FleetStore, UnknownWorkerError, Worker  # noqa: E402  (needs prime)
from flotta.worker.config import DEFAULT_TIMEOUT_S  # noqa: E402
from flotta.worker.image import worker_image  # noqa: E402

APP_NAME = "flotta-provision"
FUNCTION_NAME = "run_worker"

# Modal enforces a per-function hard cap chosen at decoration time, so it cannot
# track the per-call `timeout_s`. It is set to the ceiling below; the actual
# per-task deadline is enforced inside the container by `_run_task_core`.
MAX_TIMEOUT_S = 3600

# Live-worker cap. v0.1 is a one-worker-at-a-time system and, until now, that
# was documentation rather than behaviour: nothing counted before launching, so
# a loop or an orchestrator ignoring the skill spent money instead of erroring.
# Default 1 makes the documented scope true; raising it is an explicit opt-in to
# territory nothing has tested. 0 means unlimited, for anyone who means it.
DEFAULT_MAX_CONCURRENT = 1
MAX_CONCURRENT_ENV = "FLOTTA_MAX_CONCURRENT"

# Cost estimation (OQ3, decided in M7.3). Modal's billing API was investigated
# and **cannot** attribute cost to a single worker: `Workspace.billing.report()`
# returns line items keyed by *App* id at daily or hourly resolution, every
# worker shares the one `flotta-provision` app, and neither `Function.spawn`
# nor `with_options` accepts a per-call tag. A 12-second worker is simply not
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


def billable_seconds(worker: Worker, now: datetime | None = None) -> float | None:
    """How long the worker existed, as observed from here.

    **Not** the task's `duration_s`. That measures time *inside* `run_worker`
    and excludes image pull and container boot, so it understates what Modal
    bills — a dry run reports `0.0` while its container demonstrably ran. Wall
    time from `spawned_at` is the better proxy: it spans launch to verdict.

    It errs slightly high, because it also includes the local round-trip, and
    that is the direction to err in for a cost estimate.
    """
    started = _parse_ts(worker.spawned_at)
    if started is None:
        return None
    end = _parse_ts(worker.finished_at) or now or datetime.now(UTC)
    return max(0.0, (end - started).total_seconds())


def estimate_cost(seconds: Any, rate: float | None) -> float | None:
    """`seconds × rate`, or None when either is missing or unusable.

    Total by design: an unusable duration yields no estimate rather than an
    exception, because failing to price a worker must never fail recording it.
    """
    if rate is None or not isinstance(seconds, int | float) or isinstance(seconds, bool):
        return None
    if seconds < 0:
        return None
    return round(float(seconds) * rate, 6)


# Grace beyond a worker's own timeout before `reconcile` calls it stranded.
# The container hard-exits at its timeout, so this only has to cover the lag
# between that exit and the local process noticing.
DEFAULT_GRACE_S = 60


def resolve_max_concurrent(
    explicit: int | None = None, env: dict[str, str] | None = None
) -> int | None:
    """How many workers may be live at once: explicit -> env -> default.

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
PROVIDER_KEYS = ("FLOTTA_MODEL", "FLOTTA_MODEL_BASE_URL", "FLOTTA_API_KEY")

# Referenced **by name** rather than snapshotted (M7.1a). The old
# `Secret.from_local_environ` baked in whatever environment ran `modal deploy`,
# so editing `.env` without redeploying silently kept serving stale values.
#
# Measured behaviour, since the docs invite over-reading: a secret becomes
# environment variables when a container **starts**, not per call. Rotating it
# therefore needs no code change and no redeploy — new containers pick it up on
# their own — but a container Modal is keeping warm serves the old value until
# it scales down. `modal app stop flotta-provision -y` forces the turnover.
#
# The secret must exist for the function to start at all, even for `dry_run`
# which needs no provider — so `just deploy` creates an empty one when absent,
# and `just secret-sync` pushes local values into it.
SECRET_NAME = "flotta-provider"


class ProvisionError(Exception):
    """Base error for provisioning operations."""


class WorkerTimeout(ProvisionError):
    """The worker did not produce a result before the watch deadline.

    Adapters translate Modal's own timeout errors into this, so `watch_worker`
    never has to import or catch a Modal exception type.
    """


def _provider_secret() -> modal.Secret:
    """Reference the named provider secret; resolved at call time, not deploy time."""
    return modal.Secret.from_name(SECRET_NAME)


app = modal.App(APP_NAME)


@app.function(image=worker_image, timeout=MAX_TIMEOUT_S, secrets=[_provider_secret()])
def run_worker(
    task: str,
    worker_id: str,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run one task to completion inside a disposable container.

    Returns the same structured shape as the worker's `run_task` MCP tool, so
    the two entry paths stay interchangeable. Never raises for task-level
    failure — a missing provider, an agent exception and a timeout all come
    back as ``completed: False`` so the watcher always has a verdict to record.

    ``dry_run`` skips the agent entirely and reports success. It is the
    provider-free lifecycle path — the same trick as M2's `health` tool (D9) —
    letting the end-to-end script prove spawn → running → **done** → torn_down
    without an API key or a cent of spend.
    """
    import os
    import time

    from flotta.worker.config import WorkerConfig
    from flotta.worker.server import _run_task_core

    started = time.monotonic()

    if dry_run:
        return {
            "completed": True,
            "timed_out": False,
            "task_id": worker_id,
            "final_response": f"dry-run ok: {task}",
            "api_calls": 0,
            "dry_run": True,
            "duration_s": round(time.monotonic() - started, 3),
        }

    cfg = WorkerConfig.from_env(
        {**os.environ, "FLOTTA_TASK": task, "FLOTTA_TIMEOUT_S": str(timeout_s)}
    )
    result = _run_task_core(cfg, task, timeout_s, task_id=worker_id)
    result["dry_run"] = False
    result["duration_s"] = round(time.monotonic() - started, 3)
    return result


# -- endpoint encoding ------------------------------------------------------


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a store timestamp, tolerating anything unexpected."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def endpoint_for(call_id: str) -> str:
    """Encode a Modal function-call id as the worker's stored endpoint."""
    return f"modal://{APP_NAME}/{FUNCTION_NAME}/{call_id}"


def function_call_id(endpoint: str | None) -> str | None:
    """Recover the Modal function-call id from a stored endpoint.

    Returns None for an endpoint that is missing or not a modal:// handle, so
    callers can treat "nothing to cancel / nothing to await" uniformly.
    """
    if not endpoint or not endpoint.startswith("modal://"):
        return None
    call_id = endpoint.rsplit("/", 1)[-1]
    return call_id or None


# -- result classification (pure) -------------------------------------------


def classify_result(result: Any) -> tuple[str, str, dict[str, Any]]:
    """Map a worker result onto ``(status, event_type, payload)``.

    Pure and total: any shape of input yields a verdict, because leaving a
    worker stuck in `running` because its result was malformed is strictly
    worse than recording a failure.
    """
    if not isinstance(result, dict):
        return "failed", "failed", {"error": f"malformed worker result: {result!r}"}

    if result.get("completed"):
        payload = {
            "final_response": result.get("final_response"),
            "api_calls": result.get("api_calls"),
            "task_id": result.get("task_id"),
            "duration_s": result.get("duration_s"),
            "dry_run": bool(result.get("dry_run")),
        }
        return "done", "completed", payload

    error = result.get("error") or "worker reported failure without an error message"
    if result.get("timed_out"):
        return "failed", "timed_out", {"error": error, "duration_s": result.get("duration_s")}
    return "failed", "failed", {"error": error, "duration_s": result.get("duration_s")}


# -- Modal adapters (lazy; swapped for fakes in tests) ----------------------

Launcher = Callable[..., str]
Waiter = Callable[[str, float | None], Any]
Canceller = Callable[[str], None]


def _modal_launcher(*, task: str, worker_id: str, timeout_s: int, dry_run: bool) -> str:
    """Spawn the deployed `run_worker` and return its function-call id."""
    fn = modal.Function.from_name(APP_NAME, FUNCTION_NAME)
    call = fn.spawn(task=task, worker_id=worker_id, timeout_s=timeout_s, dry_run=dry_run)
    return str(call.object_id)


def _modal_waiter(call_id: str, timeout_s: float | None) -> Any:
    """Block on a function call's result, normalizing timeouts."""
    from modal.exception import TimeoutError as ModalTimeoutError

    call = modal.FunctionCall.from_id(call_id)
    try:
        return call.get(timeout=timeout_s)
    except ModalTimeoutError as exc:
        raise WorkerTimeout(str(exc)) from exc


def _modal_canceller(call_id: str) -> None:
    """Cancel a function call, stopping the container running it.

    Deliberately *not* ``terminate_containers=True``. The SDK accepts that
    argument, but the Modal server rejects the request outright::

        InvalidError: FunctionCallCancel request must have a function_call_id
        and terminate_containers must be false

    Because `teardown` records a cancel failure without raising, that rejection
    was silent: the worker's row closed while its container kept running and
    billing. A plain `cancel()` already stops execution and marks the inputs
    terminated, which is the whole requirement.
    """
    modal.FunctionCall.from_id(call_id).cancel()


# -- local orchestration (the only writers to the store) --------------------


def _require_worker(store: FleetStore, worker_id: str):
    worker = store.get_worker(worker_id)
    if worker is None:
        raise UnknownWorkerError(f"no worker with id {worker_id!r}")
    return worker


def spawn_worker(
    task: str,
    *,
    store: FleetStore,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    dry_run: bool = False,
    worker_id: str | None = None,
    launcher: Launcher | None = None,
    max_concurrent: int | None = None,
) -> dict[str, str]:
    """Launch a worker for `task` and record it. Returns ``{worker_id, endpoint}``.

    The row is created *before* the launch so a launch that fails still leaves
    a worker to explain it — the failure is recorded and re-raised rather than
    vanishing.
    """
    if timeout_s > MAX_TIMEOUT_S:
        raise ValueError(f"timeout_s {timeout_s} exceeds the container cap of {MAX_TIMEOUT_S}s")

    launch = launcher or _modal_launcher
    # The cap is enforced by the store so the count and the insert share one
    # transaction — checking here and inserting after would race.
    worker = store.create_worker(
        task, worker_id=worker_id, max_live=resolve_max_concurrent(max_concurrent)
    )
    store.add_event(
        worker.id, "spawned", {"task": task, "timeout_s": timeout_s, "dry_run": dry_run}
    )

    try:
        call_id = launch(task=task, worker_id=worker.id, timeout_s=timeout_s, dry_run=dry_run)
    except Exception as exc:
        detail = f"spawn failed: {type(exc).__name__}: {exc}"
        store.add_event(worker.id, "failed", {"error": detail})
        # Deliberately unpriced: the launch never happened, so no container
        # ran. Charging for compute that did not occur overstates just as
        # surely as the dry-run zero understated.
        store.update_status(worker.id, "failed")
        raise ProvisionError(detail) from exc

    endpoint = endpoint_for(call_id)
    store.update_status(worker.id, "running", endpoint=endpoint)
    store.add_event(worker.id, "running", {"endpoint": endpoint, "function_call_id": call_id})
    return {"worker_id": worker.id, "endpoint": endpoint}


def watch_worker(
    worker_id: str,
    *,
    store: FleetStore,
    timeout_s: float | None = None,
    waiter: Waiter | None = None,
    cost_per_second: float | None = None,
) -> dict[str, Any]:
    """Await a worker's result and record the terminal status it implies.

    This is the M3.4 watcher. Blocks until the container returns, the deadline
    passes, or Modal reports the call gone; every one of those outcomes writes
    a terminal state, so no worker is left stranded in `running`.
    """
    # Resolved up front, deliberately. A bad rate must never surface *after* a
    # result is in hand: raising there would leave a `completed` event recorded
    # against a row still marked `running`, and `reconcile` — which resolves the
    # same way — could not rescue it either. Failing here costs nothing and says
    # exactly what is wrong.
    rate = resolve_cost_rate(cost_per_second)

    wait = waiter or _modal_waiter
    worker = _require_worker(store, worker_id)

    if worker.status in _TERMINAL:
        return {"worker_id": worker_id, "status": worker.status, "already_terminal": True}

    call_id = function_call_id(worker.endpoint)
    if call_id is None:
        payload = {"error": f"worker has no modal endpoint to watch (endpoint={worker.endpoint!r})"}
        store.add_event(worker_id, "failed", payload)
        # Unpriced on purpose — no endpoint means nothing was ever launched.
        store.update_status(worker_id, "failed")
        return {"worker_id": worker_id, "status": "failed", "error": payload["error"]}

    try:
        result: Any = wait(call_id, timeout_s)
    except WorkerTimeout as exc:
        payload = {"error": f"watch deadline exceeded: {exc}"}
        store.add_event(worker_id, "timed_out", payload)
        # The most expensive outcome there is — the container ran to its full
        # deadline — so this is the last branch that should go unpriced.
        store.update_status(
            worker_id, "failed", cost_estimate=estimate_cost(billable_seconds(worker), rate)
        )
        return {"worker_id": worker_id, "status": "failed", "timed_out": True, **payload}
    except Exception as exc:
        payload = {"error": f"worker call failed: {type(exc).__name__}: {exc}"}
        store.add_event(worker_id, "failed", payload)
        # The call existed, so a container almost certainly ran; price it.
        store.update_status(
            worker_id, "failed", cost_estimate=estimate_cost(billable_seconds(worker), rate)
        )
        return {"worker_id": worker_id, "status": "failed", **payload}

    status, event_type, payload = classify_result(result)
    store.add_event(worker_id, event_type, payload)
    # Priced from the row as it stands *before* the write: it has no
    # `finished_at` yet, so `billable_seconds` measures to now — the same
    # instant `update_status` is about to stamp. One write, no second
    # transition (and `done -> done` would rightly be rejected anyway).
    cost = estimate_cost(billable_seconds(worker), rate)
    store.update_status(worker_id, status, cost_estimate=cost)
    return {
        "worker_id": worker_id,
        "status": status,
        "event": event_type,
        "result": result,
        "cost_estimate": cost,
    }


def worker_deadline_s(store: FleetStore, worker_id: str) -> int:
    """The `timeout_s` a worker was spawned with, from its `spawned` event.

    Falls back to the default when the event is missing or malformed — an
    unreadable payload should not make a stranded worker un-reconcilable.
    """
    for event in store.get_events(worker_id):
        if event.type == "spawned" and isinstance(event.payload, dict):
            value = event.payload.get("timeout_s")
            if isinstance(value, int) and value > 0:
                return value
    return DEFAULT_TIMEOUT_S


def overdue_workers(
    store: FleetStore, *, now: datetime | None = None, grace_s: int = DEFAULT_GRACE_S
) -> list[tuple[Worker, float]]:
    """Live workers past their own deadline, with how far past, oldest first.

    "Past its deadline" is the worker's own `timeout_s` plus a grace period —
    the container hard-exits at that timeout, so anything still `running` well
    beyond it has stopped without anyone recording the outcome.
    """
    now = now or datetime.now(UTC)
    overdue: list[tuple[Worker, float]] = []
    for worker in store.list_workers():
        if worker.status in _TERMINAL:
            continue
        started = _parse_ts(worker.spawned_at)
        if started is None:
            continue
        age = (now - started).total_seconds()
        limit = worker_deadline_s(store, worker.id) + grace_s
        if age > limit:
            overdue.append((worker, age - limit))
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
    """Resolve workers stranded in a live state past their deadline.

    Why this exists: under D10 the store is written only by local code, so a
    worker spawned without `--wait` and never watched sits at `running` forever
    once its container dies. One was found in the wild at 138 hours.

    Recovery is attempted first, and it is usually possible — a Modal call's
    result outlives the container by a long way (measured: a 9-hour-old result
    was still retrievable). So a stranded worker's *answer* is normally
    recoverable, not merely losable, and this records it properly.

    When the result cannot be fetched the worker is marked `failed` with an
    explicit reason. It is never marked `completed` — inventing a success for
    work nobody observed is exactly the lie the watcher design exists to avoid.
    """
    # Same reason as `watch_worker`: resolved before any work, so a typo'd rate
    # cannot block the very recovery this function exists to perform.
    rate = resolve_cost_rate(cost_per_second)

    wait = waiter or _modal_waiter
    outcomes: list[dict[str, Any]] = []

    for worker, over_by in overdue_workers(store, now=now, grace_s=grace_s):
        call_id = function_call_id(worker.endpoint)
        if call_id is None:
            payload = {
                "error": "stranded with no modal endpoint; never reported a result",
                "overdue_by_s": round(over_by, 1),
                "reconciled": True,
            }
            store.add_event(worker.id, "failed", payload)
            # Unpriced on purpose — no endpoint means no container ran.
            store.update_status(worker.id, "failed")
            outcomes.append({"worker_id": worker.id, "status": "failed", "recovered": False})
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
            store.add_event(worker.id, "failed", payload)
            # The call existed and ran past its deadline — the container was
            # billed even though its result is unreachable.
            store.update_status(
                worker.id, "failed", cost_estimate=estimate_cost(billable_seconds(worker), rate)
            )
            outcomes.append({"worker_id": worker.id, "status": "failed", "recovered": False})
            continue

        status, event_type, payload = classify_result(result)
        payload = {**payload, "reconciled": True, "overdue_by_s": round(over_by, 1)}
        store.add_event(worker.id, event_type, payload)
        cost = estimate_cost(billable_seconds(worker), rate)
        store.update_status(worker.id, status, cost_estimate=cost)
        outcomes.append(
            {
                "worker_id": worker.id,
                "status": status,
                "event": event_type,
                "recovered": status == "done",
                "result": result,
            }
        )

    return outcomes


def teardown(
    worker_id: str,
    *,
    store: FleetStore,
    reason: str = "requested",
    canceller: Canceller | None = None,
) -> dict[str, Any]:
    """Stop a worker's container and close its row. Idempotent.

    Calling this on an already torn-down worker is a no-op that returns
    cleanly — the store's transition table makes `torn_down` terminal, so a
    second attempt would otherwise raise. Cancellation is best-effort: a
    container that already exited cannot be cancelled, and that must not stop
    the row from closing.
    """
    cancel = canceller or _modal_canceller
    worker = _require_worker(store, worker_id)

    if worker.status == "torn_down":
        return {"worker_id": worker_id, "status": "torn_down", "already_torn_down": True}

    call_id = function_call_id(worker.endpoint)
    cancelled = False
    cancel_error: str | None = None
    if call_id is not None:
        try:
            cancel(call_id)
            cancelled = True
        except Exception as exc:
            cancel_error = f"{type(exc).__name__}: {exc}"

    store.add_event(
        worker_id,
        "torn_down",
        {
            "reason": reason,
            "cancelled": cancelled,
            "cancel_error": cancel_error,
            "previous_status": worker.status,
        },
    )
    store.update_status(worker_id, "torn_down")
    return {
        "worker_id": worker_id,
        "status": "torn_down",
        "already_torn_down": False,
        "cancelled": cancelled,
        "cancel_error": cancel_error,
    }
