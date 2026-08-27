"""Tests for the control-plane API, against a real store and the real app.

`fastapi` and `httpx` are in the `dev` dependency group, so these always run.
They used to be extra-only behind a `pytest.importorskip`, which meant CI —
a plain `uv sync --frozen` — skipped this entire file and reported green.
Import them plainly: a missing dependency should break the run, not quietly
delete sixteen tests from it.
"""

from __future__ import annotations

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
    app = create_app(store_factory=lambda: FleetStore(fleet), run_loop=False)
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

    app = create_app(store_factory=lambda: FleetStore(fleet), run_loop=False)
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
    app = create_app(store_factory=lambda: FleetStore(fleet), run_loop=False)
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
    app = create_app(store_factory=lambda: FleetStore(fleet), run_loop=False, signing_key=AUTH_KEY)
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
