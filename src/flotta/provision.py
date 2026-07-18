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

import pathlib
import sys
from collections.abc import Callable
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

from flotta.store import FleetStore, UnknownWorkerError  # noqa: E402  (needs the prime above)
from flotta.worker.config import DEFAULT_TIMEOUT_S  # noqa: E402
from flotta.worker.image import worker_image  # noqa: E402

APP_NAME = "flotta-provision"
FUNCTION_NAME = "run_worker"

# Modal enforces a per-function hard cap chosen at decoration time, so it cannot
# track the per-call `timeout_s`. It is set to the ceiling below; the actual
# per-task deadline is enforced inside the container by `_run_task_core`.
MAX_TIMEOUT_S = 3600

# Terminal states — nothing left to watch.
_TERMINAL = frozenset({"done", "failed", "torn_down"})

# Provider config forwarded from the local environment into the container as a
# Modal Secret (never as a plain function argument, which would land in call
# logs). Absent keys are simply not forwarded — the worker then reports
# "missing provider config" rather than failing to launch, and `dry_run` still
# exercises the whole lifecycle with no provider at all.
PROVIDER_KEYS = ("FLOTTA_MODEL", "FLOTTA_MODEL_BASE_URL", "FLOTTA_API_KEY")


class ProvisionError(Exception):
    """Base error for provisioning operations."""


class WorkerTimeout(ProvisionError):
    """The worker did not produce a result before the watch deadline.

    Adapters translate Modal's own timeout errors into this, so `watch_worker`
    never has to import or catch a Modal exception type.
    """


def _provider_secret() -> modal.Secret:
    """Forward whichever provider vars exist locally; never fail on absence."""
    import os

    present = [key for key in PROVIDER_KEYS if os.environ.get(key)]
    return modal.Secret.from_local_environ(present) if present else modal.Secret.from_dict({})


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
    """Cancel a function call and kill the container running it."""
    modal.FunctionCall.from_id(call_id).cancel(terminate_containers=True)


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
) -> dict[str, str]:
    """Launch a worker for `task` and record it. Returns ``{worker_id, endpoint}``.

    The row is created *before* the launch so a launch that fails still leaves
    a worker to explain it — the failure is recorded and re-raised rather than
    vanishing.
    """
    if timeout_s > MAX_TIMEOUT_S:
        raise ValueError(f"timeout_s {timeout_s} exceeds the container cap of {MAX_TIMEOUT_S}s")

    launch = launcher or _modal_launcher
    worker = store.create_worker(task, worker_id=worker_id)
    store.add_event(
        worker.id, "spawned", {"task": task, "timeout_s": timeout_s, "dry_run": dry_run}
    )

    try:
        call_id = launch(task=task, worker_id=worker.id, timeout_s=timeout_s, dry_run=dry_run)
    except Exception as exc:
        detail = f"spawn failed: {type(exc).__name__}: {exc}"
        store.add_event(worker.id, "failed", {"error": detail})
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
) -> dict[str, Any]:
    """Await a worker's result and record the terminal status it implies.

    This is the M3.4 watcher. Blocks until the container returns, the deadline
    passes, or Modal reports the call gone; every one of those outcomes writes
    a terminal state, so no worker is left stranded in `running`.
    """
    wait = waiter or _modal_waiter
    worker = _require_worker(store, worker_id)

    if worker.status in _TERMINAL:
        return {"worker_id": worker_id, "status": worker.status, "already_terminal": True}

    call_id = function_call_id(worker.endpoint)
    if call_id is None:
        payload = {"error": f"worker has no modal endpoint to watch (endpoint={worker.endpoint!r})"}
        store.add_event(worker_id, "failed", payload)
        store.update_status(worker_id, "failed")
        return {"worker_id": worker_id, "status": "failed", "error": payload["error"]}

    try:
        result: Any = wait(call_id, timeout_s)
    except WorkerTimeout as exc:
        payload = {"error": f"watch deadline exceeded: {exc}"}
        store.add_event(worker_id, "timed_out", payload)
        store.update_status(worker_id, "failed")
        return {"worker_id": worker_id, "status": "failed", "timed_out": True, **payload}
    except Exception as exc:
        payload = {"error": f"worker call failed: {type(exc).__name__}: {exc}"}
        store.add_event(worker_id, "failed", payload)
        store.update_status(worker_id, "failed")
        return {"worker_id": worker_id, "status": "failed", **payload}

    status, event_type, payload = classify_result(result)
    store.add_event(worker_id, event_type, payload)
    store.update_status(worker_id, status)
    return {"worker_id": worker_id, "status": status, "event": event_type, "result": result}


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
