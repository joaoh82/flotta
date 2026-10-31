"""Which models an agent may be given: the operator's list (FLOTTA-85).

Every agent calls its model with the fleet's one provider key, so the model is
the operator's cost, whoever picks it. Before this, `fleet:write` could point
an agent at any model the provider lists; an agency could choose the most
expensive one on someone else's key. Now a model must be on the fleet's list
(`FLOTTA_MODELS`, plus the fleet's default), checked before anything reaches a
machine, for every caller, the operator included.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_FLEET_READ, mint
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.settings import validate
from flotta.store import FleetStore

KEY = "a-signing-key-for-model-tests"
DEFAULT = "z-ai/glm-5.2"
LISTED = "anthropic/claude-sonnet-4.5"
EXPENSIVE = "openai/o1-pro"


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOTTA_MODEL", DEFAULT)
    monkeypatch.setenv("FLOTTA_MODELS", LISTED)
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.update_box_status(store.create_box("eng-a").id, "running", endpoint="fly://app/m1")
        store.create_org("agency-x", "Agency X")
        store.create_client("agency-x", "acme", "Acme")
        box = store.create_box("support", org="agency-x", client="acme")
        store.update_box_status(box.id, "running", endpoint="fly://flotta-support/m1")
    return path


@pytest.fixture
def reached(monkeypatch):
    """What reached the substrate. A refused model must leave this empty."""
    import flotta.provision as provision

    calls: list[tuple[str, object]] = []

    def set_model(box_id, model, *, store, **kwargs):
        calls.append(("set_model", model))
        return {"box_id": box_id, "name": "x", "model": model, "source": "agent"}

    def create_box(name, *, store, **kwargs):
        calls.append(("create_box", kwargs.get("model")))
        return {"box_id": "b-new", "endpoint": "fly://x/m"}

    monkeypatch.setattr(provision, "set_model", set_model)
    monkeypatch.setattr(provision, "create_box", create_box)
    return calls


def _api(fleet, *, signing_key=None):
    return create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        signing_key=signing_key,
        reachable=_unchecked,
        known_model=_uncatalogued,
    )


@pytest.fixture
def api(fleet):
    with TestClient(_api(fleet)) as client:
        yield client


@pytest.fixture
def secured(fleet):
    with TestClient(_api(fleet, signing_key=KEY)) as client:
        yield client


def _agency(*scopes):
    token = mint(scopes=set(scopes), key=KEY, ttl_s=300, subject="m", org="agency-x")
    return {"Authorization": f"Bearer {token}"}


# -- the guard ------------------------------------------------------------------


@pytest.mark.parametrize("model", [EXPENSIVE, "z-ai/glm-5.1"])
def test_a_model_off_the_list_is_refused_before_any_machine_is_touched(api, reached, model):
    for method, path, body in (
        ("PUT", "/api/boxes/eng-a/model", {"model": model}),
        ("POST", "/api/boxes", {"name": "eng-z", "model": model}),
    ):
        response = api.request(method, path, json=body)
        assert response.status_code == 422, response.text
        assert "not one of the fleet's models" in response.json()["detail"]
        assert LISTED in response.json()["detail"], "says what is allowed"
    assert reached == []


def test_an_agency_is_held_to_the_list_too(secured, reached):
    """The case the list exists for: someone else's key, someone else's bill."""
    from flotta.auth import SCOPE_FLEET_WRITE

    response = secured.put(
        "/api/boxes/support--acme--agency-x/model",
        json={"model": EXPENSIVE},
        headers=_agency(SCOPE_FLEET_READ, SCOPE_FLEET_WRITE),
    )
    assert response.status_code == 422
    assert reached == []


@pytest.mark.parametrize("model", [LISTED, DEFAULT, None, ""])
def test_a_listed_model_the_default_or_none_goes_through(api, reached, model):
    response = api.put("/api/boxes/eng-a/model", json={"model": model})
    assert response.status_code == 200, response.text
    assert reached == [("set_model", model or None)]


def test_a_stored_list_wins_over_the_environment(api, fleet, reached):
    with FleetStore(fleet) as store:
        store.set_settings({"FLOTTA_MODELS": EXPENSIVE})
    assert api.put("/api/boxes/eng-a/model", json={"model": EXPENSIVE}).status_code == 200
    assert api.put("/api/boxes/eng-a/model", json={"model": LISTED}).status_code == 422


def test_with_no_list_only_the_default_is_allowed(api, monkeypatch, reached):
    monkeypatch.delenv("FLOTTA_MODELS")
    assert api.put("/api/boxes/eng-a/model", json={"model": LISTED}).status_code == 422
    assert api.put("/api/boxes/eng-a/model", json={"model": DEFAULT}).status_code == 200


# -- what the list is -------------------------------------------------------------


def test_anyone_who_can_read_the_fleet_reads_the_list(secured):
    """A client viewer too: which models exist is not the agency's secret."""
    token = mint(
        scopes={SCOPE_FLEET_READ}, key=KEY, ttl_s=300, subject="v", org="agency-x", client="acme"
    )
    response = secured.get("/api/models", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"models": [DEFAULT, LISTED], "default": DEFAULT}


def test_the_default_is_listed_once_and_first(api, monkeypatch):
    monkeypatch.setenv("FLOTTA_MODELS", f"{LISTED}, {DEFAULT}")
    assert api.get("/api/models").json()["models"] == [DEFAULT, LISTED]


def test_a_fleet_with_no_models_says_so(api, monkeypatch):
    monkeypatch.delenv("FLOTTA_MODEL")
    monkeypatch.delenv("FLOTTA_MODELS")
    assert api.get("/api/models").json() == {"models": [], "default": None}


def test_the_list_is_cleaned_when_it_is_set():
    assert (
        validate("FLOTTA_MODELS", f" {LISTED},{EXPENSIVE}\n{LISTED} ") == f"{LISTED}, {EXPENSIVE}"
    )
    with pytest.raises(ValueError, match="claude sonnet"):
        validate("FLOTTA_MODELS", f"{LISTED}, claude sonnet")
