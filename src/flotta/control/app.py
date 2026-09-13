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
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from flotta import db
from flotta.auth import (
    SCOPE_BOX_CHAT,
    SCOPE_BOX_DESTROY,
    SCOPE_FLEET_READ,
    SCOPE_FLEET_WRITE,
    SCOPE_GIT_CREDENTIAL,
    SIGNING_KEY_ENV,
    AuthError,
    Token,
    resolve_signing_key,
    subject_box,
    verify,
)
from flotta.control.loop import DEFAULT_INTERVAL_S, LoopState, run_reconcile_loop
from flotta.store import FleetStore, UnknownEntityError, is_terminal

_log = logging.getLogger("flotta.control")

#: Live provisioning threads, held so nothing collects them mid-flight.
_provisioning: set[threading.Thread] = set()


def _build() -> str:
    """Which commit is running.

    `/health` said the process was up and nothing about *what* it was. That is
    indistinguishable from the interesting question during a deploy: a restart
    caused by a changed variable looks exactly like a restart caused by new
    code, and reading one as the other cost this project a wasted machine and
    an hour of misdiagnosis — the bug was hunted in Railway's variables, which
    were correct all along.

    Railway sets `RAILWAY_GIT_COMMIT_SHA`; `$FLOTTA_BUILD` is the override for
    anywhere else. Unknown is reported as unknown rather than guessed.
    """
    for name in ("FLOTTA_BUILD", "RAILWAY_GIT_COMMIT_SHA"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value[:12]
    return "unknown"


def _peek_for(impl: Any, name: str) -> str | None:
    """The endpoint `create` would adopt for a name, or None.

    Best-effort by construction: a probe that fails must not stop a create, or
    a Fly hiccup becomes an outage for the one verb that adds capacity.
    """
    from flotta.provision import _peek_endpoint

    try:
        return _peek_endpoint(impl, name)
    except Exception:  # noqa: BLE001
        return None


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
INTERVAL_ENV = "FLOTTA_RECONCILE_INTERVAL_S"
#: The credential boxes borrow. Held here, never on a box — see
#: `git_credential` for what that does and does not buy.
GITHUB_TOKEN_ENV = "FLOTTA_GITHUB_TOKEN"


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


def _build_region() -> str:
    """Where the throwaway build machine is created.

    `fly volumes create` refuses to run without a region off a TTY and a deploy
    wants one too, so "unset" cannot mean "decide later" — `resolved_region`
    turns an unset config into a concrete one, exactly as `fly-up` does.
    """
    from flotta.fly import FlyConfig

    return FlyConfig.from_env().resolved_region()


def _box_dict(box: Any, meta: Any = None) -> dict[str, Any]:
    """The row, plus who the agent is when the caller has looked that up.

    `display_name` / `description` / `instructions` are always present so the
    app never reads `undefined` — `None` means "not set", which is a real
    state. `meta` is optional because the identity is a side table: the list
    endpoint fetches it in one query for the fleet, the single-box endpoints
    fetch one row, and a few internal callers have no store handy and get
    Nones — which is honest, since they did not look.
    """
    from dataclasses import asdict

    out = asdict(box)
    out["display_name"] = meta.display_name if meta else None
    out["description"] = meta.description if meta else None
    out["instructions"] = meta.instructions if meta else None
    return out


def _with_meta(store: Any, box: Any) -> dict[str, Any]:
    return _box_dict(box, store.meta_for_box(box.id))


def _epoch(iso: str | None) -> float | None:
    """A store timestamp as seconds since the Unix epoch, or None.

    **Epoch rather than the ISO string the store keeps**, because the only
    consumer compares it against a Hermes session's `started_at`, which is a
    float. Handing the app two formats to reconcile would put a date parser in
    the window for a comparison that is a subtraction.

    A timestamp that will not parse returns None rather than raising: this
    decides whether to reuse a conversation, and a malformed row must not take
    down the read of an agent that is otherwise fine.
    """
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def create_app(
    *,
    store_factory: Any = _store_factory,
    run_loop: bool = True,
    #: Answer `202` and provision on a background task rather than holding the
    #: request open for minutes. Off in tests, where a synchronous create is
    #: what the assertions are about — and where there is no proxy to time out.
    background: bool = True,
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
    needs_git = Depends(require(SCOPE_GIT_CREDENTIAL))

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
            {
                "status": "ok" if healthy else "degraded",
                "build": _build(),
                "reconcile_loop": loop,
            },
            status_code=200 if healthy else 503,
        )

    @app.get("/api/settings")
    def get_settings(_: Token | None = needs_read) -> Any:
        """What the fleet is configured with, and where each value came from.

        The catalogue travels with the values so the app renders a form without
        hardcoding one: a setting added here appears in the window without the
        app being rebuilt, which is the point of having a catalogue at all.

        **No secret is reachable through this.** `flotta.settings.SETTINGS` is
        an allowlist of configuration, and the signing key, the GitHub token and
        the box password are not in it — by construction, not by filtering.
        """
        from flotta.settings import describe

        store = store_factory()
        try:
            return {"settings": describe(store)}
        finally:
            store.close()

    @app.put("/api/settings")
    def put_settings(body: dict[str, Any], _: Token | None = needs_write) -> Any:
        """Change fleet settings. `{"values": {"KEY": "value", ...}}`.

        An empty string clears an override, which is what an emptied field in
        the app means — the value falls back to the environment, or to the
        default. That is the same verb as "reset this", so there is no separate
        delete endpoint to get out of step with it.

        Values are validated *before* anything is written, and the whole
        request is refused if any of them is bad. A partial apply would leave
        the fleet in a state nobody asked for and the form showing something
        else.
        """
        from flotta.settings import UnknownSettingError, describe, validate

        values = body.get("values")
        if not isinstance(values, dict):
            raise HTTPException(status_code=422, detail="expected {'values': {key: value}}")

        cleaned: dict[str, str] = {}
        for key, raw in values.items():
            try:
                cleaned[str(key)] = validate(str(key), "" if raw is None else str(raw))
            except UnknownSettingError as exc:
                # A key nobody may set. 422 rather than 403: the caller has the
                # right scope, they named something that is not a setting.
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        store = store_factory()
        try:
            # All or nothing, so "refused as a whole" covers a database failure
            # and not only bad input.
            store.set_settings(cleaned)
            return {"settings": describe(store)}
        finally:
            store.close()

    @app.get("/api/hermes")
    def hermes_versions(_: Token | None = needs_read) -> Any:
        """Which Hermes the fleet would build next, and whether a newer exists.

        Three different facts have been one sentence until now, and the app
        needs them apart:

        - **`pinned`** — what the *next* image would be built from. It is this
          control plane's own `HERMES_REF`, so it says nothing about any
          running agent.
        - **`latest`** — the newest Hermes there is. `None` when GitHub could
          not be reached, which must read as *unknown*: reporting "up to date"
          on a failed check hides a real upgrade, and "behind" nags people into
          rebuilding for nothing.
        - **`fleet_image`** — the image an upgrade with no argument would move
          an agent onto. Reported rather than assumed correct: it is a
          deployment variable, and a stale one points at an app that may not
          exist any more.

        What an individual agent runs is none of these. That is the label on
        the image it boots, and it comes back from
        `GET /api/boxes/{id}/machine`.
        """
        from flotta.hermes import report
        from flotta.provision import _newest_release_image, resolve_fleet_image

        store = store_factory()
        try:
            built = store.last_successful_build()
        finally:
            store.close()

        found = report(fleet_ref=built.hermes_ref if built else None)
        fleet_image, source = resolve_fleet_image()

        # What the build app last released, reported even when the environment
        # won — because the two disagreeing *is* the trap. A deployment
        # variable naming an image older than the newest build makes the app
        # offer the wrong upgrade with total confidence, and there is nowhere
        # else a person could notice.
        newest = None
        app = (os.environ.get("FLOTTA_FLY_APP") or "").strip() or None
        if app:
            newest = _newest_release_image(app)

        return {
            "pinned": found.pinned,
            #: What the fleet's newest image was built at, and where that came
            #: from. `pinned` is an intention; this is what exists. They
            #: diverge permanently after the first update started from the app,
            #: which builds at a given ref and never edits source.
            "fleet_ref": found.fleet_ref,
            "fleet_ref_source": found.fleet_ref_source,
            "latest": found.latest,
            "behind": found.behind,
            "unavailable": found.unavailable,
            "fleet_image": fleet_image,
            #: `env`, `release`, or `none`. "Why is my fleet not using the
            #: image I just built" is otherwise unanswerable from a window.
            "fleet_image_source": source,
            "newest_release": newest,
            #: Which app releases are read from, or `null` when none is
            #: configured. Without this, `source: "none"` has two causes with
            #: two different fixes — no app configured, and an app configured
            #: whose releases cannot be read — and they are indistinguishable
            #: from outside. That ambiguity cost a live debugging round: the
            #: answer was the same either way, so the only way to tell was to
            #: go and look at the deployment's variables.
            "fleet_image_app": app,
        }

    @app.get("/api/hermes/builds")
    def list_builds(_: Token | None = needs_read) -> Any:
        """What the fleet has built, newest first — the app's progress view."""
        store = store_factory()
        try:
            from dataclasses import asdict

            return {"builds": [asdict(b) for b in store.list_builds()]}
        finally:
            store.close()

    @app.post("/api/hermes/update", status_code=202)
    def update_hermes(body: dict[str, Any] | None = None, _: Token | None = needs_write) -> Any:
        """Build the box image at a Hermes ref, then move every agent onto it.

        **This is the button.** Everything else in this API is a piece of it:
        `/api/hermes` says a newer Hermes exists, `images.build_box_image`
        makes an image carrying it, and `upgrade_box` moves an agent without
        touching its disk. Until this endpoint they were three things a person
        did from a terminal, which meant the window could report the problem
        and not fix it.

        **Answers 202 and works on a thread.** A cold image build is minutes
        and a proxy in front of this cuts at 60 seconds — the same reason
        create and upgrade are backgrounded. Progress is a `builds` row plus
        the usual `reimaged` / `upgrade_failed` events on each agent.

        **Agents are rolled one at a time and it stops at the first failure.**
        Not for tidiness: the first agent *is* the canary. A Hermes that will
        not serve should cost one agent a restart, not the whole fleet — and a
        failed upgrade leaves that agent exactly as it was, which is what makes
        stopping a real recovery rather than a half-migrated fleet.
        """
        from fastapi.responses import JSONResponse

        from flotta.box.image import HERMES_REF

        ref = str((body or {}).get("hermes_ref") or "").strip() or HERMES_REF

        from flotta.store import BuildInProgressError

        store = store_factory()
        try:
            # The check lives inside `start_build`'s transaction. Read here and
            # insert after — which is what this did — lets two POSTs pass the
            # same check and both start, because the guard serialises the write
            # and not the decision behind it.
            #
            # Two concurrent updates race for one app's release history, and
            # the loser's agents get rolled onto an image the winner replaced.
            build = store.start_build(ref)
        except BuildInProgressError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()

        def run() -> None:
            from flotta.images import BuildError, build_box_image
            from flotta.provision import ProvisionError, UpgradeFailed, upgrade_box

            inner = store_factory()
            try:
                app_name = (os.environ.get("FLOTTA_FLY_APP") or "").strip()
                if not app_name:
                    inner.finish_build(
                        build.id,
                        error=(
                            "no build app configured. Set FLOTTA_FLY_APP on the control "
                            "plane to the app the box image is released into."
                        ),
                    )
                    return
                try:
                    image = build_box_image(ref, app=app_name, region=_build_region())
                except BuildError as exc:
                    inner.finish_build(build.id, error=str(exc))
                    return
                # Not `finish_build`: the operation is not over. Marking it
                # done here made "one update at a time" true for the build
                # minutes and false for the roll — a second update could start
                # while agents were still moving, and the app re-offered its
                # button mid-roll.
                inner.mark_rolling(build.id)

                for box in inner.list_boxes():
                    if is_terminal("box", box.status) or box.status == "provisioning":
                        continue
                    try:
                        upgrade_box(box.id, store=inner, image=image, reason="hermes-update")
                    except (UpgradeFailed, ProvisionError) as exc:
                        # `upgrade_box` has already written `upgrade_failed`
                        # against the box, so the app can say which agent and
                        # why. Stopping here is the point: see the docstring.
                        _log.warning("rolling stopped at %s: %s", box.name, exc)
                        inner.finish_build(
                            build.id, error=f"{box.name} could not be upgraded: {exc}"
                        )
                        return

                inner.finish_build(build.id, image=image)
            except Exception as exc:  # noqa: BLE001
                with contextlib.suppress(Exception):
                    inner.finish_build(build.id, error=f"{type(exc).__name__}: {exc}")
                _log.warning("hermes update failed: %s: %s", type(exc).__name__, exc)
            finally:
                inner.close()

        worker = threading.Thread(target=run, name=f"hermes-{ref}", daemon=True)
        _provisioning.add(worker)
        worker.start()

        return JSONResponse(
            {"build_id": build.id, "hermes_ref": ref, "status": "building"}, status_code=202
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
            metas = store.meta_for_boxes([b.id for b in boxes])
            return {"boxes": [{**_box_dict(b, metas.get(b.id)), **summaries[b.id]} for b in boxes]}
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
        from flotta.store import DuplicateBoxError, InvalidBoxNameError, validate_box_name

        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="a box needs a name")

        # Here as well as in the store, and deliberately: the store is the
        # guarantee — it is what makes this true for the CLI too — while this
        # is the early exit. Without it a name that cannot work still costs a
        # `flyctl` probe (`_peek_for` runs before the row is reserved) and
        # comes back as a 409 about a duplicate, which is the wrong fix.
        try:
            name = validate_box_name(name)
        except InvalidBoxNameError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # What makes this agent different from the fleet default. Absent means
        # "the fleet decides", which is the common case and stays a one-field
        # request.
        raw_volume = body.get("volume_gb")
        volume_gb: int | None = None
        if raw_volume not in (None, ""):
            try:
                volume_gb = int(raw_volume)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422, detail=f"volume_gb must be a whole number, got {raw_volume!r}"
                ) from exc
            if volume_gb <= 0:
                raise HTTPException(
                    status_code=422, detail=f"volume_gb must be at least 1, got {volume_gb}"
                )
        region = (str(body.get("region") or "")).strip() or None

        # Who the agent is. Validated here for the early exit and again in the
        # store for the guarantee; a description that cannot be stored must
        # refuse before a name is spent.
        from flotta.store import InvalidBoxMetaError, validate_box_meta

        try:
            display_name, description, instructions = validate_box_meta(
                body.get("display_name"), body.get("description"), body.get("instructions")
            )
        except InvalidBoxMetaError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if background:
            return _create_in_background(
                name,
                volume_gb=volume_gb,
                region=region,
                display_name=display_name,
                description=description,
                instructions=instructions,
            )

        store = store_factory()
        try:
            try:
                result = create_box(
                    name,
                    store=store,
                    volume_gb=volume_gb,
                    region=region,
                    display_name=display_name,
                    description=description,
                    instructions=instructions,
                )
            except DuplicateBoxError as exc:
                # A name still held by an existing box. Since teardown releases
                # a destroyed agent's name, this now means a *live* one — which
                # is a conflict the caller can act on, not the bare 500 an
                # uncaught IntegrityError used to produce.
                raise HTTPException(status_code=409, detail=str(exc)) from exc
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
            return {"box": _with_meta(store, box), **result}
        finally:
            store.close()

    def _create_in_background(
        name: str,
        *,
        volume_gb: int | None = None,
        region: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
        instructions: str | None = None,
    ) -> Any:
        """Reserve the row, answer, and provision afterwards.

        Provisioning is an app, a volume, a machine and a boot — minutes on a
        cold path. Held open as one request it outlives the proxy in front of
        it: Railway answered `502 Application failed to respond` while the work
        carried on, so the caller was told it had failed and a machine appeared
        anyway.

        Worse, the request being cancelled took `create_box` with it, leaving a
        row at `provisioning` with no endpoint and a real machine nothing could
        address. The reconcile loop closes those now, but not creating them is
        better than sweeping them up.

        So: `202`, the box id, and a row to poll. The work runs on the event
        loop's own task, which no client disconnect can cancel.
        """
        from fastapi.responses import JSONResponse

        from flotta.provision import _backend_for, create_box, reserve_box
        from flotta.store import DuplicateBoxError

        store = store_factory()
        try:
            impl = _backend_for("fly://")
            probe = _peek_for(impl, name)
            if probe:
                for existing in store.list_boxes():
                    if existing.endpoint == probe and not is_terminal("box", existing.status):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"box {existing.id} ({existing.name}) already occupies {probe}"
                            ),
                        )
            try:
                box = reserve_box(
                    name,
                    store=store,
                    backend=impl,
                    display_name=display_name,
                    description=description,
                    instructions=instructions,
                )
            except DuplicateBoxError as exc:
                # A *live* box holds the name — a destroyed one no longer does,
                # because teardown releases it.
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"cannot create a box named {name!r}: {exc}",
                ) from exc
            # This row goes straight into the app's sidebar without waiting
            # for a poll, so it has to carry the display name or the agent
            # shows up under its address for five seconds.
            payload = {"box": _with_meta(store, box), "box_id": box.id, "status": box.status}
        finally:
            store.close()

        def provision() -> None:
            inner = store_factory()
            try:
                create_box(name, store=inner, box=box, volume_gb=volume_gb, region=region)
            except Exception as exc:  # noqa: BLE001
                # `create_box` already records the failure against the row;
                # this only stops a stray exception ending the thread in
                # silence.
                _log.warning("provisioning %s failed: %s: %s", name, type(exc).__name__, exc)
            finally:
                inner.close()

        # A thread rather than an asyncio task: this endpoint is a sync `def`,
        # so FastAPI runs it in a threadpool where there is no running loop and
        # `create_task` raises. Capturing the loop at startup would work and is
        # more machinery than the job needs — `create_box` is blocking
        # subprocess work from end to end, so it belongs on a thread either
        # way.
        #
        # If the process dies mid-provision the row is left at `provisioning`
        # with a machine possibly created. That is not ignored: `reconcile_boxes`
        # sweeps exactly that, which is why it was built alongside this.
        worker = threading.Thread(target=provision, name=f"provision-{name}", daemon=True)
        _provisioning.add(worker)
        worker.start()

        return JSONResponse(payload, status_code=202)

    @app.get("/api/boxes/{box_id}")
    def get_box(box_id: str, _: Token | None = needs_read) -> Any:
        store = store_factory()
        try:
            box = store.get_box(box_id) or store.get_box_by_name(box_id)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
            # Only on the single-box read, never the list: it is one query per
            # box, and the list is the hot path that renders the sidebar on a
            # timer. The one consumer — deciding whether to resume a
            # conversation or start a fresh one — is opening a single agent.
            return {
                "box": {
                    **_with_meta(store, box),
                    "instructions_changed_at": _epoch(store.instructions_changed_at(box.id)),
                },
                "tasks": [_task_dict(t) for t in store.list_tasks(box_id=box.id)],
            }
        finally:
            store.close()

    @app.put("/api/boxes/{box_id}/meta")
    def set_meta(box_id: str, body: dict[str, Any], _: Token | None = needs_write) -> Any:
        """Rename or re-describe an agent. The address never changes.

        **Instructions are deliberately not editable here.** The store row is
        the *record* of what the agent was seeded with; the copy it reads is
        `SOUL.md` on its own volume, which the entrypoint writes once and never
        touches again. Editing the record after creation would make it lie
        about what the agent runs. Changing a live agent's instructions means
        reaching the volume, and that is a separate verb with a restart or an
        exec behind it — not a field on this endpoint.

        **A full replace, not a merge.** Omitting `display_name` or
        `description` clears it. The app always sends both; a CLI `flotta
        meta` would have to as well. Chosen because "PUT the whole identity"
        has one meaning and "PATCH some of it" has several — and the store's
        `test_meta_is_replaced_not_merged` pins it.
        """
        from flotta.store import InvalidBoxMetaError

        store = store_factory()
        try:
            box = store.get_box(box_id) or store.get_box_by_name(box_id)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
            current = store.meta_for_box(box.id)
            try:
                meta = store.set_box_meta(
                    box.id,
                    display_name=body.get("display_name"),
                    description=body.get("description"),
                    instructions=current.instructions if current else None,
                )
            except InvalidBoxMetaError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return {"box": _box_dict(box, meta)}
        finally:
            store.close()

    @app.put("/api/boxes/{box_id}/instructions")
    def set_instructions(box_id: str, body: dict[str, Any], _: Token | None = needs_write) -> Any:
        """Rewrite what an agent *is*, on the volume it reads.

        Separate from `PUT …/meta` because it is a different kind of act. A
        display name is a label the fleet keeps; standing instructions are a
        file on a machine, so this one wakes a box, writes to its disk, and can
        fail in ways renaming cannot.

        **It does not take effect until the conversation restarts.** Hermes
        renders a system prompt once per session, so the app starts a fresh one
        after saving — `changed_at` is what lets it decide, by comparing
        against the session it was about to resume.
        """
        from flotta.provision import ProvisionError
        from flotta.provision import set_instructions as write_soul
        from flotta.store import InvalidBoxMetaError

        store = store_factory()
        try:
            # Resolved here as well as inside, so an unknown agent is a 404
            # rather than the 409 every other refusal uses.
            box = store.get_box(box_id) or store.get_box_by_name(box_id)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
            try:
                written = write_soul(box.id, body.get("instructions"), store=store)
                # Same shape and the same units as the single-box read, so the
                # window can use the answer without a second fetch.
                return {**written, "changed_at": _epoch(written["changed_at"])}
            except InvalidBoxMetaError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except ProvisionError as exc:
                # 409, not 500: the fleet is fine and the request was
                # well-formed — this box could not be written to right now.
                raise HTTPException(status_code=409, detail=str(exc)) from exc
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

    @app.get("/api/boxes/{box_id}/machine")
    def get_machine(box_id: str, _: Token | None = needs_read) -> Any:
        """What the substrate says about this box's machine, beside the row.

        Every other read the app makes is the store's *belief*. This is the one
        that asks. Both are returned, unmerged and unreconciled, because the
        disagreements are the interesting part — a row saying `running` about a
        machine Fly stopped during a host drain is a real thing that happens,
        and a single blended answer would hide precisely it.

        **Never 500s on the substrate.** A person clicked "Info"; `flyctl`
        being unreachable, slow, or logged out is an answer to render
        (`unavailable`), not a failed request. The row is always returned, so
        the panel has something to show either way.

        Not polled, and it must not become polled: it is a subprocess per call.
        The list view stays on the store, which is cheap and, for status, kept
        current by every verb that touches a box.
        """
        from flotta.backend import BackendError, backend_for
        from flotta.provision import _fleet_image, _same_image

        store = store_factory()
        try:
            box = store.get_box(box_id) or store.get_box_by_name(box_id)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
            row = _with_meta(store, box)
            if not box.endpoint:
                # Not a failure: a box being built has no machine yet, which is
                # the honest thing to say rather than an error about flyctl.
                return {
                    "box": row,
                    "machine": None,
                    "unavailable": "this agent has no machine yet",
                    "fleet_image": _fleet_image(),
                    "image_current": None,
                }
            try:
                info = backend_for(box.endpoint).inspect(box.endpoint)
            except (BackendError, OSError, NotImplementedError) as exc:
                return {
                    "box": row,
                    "machine": None,
                    "unavailable": str(exc),
                    "fleet_image": _fleet_image(),
                    "image_current": None,
                }
            from dataclasses import asdict

            # Whether this agent is on the image the fleet builds — computed
            # **here**, with `_same_image`, rather than in the app.
            #
            # Fly reports `repo:tag@sha256:…` while `$FLOTTA_FLY_IMAGE` is
            # written without the digest, so string equality never matches.
            # That comparison has been wrong three times in this repo; a second
            # implementation in TypeScript would be the fourth. `None` means
            # one side is unknown, which is not the same as "behind" and must
            # not put an Upgrade button in front of somebody.
            fleet_image = _fleet_image()
            current: bool | None = None
            if fleet_image and info.image:
                current = _same_image(info.image, fleet_image)

            return {
                "box": row,
                "machine": asdict(info),
                "unavailable": None,
                "fleet_image": fleet_image,
                "image_current": current,
            }
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}/repos")
    def list_repos(box_id: str, _: Token | None = needs_read) -> Any:
        """Which repositories a box may use."""
        store = store_factory()
        try:
            box = store.get_box(box_id) or store.get_box_by_name(box_id)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
            return {"box_id": box.id, "name": box.name, "repos": store.repos_for_box(box.id)}
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/repos")
    def grant_repo_endpoint(
        box_id: str, body: dict[str, Any], _: Token | None = needs_write
    ) -> Any:
        """Grant a box a repository. Idempotent.

        `fleet:write` rather than `git:credential`: granting is an operator's
        act, minting is the box's. A box holding its own credential scope must
        not be able to widen its own access.
        """
        store = store_factory()
        try:
            box = store.get_box(box_id) or store.get_box_by_name(box_id)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
            try:
                repo = store.grant_repo(box.id, str(body.get("repo") or ""))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            store.add_event("box", box.id, "repo_granted", {"repo": repo})
            return {"box_id": box.id, "repos": store.repos_for_box(box.id)}
        finally:
            store.close()

    @app.delete("/api/boxes/{box_id}/repos/{owner}/{name}")
    def revoke_repo_endpoint(
        box_id: str, owner: str, name: str, _: Token | None = needs_write
    ) -> Any:
        """Withdraw a grant. Takes effect on the box's next credential request.

        No redeploy and no restart: the box holds nothing to invalidate, which
        is the point of it not holding a credential.
        """
        store = store_factory()
        try:
            box = store.get_box(box_id) or store.get_box_by_name(box_id)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
            had = store.revoke_repo(box.id, f"{owner}/{name}")
            if had:
                store.add_event("box", box.id, "repo_revoked", {"repo": f"{owner}/{name}"})
            return {"box_id": box.id, "revoked": had, "repos": store.repos_for_box(box.id)}
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/upgrade")
    def upgrade_box_endpoint(
        box_id: str, body: dict[str, Any] | None = None, _: Token | None = needs_write
    ) -> Any:
        """Move an agent onto a new image, keeping its disk.

        `fleet:write` rather than `box:destroy`: this is the *opposite* of
        destroying — the volume is what it exists to preserve — and gating it
        behind the destroy scope would mean handing out the ability to delete
        an agent in order to update one.

        **Synchronous, and that is a known limit rather than a decision.**
        `flyctl machine update` waits up to 300s by default, and a proxy in
        front of this cut `POST /api/boxes` at 60s once already — answering
        `502 Application failed to respond` while the work carried on. The same
        will happen here for an upgrade that pulls a large image, and the reply
        will be wrong in the same way.

        **Backgrounded, because there is an Upgrade button now.** The note this
        docstring used to carry — "anything that puts this behind a UI should
        background it first, the way FLOTTA-27 did for create" — was the
        instruction, and this is it being followed. `flyctl machine update`
        waits up to 300s, and a proxy in front of this cut `POST /api/boxes` at
        60s once already, answering `502 Application failed to respond` while
        the work carried on.

        What stays synchronous is everything the caller can *act* on: no such
        box, a terminal or mid-provision one, no image to move to. Those are
        409s worth having in the reply. Everything after is the substrate
        taking as long as it takes, and the answer is `202` plus a timeline to
        watch — `reimaged` on success, `upgrade_failed` on failure, both
        written by `upgrade_box`.

        `background=False` keeps the synchronous path, which is what the tests
        assert against and what `flotta upgrade` gets when it talks to a local
        store.
        """
        from flotta.provision import ProvisionError, UpgradeFailed, upgrade_box

        store = store_factory()
        try:
            box = store.get_box(box_id) or store.get_box_by_name(box_id)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
            image = str((body or {}).get("image") or "").strip() or None
            if background:
                return _upgrade_in_background(box, image)
            try:
                return upgrade_box(box.id, store=store, image=image)
            except UpgradeFailed as exc:
                # The substrate broke. 502, matching what `POST /api/boxes`
                # answers for the same class of failure — and *not* 409, which
                # would tell the caller they asked for the wrong thing when
                # flyctl was simply having a bad minute.
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            except ProvisionError as exc:
                # 409: the caller asked for something this box cannot do right
                # now — terminal, mid-provision, or no image named. Every one of
                # them is actionable, which is what separates it from the above.
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()

    def _upgrade_in_background(box: Any, image: str | None) -> Any:
        """Refuse what is refusable, answer, then re-image on a thread.

        The refusals are checked here rather than in the thread because they
        are the caller's to fix: a `202` for a box that can never be upgraded
        is a lie the app would render as a spinner. `upgrade_box` checks them
        again — it is also the CLI's entry point — and doing it twice is the
        price of the early exit.
        """
        from fastapi.responses import JSONResponse

        from flotta.provision import _fleet_image

        if is_terminal("box", box.status):
            raise HTTPException(
                status_code=409,
                detail=f"box {box.id} is {box.status!r}; there is no machine left to re-image.",
            )
        if box.status == "provisioning":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"box {box.id} is still provisioning. Upgrading mid-create would race "
                    "the thread building it; wait for it to settle."
                ),
            )
        target = (image or "").strip() or _fleet_image()
        if not target:
            raise HTTPException(
                status_code=409,
                detail=(
                    "no image to upgrade to. Pass one explicitly or set $FLOTTA_FLY_IMAGE "
                    "on the control plane."
                ),
            )

        def run() -> None:
            from flotta.provision import upgrade_box

            inner = store_factory()
            try:
                upgrade_box(box.id, store=inner, image=target)
            except Exception as exc:  # noqa: BLE001
                # `upgrade_box` writes `upgrade_failed` before raising, so the
                # app learns about this from the timeline. This only stops a
                # stray exception ending the thread in silence.
                _log.warning("upgrading %s failed: %s: %s", box.name, type(exc).__name__, exc)
            finally:
                inner.close()

        # A thread, for the same reason `_create_in_background` uses one: this
        # endpoint is a sync `def`, so there is no running loop to schedule on,
        # and the work is blocking subprocess calls end to end.
        worker = threading.Thread(target=run, name=f"upgrade-{box.name}", daemon=True)
        _provisioning.add(worker)
        worker.start()

        return JSONResponse(
            {"box_id": box.id, "box": _box_dict(box), "image": target, "started": True},
            status_code=202,
        )

    @app.post("/api/boxes/{box_id}/git-credential")
    def git_credential(box_id: str, body: dict[str, Any], token: Token | None = needs_git) -> Any:
        """Mint a git credential for a repository this box is granted.

        Called by the box's git credential helper, which git invokes with the
        repository path (`credential.useHttpPath=true`). The box holds no
        GitHub credential of its own — that is the whole design: a token in the
        agent's environment is a token a prompt-injected agent can print, and
        we have watched this agent act on instructions that came from a
        repository.

        **What this does and does not enforce.** Flotta refuses to hand a
        credential for a repository the box was not granted. It does *not*
        constrain what the returned token can reach — the source is one fleet
        token, so a box that extracted it could use it beyond its grants. That
        is policy enforced here, not by GitHub, and closing it means minting
        GitHub App installation tokens scoped to `repository_ids`. Stated
        plainly because a soft boundary that reads as a hard one is worse than
        no boundary at all.

        **A box token may only ask about its own box.** Scopes say what a token
        may do, never to which box, and that gap became load-bearing the moment
        a `git:credential` token started living on a machine whose agent has
        root. Without this check one box's token would reach every other box's
        grants, which is precisely the containment the grants exist to provide.
        """
        source = (os.environ.get(GITHUB_TOKEN_ENV) or "").strip()
        if not source:
            raise HTTPException(
                status_code=503,
                detail=f"no ${GITHUB_TOKEN_ENV} configured on the control plane; "
                f"boxes can read public repositories and nothing else",
            )

        store = store_factory()
        try:
            box = store.get_box(box_id) or store.get_box_by_name(box_id)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")

            # Before anything else: is this token allowed to speak for this
            # box? A non-box subject (an operator) is unrestricted — see
            # `auth.BOX_SUBJECT_PREFIX`.
            claimed = subject_box(token.subject) if token else None
            if claimed is not None and claimed not in {box.id, box.name}:
                raise HTTPException(
                    status_code=403,
                    detail=f"token for box {claimed!r} cannot mint credentials for "
                    f"box {box.name!r}",
                )

            repo = str(body.get("repo") or "").strip()
            if not repo:
                raise HTTPException(status_code=422, detail="which repository?")
            try:
                if not store.may_use_repo(box.id, repo):
                    raise HTTPException(
                        status_code=403,
                        detail=f"box {box.name!r} is not granted {repo!r}. "
                        f"Grant it with `flotta repo grant {box.name} {repo}`.",
                    )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            store.add_event("box", box.id, "git_credential", {"repo": repo})
            # The username is ignored by GitHub when the password is a token;
            # `x-access-token` is the convention its own docs use.
            return {"username": "x-access-token", "password": source, "repo": repo}
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
