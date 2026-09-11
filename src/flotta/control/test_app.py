"""Tests for the control-plane API, against a real store and the real app.

`fastapi` and `httpx` are in the `dev` dependency group, so these always run.
They used to be extra-only behind a `pytest.importorskip`, which meant CI —
a plain `uv sync --frozen` — skipped this entire file and reported green.
Import them plainly: a missing dependency should break the run, not quietly
delete sixteen tests from it.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from flotta.control.app import InsecureBindError, check_bind, create_app
from flotta.store import FleetStore


@pytest.fixture
def fleet(tmp_path):
    """A store on disk, so the API opens its own connections like production."""
    path = tmp_path / "fleet.db"
    store = FleetStore(path)
    box = store.create_box("eng-a")
    store.update_box_status(box.id, "running", endpoint="fly://app/m1")
    task = store.create_task(box.id, "add OAuth")
    store.update_task_status(task.id, "running")
    store.add_event("box", box.id, "running", {"reason": "spawn"})
    store.add_event("task", task.id, "spawned")
    store.close()
    return path


@pytest.fixture
def client(fleet):
    app = create_app(store_factory=lambda: FleetStore(fleet), run_loop=False, background=False)
    with TestClient(app) as c:
        yield c


# -- the bind guard ---------------------------------------------------------


def test_loopback_binds_are_allowed():
    for host in ("127.0.0.1", "localhost", "::1"):
        check_bind(host, env={})


def test_a_public_bind_is_refused_without_auth():
    """`DELETE /api/boxes/{id}` destroys a box and everything it remembers.

    Scoped tokens are M5; until then the service fails closed at startup rather
    than warning in a deploy log nobody re-reads.
    """
    with pytest.raises(InsecureBindError, match="no authentication"):
        check_bind("0.0.0.0", env={})


def test_a_public_bind_is_allowed_once_a_signing_key_exists():
    """The guard's whole purpose was "there is no way to authenticate this".

    There is now, so the refusal lifts — and lifts *only* for that reason. This
    is the milestone: the bind guard stops being a placeholder and becomes a
    check on whether auth is configured.
    """
    check_bind("0.0.0.0", env={"FLOTTA_SIGNING_KEY": "a-real-key"})


def test_the_insecure_override_is_gone():
    """`FLOTTA_CONTROL_ALLOW_INSECURE_BIND` skipped the guard entirely.

    It existed because the only alternatives were "refuse" and "refuse unless
    you promise you own the network". Keeping it now would mean shipping a
    documented way to expose an unauthenticated kill switch, which is precisely
    the hole M5 closes.
    """
    with pytest.raises(InsecureBindError):
        check_bind("0.0.0.0", env={"FLOTTA_CONTROL_ALLOW_INSECURE_BIND": "1"})


def test_the_refusal_says_how_to_fix_it():
    with pytest.raises(InsecureBindError, match="flotta token key"):
        check_bind("0.0.0.0", env={})


# -- reads ------------------------------------------------------------------


def test_boxes_are_listed(client):
    body = client.get("/api/boxes").json()
    assert [b["name"] for b in body["boxes"]] == ["eng-a"]
    assert body["boxes"][0]["latest_task"] == "add OAuth"


def test_a_stopped_box_is_still_listed(client, fleet):
    """A stopped box is idle, not finished. Hiding it would hide the fleet."""
    store = FleetStore(fleet)
    box = store.list_boxes()[0]
    task = store.list_tasks(box_id=box.id)[0]
    store.update_task_status(task.id, "done")
    store.update_box_status(box.id, "stopped")
    store.close()

    names = [b["name"] for b in client.get("/api/boxes").json()["boxes"]]
    assert names == ["eng-a"]


def test_a_torn_down_box_is_hidden_unless_asked_for(client, fleet):
    store = FleetStore(fleet)
    box = store.list_boxes()[0]
    store.update_box_status(box.id, "torn_down")
    store.close()

    assert client.get("/api/boxes").json()["boxes"] == []
    assert len(client.get("/api/boxes", params={"all_": True}).json()["boxes"]) == 1


def test_a_box_can_be_fetched_by_name_or_id(client, fleet):
    store = FleetStore(fleet)
    box_id = store.list_boxes()[0].id
    store.close()

    for key in (box_id, "eng-a"):
        body = client.get(f"/api/boxes/{key}").json()
        assert body["box"]["name"] == "eng-a"
        assert [t["prompt"] for t in body["tasks"]] == ["add OAuth"]


def test_an_unknown_box_is_404_not_an_empty_object(client):
    assert client.get("/api/boxes/b-nope").status_code == 404
    assert client.get("/api/boxes/b-nope/events").status_code == 404


def test_events_span_all_three_tiers(client):
    events = client.get("/api/boxes/eng-a/events").json()["events"]
    assert [e["entity_kind"] for e in events] == ["box", "task"]
    assert [e["type"] for e in events] == ["running", "spawned"]


# -- health -----------------------------------------------------------------


def test_health_is_ok_when_the_loop_is_sweeping(fleet):
    import time

    app = create_app(store_factory=lambda: FleetStore(fleet), run_loop=False, background=False)
    app.state.loop_state.started_at = time.monotonic()
    app.state.loop_state.last_sweep_at = time.monotonic()

    with TestClient(app) as c:
        response = c.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_is_503_when_the_loop_has_stalled(fleet):
    """The assertion this endpoint exists for.

    A slept loop looks identical to an idle one from outside — both report zero
    reconciled and a healthy process. §8.3 names exactly this as the Railway
    footgun, and "the process is up" is the thing that stays true while the
    fleet stops being watched.

    Simulated with a runner that starts and then never sweeps, because that is
    what a slept platform actually produces: the task exists, the state says
    running, and `last_sweep_at` stops advancing. A real loop cannot test this
    — a real loop sweeps.
    """
    import asyncio

    async def never_sweeps(state, *, store_factory):
        state.started_at = state._clock() - 600  # started ten minutes ago
        state.stale_after_s = 30
        await asyncio.Event().wait()  # and then nothing, forever

    app = create_app(
        store_factory=lambda: FleetStore(fleet), run_loop=True, loop_runner=never_sweeps
    )

    with TestClient(app) as c:
        response = c.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["reconcile_loop"]["stale"] is True
    assert body["reconcile_loop"]["seconds_since_last_sweep"] is None


def test_a_read_only_replica_is_healthy_without_sweeping(fleet):
    """Not every replica runs the loop; one that does not must not report
    degraded for failing to do a job it was told not to do."""
    app = create_app(store_factory=lambda: FleetStore(fleet), run_loop=False, background=False)
    with TestClient(app) as c:
        response = c.get("/health")
    assert response.status_code == 200
    assert response.json()["reconcile_loop"]["running"] is False


# -- the list view has to do the arithmetic the SQL used to do ---------------


def test_a_boxs_cost_is_summed_across_its_tasks(client, fleet):
    """Cost lives on tasks; the list view is where it gets added up.

    The dashboard's direct-SQL version did `SUM(cost_estimate)`. Proxying to
    this endpoint dropped the sum without dropping the column, so every cost in
    the UI silently rendered blank — a fleet that looked free.
    """
    store = FleetStore(fleet)
    box = store.list_boxes()[0]
    first = store.list_tasks(box_id=box.id)[0]
    store.update_task_status(first.id, "done", cost_estimate=0.04)
    second = store.create_task(box.id, "write tests")
    store.update_task_status(second.id, "running")
    store.update_task_status(second.id, "done", cost_estimate=0.06)
    store.close()

    row = client.get("/api/boxes").json()["boxes"][0]
    assert row["cost_estimate"] == pytest.approx(0.10)
    assert row["task_count"] == 2


def test_an_unpriced_box_costs_none_not_zero(client):
    """A blank says "no rate configured"; a zero claims the box ran for free."""
    row = client.get("/api/boxes").json()["boxes"][0]
    assert row["cost_estimate"] is None


def test_the_detail_view_carries_the_tasks_the_list_summarises(client, fleet):
    """`latest_task` is a list-view convenience; detail returns the tasks.

    Both have to be able to name the box's task. The detail page rendered an
    empty one, because only the list shape was checked against the UI.
    """
    body = client.get("/api/boxes/eng-a").json()
    assert [t["prompt"] for t in body["tasks"]] == ["add OAuth"]


def test_the_service_and_the_cli_resolve_the_same_store(tmp_path, monkeypatch):
    """One rule for where the fleet lives, not one copy per caller.

    This rule was written out twice — once in the CLI, once here — and the
    copies disagreed: the service honoured `$FLOTTA_DATABASE_URL` but not
    `$FLOTTA_STORE`, so `flotta serve` served an empty fleet from `./fleet.db`
    while `flotta ps` in the same shell listed the real one. It now lives in
    `FleetStore` itself, and this asserts the two agree.
    """
    from flotta.cli import resolve_store_path
    from flotta.control.app import _store_factory

    path = tmp_path / "elsewhere.db"
    store = FleetStore(path)
    store.create_box("eng-a")
    store.close()

    monkeypatch.chdir(tmp_path)  # so a bad resolution finds ./fleet.db, not the repo's
    monkeypatch.setenv("FLOTTA_STORE", str(path))

    assert resolve_store_path() == path
    served = _store_factory()
    try:
        assert [b.name for b in served.list_boxes()] == ["eng-a"]
    finally:
        served.close()


# -- creating a box over the API (M8 prerequisite) --------------------------


def test_post_creates_a_box(client, fleet, monkeypatch):
    """ "Create Agent B" as a request rather than a shell recipe.

    `FlyBackend.create` shipped in M1 with no caller at all — `provision.
    create_box` was reachable only from a script and these tests — so a box was
    hand-provisioned with `just fly-up`. This endpoint is what makes it a verb.
    """
    import flotta.provision as provision

    made = {}

    def fake_create(name, *, store, **kwargs):
        made["name"] = name
        box = store.create_box(name)
        store.update_box_status(box.id, "running", endpoint="fly://app/m-new")
        return {"box_id": box.id, "endpoint": "fly://app/m-new"}

    monkeypatch.setattr(provision, "create_box", fake_create)

    response = client.post("/api/boxes", json={"name": "eng-b"})
    assert response.status_code == 201
    body = response.json()
    assert made["name"] == "eng-b"
    assert body["box"]["name"] == "eng-b"
    assert body["endpoint"] == "fly://app/m-new"

    # and it is really in the fleet, not just echoed back
    assert "eng-b" in [b["name"] for b in client.get("/api/boxes").json()["boxes"]]


def test_post_without_a_name_is_422_not_a_generated_one(client):
    """A box's name is how you address it in `flotta chat` and in the app.

    Generating one silently would leave someone with `box-4f2a91` and no idea
    which agent it is.
    """
    assert client.post("/api/boxes", json={}).status_code == 422
    assert client.post("/api/boxes", json={"name": "   "}).status_code == 422


def test_an_occupied_machine_is_409(client, monkeypatch):
    """The one create failure that is genuinely a conflict about fleet state.

    The caller can act on it — pick another app, tear the other box down — so
    it must not arrive as an opaque 5xx.
    """
    import flotta.provision as provision

    def refuse(name, *, store, **kwargs):
        raise provision.BoxOccupied("box b-1 (eng-a) already occupies fly://app/m1")

    monkeypatch.setattr(provision, "create_box", refuse)

    response = client.post("/api/boxes", json={"name": "eng-b"})
    assert response.status_code == 409
    assert "already occupies" in response.json()["detail"]


def test_a_substrate_failure_is_502_not_409(client, monkeypatch):
    """A backend that is down or timing out is not the caller's conflict.

    Every `ProvisionError` used to become 409, which told someone whose Fly
    region was having a bad minute that they had a naming conflict.
    """
    import flotta.provision as provision

    def boom(name, *, store, **kwargs):
        raise provision.ProvisionError("create failed: BackendError: flyctl timed out")

    monkeypatch.setattr(provision, "create_box", boom)

    response = client.post("/api/boxes", json={"name": "eng-b"})
    assert response.status_code == 502
    assert "flyctl timed out" in response.json()["detail"]


def test_a_created_but_not_running_box_is_201_with_its_id(client, fleet, monkeypatch):
    """A machine exists. Answering with an error would hide it.

    `create_box` records the row as `stopped` and raises, because the box is
    not usable yet. But something **was** created and is costing disk, so a
    response that says "failed" — and does not even carry the id — leaves the
    caller unable to start it *or* destroy it. That is an orphan machine
    billing against an account nobody is watching, which this repo has done
    once already.
    """
    import flotta.provision as provision

    def half_made(name, *, store, **kwargs):
        box = store.create_box(name)
        store.update_box_status(box.id, "stopped", endpoint="fly://app/m-cold")
        raise provision.BoxNotRunning(
            f"box {box.id} was created but is not running",
            box_id=box.id,
            endpoint="fly://app/m-cold",
        )

    monkeypatch.setattr(provision, "create_box", half_made)

    response = client.post("/api/boxes", json={"name": "eng-b"})
    assert response.status_code == 201
    body = response.json()
    assert body["box"]["status"] == "stopped"
    assert body["box_id"], "the caller must learn the id to start or kill it"
    assert body["endpoint"] == "fly://app/m-cold"
    assert "not running" in body["warning"]


def test_delete_goes_through_teardown_rather_than_writing_the_row(client, fleet, monkeypatch):
    """D10, enforced at the API: only code that reached the substrate may close
    the row.

    Writing `torn_down` directly here would close the row while the machine
    kept running and billing — the bug M0's review caught in `stop_box`. This
    endpoint had no test at all until the Modal cut.
    """
    import flotta.provision as provision

    called = {}

    def fake_teardown(box_id, *, store, reason=None, **kwargs):
        called["box_id"] = box_id
        called["reason"] = reason
        store.update_box_status(box_id, "torn_down")
        return {"box_id": box_id, "status": "torn_down"}

    monkeypatch.setattr(provision, "teardown_box", fake_teardown)

    box_id = client.get("/api/boxes").json()["boxes"][0]["id"]
    response = client.delete(f"/api/boxes/{box_id}")
    assert response.status_code == 200
    assert called["box_id"] == box_id
    assert called["reason"] == "control-plane"
    assert client.get(f"/api/boxes/{box_id}").json()["box"]["status"] == "torn_down"


# -- with authentication switched on ---------------------------------------
#
# Every test above runs with no signing key, which is the documented
# loopback-development state and means they exercise none of this. These are
# the ones that matter for M5: same app, same routes, a key configured.

AUTH_KEY = "a-signing-key-for-tests"


@pytest.fixture
def secured(fleet):
    """The control plane as it runs anywhere that is not someone's laptop."""
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        signing_key=AUTH_KEY,
    )
    with TestClient(app) as c:
        yield c


def _token(*scopes, subject="test", ttl_s=300):
    from flotta.auth import mint

    return mint(subject=subject, scopes=set(scopes), key=AUTH_KEY, ttl_s=ttl_s)


def _bearer(*scopes, **kw):
    return {"Authorization": f"Bearer {_token(*scopes, **kw)}"}


def test_an_unauthenticated_request_is_401_with_a_challenge(secured):
    response = secured.get("/api/boxes")
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_a_valid_token_gets_through(secured):
    from flotta.auth import SCOPE_FLEET_READ

    response = secured.get("/api/boxes", headers=_bearer(SCOPE_FLEET_READ))
    assert response.status_code == 200
    assert [b["name"] for b in response.json()["boxes"]] == ["eng-a"]


def test_a_forged_token_is_refused(secured):
    """The signature is the only thing standing between a reader and destroy."""
    from flotta.auth import mint

    forged = mint(subject="attacker", scopes={"box:destroy"}, key="not-the-servers-key")
    response = secured.delete("/api/boxes/eng-a", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401
    assert "signature" in response.json()["detail"]


def test_an_expired_token_is_refused(secured):
    import time

    from flotta.auth import SCOPE_FLEET_READ, mint

    stale = mint(
        subject="test",
        scopes={SCOPE_FLEET_READ},
        key=AUTH_KEY,
        ttl_s=1,
        now=int(time.time()) - 3600,
    )
    response = secured.get("/api/boxes", headers={"Authorization": f"Bearer {stale}"})
    assert response.status_code == 401
    assert "expired" in response.json()["detail"]


def test_reading_the_fleet_does_not_let_you_destroy_it(secured):
    """The reason `box:destroy` is its own scope.

    A dashboard token shows the fleet. If that also tore boxes down, the
    separation would be decorative — and DELETE is the verb that deletes an
    agent's entire memory.
    """
    from flotta.auth import SCOPE_FLEET_READ

    headers = _bearer(SCOPE_FLEET_READ)
    assert secured.get("/api/boxes", headers=headers).status_code == 200

    denied = secured.delete("/api/boxes/eng-a", headers=headers)
    assert denied.status_code == 403, "a read token must not destroy a box"
    assert "box:destroy" in denied.json()["detail"]


def test_creating_does_not_let_you_destroy_either(secured):
    from flotta.auth import SCOPE_FLEET_WRITE

    denied = secured.delete("/api/boxes/eng-a", headers=_bearer(SCOPE_FLEET_WRITE))
    assert denied.status_code == 403


def test_a_missing_scope_is_403_not_401(secured):
    """401 means "authenticate"; 403 means "you did, and it is not enough".

    Answering 401 here would send a caller off to re-authenticate with the same
    token forever.
    """
    from flotta.auth import SCOPE_FLEET_READ

    assert (
        secured.post(
            "/api/boxes", json={"name": "eng-b"}, headers=_bearer(SCOPE_FLEET_READ)
        ).status_code
        == 403
    )


def test_every_api_route_requires_a_token(secured, fleet):
    """Guard against a route being added without one.

    The failure this prevents is silent: a new endpoint with no dependency
    serves the fleet to anybody, and nothing else in the suite would notice
    because every other test sends a valid token.
    """
    box_id = "eng-a"
    unauthenticated = [
        secured.get("/api/boxes"),
        secured.get(f"/api/boxes/{box_id}"),
        secured.get(f"/api/boxes/{box_id}/events"),
        secured.post("/api/boxes", json={"name": "x"}),
        secured.delete(f"/api/boxes/{box_id}"),
    ]
    assert [r.status_code for r in unauthenticated] == [401] * 5


def test_health_is_not_behind_auth(secured):
    """A liveness probe cannot hold a credential.

    Deliberate, and worth stating: `/health` reports whether the reconcile loop
    is sweeping and nothing about the fleet's contents — no box names, no
    endpoints, no task text. That is what makes it safe to leave open.
    """
    response = secured.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert "reconcile_loop" in body
    assert "boxes" not in body


# -- waking a box for the front door (M5b) ----------------------------------


def test_wake_requires_box_chat_not_fleet_write(secured, monkeypatch):
    """Waking is guarded by `box:chat`, not a scope of its own.

    A box is asleep most of the time, so anything permitted to talk to one must
    be permitted to wake it — a separate `box:wake` would be a scope nobody
    could sensibly withhold. But a token that merely *creates* boxes has no
    business starting someone else's.
    """
    from flotta.auth import SCOPE_BOX_CHAT, SCOPE_FLEET_WRITE

    denied = secured.post("/api/boxes/eng-a/wake", headers=_bearer(SCOPE_FLEET_WRITE))
    assert denied.status_code == 403
    assert "box:chat" in denied.json()["detail"]

    import flotta.provision as provision

    monkeypatch.setattr(
        provision, "wake_box", lambda box_id, **kw: {"box_id": box_id, "woken": True}
    )
    assert secured.post("/api/boxes/eng-a/wake", headers=_bearer(SCOPE_BOX_CHAT)).status_code == 200


def test_wake_goes_through_wake_box_not_start_box(secured, monkeypatch):
    """`start_box` is the operator's verb and refuses anything not `stopped`.

    That is right when a human asks and wrong here: the addressing path has to
    accept an already-running box, and reconcile a row that disagrees with the
    substrate — Fly stops machines on its own during a host drain.
    """
    import flotta.provision as provision
    from flotta.auth import SCOPE_BOX_CHAT

    called = {}

    def fake_wake(box_id, *, store, reason=None, **kw):
        called["box_id"] = box_id
        called["reason"] = reason
        return {"box_id": box_id, "woken": True}

    monkeypatch.setattr(provision, "wake_box", fake_wake)
    monkeypatch.setattr(
        provision, "start_box", lambda *a, **k: pytest.fail("the addressing path used start_box")
    )

    response = secured.post("/api/boxes/eng-a/wake", headers=_bearer(SCOPE_BOX_CHAT))
    assert response.status_code == 200
    assert called["reason"] == "front-door"


def test_waking_an_unknown_box_is_404(secured):
    from flotta.auth import SCOPE_BOX_CHAT

    assert secured.post("/api/boxes/nope/wake", headers=_bearer(SCOPE_BOX_CHAT)).status_code == 404


def test_waking_a_torn_down_box_is_409_not_502(secured, monkeypatch):
    """An illegal state is the caller's problem; a failing substrate is not.

    Answering 502 for both would tell the door to retry a box that can never
    come back.
    """
    import flotta.provision as provision
    from flotta.auth import SCOPE_BOX_CHAT

    def refuse(box_id, **kw):
        raise provision.ProvisionError(
            f"box {box_id} is 'torn_down'; only a running or stopped box can be addressed"
        )

    monkeypatch.setattr(provision, "wake_box", refuse)
    assert secured.post("/api/boxes/eng-a/wake", headers=_bearer(SCOPE_BOX_CHAT)).status_code == 409


def test_a_substrate_failure_waking_is_502(secured, monkeypatch):
    import flotta.provision as provision
    from flotta.auth import SCOPE_BOX_CHAT

    def boom(box_id, **kw):
        raise provision.ProvisionError("start failed: BackendError: flyctl timed out")

    monkeypatch.setattr(provision, "wake_box", boom)
    assert secured.post("/api/boxes/eng-a/wake", headers=_bearer(SCOPE_BOX_CHAT)).status_code == 502


# -- activity, for idle sleep -----------------------------------------------


def _quieten(fleet, ago_s=3600):
    """Backdate every event so the box reads as idle.

    Needed because the fixture creates a box *now*: its `running` event is
    seconds old, so a touch is correctly skipped as redundant. Testing the
    activity path means testing it on a box that has actually gone quiet.
    """
    from datetime import UTC, datetime, timedelta

    store = FleetStore(fleet)
    try:
        old = (datetime.now(UTC) - timedelta(seconds=ago_s)).isoformat()
        store._conn.execute("UPDATE events SET ts = ?", (old,))
    finally:
        store.close()


def test_waking_records_activity(secured, fleet, monkeypatch):
    """The signal idle sleep runs on.

    It is an event rather than a `boxes.last_active_at` column because the
    store has no migration machinery — a new column would break every existing
    fleet — so this asserts the substitute is actually written.
    """
    import flotta.provision as provision
    from flotta.auth import SCOPE_BOX_CHAT
    from flotta.provision import ADDRESSED_EVENT

    monkeypatch.setattr(provision, "wake_box", lambda box_id, **kw: {"box_id": box_id})
    _quieten(fleet)  # the box must actually be quiet, or the touch is redundant
    secured.post("/api/boxes/eng-a/wake", headers=_bearer(SCOPE_BOX_CHAT))

    store = FleetStore(fleet)
    try:
        box = store.get_box_by_name("eng-a")
        assert ADDRESSED_EVENT in [e.type for e in store.get_events("box", box.id)]
    finally:
        store.close()


def test_activity_is_rate_limited(secured, fleet, monkeypatch):
    """The event log is the fleet's history, not a request log.

    One row per request would bury the transitions a human reads it for, and
    on a busy box would grow without bound.
    """
    import flotta.provision as provision
    from flotta.auth import SCOPE_BOX_CHAT
    from flotta.provision import ADDRESSED_EVENT

    monkeypatch.setattr(provision, "wake_box", lambda box_id, **kw: {"box_id": box_id})
    _quieten(fleet)
    for _ in range(5):
        secured.post("/api/boxes/eng-a/wake", headers=_bearer(SCOPE_BOX_CHAT))

    store = FleetStore(fleet)
    try:
        box = store.get_box_by_name("eng-a")
        written = [e for e in store.get_events("box", box.id) if e.type == ADDRESSED_EVENT]
        assert len(written) == 1, f"five requests wrote {len(written)} events"
    finally:
        store.close()


# -- repository grants and git credentials ----------------------------------
#
# The boundary that matters here is "which repositories may this box reach",
# and it is enforced in exactly one place. These lean on that rather than on
# the happy path.

GH_SOURCE = "ghp_a_fleet_token_for_tests"


def _grant(secured, box, repo, scope=None):
    from flotta.auth import SCOPE_FLEET_WRITE

    return secured.post(
        f"/api/boxes/{box}/repos",
        json={"repo": repo},
        headers=_bearer(scope or SCOPE_FLEET_WRITE),
    )


def test_a_box_can_be_granted_several_repositories(secured):
    """A task can legitimately span repos — fix a bug in one, update the client
    in another. That is one task, so a grant is a set."""
    _grant(secured, "eng-a", "joaoh82/flotta")
    body = _grant(secured, "eng-a", "joaoh82/flotta_parent").json()
    assert body["repos"] == ["joaoh82/flotta", "joaoh82/flotta_parent"]


def test_a_repository_is_the_same_grant_however_it_is_spelled(secured):
    """A grant recorded as a URL and one recorded as a slug would look like two
    grants for one repository, and the credential path compares for equality."""
    _grant(secured, "eng-a", "https://github.com/joaoh82/Flotta.git")
    body = _grant(secured, "eng-a", "git@github.com:joaoh82/flotta.git").json()
    assert body["repos"] == ["joaoh82/flotta"], "the same repo was granted twice"


def test_a_credential_is_refused_for_a_repository_that_was_not_granted(secured, monkeypatch):
    """The whole point. A box asks for a repo; Flotta decides."""
    from flotta.auth import SCOPE_GIT_CREDENTIAL

    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", GH_SOURCE)
    _grant(secured, "eng-a", "joaoh82/flotta")

    denied = secured.post(
        "/api/boxes/eng-a/git-credential",
        json={"repo": "someone-else/private"},
        headers=_bearer(SCOPE_GIT_CREDENTIAL),
    )
    assert denied.status_code == 403
    assert GH_SOURCE not in denied.text, "the token leaked in a refusal"


def test_a_credential_is_issued_for_a_granted_repository(secured, monkeypatch):
    from flotta.auth import SCOPE_GIT_CREDENTIAL

    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", GH_SOURCE)
    _grant(secured, "eng-a", "joaoh82/flotta")

    ok = secured.post(
        "/api/boxes/eng-a/git-credential",
        json={"repo": "https://github.com/joaoh82/flotta.git"},
        headers=_bearer(SCOPE_GIT_CREDENTIAL),
    )
    assert ok.status_code == 200
    assert ok.json()["password"] == GH_SOURCE
    assert ok.json()["username"] == "x-access-token"


def test_chatting_does_not_mint_a_git_credential(secured, monkeypatch):
    """`box:chat` is the most widely handed out token — it is what the app
    needs. It must not also be a key to the user's code."""
    from flotta.auth import SCOPE_BOX_CHAT

    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", GH_SOURCE)
    _grant(secured, "eng-a", "joaoh82/flotta")

    denied = secured.post(
        "/api/boxes/eng-a/git-credential",
        json={"repo": "joaoh82/flotta"},
        headers=_bearer(SCOPE_BOX_CHAT),
    )
    assert denied.status_code == 403
    assert GH_SOURCE not in denied.text


def test_a_box_cannot_widen_its_own_access(secured, monkeypatch):
    """Granting is an operator's act; minting is the box's.

    A box holds `git:credential`. If that also let it grant, an agent could
    add a repository to its own list and then ask for the key to it.
    """
    from flotta.auth import SCOPE_GIT_CREDENTIAL

    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", GH_SOURCE)
    denied = _grant(secured, "eng-a", "someone-else/private", scope=SCOPE_GIT_CREDENTIAL)
    assert denied.status_code == 403


def test_revoking_needs_no_redeploy(secured, monkeypatch):
    """The box holds nothing to invalidate, which is the point of it holding
    no credential: a revoke takes effect on the next request."""
    from flotta.auth import SCOPE_FLEET_WRITE, SCOPE_GIT_CREDENTIAL

    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", GH_SOURCE)
    _grant(secured, "eng-a", "joaoh82/flotta")
    ask = {"json": {"repo": "joaoh82/flotta"}, "headers": _bearer(SCOPE_GIT_CREDENTIAL)}
    assert secured.post("/api/boxes/eng-a/git-credential", **ask).status_code == 200

    secured.delete("/api/boxes/eng-a/repos/joaoh82/flotta", headers=_bearer(SCOPE_FLEET_WRITE))
    assert secured.post("/api/boxes/eng-a/git-credential", **ask).status_code == 403


def test_no_source_token_is_503_and_says_what_is_missing(secured, monkeypatch):
    """A control plane without a GitHub token is a legitimate state — public
    repositories still work — so it must not read as a broken box."""
    from flotta.auth import SCOPE_GIT_CREDENTIAL

    monkeypatch.delenv("FLOTTA_GITHUB_TOKEN", raising=False)
    _grant(secured, "eng-a", "joaoh82/flotta")

    response = secured.post(
        "/api/boxes/eng-a/git-credential",
        json={"repo": "joaoh82/flotta"},
        headers=_bearer(SCOPE_GIT_CREDENTIAL),
    )
    assert response.status_code == 503
    assert "FLOTTA_GITHUB_TOKEN" in response.json()["detail"]


def test_a_malformed_repository_is_422_not_a_silent_grant(secured):
    assert _grant(secured, "eng-a", "not-a-repo").status_code == 422
    assert _grant(secured, "eng-a", "").status_code == 422


# -- a box token speaks for its own box, and no other ------------------------
#
# Scopes say what a token may do; they never said *to which box*. That gap was
# harmless while every token was held by a person. It stopped being harmless
# when part 2 put a `git:credential` token on a machine whose agent has root:
# one box's token would otherwise reach every other box's grants, and per-box
# grants would be decoration.


def _box_ids(secured):
    from flotta.auth import SCOPE_FLEET_READ

    listed = secured.get("/api/boxes", headers=_bearer(SCOPE_FLEET_READ)).json()
    return {b["name"]: b["id"] for b in listed["boxes"]}


@pytest.fixture
def two_boxes(secured, fleet):
    """`eng-a` from the fixture, plus a second box with its own grant."""
    from flotta.store import FleetStore

    store = FleetStore(fleet)
    other = store.create_box("eng-b")
    store.update_box_status(other.id, "running", endpoint="fly://app/m2")
    store.grant_repo(other.id, "someone/secret")
    store.close()
    return _box_ids(secured)


def test_a_box_token_cannot_mint_for_another_box(two_boxes, secured, monkeypatch):
    """The containment the grants exist to provide."""
    from flotta.auth import SCOPE_GIT_CREDENTIAL, box_subject

    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", GH_SOURCE)

    denied = secured.post(
        "/api/boxes/eng-b/git-credential",
        json={"repo": "someone/secret"},
        headers=_bearer(SCOPE_GIT_CREDENTIAL, subject=box_subject(two_boxes["eng-a"])),
    )
    assert denied.status_code == 403
    assert GH_SOURCE not in denied.text


def test_a_box_token_works_for_its_own_box(two_boxes, secured, monkeypatch):
    """The check must not cost a box the thing it exists to do."""
    from flotta.auth import SCOPE_GIT_CREDENTIAL, box_subject

    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", GH_SOURCE)

    ok = secured.post(
        "/api/boxes/eng-b/git-credential",
        json={"repo": "someone/secret"},
        headers=_bearer(SCOPE_GIT_CREDENTIAL, subject=box_subject(two_boxes["eng-b"])),
    )
    assert ok.status_code == 200
    assert ok.json()["password"] == GH_SOURCE


def test_a_box_token_may_name_its_box_by_id_or_by_name(two_boxes, secured, monkeypatch):
    """The URL takes either, so the check has to accept either — otherwise
    `flotta chat eng-b`-shaped addressing works everywhere except here."""
    from flotta.auth import SCOPE_GIT_CREDENTIAL, box_subject

    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", GH_SOURCE)

    ok = secured.post(
        f"/api/boxes/{two_boxes['eng-b']}/git-credential",
        json={"repo": "someone/secret"},
        headers=_bearer(SCOPE_GIT_CREDENTIAL, subject=box_subject("eng-b")),
    )
    assert ok.status_code == 200


def test_an_operator_token_is_not_restricted_to_one_box(two_boxes, secured, monkeypatch):
    """A human debugging a grant should not have to impersonate a box. The rule
    is one-directional: a box subject is confined, anything else is not."""
    from flotta.auth import SCOPE_GIT_CREDENTIAL

    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", GH_SOURCE)

    ok = secured.post(
        "/api/boxes/eng-b/git-credential",
        json={"repo": "someone/secret"},
        headers=_bearer(SCOPE_GIT_CREDENTIAL, subject="joao"),
    )
    assert ok.status_code == 200


def test_the_refusal_names_a_command_that_exists(secured, monkeypatch):
    """The 403 is the only guidance anyone gets — it is what the box's helper
    passes through to git's stderr, and the agent reads that and nothing else.

    It said `flotta grant eng-a x/y` for a command that is `flotta repo grant`,
    which is a wrong instruction delivered at exactly the moment someone is
    stuck. Found by running the helper against a real control plane; no test
    reads an error message for whether it is *true*.
    """
    import re

    from typer.testing import CliRunner

    from flotta.auth import SCOPE_GIT_CREDENTIAL
    from flotta.cli import app as cli

    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", GH_SOURCE)
    denied = secured.post(
        "/api/boxes/eng-a/git-credential",
        json={"repo": "someone-else/private"},
        headers=_bearer(SCOPE_GIT_CREDENTIAL),
    )
    quoted = re.findall(r"`flotta ([^`]+)`", denied.json()["detail"])
    assert quoted, "the refusal stopped naming a way out"

    for suggestion in quoted:
        # Command words only: the rest are this box's name and repository.
        words = list(suggestion.split())
        while words and not words[-1].replace("-", "").isalpha():
            words.pop()
        result = CliRunner().invoke(cli, [*words[:2], "--help"])
        assert result.exit_code == 0, f"`flotta {suggestion}` is not a command"


# -- FLOTTA-27: creating an agent answers immediately ------------------------


def _async_client(fleet, created):
    """A control plane that provisions in the background, like production."""
    from flotta.control.app import create_app

    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=True,
        signing_key=AUTH_KEY,
    )
    return app


def test_creating_an_agent_answers_before_the_machine_exists(fleet, monkeypatch):
    """Provisioning is an app, a volume, a machine and a boot. Held open as one
    request it outlives the proxy in front of it — Railway answered `502
    Application failed to respond` while the work carried on, so the caller was
    told it failed and a machine appeared anyway."""
    from flotta.auth import SCOPE_FLEET_WRITE

    slow: list[str] = []

    def never_returns(name, **kwargs):
        slow.append(name)
        raise AssertionError("the request must not wait for this")

    monkeypatch.setattr("flotta.provision.create_box", never_returns, raising=False)

    with TestClient(_async_client(fleet, slow)) as client:
        response = client.post(
            "/api/boxes",
            json={"name": "eng-x"},
            headers=_bearer(SCOPE_FLEET_WRITE),
        )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "provisioning"
    assert body["box_id"], "the caller gets something real to poll"


def test_a_duplicate_name_is_a_refusal_not_a_500(fleet):
    """`boxes.name` is UNIQUE across every row including torn-down ones, so
    recreating a destroyed agent raised an IntegrityError that surfaced as a
    bare 500 with nothing in it for the caller."""
    from flotta.auth import SCOPE_FLEET_WRITE

    with TestClient(_async_client(fleet, [])) as client:
        response = client.post(
            "/api/boxes",
            json={"name": "eng-a"},  # already in the fixture
            headers=_bearer(SCOPE_FLEET_WRITE),
        )

    assert response.status_code == 409, response.text
    assert "eng-a" in response.json()["detail"]


def test_a_name_that_cannot_be_an_address_is_422_before_anything_is_made(client, monkeypatch):
    """422, not 409.

    409 is what this answered before — routed through the generic "cannot
    create a box named X" arm — and a conflict tells the caller to pick a
    different name when the fix is to spell this one properly. It is also
    refused *before* `_peek_for` shells out to Fly and before a row exists,
    which is what stops a typo consuming the name: a terminal row keeps its
    name forever, because `release_name` runs only in `teardown_box`.
    """
    import flotta.provision as provision

    def never(*args, **kwargs):
        raise AssertionError("nothing should be provisioned for an impossible name")

    monkeypatch.setattr(provision, "create_box", never)
    monkeypatch.setattr(provision, "reserve_box", never)

    response = client.post("/api/boxes", json={"name": "Eng-f"})
    assert response.status_code == 422
    assert "eng-f" in response.json()["detail"]

    # Nothing was written, so neither spelling is taken.
    names = [b["name"] for b in client.get("/api/boxes").json()["boxes"]]
    assert "Eng-f" not in names and "eng-f" not in names


def test_surrounding_whitespace_is_still_accepted_and_trimmed(client, monkeypatch):
    """Invisible and almost never meant — the one thing corrected rather than
    refused. This endpoint already stripped it; the validator must not start
    rejecting what it used to accept."""
    import flotta.provision as provision

    made = {}

    def fake_create(name, *, store, **kwargs):
        made["name"] = name
        box = store.create_box(name)
        return {"box_id": box.id, "endpoint": "fly://app/m-new"}

    monkeypatch.setattr(provision, "create_box", fake_create)

    assert client.post("/api/boxes", json={"name": "  eng-b  "}).status_code == 201
    assert made["name"] == "eng-b"


# -- fleet settings ---------------------------------------------------------
#
# The app is the product surface, so how the fleet behaves has to be settable
# from a window rather than from a deployment variable and a restart.


def test_settings_carry_the_catalogue_and_where_each_value_came_from(client):
    rows = client.get("/api/settings").json()["settings"]
    by_key = {row["key"]: row for row in rows}

    assert "FLOTTA_IDLE_AFTER_S" in by_key
    # Label and help travel with the value so the app renders a form without
    # hardcoding one — a setting added server-side appears without a rebuild.
    assert by_key["FLOTTA_IDLE_AFTER_S"]["label"]
    assert by_key["FLOTTA_IDLE_AFTER_S"]["help"]
    assert by_key["FLOTTA_IDLE_AFTER_S"]["source"] in {"store", "env", "default"}


def test_a_setting_survives_being_written_and_read_back(client):
    written = client.put("/api/settings", json={"values": {"FLOTTA_IDLE_AFTER_S": "300"}})
    assert written.status_code == 200

    rows = {row["key"]: row for row in client.get("/api/settings").json()["settings"]}
    assert rows["FLOTTA_IDLE_AFTER_S"]["value"] == "300"
    assert rows["FLOTTA_IDLE_AFTER_S"]["source"] == "store"


def test_an_emptied_field_clears_the_override(client):
    client.put("/api/settings", json={"values": {"FLOTTA_IDLE_AFTER_S": "300"}})
    client.put("/api/settings", json={"values": {"FLOTTA_IDLE_AFTER_S": ""}})

    rows = {row["key"]: row for row in client.get("/api/settings").json()["settings"]}
    assert rows["FLOTTA_IDLE_AFTER_S"]["source"] != "store"


def test_a_credential_cannot_be_set_through_the_settings_api(client):
    """The security boundary, stated as a test.

    `settings.layered` shadows environment lookups, so a settings endpoint that
    accepted arbitrary keys would be a way to override the signing key — the
    thing every token in the fleet is verified against — over authenticated
    HTTP. The catalogue is an allowlist for exactly this reason.
    """
    for key in ("FLOTTA_SIGNING_KEY", "FLOTTA_GITHUB_TOKEN", "FLOTTA_BOX_PASSWORD"):
        response = client.put("/api/settings", json={"values": {key: "stolen"}})
        assert response.status_code == 422, key

    # And nothing about them is readable either.
    body = client.get("/api/settings").text
    assert "SIGNING_KEY" not in body
    assert "GITHUB_TOKEN" not in body


def test_a_value_that_cannot_be_parsed_is_refused_whole(client):
    """Validated before anything is written, and the request refused entirely.

    A partial apply would leave the fleet in a state nobody asked for and the
    form showing something else.
    """
    response = client.put(
        "/api/settings",
        json={"values": {"FLOTTA_IDLE_AFTER_S": "300", "FLOTTA_MAX_CONCURRENT": "lots"}},
    )
    assert response.status_code == 422

    rows = {row["key"]: row for row in client.get("/api/settings").json()["settings"]}
    assert rows["FLOTTA_IDLE_AFTER_S"]["source"] != "store", "the good half was applied anyway"


def test_settings_need_write_to_change_and_read_to_see(secured):
    """Reading how the fleet is configured is not permission to reconfigure it.

    The same split as the repo grants: `fleet:read` sees, `fleet:write` changes.
    A dashboard token that could retune the sweep interval would be a surprise.
    """
    assert secured.get("/api/settings", headers=_bearer("fleet:read")).status_code == 200

    refused = secured.put(
        "/api/settings",
        json={"values": {"FLOTTA_IDLE_AFTER_S": "300"}},
        headers=_bearer("fleet:read"),
    )
    assert refused.status_code == 403

    allowed = secured.put(
        "/api/settings",
        json={"values": {"FLOTTA_IDLE_AFTER_S": "300"}},
        headers=_bearer("fleet:write"),
    )
    assert allowed.status_code == 200


# -- FLOTTA-42: an agent that is not like the others ------------------------


def test_create_carries_per_agent_resources_through(client, monkeypatch):
    import flotta.provision as provision

    seen = {}

    def fake_create(name, *, store, **kwargs):
        seen.update(kwargs)
        box = store.create_box(name)
        return {"box_id": box.id, "endpoint": "fly://app/m-new"}

    monkeypatch.setattr(provision, "create_box", fake_create)

    assert (
        client.post(
            "/api/boxes", json={"name": "eng-big", "volume_gb": 10, "region": "lhr"}
        ).status_code
        == 201
    )
    assert seen["volume_gb"] == 10
    assert seen["region"] == "lhr"


def test_create_without_resources_lets_the_fleet_decide(client, monkeypatch):
    """The common case stays a one-field request, and `None` means "fleet
    default" all the way down rather than a number invented at the edge."""
    import flotta.provision as provision

    seen = {}

    def fake_create(name, *, store, **kwargs):
        seen.update(kwargs)
        box = store.create_box(name)
        return {"box_id": box.id, "endpoint": "fly://app/m-new"}

    monkeypatch.setattr(provision, "create_box", fake_create)

    assert client.post("/api/boxes", json={"name": "eng-plain"}).status_code == 201
    assert seen["volume_gb"] is None
    assert seen["region"] is None


def test_a_nonsense_volume_is_422_before_anything_is_provisioned(client, monkeypatch):
    import flotta.provision as provision

    def never(*args, **kwargs):
        raise AssertionError("nothing should be provisioned for an impossible size")

    monkeypatch.setattr(provision, "create_box", never)

    assert client.post("/api/boxes", json={"name": "a", "volume_gb": "big"}).status_code == 422
    assert client.post("/api/boxes", json={"name": "a", "volume_gb": 0}).status_code == 422
    assert client.post("/api/boxes", json={"name": "a", "volume_gb": -5}).status_code == 422


# -- FLOTTA-38: upgrading an agent through the API --------------------------


def test_upgrade_moves_the_box_and_reports_both_images(client, monkeypatch):
    import flotta.provision as provision

    seen = {}

    def fake_upgrade(box_id, *, store, image=None, **kwargs):
        seen["box_id"] = box_id
        seen["image"] = image
        return {"box_id": box_id, "image": image, "previous_image": "old", "already_current": False}

    monkeypatch.setattr(provision, "upgrade_box", fake_upgrade)

    response = client.post("/api/boxes/eng-a/upgrade", json={"image": "registry/flotta:new"})
    assert response.status_code == 200
    assert seen["image"] == "registry/flotta:new"
    assert response.json()["previous_image"] == "old"


def test_upgrade_without_a_body_uses_the_fleet_image(client, monkeypatch):
    import flotta.provision as provision

    seen = {}

    def fake_upgrade(box_id, *, store, image=None, **kwargs):
        seen["image"] = image
        return {"box_id": box_id, "image": "fleet", "previous_image": None}

    monkeypatch.setattr(provision, "upgrade_box", fake_upgrade)
    assert client.post("/api/boxes/eng-a/upgrade").status_code == 200
    assert seen["image"] is None, "an absent body means the fleet decides, not an empty string"


def test_a_refused_upgrade_is_409_not_502(client, monkeypatch):
    """Terminal, mid-provision, or no image named — all actionable by the
    caller, which is what separates them from a substrate failure."""
    import flotta.provision as provision

    def refuse(box_id, *, store, image=None, **kwargs):
        raise provision.ProvisionError("box b-1 is 'torn_down'; there is no machine left")

    monkeypatch.setattr(provision, "upgrade_box", refuse)
    response = client.post("/api/boxes/eng-a/upgrade", json={})
    assert response.status_code == 409
    assert "no machine left" in response.json()["detail"]


def test_upgrading_an_unknown_agent_is_404(client):
    assert client.post("/api/boxes/nobody/upgrade", json={}).status_code == 404


def test_upgrade_needs_write_not_destroy(secured):
    """Deliberately not `box:destroy`. Upgrading is the opposite of destroying —
    the volume is what it exists to preserve — and gating it there would mean
    handing out the ability to delete an agent in order to update one.
    """
    assert (
        secured.post("/api/boxes/eng-a/upgrade", json={}, headers=_bearer("fleet:read")).status_code
        == 403
    )
    # `fleet:write` gets past auth; 404/409 afterwards is the endpoint working.
    assert secured.post(
        "/api/boxes/eng-a/upgrade", json={}, headers=_bearer("fleet:write")
    ).status_code in (200, 404, 409)


def test_a_substrate_failure_upgrading_is_502_not_409(client, monkeypatch):
    """Matching `POST /api/boxes`, which answers the same class of failure the
    same way. 409 would tell the caller they asked for the wrong thing."""
    import flotta.provision as provision

    def explode(box_id, *, store, image=None, **kwargs):
        raise provision.UpgradeFailed("could not upgrade box b-1: flyctl exploded")

    monkeypatch.setattr(provision, "upgrade_box", explode)
    response = client.post("/api/boxes/eng-a/upgrade", json={})
    assert response.status_code == 502
    assert "flyctl exploded" in response.json()["detail"]


# -- the machine panel ------------------------------------------------------


def _fake_backend(monkeypatch, inspect):
    """Swap the substrate lookup the endpoint resolves through."""
    from flotta import backend as backend_mod

    class Fake:
        scheme = "fly"

        def inspect(self, box_id):
            return inspect(box_id)

    monkeypatch.setattr(backend_mod, "backend_for", lambda endpoint: Fake())


def test_the_machine_endpoint_returns_the_row_and_the_substrate_unmerged(client, monkeypatch):
    """Both, side by side, because the disagreements are the point.

    A merged answer would have to pick a winner, and the app would then be
    reporting a choice it never told anyone it made.
    """
    from flotta.backend import MachineInfo

    _fake_backend(
        monkeypatch,
        lambda box_id: MachineInfo(state="started", region="ams", image="reg/x:tag"),
    )
    body = client.get("/api/boxes/eng-a/machine").json()

    assert body["box"]["status"] == "running"
    assert body["machine"]["state"] == "started"
    assert body["machine"]["region"] == "ams"
    assert body["unavailable"] is None


def test_the_machine_endpoint_reports_a_substrate_failure_rather_than_500ing(client, monkeypatch):
    """A person clicked Info. `flyctl` being logged out is something to render,
    not a failed request — and the row must still arrive so the panel has
    something to show."""
    from flotta.backend import BackendError

    def boom(box_id):
        raise BackendError("flyctl: not authenticated")

    _fake_backend(monkeypatch, boom)
    response = client.get("/api/boxes/eng-a/machine")

    assert response.status_code == 200
    body = response.json()
    assert body["machine"] is None
    assert "not authenticated" in body["unavailable"]
    assert body["box"]["name"] == "eng-a"


def test_a_box_with_no_machine_yet_is_not_an_error(client, fleet, monkeypatch):
    """`provisioning` has a row and no endpoint. Saying "no machine yet" is the
    truth; an error about the substrate would be a lie about a creation that is
    going perfectly well — FLOTTA-29's exact shape."""

    def never(box_id):  # pragma: no cover - the endpoint must not reach here
        raise AssertionError("must not ask the substrate about a box with no endpoint")

    _fake_backend(monkeypatch, never)
    with FleetStore(fleet) as store:
        assert store.create_box("eng-new").endpoint is None

    body = client.get("/api/boxes/eng-new/machine").json()

    assert body["machine"] is None
    assert body["unavailable"] == "this agent has no machine yet"
    assert body["box"]["status"] == "provisioning"


def test_the_machine_endpoint_404s_for_a_box_that_does_not_exist(client):
    """The body is a contract, not decoration.

    The app has to tell this 404 from the other one — a control plane too old
    to have this route, which FastAPI answers with a bare `Not Found`. It
    splits on exactly this text, so a reworded detail here would retarget every
    missing agent as "deploy the current control plane".
    """
    response = client.get("/api/boxes/nope/machine")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail != "Not Found"
    assert "nope" in detail


# -- Hermes versions --------------------------------------------------------


def test_the_version_endpoint_separates_the_pin_from_the_latest(client, monkeypatch):
    """Three facts that were one line of justfile output. The app needs them
    apart, because only one of them is about any running agent — and it is not
    either of these."""
    import flotta.hermes as hermes

    monkeypatch.setattr(hermes, "latest_release", lambda **kw: ("v2026.9.7", None))
    body = client.get("/api/hermes").json()

    assert body["latest"] == "v2026.9.7"
    assert body["pinned"]  # whatever this checkout pins
    assert body["behind"] is (body["pinned"] != "v2026.9.7")
    assert body["unavailable"] is None


def test_an_unreachable_github_reads_as_unknown_not_as_up_to_date(client, monkeypatch):
    """The failure that matters. "Up to date" on a failed check hides a real
    upgrade behind a reassuring word."""
    import flotta.hermes as hermes

    monkeypatch.setattr(hermes, "latest_release", lambda **kw: (None, "could not reach GitHub"))
    body = client.get("/api/hermes").json()

    assert body["latest"] is None
    assert body["behind"] is False  # not true either — see `unavailable`
    assert "could not reach GitHub" in body["unavailable"]


def test_the_version_endpoint_needs_a_token(client):
    """It is behind `fleet:read` like everything else. Nothing here is secret,
    but an unauthenticated route on this app is a precedent, not a convenience."""
    from flotta.control.app import create_app

    app = create_app(
        store_factory=lambda: FleetStore(":memory:"),
        run_loop=False,
        background=False,
        signing_key="k" * 32,
    )
    with TestClient(app) as guarded:
        assert guarded.get("/api/hermes").status_code == 401


# -- upgrading from a button ------------------------------------------------


@pytest.fixture
def async_client(fleet):
    """The endpoint as the app meets it: backgrounded, like production."""
    app = create_app(store_factory=lambda: FleetStore(fleet), run_loop=False, background=True)
    with TestClient(app) as c:
        yield c


def test_an_upgrade_answers_before_the_substrate_does(async_client, monkeypatch):
    """`flyctl machine update` waits up to 300s and the proxy in front of this
    cuts at 60. Held open, this answers `502 Application failed to respond`
    while the upgrade carries on — the exact failure `POST /api/boxes` had."""
    import flotta.provision as provision

    started = threading.Event()

    def slow(box_id, **kwargs):
        started.set()
        time.sleep(0.2)
        return {"box_id": box_id}

    monkeypatch.setattr(provision, "upgrade_box", slow)
    monkeypatch.setattr(provision, "_fleet_image", lambda env=None: "registry/x:new")

    response = async_client.post("/api/boxes/eng-a/upgrade")

    assert response.status_code == 202
    body = response.json()
    assert body["image"] == "registry/x:new"
    assert body["started"] is True
    assert started.wait(timeout=5), "the upgrade should be running on a thread"


def test_a_box_that_cannot_be_upgraded_is_refused_in_the_reply(async_client, fleet):
    """Not everything moves to the thread. A `202` for a box that can never be
    upgraded is a lie the app renders as a spinner that never ends."""
    with FleetStore(fleet) as store:
        box = store.create_box("eng-doomed")
        store.update_box_status(box.id, "torn_down")

    response = async_client.post("/api/boxes/eng-doomed/upgrade")

    assert response.status_code == 409
    assert "torn_down" in response.json()["detail"]


def test_an_upgrade_with_no_image_anywhere_is_refused_rather_than_started(
    async_client, monkeypatch
):
    """The live trap this endpoint would otherwise hide: the fleet image is a
    deployment variable, and an unset one must not become a background thread
    that fails where nobody is looking."""
    import flotta.provision as provision

    monkeypatch.setattr(provision, "_fleet_image", lambda env=None: None)
    response = async_client.post("/api/boxes/eng-a/upgrade")

    assert response.status_code == 409
    assert "no image to upgrade to" in response.json()["detail"]


def test_an_explicit_image_wins_over_the_fleet_default(async_client, monkeypatch):
    import flotta.provision as provision

    seen = {}

    def record(box_id, **kwargs):
        seen.update(kwargs)
        return {"box_id": box_id}

    monkeypatch.setattr(provision, "upgrade_box", record)
    monkeypatch.setattr(provision, "_fleet_image", lambda env=None: "registry/x:fleet")

    body = async_client.post("/api/boxes/eng-a/upgrade", json={"image": "registry/x:pinned"}).json()

    assert body["image"] == "registry/x:pinned"


def test_the_machine_endpoint_says_whether_the_agent_is_on_the_fleet_image(client, monkeypatch):
    """Computed here, with `_same_image`, and not in the app.

    Fly reports `repo:tag@sha256:…` while the configured image is written
    without the digest, so string equality never matches. That comparison has
    been wrong three times in this repo; a TypeScript copy would be the fourth.
    """
    import flotta.provision as provision
    from flotta.backend import MachineInfo

    monkeypatch.setattr(provision, "_fleet_image", lambda env=None: "registry/x:new")
    _fake_backend(
        monkeypatch,
        lambda box_id: MachineInfo(state="started", image="registry/x:new@sha256:abc"),
    )
    body = client.get("/api/boxes/eng-a/machine").json()

    assert body["fleet_image"] == "registry/x:new"
    assert body["image_current"] is True, "a digest must not make the same image look different"


def test_an_agent_on_an_older_image_is_reported_as_behind(client, monkeypatch):
    import flotta.provision as provision
    from flotta.backend import MachineInfo

    monkeypatch.setattr(provision, "_fleet_image", lambda env=None: "registry/x:new")
    _fake_backend(monkeypatch, lambda box_id: MachineInfo(state="started", image="registry/x:old"))

    assert client.get("/api/boxes/eng-a/machine").json()["image_current"] is False


def test_an_unknown_side_is_not_reported_as_behind(client, monkeypatch):
    """`None`, not `False`. "Behind" puts an Upgrade button in front of
    somebody, and offering one on no evidence is how an agent gets restarted
    for nothing."""
    import flotta.provision as provision
    from flotta.backend import MachineInfo

    monkeypatch.setattr(provision, "_fleet_image", lambda env=None: None)
    _fake_backend(monkeypatch, lambda box_id: MachineInfo(state="started", image="registry/x:old"))

    assert client.get("/api/boxes/eng-a/machine").json()["image_current"] is None


def test_the_version_endpoint_says_where_the_fleet_image_came_from(client, monkeypatch):
    """"Why is my fleet not using the image I just built" is otherwise
    unanswerable from a window."""
    import flotta.provision as provision

    monkeypatch.setattr(
        provision, "resolve_fleet_image", lambda env=None, **kw: ("registry/x:pinned", "env")
    )
    monkeypatch.setattr(provision, "_newest_release_image", lambda app, **kw: "registry/x:newest")
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")

    body = client.get("/api/hermes").json()

    assert body["fleet_image"] == "registry/x:pinned"
    assert body["fleet_image_source"] == "env"
    # The divergence is the trap, and this is the only place it is visible.
    assert body["newest_release"] == "registry/x:newest"


def test_the_newest_release_is_reported_as_the_source_when_it_decides(client, monkeypatch):
    import flotta.provision as provision

    monkeypatch.setattr(
        provision, "resolve_fleet_image", lambda env=None, **kw: ("registry/x:newest", "release")
    )
    monkeypatch.setattr(provision, "_newest_release_image", lambda app, **kw: "registry/x:newest")
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")

    body = client.get("/api/hermes").json()

    assert body["fleet_image_source"] == "release"
    assert body["fleet_image"] == body["newest_release"]


def test_no_fly_app_means_no_release_lookup(client, monkeypatch):
    """No app configured is not an app named 'flotta-box'. Guessing would read
    a stranger's registry."""
    import flotta.provision as provision

    monkeypatch.delenv("FLOTTA_FLY_APP", raising=False)
    monkeypatch.setattr(provision, "resolve_fleet_image", lambda env=None, **kw: (None, "none"))

    def must_not_run(app, **kw):  # pragma: no cover - the guard
        raise AssertionError("looked up releases with no app configured")

    monkeypatch.setattr(provision, "_newest_release_image", must_not_run)

    body = client.get("/api/hermes").json()
    assert body["fleet_image"] is None
    assert body["fleet_image_source"] == "none"
    assert body["newest_release"] is None


def test_no_image_says_which_kind_of_nothing(client, monkeypatch):
    """`source: "none"` has two causes and two different fixes.

    No app configured means "set FLOTTA_FLY_APP". An app configured whose
    releases cannot be read means "that app is wrong, or flyctl cannot reach
    it". From outside they were identical, and telling them apart meant opening
    the deployment's variables — which is exactly the trip this endpoint exists
    to save.
    """
    import flotta.provision as provision

    monkeypatch.setattr(provision, "resolve_fleet_image", lambda env=None, **kw: (None, "none"))
    monkeypatch.setattr(provision, "_newest_release_image", lambda app, **kw: None)

    monkeypatch.delenv("FLOTTA_FLY_APP", raising=False)
    assert client.get("/api/hermes").json()["fleet_image_app"] is None

    monkeypatch.setenv("FLOTTA_FLY_APP", "a-deleted-app")
    body = client.get("/api/hermes").json()
    assert body["fleet_image_app"] == "a-deleted-app"
    assert body["newest_release"] is None


# -- the button -------------------------------------------------------------


def _no_build(monkeypatch, image="registry/x:built", fails=None):
    """Replace the builder. Nothing in this file may run flyctl."""
    import flotta.images as images

    def fake(ref, *, app, region="ams", **kwargs):
        if fails:
            raise images.BuildError(fails)
        return image

    monkeypatch.setattr(images, "build_box_image", fake)


def _status(store):
    """The latest build's status, or None — the thing every wait here is about."""
    build = store.latest_build()
    return build.status if build else None


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_the_update_answers_immediately_and_builds_on_a_thread(async_client, fleet, monkeypatch):
    """A cold build is minutes; the proxy cuts at 60 seconds."""
    _no_build(monkeypatch)
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")
    import flotta.provision as provision

    monkeypatch.setattr(provision, "upgrade_box", lambda box_id, **kw: {"box_id": box_id})

    response = async_client.post("/api/hermes/update", json={"hermes_ref": "v2026.9.8"})

    assert response.status_code == 202
    assert response.json()["hermes_ref"] == "v2026.9.8"

    with FleetStore(fleet) as store:
        assert _wait(lambda: _status(store) == "done")
        assert store.latest_build().image == "registry/x:built"


def test_every_live_agent_is_rolled_onto_the_new_image(async_client, fleet, monkeypatch):
    _no_build(monkeypatch)
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")
    import flotta.provision as provision

    rolled: list[str] = []
    monkeypatch.setattr(
        provision,
        "upgrade_box",
        lambda box_id, **kw: rolled.append(kw.get("image")) or {"box_id": box_id},
    )

    async_client.post("/api/hermes/update", json={"hermes_ref": "v1"})

    assert _wait(lambda: rolled), "no agent was rolled"
    assert rolled == ["registry/x:built"]


def test_rolling_stops_at_the_first_agent_that_fails(async_client, fleet, monkeypatch):
    """The first agent is the canary.

    A Hermes that will not serve should cost one agent a restart, not the
    fleet — and `upgrade_box` leaves a failed agent exactly as it was, which is
    what makes stopping a recovery rather than a half-migrated fleet.
    """
    _no_build(monkeypatch)
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")
    import flotta.provision as provision
    from flotta.provision import UpgradeFailed

    attempts: list[str] = []

    def refuse(box_id, **kw):
        attempts.append(box_id)
        raise UpgradeFailed("the substrate said no")

    monkeypatch.setattr(provision, "upgrade_box", refuse)
    with FleetStore(fleet) as store:
        second = store.create_box("eng-second")
        store.update_box_status(second.id, "running", endpoint="fly://app/m2")

    async_client.post("/api/hermes/update", json={"hermes_ref": "v1"})

    assert _wait(lambda: attempts)
    time.sleep(0.3)
    assert len(attempts) == 1, f"rolling continued past a failure: {attempts}"


def test_a_failed_build_touches_no_agent(async_client, fleet, monkeypatch):
    _no_build(monkeypatch, fails="build failed: no space left")
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")
    import flotta.provision as provision

    rolled: list[str] = []
    monkeypatch.setattr(provision, "upgrade_box", lambda box_id, **kw: rolled.append(box_id))

    async_client.post("/api/hermes/update", json={"hermes_ref": "v1"})

    with FleetStore(fleet) as store:
        assert _wait(lambda: _status(store) == "failed")
        assert "no space left" in store.latest_build().error
    assert rolled == [], "a build that failed must not move any agent"


def test_a_second_update_while_one_is_running_is_refused(async_client, fleet, monkeypatch):
    """Two builds race for the same app's release history, and the loser's
    agents get rolled onto an image the winner replaced."""
    _no_build(monkeypatch)
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")
    with FleetStore(fleet) as store:
        store.start_build("v1")

    response = async_client.post("/api/hermes/update", json={"hermes_ref": "v2"})

    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_no_build_app_is_recorded_rather_than_crashing_the_thread(
    async_client, fleet, monkeypatch
):
    """The thread is where nobody is looking. A build that cannot start must
    leave a row saying why, not a log line."""
    monkeypatch.delenv("FLOTTA_FLY_APP", raising=False)

    async_client.post("/api/hermes/update", json={"hermes_ref": "v1"})

    with FleetStore(fleet) as store:
        assert _wait(lambda: _status(store) == "failed")
        assert "FLOTTA_FLY_APP" in store.latest_build().error


def test_builds_are_listed_newest_first(client, fleet):
    with FleetStore(fleet) as store:
        first = store.start_build("v1")
        store.finish_build(first.id, image="registry/x:1")
        store.start_build("v2")

    builds = client.get("/api/hermes/builds").json()["builds"]
    assert [b["hermes_ref"] for b in builds] == ["v2", "v1"]
    assert builds[0]["status"] == "building"


def test_a_second_update_during_the_roll_is_refused_too(async_client, fleet, monkeypatch):
    """The lock has to cover the whole operation.

    `finish_build` used to run the moment the image existed, so "one update at
    a time" was true for the build minutes and false for the roll — a second
    POST could start while agents were still moving.
    """
    _no_build(monkeypatch)
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")
    import flotta.provision as provision

    rolling = threading.Event()
    release = threading.Event()

    def slow_roll(box_id, **kw):
        rolling.set()
        release.wait(timeout=5)
        return {"box_id": box_id}

    monkeypatch.setattr(provision, "upgrade_box", slow_roll)
    async_client.post("/api/hermes/update", json={"hermes_ref": "v1"})

    assert rolling.wait(timeout=5), "the roll never started"
    try:
        second = async_client.post("/api/hermes/update", json={"hermes_ref": "v2"})
        assert second.status_code == 409, "a second update started while agents were moving"
        assert "rolling" in second.json()["detail"]
    finally:
        release.set()


def test_a_roll_that_fails_records_which_agent(async_client, fleet, monkeypatch):
    """Otherwise the build row says `rolling` forever and the only trace is a
    per-agent event somebody has to go looking for."""
    _no_build(monkeypatch)
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")
    import flotta.provision as provision
    from flotta.provision import UpgradeFailed

    monkeypatch.setattr(
        provision, "upgrade_box", lambda box_id, **kw: (_ for _ in ()).throw(UpgradeFailed("nope"))
    )

    async_client.post("/api/hermes/update", json={"hermes_ref": "v1"})

    with FleetStore(fleet) as store:
        assert _wait(lambda: _status(store) == "failed")
        assert "eng-a" in store.latest_build().error


def test_an_abandoned_build_stops_blocking_after_an_hour(fleet):
    """A control plane killed mid-build leaves `building` behind, and without a
    timeout every later update is refused by a job that stopped existing when
    the process did. The stale row is not marked failed — it is unknown, not
    finished, and saying otherwise would invent an outcome nothing saw."""
    from datetime import UTC, datetime, timedelta

    from flotta.store import BuildInProgressError

    with FleetStore(fleet) as store:
        abandoned = store.start_build("v1")
        with pytest.raises(BuildInProgressError):
            store.start_build("v2")

        later = datetime.now(UTC) + timedelta(hours=2)
        assert store.start_build("v2", now=later).hermes_ref == "v2"
        assert store.get_build(abandoned.id).status == "building"


def test_the_version_check_measures_what_was_built_not_the_source_pin(client, fleet, monkeypatch):
    """End to end, the bug that would have bitten on first use.

    An update started from the app builds at a given ref and never edits
    source, so the pin stays put. Compared against the pin, a fleet that had
    just been updated still read as behind and the banner went on offering an
    update it had already applied.
    """
    import flotta.hermes as hermes
    from flotta.box.image import HERMES_REF

    newer = "v9999.1.1"
    monkeypatch.setattr(hermes, "latest_release", lambda **kw: (newer, None))

    # Before any build: nothing to go on but the pin, and upstream is ahead.
    first = client.get("/api/hermes").json()
    assert first["fleet_ref"] == HERMES_REF
    assert first["fleet_ref_source"] == "pin"
    assert first["behind"] is True

    # Now the fleet is built at that newer ref — as the button would do.
    with FleetStore(fleet) as store:
        build = store.start_build(newer)
        store.finish_build(build.id, image="registry/x:new")

    after = client.get("/api/hermes").json()
    assert after["fleet_ref"] == newer
    assert after["fleet_ref_source"] == "build"
    assert after["behind"] is False, "the banner would have offered the update it just applied"
    # The pin is unchanged, and still reported — it is a different fact.
    assert after["pinned"] == HERMES_REF


def test_a_failed_build_does_not_count_as_what_the_fleet_runs(client, fleet, monkeypatch):
    """Otherwise a build that never produced an image would mark the fleet as
    up to date, which is the reassuring half of the same lie."""
    import flotta.hermes as hermes

    newer = "v9999.1.1"
    monkeypatch.setattr(hermes, "latest_release", lambda **kw: (newer, None))
    with FleetStore(fleet) as store:
        build = store.start_build(newer)
        store.finish_build(build.id, error="no space left")

    body = client.get("/api/hermes").json()
    assert body["fleet_ref_source"] == "pin"
    assert body["behind"] is True


# -- FLOTTA-40: who an agent is ---------------------------------------------


def _fake_create_recording(monkeypatch):
    """`create_box` that records what it was handed and writes a plausible row."""
    import flotta.provision as provision

    seen: dict = {}

    def fake(name, *, store, box=None, **kwargs):
        seen.update(kwargs)
        row = box or store.create_box(name)
        # What the real `reserve_box` does with these; the fake has to too, or
        # it is testing the fake.
        identity = {k: kwargs.get(k) for k in ("display_name", "description", "instructions")}
        if any(identity.values()):
            store.set_box_meta(row.id, **identity)
        store.update_box_status(row.id, "running", endpoint="fly://app/m9")
        return {"box_id": row.id, "endpoint": "fly://app/m9"}

    monkeypatch.setattr(provision, "create_box", fake)
    return seen


def test_an_agent_is_created_with_a_name_a_description_and_instructions(client, monkeypatch):
    seen = _fake_create_recording(monkeypatch)

    body = client.post(
        "/api/boxes",
        json={
            "name": "eng-r",
            "display_name": "Reviewer — backend PRs",
            "description": "Reviews backend pull requests.",
            "instructions": "You are a careful backend reviewer.",
        },
    ).json()

    assert body["box"]["name"] == "eng-r"  # the address
    assert body["box"]["display_name"] == "Reviewer — backend PRs"
    assert body["box"]["description"] == "Reviews backend pull requests."
    assert body["box"]["instructions"] == "You are a careful backend reviewer."
    assert seen["instructions"] == "You are a careful backend reviewer."


def test_the_202_row_already_carries_the_display_name(async_client, fleet, monkeypatch):
    """That row goes straight into the app's sidebar without waiting for a
    poll. Without the name on it, the agent appears under its address for five
    seconds and then changes — the window contradicting itself."""
    from types import SimpleNamespace

    import flotta.control.app as app_module
    import flotta.provision as provision

    # No substrate: `_peek_for` is the app's probe, `_backend_for` is what the
    # row's `provisioning` event records a scheme from.
    monkeypatch.setattr(app_module, "_peek_for", lambda impl, name: None)
    monkeypatch.setattr(provision, "_backend_for", lambda scheme: SimpleNamespace(scheme="fly"))
    monkeypatch.setattr(provision, "create_box", lambda name, **kw: {"box_id": "x"})

    response = async_client.post(
        "/api/boxes", json={"name": "eng-r", "display_name": "Reviewer", "instructions": "Be kind."}
    )

    assert response.status_code == 202
    assert response.json()["box"]["display_name"] == "Reviewer"
    with FleetStore(fleet) as store:
        meta = store.meta_for_box(response.json()["box_id"])
        assert meta.instructions == "Be kind.", "the thread reads instructions from the store"


def test_the_fleet_list_carries_identity_in_one_query(client, fleet):
    with FleetStore(fleet) as store:
        box = store.get_box_by_name("eng-a")
        store.set_box_meta(box.id, display_name="Alpha", description="First agent")

    rows = {b["name"]: b for b in client.get("/api/boxes").json()["boxes"]}
    assert rows["eng-a"]["display_name"] == "Alpha"
    assert rows["eng-a"]["description"] == "First agent"


def test_an_agent_with_no_identity_reports_none_not_undefined(client):
    """`None` is a state the app renders as "no description". A missing key
    renders as the word `undefined`."""
    row = client.get("/api/boxes").json()["boxes"][0]
    assert row["display_name"] is None
    assert "description" in row and "instructions" in row


def test_a_description_that_cannot_be_stored_is_refused_before_the_name_is_spent(
    client, fleet, monkeypatch
):
    seen = _fake_create_recording(monkeypatch)
    response = client.post("/api/boxes", json={"name": "eng-r", "description": "x" * 501})

    assert response.status_code == 422
    assert "too long" in response.json()["detail"]
    assert seen == {}, "create_box ran despite an invalid description"
    with FleetStore(fleet) as store:
        assert store.get_box_by_name("eng-r") is None


def test_an_agent_can_be_renamed_and_redescribed_but_not_reinstructed(client, fleet):
    """The store row is the *record* of what the agent was seeded with; the
    copy it reads is SOUL.md on its own volume. Editing the record here would
    make it lie about what the agent runs."""
    with FleetStore(fleet) as store:
        box = store.get_box_by_name("eng-a")
        store.set_box_meta(box.id, display_name="Old", instructions="Original seed.")

    body = client.put(
        "/api/boxes/eng-a/meta",
        json={"display_name": "New", "description": "Now described", "instructions": "Hijack"},
    ).json()

    assert body["box"]["display_name"] == "New"
    assert body["box"]["description"] == "Now described"
    assert body["box"]["instructions"] == "Original seed.", "instructions changed through PUT"


def test_renaming_never_touches_the_address(client):
    body = client.put("/api/boxes/eng-a/meta", json={"display_name": "Anything At All!"}).json()
    assert body["box"]["name"] == "eng-a"


def test_renaming_a_missing_agent_is_404(client):
    assert client.put("/api/boxes/nope/meta", json={"display_name": "x"}).status_code == 404
