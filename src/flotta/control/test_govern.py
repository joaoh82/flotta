"""`org:govern`: the hosted product's billing credential (FLOTTA-183, M15.1).

The first test is the guard. A governing token may read and set an org's
limits, and **nothing else**: it is sent to every route the app has, every
method, with real ids where a route takes them, and every route but the two
limits routes must refuse it with nothing changed. A route added later is in
the sweep without anyone remembering to add it, because the sweep reads the
app's own route table.

The sweep holds the scopes: widen any route's dependency to admit
`org:govern` and it fails for that route. What it cannot see is the second
half: a governing token is the default org's and confined to no client, which
is exactly what the operator's token looks like, so `_is_operator` must ask
`is_governor` first. Today every operator route also needs a scope this token
lacks, which hides that line; `test_a_governing_token_is_not_the_operator`
fails without it, and so would any later route that needs only a valid
credential and then asks `_is_operator`.
"""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from flotta.auth import (
    SCOPE_FLEET_READ,
    SCOPE_FLEET_WRITE,
    SCOPE_ORG_ADMIN,
    SCOPE_ORG_GOVERN,
    AuthError,
    mint,
)
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.control.test_route_scope import OPEN_ROUTES
from flotta.store import FleetStore

KEY = "a-signing-key-for-govern-tests"

#: What a governing token may do: an org's limits, and nothing more.
GOVERN_ROUTES = frozenset(
    {
        ("GET", "/api/orgs/{org_id}/limits"),
        ("PUT", "/api/orgs/{org_id}/limits"),
        # FLOTTA-184: how a final failed payment ends, and how paying undoes it.
        ("POST", "/api/orgs/{org_id}/suspend"),
        ("POST", "/api/orgs/{org_id}/resume"),
        # FLOTTA-185: a self-serve approval, which always comes with a starting
        # cap, and lifting that cap once a renewal is paid.
        ("POST", "/api/orgs/{org_id}/approve"),
        ("DELETE", "/api/orgs/{org_id}/starting-cap"),
    }
)
#: What any valid credential may ask about itself: who it is and what it holds.
#: Answered for a governing token too, saying it is not the operator.
ABOUT_ITSELF = frozenset({("GET", "/api/me")})


def _bearer(scopes, **claims):
    return {"Authorization": f"Bearer {mint(subject='billing', scopes=scopes, key=KEY, **claims)}"}


GOVERNOR = _bearer({SCOPE_ORG_GOVERN})


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.update_box_status(store.create_box("eng-a").id, "running", endpoint="fly://app/m1")
        store.create_org("agency-x", "Agency X")
        store.create_client("agency-x", "acme", "Acme")
        box = store.create_box("support", org="agency-x", client="acme")
        store.update_box_status(box.id, "running", endpoint="fly://flotta-support/m1")
        owner = store.create_user("owner@agency-x.test")
        store.add_membership(owner.id, "agency-x", "owner")
    return path


def _app(store_factory):
    return create_app(
        store_factory=store_factory,
        run_loop=False,
        background=False,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
    )


@pytest.fixture
def api(fleet):
    with TestClient(_app(lambda: FleetStore(fleet))) as client:
        yield client


def _routes():
    """Every (method, path) the app answers that needs a credential."""
    found = []
    for route in _app(lambda: None).routes:
        if not isinstance(route, APIRoute) or route.name in OPEN_ROUTES:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            if (method, route.path) not in GOVERN_ROUTES | ABOUT_ITSELF:
                found.append((method, route.path))
    return found


def _snapshot(fleet):
    """Everything a request let through could have changed, or read and shown."""
    with FleetStore(fleet) as store:
        return (
            [(b.id, b.name, b.status) for b in store.list_boxes()],
            [(o.id, o.status, o.max_clients, o.max_seats, o.max_agents) for o in store.list_orgs()],
            [(o, c.id) for o in ("default", "agency-x") for c in store.list_clients(o)],
            [(u.id, u.disabled_at) for u in store.list_users()],
            [
                (m.user_id, m.org, m.role)
                for o in ("default", "agency-x")
                for m in store.members_of(o)
            ],
        )


# -- the guard ------------------------------------------------------------------


def test_the_sweep_finds_the_routes():
    """A sweep that found nothing would pass vacuously."""
    routes = _routes()
    assert len(routes) > 60
    for expected in [
        ("GET", "/api/boxes"),
        ("POST", "/api/boxes"),
        ("GET", "/api/boxes/{box_id}"),
        ("DELETE", "/api/boxes/{box_id}"),
        ("POST", "/api/boxes/{box_id}/ticket"),
        ("GET", "/api/orgs"),
        ("POST", "/api/orgs/{org_id}/decline"),
        ("GET", "/api/orgs/{org_id}/members"),
        ("GET", "/api/orgs/{org_id}/usage"),
        ("POST", "/api/orgs/{org_id}/clients"),
        ("POST", "/api/orgs/{org_id}/tokens"),
        ("PUT", "/api/settings"),
    ]:
        assert expected in routes


@pytest.mark.parametrize(("method", "path"), _routes())
def test_a_governing_token_reaches_no_route_but_its_own(api, fleet, method, path):
    with FleetStore(fleet) as store:
        box = store.get_box_by_name("support--acme--agency-x")
    # Real ids where a route takes them, so a missing guard reaches the route
    # and answers, rather than meeting a 404 that looks like a refusal.
    fill = {
        "box_id": box.id,
        "org_id": "agency-x",
        "client_id": "acme",
        "user_id": "nobody",
        "session_id": "nothing",
        "owner": "o",
        "name": "n",
        "peer": "p",
    }
    url = re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: fill.get(m.group(1), "x"), path)
    before = _snapshot(fleet)
    answer = api.request(
        method, url, headers=GOVERNOR, json={"name": "new", "id": "new", "client": "acme"}
    )
    assert answer.status_code in (401, 403, 404), (answer.status_code, answer.text)
    assert _snapshot(fleet) == before


# -- what it may do ---------------------------------------------------------------


def test_a_governing_token_reads_and_sets_any_orgs_limits(api, fleet):
    answer = api.put("/api/orgs/agency-x/limits", headers=GOVERNOR, json={"clients": 1})
    assert answer.status_code == 200, answer.text
    assert answer.json()["limits"] == {"clients": 1, "seats": None, "agents": None}
    read = api.get("/api/orgs/agency-x/limits", headers=GOVERNOR).json()
    assert read["counts"] == {"clients": 1, "seats": 1, "agents": 1}
    assert read["over"] == {"clients": False, "seats": False, "agents": False}


def test_a_governing_token_is_not_the_operator(api):
    """The default org's, unconfined, and still not the one who runs the fleet."""
    me = api.get("/api/me", headers=GOVERNOR).json()
    assert me["operator"] is False
    assert me["scopes"] == [SCOPE_ORG_GOVERN]
    assert api.get("/api/orgs", headers=GOVERNOR).status_code == 403
    assert api.post("/api/orgs", headers=GOVERNOR, json={"id": "x", "name": "X"}).status_code == 403


# -- who may mint one -------------------------------------------------------------


@pytest.mark.parametrize(
    ("scopes", "claims"),
    [
        ({SCOPE_ORG_GOVERN, SCOPE_FLEET_READ}, {}),
        ({SCOPE_ORG_GOVERN, SCOPE_ORG_ADMIN}, {}),
        ({SCOPE_ORG_GOVERN}, {"org": "agency-x"}),
        ({SCOPE_ORG_GOVERN}, {"client": "acme"}),
        ({SCOPE_ORG_GOVERN}, {"box": "b-1"}),
    ],
)
def test_mint_holds_org_govern_alone_and_for_the_fleet(scopes, claims):
    with pytest.raises(AuthError, match="org:govern"):
        mint(subject="billing", scopes=scopes, key=KEY, **claims)


def test_mint_allows_org_govern_for_the_default_org_named_or_not():
    for claims in ({}, {"org": "default"}):
        assert mint(subject="billing", scopes={SCOPE_ORG_GOVERN}, key=KEY, **claims)


def test_no_role_holds_org_govern():
    from flotta.auth import ROLES

    assert all(SCOPE_ORG_GOVERN not in scopes for scopes in ROLES.values())


def test_an_orgs_owner_cannot_mint_the_billing_subject(api):
    owner = _bearer({SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN}, org="agency-x")
    answer = api.post(
        "/api/orgs/agency-x/tokens", headers=owner, json={"subject": "billing", "role": "owner"}
    )
    assert answer.status_code == 422
