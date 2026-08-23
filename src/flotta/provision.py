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
import pathlib
import sys
import uuid
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

from flotta.store import (  # noqa: E402  (needs the sys.path prime above)
    Box,
    ConcurrencyLimitError,
    FleetStore,
    Task,
    UnknownEntityError,
    is_terminal,
)
from flotta.worker.config import DEFAULT_TIMEOUT_S  # noqa: E402
from flotta.worker.image import HERMES_REF, worker_image  # noqa: E402

APP_NAME = "flotta-provision"
FUNCTION_NAME = "run_worker"

# Modal enforces a per-function hard cap chosen at decoration time, so it cannot
# track the per-call `timeout_s`. It is set to the ceiling below; the actual
# per-task deadline is enforced inside the container by `_run_task_core`.
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


class TaskTimeout(ProvisionError):
    """The task did not produce a result before the watch deadline.

    Adapters translate Modal's own timeout errors into this, so `watch_task`
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
    """Encode a Modal function-call id as the box's stored endpoint."""
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
        raise TaskTimeout(str(exc)) from exc


def _modal_canceller(call_id: str) -> None:
    """Cancel a function call, stopping the container running it.

    Deliberately *not* ``terminate_containers=True``. The SDK accepts that
    argument, but the Modal server rejects the request outright::

        InvalidError: FunctionCallCancel request must have a function_call_id
        and terminate_containers must be false

    Because `teardown` records a cancel failure without raising, that rejection
    was silent: the box's row closed while its container kept running and
    billing. A plain `cancel()` already stops execution and marks the inputs
    terminated, which is the whole requirement.
    """
    modal.FunctionCall.from_id(call_id).cancel()


# -- local orchestration (the only writers to the store) --------------------


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


def spawn_box(
    task: str,
    *,
    store: FleetStore,
    name: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    dry_run: bool = False,
    box_id: str | None = None,
    task_id: str | None = None,
    launcher: Launcher | None = None,
    max_concurrent: int | None = None,
) -> dict[str, str]:
    """Create a box, put one task on it, launch it. Returns ids + endpoint.

    The successor to v0.1's `spawn_worker`, and the place the pivot is most
    visible: what used to be one row is now two. The **box** is the machine and
    owns the endpoint; the **task** is the work and owns the verdict.

    Under the Modal backend a box is still disposable — Modal cannot stop and
    resume a container, so every spawn mints a fresh one. That is not the
    target shape; it is the honest shape *for this backend*, and it is exactly
    the asymmetry M1's `Backend` protocol exists to make explicit.

    Both rows are created *before* the launch so a launch that fails still
    leaves something to explain it — the failure is recorded and re-raised
    rather than vanishing.
    """
    if timeout_s > MAX_TIMEOUT_S:
        raise ValueError(f"timeout_s {timeout_s} exceeds the container cap of {MAX_TIMEOUT_S}s")

    launch = launcher or _modal_launcher
    cap = resolve_max_concurrent(max_concurrent)

    # Pre-check before creating anything, so the common refusal leaves no rows
    # behind. The cap is *also* passed to `create_task`, where it shares a
    # transaction with the insert — that is the backstop against two spawns
    # racing, which this check alone cannot close.
    if cap is not None:
        live = store.list_tasks()
        live_ids = [t.id for t in live if not is_terminal("task", t.status)]
        if len(live_ids) >= cap:
            raise ConcurrencyLimitError(cap, live_ids, noun="task")

    box = store.create_box(name or f"box-{uuid.uuid4().hex[:8]}", box_id=box_id)
    try:
        work = store.create_task(box.id, task, task_id=task_id, max_live=cap)
    except ConcurrencyLimitError:
        # Lost the race between the pre-check and here. Close the orphan box
        # rather than leaving a machine row nothing will ever claim.
        store.add_event("box", box.id, "torn_down", {"reason": "concurrency limit"})
        store.update_box_status(box.id, "torn_down")
        raise

    # `hermes_ref` is recorded per task, not just configured globally: the pin
    # moves over time, so "which Hermes ran this task?" is a fact about the
    # task, answerable months later, not a fact about today's config.
    store.add_event(
        "task",
        work.id,
        "spawned",
        {
            "task": task,
            "timeout_s": timeout_s,
            "dry_run": dry_run,
            "hermes_ref": HERMES_REF,
            "box_id": box.id,
        },
    )

    try:
        call_id = launch(task=task, worker_id=work.id, timeout_s=timeout_s, dry_run=dry_run)
    except Exception as exc:
        detail = f"spawn failed: {type(exc).__name__}: {exc}"
        store.add_event("task", work.id, "failed", {"error": detail})
        # Deliberately unpriced: the launch never happened, so no container
        # ran. Charging for compute that did not occur overstates just as
        # surely as the dry-run zero understated.
        store.update_task_status(work.id, "failed")
        store.add_event("box", box.id, "torn_down", {"reason": "spawn failed"})
        store.update_box_status(box.id, "torn_down")
        raise ProvisionError(detail) from exc

    endpoint = endpoint_for(call_id)
    store.update_box_status(box.id, "running", endpoint=endpoint)
    store.add_event("box", box.id, "running", {"endpoint": endpoint, "function_call_id": call_id})
    store.update_task_status(work.id, "running")
    return {"box_id": box.id, "task_id": work.id, "endpoint": endpoint}


def stop_box(box_id: str, *, store: FleetStore, reason: str = "idle") -> dict[str, Any]:
    """Record a box as ``stopped`` — disk retained, no CPU. Idempotent.

    Store-side only in M0. There is no backend that can actually suspend a
    machine yet: `ModalBackend` will raise `NotSupported` on `stop` (M1), and
    `FlyBackend` is what makes this real. Writing the transition now means the
    state machine the rest of the system reasons about is already correct when
    a backend arrives to drive it.

    **Refuses while the box has live tasks**, because until suspend is real a
    "stopped" box with a running container is still being billed — the row
    would say idle and the invoice would disagree.
    """
    box = _require_box(store, box_id)
    if box.status == "stopped":
        return {"box_id": box_id, "status": "stopped", "already_stopped": True}

    # Legality is checked *before* the event is written. `add_event` then
    # `update_box_status` looks harmless but is not: `provisioning -> stopped`
    # and `torn_down -> stopped` are both illegal, so the naive order leaves a
    # `stopped` event on a box that never stopped and *then* raises. Events are
    # the audit trail; a lie in them outlives the traceback.
    if box.status != "running":
        raise ProvisionError(f"box {box_id} is {box.status!r}; only a running box can be stopped")

    # Refuse while work is in flight. Under a real backend this would suspend
    # the machine; under Modal it changes a row and nothing else, so the
    # container keeps running and keeps billing while `count_active_boxes()`
    # reports zero. A `stop` that does not stop spend is a money footgun with a
    # reassuring name — worse than not having the command. When M2 lands real
    # suspend, this becomes a decision (snapshot mid-task) rather than a
    # refusal.
    live = [t.id for t in store.list_tasks(box_id=box_id) if not is_terminal("task", t.status)]
    if live:
        raise ProvisionError(
            f"box {box_id} has {len(live)} task(s) still running ({', '.join(live)}); "
            "stopping would not stop the spend, only the bookkeeping. "
            "Wait for them, or use `flotta kill` to cancel and destroy the box."
        )

    store.add_event("box", box_id, "stopped", {"reason": reason, "previous_status": box.status})
    store.update_box_status(box_id, "stopped")
    return {"box_id": box_id, "status": "stopped", "already_stopped": False}


def start_box(box_id: str, *, store: FleetStore, reason: str = "requested") -> dict[str, Any]:
    """Wake a stopped box back to ``running``. Idempotent.

    The other half of the pivot's central transition. `wake` and `create` stay
    distinct on purpose (M7): conflating them is how you end up with forty
    half-remembered agents instead of forty agents.
    """
    box = _require_box(store, box_id)
    if box.status == "running":
        return {"box_id": box_id, "status": "running", "already_running": True}

    # Only a *stopped* box may start. `provisioning -> running` is legal in the
    # store — it is how `spawn_box` records a successful launch — so without
    # this guard `flotta start` on a box mid-spawn marks it running with no
    # endpoint, which is precisely the wake/create collapse this function is
    # named to avoid. Worse, it then makes `spawn_box`'s own
    # `provisioning -> running` illegal, so a launched container is left
    # billing with its call id never recorded.
    if box.status != "stopped":
        raise ProvisionError(
            f"box {box_id} is {box.status!r}; only a stopped box can be started. "
            "Starting is waking an existing box, never creating one."
        )

    store.add_event("box", box_id, "running", {"reason": reason, "previous_status": box.status})
    store.update_box_status(box_id, "running")
    return {"box_id": box_id, "status": "running", "already_running": False}


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

    wait = waiter or _modal_waiter
    task = _require_task(store, task_id)

    if is_terminal("task", task.status):
        return {"task_id": task_id, "status": task.status, "already_terminal": True}

    box = _require_box(store, task.box_id)
    call_id = function_call_id(box.endpoint)
    if call_id is None:
        payload = {"error": f"box has no modal endpoint to watch (endpoint={box.endpoint!r})"}
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

    wait = waiter or _modal_waiter
    outcomes: list[dict[str, Any]] = []

    for task, over_by in overdue_tasks(store, now=now, grace_s=grace_s):
        box = store.get_box(task.box_id)
        call_id = function_call_id(box.endpoint) if box else None
        if call_id is None:
            payload = {
                "error": "stranded with no modal endpoint; never reported a result",
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
    cancel = canceller or _modal_canceller
    box = _require_box(store, box_id)

    if box.status == "torn_down":
        return {"box_id": box_id, "status": "torn_down", "already_torn_down": True}

    call_id = function_call_id(box.endpoint)
    cancelled = False
    cancel_error: str | None = None
    if call_id is not None:
        try:
            cancel(call_id)
            cancelled = True
        except Exception as exc:
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
