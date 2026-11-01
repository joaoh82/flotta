"""The control plane shows and repairs an agent's connection (FLOTTA-98)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, mint
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.store import FleetStore

KEY = "a-signing-key-for-connections"
HERE = "https://flotta-control.onrender.com"
RAILWAY = "https://flotta-production.up.railway.app"


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.update_box_status(store.create_box("eng-d").id, "running", endpoint="fly://app/m1")
    return path


def _api(fleet, *, background):
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=background,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
    )
    return TestClient(app)


def _headers(*scopes):
    token = mint(scopes=set(scopes), key=KEY, ttl_s=300, subject="p")
    return {"Authorization": f"Bearer {token}"}


WRITER = _headers(SCOPE_FLEET_READ, SCOPE_FLEET_WRITE)
READER = _headers(SCOPE_FLEET_READ)


@pytest.fixture
def repairs(monkeypatch):
    import flotta.provision as provision

    calls: list[str] = []

    def repair_connection(box_id, *, store, **kwargs):
        calls.append(box_id)
        return {"box_id": box_id, "rewrote": ["FLOTTA_CONTROL_URL"]}

    monkeypatch.setattr(provision, "repair_connection", repair_connection)
    return calls


def test_the_machine_reading_says_when_an_agent_calls_another_control_plane(fleet, monkeypatch):
    from flotta import backend as backend_mod
    from flotta.backend import MachineInfo

    class Railway:
        scheme = "fly"

        def inspect(self, endpoint):
            return MachineInfo(state="started", control_url=RAILWAY, env_box_id="b-old")

    monkeypatch.setenv("FLOTTA_CONTROL_URL", HERE)
    monkeypatch.setattr(backend_mod, "backend_for", lambda endpoint: Railway())
    with _api(fleet, background=False) as api:
        body = api.get("/api/boxes/eng-d/machine", headers=WRITER).json()

    connection = body["connection"]
    assert connection["ok"] is False
    assert connection["control_url"] == RAILWAY
    assert connection["wanted_control_url"] == HERE
    assert connection["problems"]


def test_a_repair_answers_at_once_and_runs_on_a_thread(fleet, repairs, monkeypatch):
    """A running agent restarts twice, and the web app gives a call ten
    seconds: answering after the work is how a success arrives as a failure."""
    monkeypatch.setenv("FLOTTA_CONTROL_URL", HERE)
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", KEY)
    with _api(fleet, background=True) as api:
        response = api.post("/api/boxes/eng-d/connection", headers=WRITER)
        assert response.status_code == 202, response.text
        assert response.json()["started"] is True
        import flotta.control.app as control

        for worker in list(control._provisioning):
            worker.join(timeout=5)
    assert len(repairs) == 1


def test_a_repair_is_refused_when_this_control_plane_has_no_address(fleet, repairs, monkeypatch):
    monkeypatch.delenv("FLOTTA_CONTROL_URL", raising=False)
    with _api(fleet, background=True) as api:
        response = api.post("/api/boxes/eng-d/connection", headers=WRITER)
    assert response.status_code == 409
    assert "FLOTTA_CONTROL_URL" in response.json()["detail"]
    assert repairs == []


def test_a_repair_is_refused_when_no_token_could_be_issued(fleet, repairs, monkeypatch):
    """Refused before the thread starts: the repair would rewrite and restart
    the machine, then fail at the token it cannot mint."""
    monkeypatch.setenv("FLOTTA_CONTROL_URL", HERE)
    monkeypatch.delenv("FLOTTA_SIGNING_KEY", raising=False)
    with _api(fleet, background=True) as api:
        response = api.post("/api/boxes/eng-d/connection", headers=WRITER)
    assert response.status_code == 409
    assert "FLOTTA_SIGNING_KEY" in response.json()["detail"]
    assert repairs == []


def test_reading_the_fleet_is_not_enough_to_restart_an_agent(fleet, repairs, monkeypatch):
    monkeypatch.setenv("FLOTTA_CONTROL_URL", HERE)
    with _api(fleet, background=False) as api:
        response = api.post("/api/boxes/eng-d/connection", headers=READER)
    assert response.status_code == 403
    assert repairs == []
