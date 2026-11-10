"""An org's limits on clients, seats and agents (FLOTTA-183, M15.1).

The guard first: **a limit refuses the next one on every path that makes one**,
and leaves nothing behind when it does. The limit is held in the store, so the
store's own entry points are tested as well as the API's: the CLI, sign-up and
invites all reach the store, and none of them can skip it.

Then: lowering a limit takes nothing away, raising it lets the same call
through, and two creates racing for the last slot cannot both get it.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN, mint
from flotta.control.test_sign_in import KEY, Outbox, _app
from flotta.store import FleetStore, LimitReachedError


def _bearer(scopes, **claims):
    return {"Authorization": f"Bearer {mint(subject='t', scopes=scopes, key=KEY, **claims)}"}


OPERATOR = _bearer({SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN})
OWNER = _bearer({SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN}, org="agency-x")
CLIENT_VIEWER = _bearer({SCOPE_FLEET_READ}, org="agency-x", client="acme")


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.create_org("agency-x", "Agency X")
        store.create_client("agency-x", "acme", "Acme")
        ana = store.create_user("ana@example.com")
        store.add_membership(ana.id, "agency-x", "owner")
        store.create_box("support", org="agency-x", client="acme")
    return path


@pytest.fixture
def outbox():
    return Outbox()


@pytest.fixture
def api(fleet, outbox):
    with TestClient(_app(fleet, mailer=outbox)) as client:
        yield client


def _limit(fleet, **limits):
    with FleetStore(fleet) as store:
        store.set_org_limits("agency-x", limits)


def _counts(fleet):
    with FleetStore(fleet) as store:
        return store.org_counts("agency-x")


# -- the guard: every path that makes one ------------------------------------------


def test_a_full_org_adds_no_client_through_the_api(api, fleet):
    _limit(fleet, clients=1)
    refused = api.post(
        "/api/orgs/agency-x/clients", json={"id": "beta", "name": "Beta"}, headers=OWNER
    )
    assert refused.status_code == 402, refused.text
    assert "1 of its 1 clients" in refused.json()["detail"]
    assert refused.json()["limit"] == {"kind": "clients", "limit": 1}
    assert _counts(fleet)["clients"] == 1


def test_a_full_org_adds_no_client_through_the_store(fleet):
    """The CLI's `flotta client add` and every other caller."""
    _limit(fleet, clients=1)
    with FleetStore(fleet) as store, pytest.raises(LimitReachedError):
        store.create_client("agency-x", "beta", "Beta")
    assert _counts(fleet)["clients"] == 1


def test_a_full_org_creates_no_agent_and_never_reaches_a_substrate(api, fleet, monkeypatch):
    import flotta.control.app as app_module
    import flotta.provision as provision

    def nowhere(*_a, **_k):
        raise AssertionError("an agent past its org's limit reached a substrate")

    monkeypatch.setattr(app_module, "_peek_for", nowhere)
    monkeypatch.setattr(provision, "_peek_endpoint", nowhere)
    monkeypatch.setattr(provision, "_backend_for", nowhere)
    _limit(fleet, agents=1)
    refused = api.post("/api/boxes", json={"name": "sales", "client": "acme"}, headers=OWNER)
    assert refused.status_code == 402, refused.text
    assert "1 of its 1 agents" in refused.json()["detail"]
    assert _counts(fleet)["agents"] == 1


@pytest.mark.parametrize("background", [True, False], ids=["202-path", "synchronous"])
def test_the_store_refuses_the_agents_row_on_both_create_paths(fleet, background, monkeypatch):
    """Past the API's early answer: the store's own check, which is the one that counts."""
    import flotta.control.app as app_module
    from flotta.control.app import create_app
    from flotta.control.test_app import _uncatalogued, _unchecked

    _limit(fleet, agents=1)
    # The early check is skipped, as a route that forgot it would.
    monkeypatch.setattr(FleetStore, "require_room", lambda *_a, **_k: None)
    monkeypatch.setattr(app_module, "_peek_for", lambda *_a, **_k: None)
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=background,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
    )
    with TestClient(app) as api:
        refused = api.post("/api/boxes", json={"name": "sales", "client": "acme"}, headers=OWNER)
    assert refused.status_code == 402, refused.text
    assert _counts(fleet)["agents"] == 1


def test_a_full_org_creates_no_agent_through_the_store(fleet):
    """`flotta create` and the 202 path both write this row first."""
    _limit(fleet, agents=1)
    with FleetStore(fleet) as store, pytest.raises(LimitReachedError):
        store.create_box("sales", org="agency-x", client="acme")
    assert _counts(fleet)["agents"] == 1


def test_a_torn_down_agent_frees_its_slot(fleet):
    _limit(fleet, agents=1)
    with FleetStore(fleet) as store:
        box = store.get_box_by_name("support--acme--agency-x")
        store.update_box_status(box.id, "torn_down")
        assert store.create_box("sales", org="agency-x", client="acme")


def test_a_full_org_sends_no_invite_to_a_seat(api, fleet, outbox):
    _limit(fleet, seats=1)
    refused = api.post(
        "/api/orgs/agency-x/invites",
        json={"email": "bo@example.com", "role": "member"},
        headers=OWNER,
    )
    assert refused.status_code == 402, refused.text
    assert outbox == []


def test_a_clients_people_take_no_seat(api, fleet, outbox):
    _limit(fleet, seats=1)
    for role in ("client_viewer", "client_user"):
        sent = api.post(
            "/api/orgs/agency-x/invites",
            json={"email": f"{role}@acme.test", "role": role, "client": "acme"},
            headers=OWNER,
        )
        assert sent.status_code == 202, sent.text


def test_open_invites_to_a_seat_count_as_seats(api, fleet, outbox):
    _limit(fleet, seats=2)
    sent = api.post(
        "/api/orgs/agency-x/invites",
        json={"email": "bo@example.com", "role": "member"},
        headers=OWNER,
    )
    assert sent.status_code == 202, sent.text
    refused = api.post(
        "/api/orgs/agency-x/invites",
        json={"email": "cy@example.com", "role": "member"},
        headers=OWNER,
    )
    assert refused.status_code == 402, refused.text
    # Sending the same person a new invite promises no second seat.
    again = api.post(
        "/api/orgs/agency-x/invites",
        json={"email": "bo@example.com", "role": "member"},
        headers=OWNER,
    )
    assert again.status_code == 202, again.text


def test_an_invite_sent_before_the_org_filled_is_refused_and_kept(api, fleet, outbox):
    sent = api.post(
        "/api/orgs/agency-x/invites",
        json={"email": "bo@example.com", "role": "member"},
        headers=OWNER,
    )
    assert sent.status_code == 202, sent.text
    _limit(fleet, seats=1)
    secret = outbox.secret()
    refused = api.post("/api/auth/redeem", json={"secret": secret})
    assert refused.status_code == 402, refused.text
    assert _counts(fleet)["seats"] == 1
    # Not spent: once the limit is raised, the same link lets them in.
    _limit(fleet, seats=2)
    joined = api.post("/api/auth/redeem", json={"secret": secret})
    assert joined.status_code == 200, joined.text
    assert _counts(fleet)["seats"] == 2


def test_a_full_org_moves_nobody_into_a_seat(api, fleet):
    with FleetStore(fleet) as store:
        viewer = store.create_user("cy@acme.test")
        store.add_membership(viewer.id, "agency-x", "client_viewer", client_id="acme")
    _limit(fleet, seats=1)
    refused = api.patch(
        f"/api/orgs/agency-x/members/{viewer.id}", json={"role": "member"}, headers=OWNER
    )
    assert refused.status_code == 402, refused.text
    with FleetStore(fleet) as store:
        assert store.membership(viewer.id, "agency-x").role == "client_viewer"


def test_a_full_org_adds_nobody_to_a_seat_through_the_store(fleet):
    """`flotta user add` and the operator's own adds."""
    _limit(fleet, seats=1)
    with FleetStore(fleet) as store:
        with pytest.raises(LimitReachedError):
            store.add_person("bo@example.com", "agency-x", "member")
        assert store.get_user_by_email("bo@example.com") is None
        # A client's person is free.
        store.add_person("cy@acme.test", "agency-x", "client_user", client_id="acme")


# -- lowering, raising, and none --------------------------------------------------


def test_a_limit_below_what_an_org_holds_takes_nothing_away(api, fleet):
    answer = api.put(
        "/api/orgs/agency-x/limits", json={"clients": 0, "agents": 0}, headers=OPERATOR
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["over"] == {"clients": True, "seats": False, "agents": True}
    assert _counts(fleet) == {"clients": 1, "seats": 1, "agents": 1}


def test_raising_a_limit_lets_the_same_call_through(api, fleet):
    _limit(fleet, clients=1)
    body = {"id": "beta", "name": "Beta"}
    assert api.post("/api/orgs/agency-x/clients", json=body, headers=OWNER).status_code == 402
    _limit(fleet, clients=2)
    assert api.post("/api/orgs/agency-x/clients", json=body, headers=OWNER).status_code == 201


def test_null_is_no_limit_and_a_kind_left_out_is_kept(api, fleet):
    api.put("/api/orgs/agency-x/limits", json={"clients": 3, "seats": 2}, headers=OPERATOR)
    answer = api.put("/api/orgs/agency-x/limits", json={"clients": None}, headers=OPERATOR)
    assert answer.json()["limits"] == {"clients": None, "seats": 2, "agents": None}


def test_every_org_starts_unlimited(fleet):
    with FleetStore(fleet) as store:
        for org in store.list_orgs():
            assert (org.max_clients, org.max_seats, org.max_agents) == (None, None, None)


@pytest.mark.parametrize(
    "body",
    [{"clients": -1}, {"clients": 1.5}, {"clients": "5"}, {"clients": True}, {"tokens": 3}],
)
def test_a_limit_is_a_whole_number_from_zero(api, body):
    answer = api.put("/api/orgs/agency-x/limits", json=body, headers=OPERATOR)
    assert answer.status_code == 422, answer.text


def test_an_unknown_org_has_no_limits(api):
    assert (
        api.put("/api/orgs/nope/limits", json={"clients": 1}, headers=OPERATOR).status_code == 404
    )
    assert api.get("/api/orgs/nope/limits", headers=OPERATOR).status_code == 404


# -- who reads and sets them ------------------------------------------------------


def test_an_orgs_owner_reads_its_limits_and_cannot_set_them(api, fleet):
    _limit(fleet, clients=5)
    read = api.get("/api/orgs/agency-x/limits", headers=OWNER)
    assert read.status_code == 200, read.text
    assert read.json()["limits"]["clients"] == 5
    assert (
        api.put("/api/orgs/agency-x/limits", json={"clients": 99}, headers=OWNER).status_code == 403
    )


def test_a_client_and_another_org_see_no_limits(api, fleet):
    with FleetStore(fleet) as store:
        store.create_org("agency-y", "Agency Y")
    other = _bearer({SCOPE_FLEET_READ, SCOPE_ORG_ADMIN}, org="agency-y")
    assert api.get("/api/orgs/agency-x/limits", headers=CLIENT_VIEWER).status_code == 404
    assert api.get("/api/orgs/agency-x/limits", headers=other).status_code == 404


def test_the_operator_lists_every_orgs_limits(api, fleet):
    _limit(fleet, seats=1)
    orgs = {o["id"]: o for o in api.get("/api/orgs", headers=OPERATOR).json()["orgs"]}
    assert orgs["agency-x"]["max_seats"] == 1
    assert orgs["agency-x"]["max_clients"] is None


# -- two at once --------------------------------------------------------------------


def test_two_agents_racing_for_the_last_slot_get_one(fleet):
    """BEGIN IMMEDIATE on SQLite, a table lock on Postgres: the count and the
    insert are one step, so only one of two concurrent creates fits."""
    _limit(fleet, agents=2)
    results: list[str] = []
    start = threading.Barrier(2)

    def create(name):
        with FleetStore(fleet) as store:
            start.wait()
            try:
                store.create_box(name, org="agency-x", client="acme")
                results.append("made")
            except LimitReachedError:
                results.append("refused")

    threads = [threading.Thread(target=create, args=(n,)) for n in ("sales", "ops")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["made", "refused"]
    assert _counts(fleet)["agents"] == 2
