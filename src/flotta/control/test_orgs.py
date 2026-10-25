"""Orgs, clients and their people's tokens, over the fleet API (M11.7).

The operator creates an org and hands its owner one token; from then on the
owner adds the org's clients and mints its people's tokens without the
operator. Another org is the 404 of no org, as another org's box is the 404 of
no box.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flotta.auth import (
    SCOPE_FLEET_READ,
    SCOPE_FLEET_WRITE,
    SCOPE_ORG_ADMIN,
    mint,
    scopes_for_role,
    verify,
)
from flotta.control.app import create_app
from flotta.store import FleetStore

KEY = "a-signing-key-for-org-tests"


def _bearer(*scopes, **claims):
    claims.setdefault("subject", "test")
    return {"Authorization": f"Bearer {mint(scopes=set(scopes), key=KEY, ttl_s=300, **claims)}"}


def _as(role, **claims):
    client = claims.get("client")
    return _bearer(*scopes_for_role(role, client=client), **claims)


OPERATOR = _bearer(SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN)


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.create_box("eng-a")
        for org in ("agency-x", "agency-y"):
            store.create_org(org, org.title())
            store.create_client(org, "acme", "Acme")
            store.create_box("support", org=org, client="acme")
    return path


@pytest.fixture
def api(fleet):
    app = create_app(
        store_factory=lambda: FleetStore(fleet), run_loop=False, background=False, signing_key=KEY
    )
    with TestClient(app) as client:
        yield client


# -- orgs ----------------------------------------------------------------------


def test_the_operator_creates_an_org(api, fleet):
    response = api.post("/api/orgs", json={"id": "agency-z", "name": "Z"}, headers=OPERATOR)

    assert response.status_code == 201, response.text
    assert response.json()["org"]["id"] == "agency-z"
    with FleetStore(fleet) as store:
        assert store.get_org("agency-z").name == "Z"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"id": "agency-x", "name": "Again"}, 409),
        ({"id": "a--b", "name": "Joins"}, 422),
        ({"id": "Agency", "name": "Capital"}, 422),
        ({"id": "agency-z"}, 422),
    ],
    ids=["duplicate", "separator", "not-a-label", "no-name"],
)
def test_an_org_that_could_not_be_one_is_refused(api, body, status):
    assert api.post("/api/orgs", json=body, headers=OPERATOR).status_code == status


@pytest.mark.parametrize(
    "headers",
    [
        _as("owner", org="agency-x"),
        _bearer(SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN, subject="box:b-1"),
    ],
    ids=["another-orgs-owner", "a-box"],
)
def test_only_the_operator_creates_an_org(api, fleet, headers):
    response = api.post("/api/orgs", json={"id": "agency-z", "name": "Z"}, headers=headers)

    assert response.status_code == 403
    with FleetStore(fleet) as store:
        assert store.get_org("agency-z") is None


def test_creating_an_org_needs_org_admin(api):
    response = api.post(
        "/api/orgs", json={"id": "agency-z", "name": "Z"}, headers=_bearer(SCOPE_FLEET_WRITE)
    )
    assert response.status_code == 403
    assert "org:admin" in response.json()["detail"]


def test_the_operator_lists_every_org_and_an_org_only_its_own(api):
    def ids(headers):
        return [o["id"] for o in api.get("/api/orgs", headers=headers).json()["orgs"]]

    assert ids(OPERATOR) == ["default", "agency-x", "agency-y"]
    assert ids(_as("member", org="agency-x")) == ["agency-x"]
    assert ids(_as("client_viewer", org="agency-y", client="acme")) == ["agency-y"]


# -- clients -------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers", [OPERATOR, _as("owner", org="agency-x")], ids=["operator", "owner"]
)
def test_the_operator_or_the_orgs_owner_adds_a_client(api, fleet, headers):
    response = api.post(
        "/api/orgs/agency-x/clients", json={"id": "globex", "name": "Globex"}, headers=headers
    )

    assert response.status_code == 201, response.text
    with FleetStore(fleet) as store:
        assert store.get_client("agency-x", "globex").name == "Globex"


def test_an_owner_cannot_add_a_client_to_another_org(api, fleet):
    response = api.post(
        "/api/orgs/agency-y/clients",
        json={"id": "globex", "name": "Globex"},
        headers=_as("owner", org="agency-x"),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "no org 'agency-y'"}
    with FleetStore(fleet) as store:
        assert store.get_client("agency-y", "globex") is None


def test_a_member_cannot_add_a_client(api):
    response = api.post(
        "/api/orgs/agency-x/clients",
        json={"id": "globex", "name": "Globex"},
        headers=_as("member", org="agency-x"),
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"id": "acme", "name": "Again"}, 409),
        ({"id": "a--b", "name": "Joins"}, 422),
        ({"id": "x" * 17, "name": "Long"}, 422),
    ],
    ids=["duplicate", "separator", "too-long"],
)
def test_a_client_that_could_not_be_one_is_refused(api, body, status):
    response = api.post("/api/orgs/agency-x/clients", json=body, headers=OPERATOR)
    assert response.status_code == status


def test_a_client_of_an_org_that_does_not_exist_is_404(api):
    response = api.post(
        "/api/orgs/agency-z/clients", json={"id": "acme", "name": "Acme"}, headers=OPERATOR
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("headers", "org", "expected"),
    [
        (OPERATOR, "agency-y", ["acme"]),
        (_as("member", org="agency-x"), "agency-x", ["acme", "globex"]),
        (_as("client_viewer", org="agency-x", client="globex"), "agency-x", ["globex"]),
    ],
    ids=["operator", "member", "client-viewer"],
)
def test_who_sees_which_clients(api, fleet, headers, org, expected):
    with FleetStore(fleet) as store:
        store.create_client("agency-x", "globex", "Globex")

    response = api.get(f"/api/orgs/{org}/clients", headers=headers)

    assert [c["id"] for c in response.json()["clients"]] == expected


def test_another_orgs_clients_are_the_404_of_no_org(api):
    response = api.get("/api/orgs/agency-y/clients", headers=_as("member", org="agency-x"))
    assert response.status_code == 404


# -- tokens --------------------------------------------------------------------


def _mint(api, org, headers, **body):
    return api.post(f"/api/orgs/{org}/tokens", json=body, headers=headers)


def test_an_owner_mints_a_member_for_one_of_its_clients(api):
    response = _mint(
        api, "agency-x", _as("owner", org="agency-x"), subject="sam", role="member", client="acme"
    )

    assert response.status_code == 201, response.text
    token = verify(response.json()["token"], key=KEY)
    assert (token.subject, token.org, token.client) == ("sam", "agency-x", "acme")
    assert token.scopes == scopes_for_role("member", client="acme")

    # And it is what it says: that client's agents, nobody else's.
    listed = api.get("/api/boxes", headers={"Authorization": f"Bearer {response.json()['token']}"})
    assert [b["name"] for b in listed.json()["boxes"]] == ["support--acme--agency-x"]


def test_a_member_cannot_mint_org_admin(api):
    """Nor anything else: minting is `org:admin`, and a member does not hold it
    — so a member cannot make itself, or anyone, an owner."""
    for role in ("owner", "member"):
        response = _mint(api, "agency-x", _as("member", org="agency-x"), subject="me", role=role)
        assert response.status_code == 403, role
        assert "org:admin" in response.json()["detail"]


def test_an_owner_cannot_mint_for_another_org(api):
    response = _mint(api, "agency-y", _as("owner", org="agency-x"), subject="spy", role="owner")
    assert response.status_code == 404


def test_the_operator_mints_an_orgs_first_owner(api):
    """The hand-over: the one token the operator gives an agency."""
    response = _mint(api, "agency-x", OPERATOR, subject="alice", role="owner")

    token = verify(response.json()["token"], key=KEY)
    assert (token.org, token.client) == ("agency-x", None)
    assert SCOPE_ORG_ADMIN in token.scopes


def test_the_default_orgs_tokens_carry_no_org_claim(api):
    """Byte-for-byte the shape the single-operator fleet has always used."""
    response = _mint(api, "default", OPERATOR, subject="bob", role="member")
    token = verify(response.json()["token"], key=KEY)
    assert (token.org, token.client) == (None, None)


@pytest.mark.parametrize(
    ("body", "status", "says"),
    [
        ({"subject": "x", "role": "admin"}, 422, "unknown role"),
        ({"subject": "x", "role": "owner", "client": "acme"}, 422, "whole org"),
        ({"subject": "x", "role": "client_viewer"}, 422, "name the client"),
        ({"subject": "x", "role": "member", "client": "globex"}, 404, "no client 'globex'"),
        ({"subject": "", "role": "member"}, 422, "subject"),
        ({"subject": "box:b-1", "role": "member"}, 422, "machine"),
        ({"subject": "relay", "role": "member"}, 422, "machine"),
        ({"subject": "x", "role": "member", "days": 0}, 422, "days"),
        ({"subject": "x", "role": "member", "days": 366}, 422, "days"),
        ({"subject": "x", "role": "member", "days": "30"}, 422, "days"),
    ],
)
def test_a_token_that_should_not_exist_is_refused(api, body, status, says):
    response = _mint(api, "agency-x", _as("owner", org="agency-x"), **body)
    assert response.status_code == status, response.text
    assert says in response.json()["detail"]


def test_without_a_signing_key_nothing_is_minted(fleet):
    app = create_app(store_factory=lambda: FleetStore(fleet), run_loop=False, background=False)
    with TestClient(app) as client:
        response = client.post("/api/orgs/agency-x/tokens", json={"subject": "x", "role": "member"})
    assert response.status_code == 503


def test_an_agency_set_up_end_to_end_through_the_api(api):
    """Operator: org, client, owner. Owner: a viewer for the client. Viewer:
    sees that client's agents, and cannot touch them."""
    assert api.post("/api/orgs", json={"id": "agency-z", "name": "Z"}, headers=OPERATOR).is_success
    owner = _mint(api, "agency-z", OPERATOR, subject="zoe", role="owner").json()["token"]
    as_owner = {"Authorization": f"Bearer {owner}"}
    assert api.post(
        "/api/orgs/agency-z/clients", json={"id": "initech", "name": "Initech"}, headers=as_owner
    ).is_success
    viewer = _mint(
        api, "agency-z", as_owner, subject="peter", role="client_viewer", client="initech"
    ).json()["token"]
    as_viewer = {"Authorization": f"Bearer {viewer}"}

    assert api.get("/api/boxes", headers=as_viewer).json()["boxes"] == []
    assert api.post("/api/boxes", json={"name": "x"}, headers=as_viewer).status_code == 403
    assert api.get("/api/settings", headers=as_owner).status_code == 403
