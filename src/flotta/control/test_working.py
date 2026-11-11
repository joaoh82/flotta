"""What a run is working on, for the door to tell a conversation's turn (FLOTTA-197)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_ROUTE, SCOPE_FLEET_WRITE, mint
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.store import FleetStore

KEY = "a-signing-key-for-working-tests"


def _api(path):
    app = create_app(
        store_factory=lambda: FleetStore(path),
        run_loop=False,
        background=False,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
    )
    return TestClient(app)


def _as(*scopes):
    return {
        "Authorization": f"Bearer {mint(subject='door', scopes=set(scopes), key=KEY, ttl_s=300)}"
    }


def _box(path):
    with FleetStore(path) as store:
        box = store.create_box("eng-a")
        store.update_box_status(box.id, "running", endpoint="fly://app/eng-a")
        return box.id


def test_nothing_working_says_so(tmp_path):
    path = tmp_path / "fleet.db"
    box = _box(path)
    with _api(path) as api:
        got = api.get(f"/api/boxes/{box}/working", headers=_as(SCOPE_FLEET_ROUTE))
    assert got.status_code == 200, got.text
    assert got.json() == {"working": None}


def test_a_running_run_is_named_by_its_first_line(tmp_path):
    path = tmp_path / "fleet.db"
    box = _box(path)
    with FleetStore(path) as store:
        run = store.create_run(
            box, "\nMorning report: check the orders feed\nand more", trigger_kind="api"
        )
        store.start_run(run.id)
    with _api(path) as api:
        got = api.get(f"/api/boxes/{box}/working", headers=_as(SCOPE_FLEET_ROUTE)).json()
    assert got == {
        "working": {
            "run_id": run.id,
            "trigger_kind": "api",
            "about": "Morning report: check the orders feed",
        }
    }


def test_a_requests_run_is_named_by_the_request(tmp_path):
    path = tmp_path / "fleet.db"
    box = _box(path)
    with FleetStore(path) as store:
        filed = store.file_request(
            box,
            title="Adjust the columnists' banners",
            details="",
            verdict=None,
            judge_error=None,
            within=None,
            approved=True,
            why="small",
        )
        run = store.create_run(
            box, "The agency approved…", trigger_kind="request", trigger_id=filed.id
        )
        store.start_run(run.id)
    with _api(path) as api:
        got = api.get(f"/api/boxes/{box}/working", headers=_as(SCOPE_FLEET_ROUTE)).json()
    assert got["working"]["about"] == "Adjust the columnists' banners"


def test_a_conversations_own_turn_is_not_a_run_to_be_told_about(tmp_path):
    path = tmp_path / "fleet.db"
    box = _box(path)
    with FleetStore(path) as store:
        store.open_chat_run(box, "Hello.")
    with _api(path) as api:
        got = api.get(f"/api/boxes/{box}/working", headers=_as(SCOPE_FLEET_ROUTE)).json()
    assert got == {"working": None}


def test_only_the_door_asks(tmp_path):
    path = tmp_path / "fleet.db"
    box = _box(path)
    with _api(path) as api:
        for scopes in ((SCOPE_FLEET_READ,), (SCOPE_FLEET_WRITE,)):
            assert api.get(f"/api/boxes/{box}/working", headers=_as(*scopes)).status_code == 403
