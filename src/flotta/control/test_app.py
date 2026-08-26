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


def test_the_insecure_override_exists_for_a_network_you_own():
    """Refusing outright would push people to a worse workaround — M3 already
    demonstrated that, when the loopback plan led straight to a socat
    forwarder that would have bypassed Hermes's gate entirely."""
    check_bind("0.0.0.0", env={"FLOTTA_CONTROL_ALLOW_INSECURE_BIND": "1"})


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
