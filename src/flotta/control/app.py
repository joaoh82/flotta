"""The control plane — fleet state over HTTP (M4.5).

§8.6 gives this one line: "control plane as a deployable service; Postgres;
Dockerfile." M4 landed the Postgres half. This is the service: a small
always-on process that owns the reconcile loop and serves the fleet to
whatever wants to read it.

It is §8.1's "boring block" — ~256MB and a public HTTPS endpoint, deployable on
anything. The interesting requirements all live below the `Backend` line.

## Authentication (M5)

Every `/api/*` route requires a scoped token signed with `$FLOTTA_SIGNING_KEY`
— see `flotta.auth` for the token design and why revocation is a key rotation.
Routes declare the scope they need; `box:destroy` is separate from
`fleet:write` because `DELETE /api/boxes/{id}` destroys someone's agent *and
its memory*, and a dashboard that shows a fleet should not carry that.

`/health` is deliberately open. It reports whether the reconcile loop is
sweeping and nothing about the fleet's contents, and a liveness probe cannot
hold a credential.

The bind guard survives, with a smaller job. It used to mean "there is no way
to authenticate this, so do not expose it"; it now means "auth is not
configured, so do not expose it". Copying Hermes's own gate was deliberate: M3
hit that gate, tried to work around it with a socat forwarder, and the
workaround was the wrong answer — the gate was right.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from flotta import db
from flotta.auth import (
    SCOPE_BOX_CHAT,
    SCOPE_BOX_DESTROY,
    SCOPE_FLEET_READ,
    SCOPE_FLEET_WRITE,
    SIGNING_KEY_ENV,
    AuthError,
    Token,
    resolve_signing_key,
    verify,
)
from flotta.control.loop import DEFAULT_INTERVAL_S, LoopState, run_reconcile_loop
from flotta.store import FleetStore, UnknownEntityError, is_terminal

_log = logging.getLogger("flotta.control")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
INTERVAL_ENV = "FLOTTA_RECONCILE_INTERVAL_S"


class InsecureBindError(RuntimeError):
    """Refused to expose the fleet without authentication."""


def check_bind(host: str, *, env: dict[str, str] | None = None) -> None:
    """Refuse a non-loopback bind while there is no auth to put in front of it.

    Three states, and each is defensible on its own:

    - **no key, loopback** — runs unauthenticated. This is local development,
      where the port is already only reachable by whoever is sitting there, and
      demanding a token to run `just serve` would buy nothing.
    - **no key, public** — refused. Fails closed at startup rather than
      warning: a warning in a deploy log is read once, by the person who
      already knew, and the thing being protected is a kill switch for an
      agent's whole memory.
    - **key set, any bind** — every request needs a scoped token.

    `FLOTTA_CONTROL_ALLOW_INSECURE_BIND` is **gone**. It existed because there
    was no way to authenticate a public bind, so the only options were "refuse"
    or "refuse unless you promise you own the network". There is a way now, and
    keeping an override that skips it would mean shipping the hole this
    milestone closed.
    """
    env = os.environ if env is None else env
    if host in LOOPBACK_HOSTS:
        return
    if resolve_signing_key(env):
        return
    raise InsecureBindError(
        f"refusing to bind {host!r} with no authentication: "
        f"DELETE /api/boxes/<id> destroys a box and everything it remembers.\n"
        f"Configure a signing key — `flotta token key` and set ${SIGNING_KEY_ENV} "
        f"— or bind 127.0.0.1 and reach it over a tunnel."
    )


def _store_factory() -> FleetStore:
    """A fresh store per request.

    Per-request rather than one long-lived connection, for the same reason the
    dashboard opens SQLite per request: a pinned snapshot served to a polling
    UI is stale in the one way a fleet view must never be. Connecting is cheap
    on both engines.
    """
    # No argument on purpose: FleetStore owns the $FLOTTA_DATABASE_URL /
    # $FLOTTA_STORE / ./fleet.db chain. This function used to re-implement it
    # and got it wrong — the service honoured the database URL but not
    # $FLOTTA_STORE, and served an empty fleet from a file nobody meant while
    # `flotta ps` in the same shell listed the real one.
    return FleetStore()


#: Do not write an `addressed` event more often than this. Without a floor,
#: every HTTP request through the door would append a row — the event log is
#: the fleet's history, not a request log, and one row per request would bury
#: the transitions a human actually reads it for.
ACTIVITY_FLOOR_S = 60.0


def _record_activity(store: Any, box_id: str) -> bool:
    """Note that a box was addressed, at most once a minute.

    This is the activity signal idle-sleep runs on. It lives in the event log
    rather than a `boxes.last_active_at` column because the store has no
    migration machinery — the schema is `CREATE TABLE IF NOT EXISTS` and
    nothing else, so a new column would silently break every existing fleet.

    Rate-limited by reading the newest event first, which costs one query on a
    path that is already making an HTTP round trip.
    """
    from datetime import UTC, datetime

    from flotta.provision import ADDRESSED_EVENT, last_activity_at

    try:
        seen = last_activity_at(store, box_id)
    except Exception:  # pragma: no cover - a box that vanished mid-request
        return False
    if seen is not None and (datetime.now(UTC) - seen).total_seconds() < ACTIVITY_FLOOR_S:
        return False
    store.add_event("box", box_id, ADDRESSED_EVENT, {"via": "front-door"})
    return True


def _box_dict(box: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(box)


def create_app(
    *,
    store_factory: Any = _store_factory,
    run_loop: bool = True,
    interval_s: float | None = None,
    loop_runner: Any = None,
    signing_key: str | None = None,
) -> Any:
    """Build the control-plane app.

    `run_loop=False` is for tests and for anyone running a second replica that
    should serve reads without racing the first one's reconciliation — two
    loops reconciling the same fleet is not harmful (the store's transitions
    are validated) but it is wasted backend calls.

    `signing_key` overrides `$FLOTTA_SIGNING_KEY`. **Resolved once, here, at
    construction** rather than per request: an app that picks up a key change
    mid-flight would silently start accepting tokens it was rejecting a moment
    earlier, and "is this thing authenticated?" would have no stable answer.
    Rotating the key means restarting the process, which is also what makes
    rotation a real revocation.
    """
    from fastapi import Depends, FastAPI, Header, HTTPException

    resolved_interval = interval_s
    if resolved_interval is None:
        raw = (os.environ.get(INTERVAL_ENV) or "").strip()
        resolved_interval = float(raw) if raw else DEFAULT_INTERVAL_S

    state = LoopState(interval_s=resolved_interval)

    key = signing_key if signing_key is not None else resolve_signing_key()
    if key is None:
        _log.warning(
            "no %s configured: the fleet API is UNAUTHENTICATED. Anyone who can "
            "reach this port can destroy any box and everything it remembers. "
            "This is refused on a non-loopback bind; mint a key with "
            "`flotta token key` before exposing it.",
            SIGNING_KEY_ENV,
        )

    def require(*scopes: str):
        """A dependency that admits a request carrying **all** of `scopes`.

        All rather than any: a route that needs two permissions needs two, and
        an "any" default is the kind of thing that reads as fine until someone
        adds a second scope to a route and quietly widens it.

        With no key configured this admits everything, which is the documented
        loopback-development state — `check_bind` is what stops that reaching a
        public interface, and it is enforced at startup rather than here so the
        failure is a refusal to boot rather than a 401 nobody sees.
        """

        def dependency(authorization: str | None = Header(default=None)) -> Token | None:
            if key is None:
                return None
            if not authorization or not authorization.lower().startswith("bearer "):
                # 401 with the challenge, not 403: the caller sent nothing, so
                # the answer is "authenticate", not "you may not".
                raise HTTPException(
                    status_code=401,
                    detail="missing bearer token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            try:
                token = verify(authorization.split(" ", 1)[1].strip(), key=key)
            except AuthError as exc:
                raise HTTPException(
                    status_code=401,
                    detail=str(exc),
                    headers={"WWW-Authenticate": "Bearer"},
                ) from exc

            missing = [s for s in scopes if not token.allows(s)]
            if missing:
                # 403, not 401: the token is valid and re-authenticating with
                # it will not help. Name the scope so the fix is obvious.
                raise HTTPException(
                    status_code=403,
                    detail=f"token for {token.subject!r} lacks scope(s): {', '.join(missing)}",
                )
            return token

        return dependency

    # Hoisted rather than called inline in each route's default: a call in an
    # argument default is evaluated once at import, which is fine for Depends
    # but is a real footgun in general — so the linter flags it, and naming the
    # three dependencies is clearer than suppressing it five times.
    needs_read = Depends(require(SCOPE_FLEET_READ))
    needs_write = Depends(require(SCOPE_FLEET_WRITE))
    needs_destroy = Depends(require(SCOPE_BOX_DESTROY))
    needs_chat = Depends(require(SCOPE_BOX_CHAT))

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
    def list_boxes(all_: bool = False, _: Token | None = needs_read) -> Any:
        store = store_factory()
        try:
            boxes = store.list_boxes()
            if not all_:
                # A stopped box is idle, not finished — hiding it would hide
                # the point of the fleet.
                boxes = [b for b in boxes if not is_terminal("box", b.status)]
            # Each box's newest task and its total spend. Cost lives on tasks
            # (the formula measures start-to-verdict, meaningless across a
            # machine that spans months), so the list view has to sum it. The
            # direct-SQL version this replaces did `SUM(cost_estimate)`, and
            # dropping it silently turned every cost in the UI into a blank.
            summaries: dict[str, dict[str, Any]] = {}
            for box in boxes:
                tasks = store.list_tasks(box_id=box.id)
                costs = [t.cost_estimate for t in tasks if t.cost_estimate is not None]
                summaries[box.id] = {
                    "latest_task": tasks[0].prompt if tasks else None,
                    "task_count": len(tasks),
                    # None rather than 0.0 when nothing is priced: a blank says
                    # "no rate configured", a zero claims the box ran for free.
                    "cost_estimate": sum(costs) if costs else None,
                }
            return {"boxes": [{**_box_dict(b), **summaries[b.id]} for b in boxes]}
        finally:
            store.close()

    @app.post("/api/boxes", status_code=201)
    def create_box_endpoint(body: dict[str, Any], _: Token | None = needs_write) -> Any:
        """Create a box. The API half of "create Agent B" being a button.

        Goes through `provision.create_box` rather than writing a row, for the
        same reason `DELETE` goes through `teardown_box`: the store must never
        claim a machine that was not provisioned. `create_box` also refuses to
        mint a second row for a machine another box already occupies — a guard
        that was unreachable until this endpoint existed to reach it.
        """
        from fastapi.responses import JSONResponse

        from flotta.provision import (
            BoxNotRunning,
            BoxOccupied,
            ProvisionError,
            create_box,
        )

        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="a box needs a name")

        store = store_factory()
        try:
            try:
                result = create_box(name, store=store)
            except BoxOccupied as exc:
                # The only create failure that is genuinely a *conflict*: the
                # machine is taken, and the caller can act on that.
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except BoxNotRunning as exc:
                # A box **was** created, and the row says `stopped`. Answering
                # with an error would tell the caller nothing was made while a
                # machine sits there costing disk — and they would not even
                # learn its id, so they could neither start it nor destroy it.
                # That is how an orphan machine bills against an account nobody
                # is watching, which this repo has done once already.
                #
                # 201 with the box, plus a warning saying it is not up.
                box = store.get_box(exc.box_id)
                return JSONResponse(
                    {
                        "box": _box_dict(box),
                        "box_id": exc.box_id,
                        "endpoint": exc.endpoint,
                        "warning": str(exc),
                    },
                    status_code=201,
                )
            except ProvisionError as exc:
                # Everything else is the substrate failing, not the caller
                # asking for something impossible. 502 matches DELETE, which
                # already reports a failed teardown that way.
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            box = store.get_box(result["box_id"])
            return {"box": _box_dict(box), **result}
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}")
    def get_box(box_id: str, _: Token | None = needs_read) -> Any:
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
    def get_events(box_id: str, _: Token | None = needs_read) -> Any:
        store = store_factory()
        try:
            box = store.get_box(box_id) or store.get_box_by_name(box_id)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
            return {"events": [_event_dict(e) for e in store.get_box_timeline(box.id)]}
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/wake")
    def wake_box_endpoint(box_id: str, _: Token | None = needs_chat) -> Any:
        """Ensure a box is up so it can be addressed. Idempotent.

        Exists for the front door (M5b), which cannot reach the substrate
        itself: a request for `<box>.flotta.dev` normally arrives while the box
        is *asleep* — that is the cost argument, not an edge case — so
        something has to start it, and D10 says that something must be code
        that can reach the substrate. The door asks; the control plane acts.

        **Guarded by `box:chat`, not a scope of its own.** A box is asleep most
        of the time, so anything permitted to talk to one must be permitted to
        wake it or the permission means nothing. A separate `box:wake` would be
        a scope nobody could sensibly withhold.

        `wake_box`, not `start_box`: the operator's verb refuses anything that
        is not `stopped`, which is right when a human asks and wrong here — the
        addressing path has to accept an already-running box, and reconcile a
        row that disagrees with the substrate. Fly stops machines on its own
        during a host drain.
        """
        from flotta.provision import ProvisionError, wake_box

        store = store_factory()
        try:
            box = store.get_box(box_id) or store.get_box_by_name(box_id)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
            _record_activity(store, box.id)
            try:
                result = wake_box(box.id, store=store, reason="front-door")
            except ProvisionError as exc:
                # 409 for an illegal state (a torn-down box cannot be woken),
                # 502 for the substrate failing to start it. The caller can act
                # on the first and only retry the second.
                status = 409 if "only a running or stopped box" in str(exc) else 502
                raise HTTPException(status_code=status, detail=str(exc)) from exc
            return {"box": _box_dict(store.get_box(box.id)), **result}
        finally:
            store.close()

    @app.delete("/api/boxes/{box_id}")
    def destroy_box(box_id: str, _: Token | None = needs_destroy) -> Any:
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
