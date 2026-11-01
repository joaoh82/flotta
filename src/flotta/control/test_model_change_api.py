"""The model route answers at once and the box says a change is under way (FLOTTA-99)."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.store import FleetStore

DEFAULT = "z-ai/glm-5.2"
OWN = "anthropic/claude-sonnet-4.5"


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOTTA_MODEL", DEFAULT)
    monkeypatch.setenv("FLOTTA_MODELS", OWN)
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.update_box_status(store.create_box("eng-a").id, "running", endpoint="fly://app/m1")
    return path


def _api(fleet):
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=True,
        reachable=_unchecked,
        known_model=_uncatalogued,
    )
    return TestClient(app)


@pytest.fixture
def held(monkeypatch):
    """`set_model` held until the test lets it go: the restart, in slow motion."""
    import flotta.provision as provision

    release = threading.Event()
    real = provision.set_model
    outcome: dict[str, BaseException | None] = {"raise": None}

    class Applied:
        scheme = "fly"

        def apply_secrets(self, endpoint, secrets):
            release.wait(timeout=5)
            if outcome["raise"]:
                raise outcome["raise"]

    def set_model(box_id, model, *, store, **kwargs):
        return real(box_id, model, store=store, backend=Applied())

    monkeypatch.setattr(provision, "set_model", set_model)
    return release, outcome


def _finish():
    import flotta.control.app as control

    for worker in list(control._provisioning):
        worker.join(timeout=5)


def test_a_change_answers_before_the_restart_and_says_it_is_under_way(fleet, held):
    release, _ = held
    with _api(fleet) as api:
        response = api.put("/api/boxes/eng-a/model", json={"model": OWN})
        assert response.status_code == 202, response.text
        body = response.json()
        assert (body["started"], body["model"], body["source"]) == (True, OWN, "agent")
        assert body["box"]["model_change"]["state"] == "pending"

        during = api.get("/api/boxes/eng-a").json()["box"]
        assert during["model_change"] == {
            **during["model_change"],
            "state": "pending",
            "model": OWN,
        }
        # Still what it runs until the machine has it.
        assert during["model"] == DEFAULT

        release.set()
        _finish()
        after = api.get("/api/boxes/eng-a").json()["box"]
    assert after["model_change"] is None
    assert (after["model"], after["model_source"]) == (OWN, "agent")


def test_a_second_change_meanwhile_is_refused(fleet, held):
    release, _ = held
    with _api(fleet) as api:
        assert api.put("/api/boxes/eng-a/model", json={"model": OWN}).status_code == 202
        second = api.put("/api/boxes/eng-a/model", json={"model": None})
        release.set()
        _finish()
    assert second.status_code == 409
    assert "already switching" in second.json()["detail"]


def test_a_change_that_fails_on_its_thread_is_reported_on_the_box(fleet, held):
    from flotta.backend import BackendError

    release, outcome = held
    outcome["raise"] = BackendError("flyctl secrets import timed out")
    with _api(fleet) as api:
        assert api.put("/api/boxes/eng-a/model", json={"model": OWN}).status_code == 202
        release.set()
        _finish()
        box = api.get("/api/boxes/eng-a").json()["box"]
    assert box["model_change"]["state"] == "failed"
    assert "timed out" in box["model_change"]["detail"]
    assert box["model"] == DEFAULT


def test_an_unexpected_failure_on_the_thread_still_ends_the_change(fleet, monkeypatch):
    """Not only a refused write: anything that stops the thread must end the
    change, or the window says "restarting" until it goes stale."""
    import flotta.provision as provision

    def set_model(box_id, model, *, store, **kwargs):
        raise RuntimeError("the store went away")

    monkeypatch.setattr(provision, "set_model", set_model)
    with _api(fleet) as api:
        assert api.put("/api/boxes/eng-a/model", json={"model": OWN}).status_code == 202
        _finish()
        box = api.get("/api/boxes/eng-a").json()["box"]
        # And the next change is not blocked by it.
        monkeypatch.setattr(provision, "set_model", lambda *a, **k: {})
        assert api.put("/api/boxes/eng-a/model", json={"model": OWN}).status_code == 202
        _finish()
    assert box["model_change"]["state"] == "failed"
    assert "store went away" in box["model_change"]["detail"]


def test_an_agent_with_no_record_reports_the_environments_model_not_a_later_default(fleet):
    """The live check: the operator stored a new default, and every agent made
    before FLOTTA-39 claimed to run it."""
    with FleetStore(fleet) as store:
        store.set_settings({"FLOTTA_MODEL": OWN})
    with _api(fleet) as api:
        listed = api.get("/api/boxes").json()["boxes"][0]
        one = api.get("/api/boxes/eng-a").json()["box"]
    for row in (listed, one):
        assert (row["model"], row["model_source"], row["fleet_model"]) == (DEFAULT, "fleet", OWN)
