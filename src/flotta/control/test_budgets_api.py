"""`/api/orgs/{org}/budgets` (FLOTTA-89): owners write, members read, tenant-scoped."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flotta.auth import scopes_for_role
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.store import FleetStore

SIGNING_KEY = "a-signing-key-for-these-tests"
BODY = {"client": "acme", "period": "day", "limit_eur": "20", "action": "pause"}


@pytest.fixture
def fleet(tmp_path):
    db = str(tmp_path / "fleet.db")
    with FleetStore(db) as store:
        store.create_org("studio-nord", "Studio Nord")
        store.create_client("studio-nord", "acme", "Acme Dental")
        store.create_box("one", box_id="b-one", org="studio-nord", client="acme")
        store.create_org("other", "Other Agency")
        store.create_client("other", "acme", "Another Acme")
        store.create_box("four", box_id="b-four", org="other", client="acme")
    return db


@pytest.fixture
def api(fleet):
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        reachable=_unchecked,
        known_model=_uncatalogued,
        signing_key=SIGNING_KEY,
    )
    with TestClient(app) as c:
        yield c


def _as(role, org="studio-nord", client=None):
    from flotta.auth import mint

    token = mint(
        subject="cli",
        scopes=scopes_for_role(role, client=client),
        key=SIGNING_KEY,
        org=None if org == "default" else org,
        client=client,
    )
    return {"Authorization": f"Bearer {token}"}


ROUTE = "/api/orgs/studio-nord/budgets"


def test_an_owner_sets_lists_and_deletes_a_budget(api):
    set_ = api.put(ROUTE, json=BODY, headers=_as("owner"))
    assert set_.status_code == 200, set_.text
    budget = set_.json()
    assert budget["limit_eur"] == "20"
    assert budget["client"] == "acme"
    assert budget["scope"] == "client"
    assert budget["soft_pct"] == 0.8

    listed = api.get(ROUTE, headers=_as("owner")).json()
    assert listed["budgets"] == [budget]

    gone = api.delete(ROUTE, params={"client": "acme", "period": "day"}, headers=_as("owner"))
    assert gone.status_code == 200
    assert api.get(ROUTE, headers=_as("owner")).json()["budgets"] == []


def test_a_member_reads_and_cannot_write(api, fleet):
    api.put(ROUTE, json=BODY, headers=_as("owner"))
    assert api.get(ROUTE, headers=_as("member")).status_code == 200
    assert api.put(ROUTE, json=BODY, headers=_as("member")).status_code == 403
    assert (
        api.delete(
            ROUTE, params={"client": "acme", "period": "day"}, headers=_as("member")
        ).status_code
        == 403
    )
    with FleetStore(fleet) as store:
        assert len(store.budgets_for_org("studio-nord")) == 1


def test_a_clients_viewer_does_not_see_the_agencys_budgets(api):
    """What an agency allows a client's agents is the agency's business."""
    api.put(ROUTE, json=BODY, headers=_as("owner"))
    viewer = _as("client_viewer", client="acme")
    assert api.get(ROUTE, headers=viewer).status_code == 403


@pytest.mark.parametrize("role", ["owner", "member"])
def test_another_orgs_budgets_are_a_404_whichever_the_role(api, fleet, role):
    with FleetStore(fleet) as store:
        store.set_budget("other", period="day", limit_eur_nano=1, action="pause")
    assert api.get("/api/orgs/other/budgets", headers=_as(role)).status_code in (403, 404)
    assert api.get("/api/orgs/other/budgets", headers=_as(role)).status_code != 200


def test_an_owner_cannot_write_another_orgs_budget(api, fleet):
    response = api.put("/api/orgs/other/budgets", json=BODY, headers=_as("owner"))
    assert response.status_code in (403, 404)
    with FleetStore(fleet) as store:
        assert store.budgets_for_org("other") == []


def test_an_owner_cannot_budget_another_orgs_agent(api, fleet):
    body = {"box": "b-four", "period": "day", "limit_eur": "1", "action": "pause"}
    assert api.put(ROUTE, json=body, headers=_as("owner")).status_code == 404
    with FleetStore(fleet) as store:
        assert store.budgets_for_org("studio-nord") == []


def test_the_operator_reaches_any_org(api, fleet):
    operator = _as("owner", org="default")
    assert api.put(ROUTE, json=BODY, headers=operator).status_code == 200
    assert len(api.get(ROUTE, headers=operator).json()["budgets"]) == 1


def test_a_box_token_cannot_read_or_change_a_budget(api):
    from flotta.auth import SCOPE_MODEL_CALL, box_subject, mint

    token = mint(
        subject=box_subject("b-one"),
        scopes={SCOPE_MODEL_CALL},
        key=SIGNING_KEY,
        org="studio-nord",
        client="acme",
    )
    headers = {"Authorization": f"Bearer {token}"}
    assert api.get(ROUTE, headers=headers).status_code == 403
    assert api.put(ROUTE, json=BODY, headers=headers).status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "limit_eur": "lots"},
        {**BODY, "limit_eur": "0"},
        {**BODY, "period": "week"},
        {**BODY, "action": "explode"},
        {**BODY, "action": "downgrade"},
        {**BODY, "soft_pct": 2},
        {**BODY, "soft_pct": "most"},
        {**BODY, "client": 7},
        {**BODY, "box": ["b-one"]},
        {**BODY, "fallback_model": 7},
        {**BODY, "limit_eur": "1e30"},
        {"client": "acme"},
    ],
)
def test_a_budget_that_cannot_be_one_is_a_422(api, body):
    assert api.put(ROUTE, json=body, headers=_as("owner")).status_code == 422


def test_deleting_what_is_not_there_is_a_404(api):
    response = api.delete(ROUTE, params={"period": "day"}, headers=_as("owner"))
    assert response.status_code == 404


def test_deleting_a_scope_that_cannot_be_one_is_a_404(api, fleet):
    with FleetStore(fleet) as store:
        store.create_client("studio-nord", "bravo", "Bravo Legal")
    params = {"client": "bravo", "box": "b-one", "period": "day"}
    assert api.delete(ROUTE, params=params, headers=_as("owner")).status_code == 404


def test_an_org_that_does_not_exist_is_a_404_to_the_operator(api):
    assert (
        api.get("/api/orgs/nobody/budgets", headers=_as("owner", org="default")).status_code == 404
    )
