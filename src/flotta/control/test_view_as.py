"""The operator's view (M12.9): "view as owner", everyone, and every org's agents.

The first test is the guard. A read-only session is the operator looking at an
agency as its owner sees it, and **nothing made with it changes anything**. It
is sent to every route the app has that is not a read, with whatever path it
takes, and each must refuse it before the route runs. A route added later is
in the sweep without anyone remembering to add it, because the sweep reads the
app's own route table.

The rest: who may ask for one, what it sees, and that it ends when its holder
stops being the operator, as an ordinary session ends with its membership.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_FLEET_READ, SCOPE_ORG_ADMIN, mint
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.control.test_route_scope import OPEN_ROUTES
from flotta.store import (
    VIEW_SESSION_TTL_S,
    FleetStore,
    InvalidAccountError,
    NotOperatorError,
    UnknownEntityError,
)

KEY = "a-signing-key-for-view-as-tests"


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.update_box_status(store.create_box("eng-a").id, "running", endpoint="fly://app/m1")
        store.create_client("default", "acme", "Acme")
        store.create_org("agency-x", "Agency X")
        store.create_client("agency-x", "acme", "Acme")
        box = store.create_box("support", org="agency-x", client="acme")
        store.update_box_status(box.id, "running", endpoint="fly://flotta-support/m1")
        owner = store.create_user("owner@agency-x.test")
        store.add_membership(owner.id, "agency-x", "owner")
    return path


def _app(fleet):
    return create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
    )


@pytest.fixture
def api(fleet):
    with TestClient(_app(fleet)) as client:
        yield client


def _person(fleet, email, org, role, client=None):
    """A user with one membership and a session in it: (user id, headers)."""
    with FleetStore(fleet) as store:
        user = store.get_user_by_email(email) or store.create_user(email)
        store.add_membership(user.id, org, role, client_id=client)
        secret = store.create_session(user.id, org)
    return user.id, {"Authorization": f"Session {secret}"}


def _viewing(api, fleet, org="agency-x"):
    """The operator, and a read-only session of theirs in `org`: (user id, headers)."""
    user_id, operator = _person(fleet, "josh@flotta.test", "default", "owner")
    answer = api.post(f"/api/orgs/{org}/view-as", headers=operator)
    assert answer.status_code == 201, answer.text
    return user_id, {"Authorization": f"Session {answer.json()['session']}"}


def _snapshot(fleet):
    """Everything a refused request could have changed."""
    with FleetStore(fleet) as store:
        return (
            [(b.id, b.name, b.status) for b in store.list_boxes()],
            [(o.id, o.status) for o in store.list_orgs()],
            [(o, c.id) for o in ("default", "agency-x") for c in store.list_clients(o)],
            [(u.id, u.disabled_at) for u in store.list_users()],
            [
                (m.user_id, m.org, m.role)
                for o in ("default", "agency-x")
                for m in store.members_of(o)
            ],
        )


# -- the guard: a read-only session changes nothing --------------------------------

_LOOKING = {"GET", "HEAD", "OPTIONS"}
#: The one change a read-only session may make: ending itself.
_ENDS_ITSELF = {("POST", "/api/auth/sign-out")}


def _changing_routes():
    """Every (method, path) the app answers that is not a read, from its own table."""
    app = _app_for_listing()
    found = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.name in OPEN_ROUTES:
            continue
        for method in sorted(route.methods - _LOOKING):
            if (method, route.path) not in _ENDS_ITSELF:
                found.append((method, route.path))
    return found


def _app_for_listing():
    return create_app(
        store_factory=lambda: None,
        run_loop=False,
        background=False,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
    )


def test_the_sweep_finds_the_routes_that_change_things():
    """A sweep that found nothing would pass vacuously."""
    routes = _changing_routes()
    assert len(routes) > 25
    for expected in [
        ("POST", "/api/boxes"),
        ("DELETE", "/api/boxes/{box_id}"),
        ("POST", "/api/boxes/{box_id}/sleep"),
        ("POST", "/api/boxes/{box_id}/ticket"),
        ("POST", "/api/orgs/{org_id}/clients"),
        ("POST", "/api/orgs/{org_id}/invites"),
        ("POST", "/api/orgs/{org_id}/approve"),
        ("POST", "/api/orgs/{org_id}/view-as"),
        ("POST", "/api/auth/switch"),
        ("PUT", "/api/settings"),
    ]:
        assert expected in routes


@pytest.mark.parametrize(("method", "path"), _changing_routes())
def test_every_route_that_changes_anything_refuses_a_read_only_session(api, fleet, method, path):
    _, viewing = _viewing(api, fleet)
    with FleetStore(fleet) as store:
        box = store.get_box_by_name("support--acme--agency-x")
    # Real ids where a route takes them, so a missing guard reaches the route
    # and changes something, rather than meeting a 404 that looks like a pass.
    fill = {
        "box_id": box.id,
        "org_id": "agency-x",
        "user_id": "nobody",
        "session_id": "nothing",
        "owner": "o",
        "name": "n",
        "peer": "p",
    }
    url = re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: fill.get(m.group(1), "x"), path)
    before = _snapshot(fleet)
    answer = api.request(method, url, headers=viewing, json={"name": "new", "id": "new"})
    assert answer.status_code == 403, answer.text
    assert "read-only" in answer.json()["detail"]
    assert _snapshot(fleet) == before


def test_a_read_only_session_signs_itself_out(api, fleet):
    _, viewing = _viewing(api, fleet)
    assert api.post("/api/auth/sign-out", headers=viewing).status_code == 200
    assert api.get("/api/me", headers=viewing).status_code == 401


def test_a_read_only_token_holds_no_scope_that_writes(api, fleet):
    """The second layer: a route that forgot to be a GET still meets a 403."""
    _, viewing = _viewing(api, fleet)
    me = api.get("/api/me", headers=viewing).json()
    assert me["scopes"] == sorted({SCOPE_FLEET_READ, SCOPE_ORG_ADMIN})


# -- what it sees -----------------------------------------------------------------


def test_a_read_only_session_sees_the_agency_as_its_owner_does(api, fleet):
    _, viewing = _viewing(api, fleet)
    me = api.get("/api/me", headers=viewing).json()
    assert me["org"] == "agency-x"
    assert me["viewing_as"] is True
    assert me["operator"] is False
    assert me["client"] is None
    assert me["session"]["read_only"] is True

    boxes = api.get("/api/boxes", headers=viewing).json()["boxes"]
    assert [b["name"] for b in boxes] == ["support--acme--agency-x"]
    clients = api.get("/api/orgs/agency-x/clients", headers=viewing).json()["clients"]
    assert [c["id"] for c in clients] == ["acme"]
    members = api.get("/api/orgs/agency-x/members", headers=viewing).json()["members"]
    assert [m["user"]["email"] for m in members] == ["owner@agency-x.test"]
    events = api.get(f"/api/boxes/{boxes[0]['id']}/events", headers=viewing)
    assert events.status_code == 200


def test_a_read_only_session_sees_only_the_agency_it_views(api, fleet):
    _, viewing = _viewing(api, fleet)
    with FleetStore(fleet) as store:
        own = store.get_box_by_name("eng-a")
    assert api.get(f"/api/boxes/{own.id}", headers=viewing).status_code == 404
    assert api.get("/api/orgs/default/members", headers=viewing).status_code == 404
    assert api.get("/api/users", headers=viewing).status_code == 403
    assert api.get("/api/fleet/boxes", headers=viewing).status_code == 403


def test_viewing_as_leaves_the_operators_own_session_alone(api, fleet):
    user_id, operator = _person(fleet, "josh@flotta.test", "default", "owner")
    viewing = api.post("/api/orgs/agency-x/view-as", headers=operator).json()
    me = api.get("/api/me", headers=operator).json()
    assert me["org"] == "default" and me["operator"] is True and me["viewing_as"] is False
    expires = datetime.fromisoformat(viewing["expires_at"])
    left = (expires - datetime.now(UTC)).total_seconds()
    assert VIEW_SESSION_TTL_S - 60 < left <= VIEW_SESSION_TTL_S
    assert viewing["org"] == "agency-x"


# -- who may look -----------------------------------------------------------------


def test_an_operator_who_is_a_member_may_view_as(api, fleet):
    _, member = _person(fleet, "colleague@flotta.test", "default", "member")
    assert api.post("/api/orgs/agency-x/view-as", headers=member).status_code == 201


@pytest.mark.parametrize(
    ("org", "role", "client"),
    [
        ("agency-x", "owner", None),
        ("default", "member", "acme"),
        ("default", "client_viewer", "acme"),
    ],
)
def test_only_the_operator_may_view_as(api, fleet, org, role, client):
    _, someone = _person(fleet, "someone@else.test", org, role, client)
    answer = api.post("/api/orgs/agency-x/view-as", headers=someone)
    assert answer.status_code == 403, answer.text
    assert "operator" in answer.json()["detail"]


def test_viewing_as_is_for_a_person_not_a_token(api):
    token = mint(subject="josh", scopes={SCOPE_FLEET_READ}, key=KEY, ttl_s=300)
    answer = api.post("/api/orgs/agency-x/view-as", headers={"Authorization": f"Bearer {token}"})
    assert answer.status_code == 403
    assert "signed-in person" in answer.json()["detail"]


def test_viewing_as_names_an_org_that_exists_and_is_not_the_operators(api, fleet):
    _, operator = _person(fleet, "josh@flotta.test", "default", "owner")
    assert api.post("/api/orgs/agency-y/view-as", headers=operator).status_code == 404
    own = api.post("/api/orgs/default/view-as", headers=operator)
    assert own.status_code == 409
    assert "operator's own" in own.json()["detail"]


# -- it ends with the operator ------------------------------------------------------


def test_a_read_only_session_ends_when_its_holder_stops_being_the_operator(api, fleet):
    user_id, viewing = _viewing(api, fleet)
    _person(fleet, "second@flotta.test", "default", "owner")
    assert api.get("/api/boxes", headers=viewing).status_code == 200
    with FleetStore(fleet) as store:
        store.remove_membership(user_id, "default")
    assert api.get("/api/boxes", headers=viewing).status_code == 401


def test_a_read_only_session_ends_when_its_holder_is_confined_to_a_client(api, fleet):
    """Still in the default org, but one client's: no longer the operator."""
    user_id, viewing = _viewing(api, fleet)
    _person(fleet, "second@flotta.test", "default", "owner")
    with FleetStore(fleet) as store:
        store.remove_membership(user_id, "default")
        store.add_membership(user_id, "default", "member", client_id="acme")
    assert api.get("/api/boxes", headers=viewing).status_code == 401


def test_a_read_only_session_ends_with_its_holders_account(api, fleet):
    user_id, viewing = _viewing(api, fleet)
    with FleetStore(fleet) as store:
        store.disable_user(user_id)
    assert api.get("/api/boxes", headers=viewing).status_code == 401


# -- the store's half ---------------------------------------------------------------


def test_the_store_makes_a_read_only_session_only_for_the_operator(fleet):
    with FleetStore(fleet) as store:
        owner = store.get_user_by_email("owner@agency-x.test")
        with pytest.raises(NotOperatorError):
            store.create_view_session(owner.id, "agency-x")
        josh = store.create_user("josh@flotta.test")
        store.add_membership(josh.id, "default", "owner")
        with pytest.raises(InvalidAccountError):
            store.create_view_session(josh.id, "default")
        with pytest.raises(UnknownEntityError):
            store.create_view_session(josh.id, "agency-y")
        secret = store.create_view_session(josh.id, "agency-x")
        assert store.session(secret).read_only is True
        assert store.session(store.create_session(josh.id, "default")).read_only is False


# -- everyone, and every org's agents -----------------------------------------------


def test_the_operator_lists_everyone_and_where_they_belong(api, fleet):
    _, operator = _person(fleet, "josh@flotta.test", "default", "owner")
    people = {u["email"]: u for u in api.get("/api/users", headers=operator).json()["users"]}
    assert set(people) == {"owner@agency-x.test", "josh@flotta.test"}
    assert [(m["org"], m["role"]) for m in people["owner@agency-x.test"]["memberships"]] == [
        ("agency-x", "owner")
    ]
    assert people["josh@flotta.test"]["disabled_at"] is None


def test_the_operator_lists_every_orgs_agents(api, fleet):
    _, operator = _person(fleet, "josh@flotta.test", "default", "owner")
    names = {b["name"] for b in api.get("/api/fleet/boxes", headers=operator).json()["boxes"]}
    assert names == {"eng-a", "support--acme--agency-x"}
    # `GET /api/boxes` stays the caller's own org's, as M11 made it.
    own = {b["name"] for b in api.get("/api/boxes", headers=operator).json()["boxes"]}
    assert own == {"eng-a"}


def test_every_orgs_agents_include_the_ended_ones_when_asked(api, fleet):
    _, operator = _person(fleet, "josh@flotta.test", "default", "owner")
    with FleetStore(fleet) as store:
        store.update_box_status(store.get_box_by_name("eng-a").id, "torn_down")
    live = api.get("/api/fleet/boxes", headers=operator).json()["boxes"]
    every = api.get("/api/fleet/boxes?all_=true", headers=operator).json()["boxes"]
    assert len(live) == 1 and len(every) == 2


@pytest.mark.parametrize("path", ["/api/users", "/api/fleet/boxes"])
def test_only_the_operator_lists_everyone(api, fleet, path):
    _, owner = _person(fleet, "owner2@agency-x.test", "agency-x", "owner")
    answer = api.get(path, headers=owner)
    assert answer.status_code == 403
    assert "operator" in answer.json()["detail"]
