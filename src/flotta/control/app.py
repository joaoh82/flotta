"""The control plane — fleet state over HTTP (M4.5).

§8.6 gives this one line: "control plane as a deployable service; Postgres;
Dockerfile." M4 landed the Postgres half. This is the service: a small
always-on process that owns the reconcile loop and serves the fleet to
whatever wants to read it.

It is §8.1's "boring block" — ~256MB and a public HTTPS endpoint, deployable on
anything. The interesting requirements all live below the `Backend` line.

## It refuses to serve the world without auth

There is no auth here yet — scoped, expiring tokens are §M5, and §8.6 is
explicit that the Railway template ships *with* that work and never before it.
So rather than shipping something that could be deployed publicly and would be
an unauthenticated fleet-control API, this **refuses a non-loopback bind**
unless auth is configured.

That is Hermes's own gate, and copying it is deliberate. M3 hit it, tried to
work around it with a socat forwarder, and the workaround was the wrong answer:
the gate was right. A `DELETE /api/boxes/{id}` open to the internet destroys
someone's agent and its memory — the README already calls the unauthenticated
dashboard "disqualifying for a hosted one", and this has the same reach.

`FLOTTA_CONTROL_ALLOW_INSECURE_BIND=1` exists for a private network where the
operator genuinely owns the perimeter (Fly's 6PN, Tailscale), because refusing
*that* would just push people to a worse workaround. It is loud in the logs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from flotta import db
from flotta.control.loop import DEFAULT_INTERVAL_S, LoopState, run_reconcile_loop
from flotta.store import FleetStore, UnknownEntityError, is_terminal

_log = logging.getLogger("flotta.control")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ALLOW_INSECURE_ENV = "FLOTTA_CONTROL_ALLOW_INSECURE_BIND"
INTERVAL_ENV = "FLOTTA_RECONCILE_INTERVAL_S"


class InsecureBindError(RuntimeError):
    """Refused to expose the fleet without authentication."""


def check_bind(host: str, *, env: dict[str, str] | None = None) -> None:
    """Refuse a non-loopback bind while there is no auth to put in front of it.

    Fails closed at startup rather than warning: a warning in a deploy log is
    read once, by the person who already knew, and the thing being protected is
    a kill switch for an agent's whole memory.
    """
    env = os.environ if env is None else env
    if host in LOOPBACK_HOSTS:
        return
    if (env.get(ALLOW_INSECURE_ENV) or "").strip() in ("1", "true", "yes", "on"):
        _log.warning(
            "binding %s with no authentication because %s is set. Anyone who can "
            "reach this port can destroy any box in the fleet, and its memory. "
            "This is only defensible on a network you own (Fly 6PN, Tailscale). "
            "Scoped tokens land in M5.",
            host,
            ALLOW_INSECURE_ENV,
        )
        return
    raise InsecureBindError(
        f"refusing to bind {host!r}: the control plane has no authentication yet "
        f"(scoped tokens are M5), and DELETE /api/boxes/<id> destroys a box and "
        f"everything it remembers.\n"
        f"Bind 127.0.0.1 and reach it over a tunnel, or set {ALLOW_INSECURE_ENV}=1 "
        f"if this port is only reachable on a network you control."
    )


def _store_factory() -> FleetStore:
    """A fresh store per request.

    Per-request rather than one long-lived connection, for the same reason the
    dashboard opens SQLite per request: a pinned snapshot served to a polling
    UI is stale in the one way a fleet view must never be. Connecting is cheap
    on both engines.
    """
    # Resolved the same way the CLI resolves it: $FLOTTA_DATABASE_URL, else
    # $FLOTTA_STORE, else ./fleet.db. `FleetStore()` with no argument only
    # consults the database URL and silently falls back to ./fleet.db — so a
    # service started with $FLOTTA_STORE set served an empty fleet from a file
    # nobody meant. Found by running it against a seeded store.
    url = db.resolve_url(None)
    if url:
        return FleetStore(url)
    return FleetStore(os.environ.get("FLOTTA_STORE", "").strip() or "fleet.db")


def _box_dict(box: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(box)


def create_app(
    *,
    store_factory: Any = _store_factory,
    run_loop: bool = True,
    interval_s: float | None = None,
    loop_runner: Any = None,
) -> Any:
    """Build the control-plane app.

    `run_loop=False` is for tests and for anyone running a second replica that
    should serve reads without racing the first one's reconciliation — two
    loops reconciling the same fleet is not harmful (the store's transitions
    are validated) but it is wasted backend calls.
    """
    from fastapi import FastAPI, HTTPException

    resolved_interval = interval_s
    if resolved_interval is None:
        raw = (os.environ.get(INTERVAL_ENV) or "").strip()
        resolved_interval = float(raw) if raw else DEFAULT_INTERVAL_S

    state = LoopState(interval_s=resolved_interval)

    @asynccontextmanager
    async def lifespan(app: Any):
        task: asyncio.Task | None = None
        if run_loop:
            # `loop_runner` makes the *interesting* failure testable: a loop
            # that is configured and supposed to be running, but is not
            # sweeping. That is what a slept platform looks like from inside
            # the process, and it is the case `/health` exists to catch — a
            # real loop cannot test it, because a real loop sweeps.
            runner = loop_runner or run_reconcile_loop
            task = asyncio.create_task(runner(state, store_factory=store_factory))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Flotta control plane", lifespan=lifespan)
    app.state.loop_state = state

    @app.get("/health")
    def health() -> Any:
        """Liveness, and whether the reconcile loop is actually sweeping.

        Returns 503 when the loop has gone stale. "The process is up" is not
        the interesting question — a slept loop looks exactly like a healthy
        one from the outside, right up until a task strands. This is the
        difference between finding that out from a health check and finding it
        out at 138 hours.
        """
        from fastapi.responses import JSONResponse

        loop = state.snapshot()
        # Stale *or* failing. A loop that runs on schedule and errors every
        # time keeps `last_sweep_at` fresh, so staleness alone would call it
        # healthy — which it did, on the first live run, while every sweep was
        # failing on a threading bug.
        healthy = not (loop["stale"] or loop["failing"]) if run_loop else True
        return JSONResponse(
            {"status": "ok" if healthy else "degraded", "reconcile_loop": loop},
            status_code=200 if healthy else 503,
        )

    @app.get("/api/boxes")
    def list_boxes(all_: bool = False) -> Any:
        store = store_factory()
        try:
            boxes = store.list_boxes()
            if not all_:
                # A stopped box is idle, not finished — hiding it would hide
                # the point of the fleet.
                boxes = [b for b in boxes if not is_terminal("box", b.status)]
            latest = {}
            for box in boxes:
                tasks = store.list_tasks(box_id=box.id)
                if tasks:
                    latest[box.id] = tasks[0].prompt
            return {"boxes": [{**_box_dict(b), "latest_task": latest.get(b.id)} for b in boxes]}
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}")
    def get_box(box_id: str) -> Any:
        store = store_factory()
        try:
            box = store.get_box(box_id) or store.get_box_by_name(box_id)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
            return {
                "box": _box_dict(box),
                "tasks": [_task_dict(t) for t in store.list_tasks(box_id=box.id)],
            }
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}/events")
    def get_events(box_id: str) -> Any:
        store = store_factory()
        try:
            box = store.get_box(box_id) or store.get_box_by_name(box_id)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
            return {"events": [_event_dict(e) for e in store.get_box_timeline(box.id)]}
        finally:
            store.close()

    @app.delete("/api/boxes/{box_id}")
    def destroy_box(box_id: str) -> Any:
        """Destroy a box and everything it remembers. Idempotent.

        Goes through `teardown_box`, which cancels the backend's machine and
        fails any live tasks — writing `torn_down` straight into the store from
        here would close the row while the machine kept running and billing,
        the bug M0's review caught and M1 encoded a refusal for.
        """
        from flotta.provision import ProvisionError, teardown_box

        store = store_factory()
        try:
            box = store.get_box(box_id) or store.get_box_by_name(box_id)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
            try:
                return {"result": teardown_box(box.id, store=store, reason="control-plane")}
            except (ProvisionError, UnknownEntityError) as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            store.close()

    return app


def _task_dict(task: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(task)


def _event_dict(event: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(event)


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:  # pragma: no cover - entrypoint
    """Run the control plane. Refuses an unauthenticated public bind."""
    import uvicorn

    check_bind(host)
    described = (
        db.describe_url(os.environ.get(db.DATABASE_URL_ENV, ""))
        if os.environ.get(db.DATABASE_URL_ENV)
        else "a local SQLite file"
    )
    _log.info("control plane on %s:%s, fleet state in %s", host, port, described)
    uvicorn.run(create_app(), host=host, port=port)
