"""Moving an agent onto the model gateway, and back (FLOTTA-90): the operator's act."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, mint, scopes_for_role
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.store import FleetStore

KEY = "a-signing-key-for-gateway-moves"
GATEWAY = "https://gateway.example"


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.update_box_status(store.create_box("eng-d").id, "running", endpoint="fly://app/m1")
        store.create_org("studio-nord", "Studio Nord")
        store.create_client("studio-nord", "acme", "Acme Dental")
        theirs = store.create_box("support", box_id="b-theirs", org="studio-nord", client="acme")
        store.update_box_status(theirs.id, "running", endpoint="fly://other/m2")
        store.create_box("new")
    return path


def _api(fleet, *, background=False):
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=background,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
    )
    return TestClient(app)


def _headers(*scopes, **claims):
    token = mint(scopes=set(scopes), key=KEY, ttl_s=300, subject="p", **claims)
    return {"Authorization": f"Bearer {token}"}


OPERATOR = _headers(SCOPE_FLEET_READ, SCOPE_FLEET_WRITE)


@pytest.fixture
def moves(monkeypatch):
    import flotta.provision as provision

    calls: list[tuple[str, str]] = []

    def join(box_id, *, store, **kwargs):
        calls.append(("join", box_id))
        return {"box_id": box_id, "name": "eng-d", "via": GATEWAY}

    def leave(box_id, *, store, **kwargs):
        calls.append(("leave", box_id))
        return {"box_id": box_id, "name": "eng-d", "via": "direct"}

    monkeypatch.setattr(provision, "join_gateway", join)
    monkeypatch.setattr(provision, "leave_gateway", leave)
    monkeypatch.setenv("FLOTTA_GATEWAY_URL", GATEWAY)
    return calls


def test_the_operator_moves_an_agent_onto_the_gateway(fleet, moves):
    with _api(fleet) as api:
        response = api.post("/api/boxes/eng-d/gateway", headers=OPERATOR)
    assert response.status_code == 200, response.text
    assert [verb for verb, _ in moves] == ["join"]


def test_the_operator_moves_it_back(fleet, moves):
    with _api(fleet) as api:
        assert api.delete("/api/boxes/eng-d/gateway", headers=OPERATOR).status_code == 200
    assert [verb for verb, _ in moves] == ["leave"]


def test_the_operator_reaches_any_orgs_agent(fleet, moves):
    with _api(fleet) as api:
        assert api.post("/api/boxes/b-theirs/gateway", headers=OPERATOR).status_code == 200


def test_with_a_thread_it_answers_202_before_the_restarts(fleet, moves):
    with _api(fleet, background=True) as api:
        response = api.post("/api/boxes/eng-d/gateway", headers=OPERATOR)
    assert response.status_code == 202
    assert response.json()["started"] is True


def test_an_agencys_owner_may_not_move_its_own_agent(fleet, moves):
    """It is the fleet's key and the fleet's bill."""
    owner = _headers(*scopes_for_role("owner"), org="studio-nord")
    with _api(fleet) as api:
        assert api.post("/api/boxes/b-theirs/gateway", headers=owner).status_code == 403
        assert api.delete("/api/boxes/b-theirs/gateway", headers=owner).status_code == 403
    assert moves == []


def test_another_orgs_agent_is_a_404_to_an_owner(fleet, moves):
    owner = _headers(*scopes_for_role("owner"), org="studio-nord")
    with _api(fleet) as api:
        assert api.post("/api/boxes/eng-d/gateway", headers=owner).status_code == 404
    assert moves == []


def test_a_reader_may_not_move_an_agent(fleet, moves):
    with _api(fleet) as api:
        response = api.post("/api/boxes/eng-d/gateway", headers=_headers(SCOPE_FLEET_READ))
    assert response.status_code == 403
    assert moves == []


def test_with_no_gateway_configured_joining_is_refused_and_leaving_is_not(
    fleet, moves, monkeypatch
):
    monkeypatch.delenv("FLOTTA_GATEWAY_URL")
    with _api(fleet) as api:
        joining = api.post("/api/boxes/eng-d/gateway", headers=OPERATOR)
        assert joining.status_code == 409
        assert "FLOTTA_GATEWAY_URL" in joining.json()["detail"]
        assert api.delete("/api/boxes/eng-d/gateway", headers=OPERATOR).status_code == 200
    assert [verb for verb, _ in moves] == ["leave"]


def test_an_agent_with_no_settled_machine_is_a_409(fleet, moves):
    with _api(fleet) as api:
        assert api.post("/api/boxes/new/gateway", headers=OPERATOR).status_code == 409
    assert moves == []
