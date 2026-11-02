"""Moving an agent onto the model gateway, and back (FLOTTA-90): the operator's act."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, mint, scopes_for_role
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.provision import MODEL_CHANGE_STARTED, MODEL_SET, join_gateway, leave_gateway
from flotta.store import FleetStore
from flotta.test_gateway_move import _Machine

KEY = "a-signing-key-for-gateway-moves"
GATEWAY = "https://gateway.example"
GIVEN = "z-ai/glm-5.2"
LATER_DEFAULT = "openai/gpt-6"


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
    monkeypatch.setenv("FLOTTA_MODEL", GIVEN)
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


def test_a_model_change_under_way_is_a_409_not_a_202(fleet, moves):
    """On a thread the refusal would reach only a log, after saying it had started."""
    with FleetStore(fleet) as store:
        box_id = store.get_box_by_name("eng-d").id
        store.add_event("box", box_id, MODEL_CHANGE_STARTED, {"model": "openai/gpt-5"})
    with _api(fleet, background=True) as api:
        response = api.post("/api/boxes/eng-d/gateway", headers=OPERATOR)
    assert response.status_code == 409
    assert "openai/gpt-5" in response.json()["detail"]
    assert moves == []


def test_an_agent_whose_model_nothing_records_is_a_409_not_a_202(fleet, moves, monkeypatch):
    monkeypatch.delenv("FLOTTA_MODEL")
    with FleetStore(fleet) as store:
        store.set_setting("FLOTTA_MODEL", LATER_DEFAULT)
    with _api(fleet, background=True) as api:
        assert api.delete("/api/boxes/eng-d/gateway", headers=OPERATOR).status_code == 409
    assert moves == []


def _agrees(api, machine, box_id, model):
    """The machine was written what the API says it runs, by the box and by the list."""
    assert machine.calls[-2][2]["FLOTTA_MODEL"] == model
    assert api.get(f"/api/boxes/{box_id}", headers=OPERATOR).json()["box"]["model"] == model
    listed = api.get("/api/boxes", headers=OPERATOR).json()["boxes"]
    assert next(b for b in listed if b["id"] == box_id)["model"] == model


@pytest.mark.parametrize(
    ("own", "recorded", "environment", "wanted"),
    [
        pytest.param(None, GIVEN, "openai/gpt-5", GIVEN, id="follows-the-default"),
        pytest.param(None, None, GIVEN, GIVEN, id="older-than-the-record"),
        pytest.param(GIVEN, GIVEN, "openai/gpt-5", GIVEN, id="its-own"),
        pytest.param(GIVEN, "openai/gpt-5", "openai/gpt-5", GIVEN, id="its-own-over-the-record"),
    ],
)
def test_after_each_move_the_machine_and_the_api_agree(
    fleet, monkeypatch, own, recorded, environment, wanted
):
    """What the review of #157 found: a move wrote today's default, and the API kept
    reporting what the agent was given."""
    monkeypatch.setenv("FLOTTA_GATEWAY_URL", GATEWAY)
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", KEY)
    monkeypatch.setenv("FLOTTA_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("FLOTTA_API_KEY", "sk-or-the-fleets-own-key")
    monkeypatch.setenv("FLOTTA_MODEL", environment)
    machine = _Machine()
    with FleetStore(fleet) as store:
        box_id = store.get_box_by_name("eng-d").id
        if own:
            store.set_box_model(box_id, own)
        if recorded:
            store.add_event("box", box_id, MODEL_SET, {"model": recorded, "source": "fleet"})
        store.set_setting("FLOTTA_MODEL", LATER_DEFAULT)
        with _api(fleet) as api:
            join_gateway(box_id, store=store, backend=machine)
            _agrees(api, machine, box_id, wanted)
            leave_gateway(box_id, store=store, backend=machine)
            _agrees(api, machine, box_id, wanted)
