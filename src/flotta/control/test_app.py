"""Tests for the control-plane API, against a real store and the real app.

`fastapi` and `httpx` come with the `control` extra, so these skip on a plain
`uv sync` rather than forcing a web framework on the 460 hermetic tests.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="needs `uv sync --extra control`")
pytest.importorskip("httpx", reason="fastapi's TestClient needs httpx")

from fastapi.testclient import TestClient  # noqa: E402

from flotta.control.app import InsecureBindError, check_bind, create_app  # noqa: E402
from flotta.store import FleetStore  # noqa: E402


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
