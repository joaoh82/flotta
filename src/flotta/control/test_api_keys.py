"""API keys (FLOTTA-150, M14.5): a client's own systems start one agent's runs.

The guards first. A key for one agent reaches nothing of another's through any
route there is, a revoked key is refused on its next request, the store holds
no key, and a key opens no conversation, here or at the door.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from flotta.auth import (
    SCOPE_BOX_CHAT,
    SCOPE_FLEET_READ,
    SCOPE_FLEET_WRITE,
    SCOPE_RUN_CREATE,
    AuthError,
    mint,
)
from flotta.control import test_tenancy
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.control.test_route_scope import OPEN_ROUTES
from flotta.control.test_tenancy import KEY, X, Y, _bearer, _ids
from flotta.store import FleetStore
from flotta.test_accounts import _every_value

AGENCY_X = _bearer(SCOPE_FLEET_WRITE, SCOPE_FLEET_READ, org="agency-x")
AGENCY_Y = _bearer(SCOPE_FLEET_WRITE, SCOPE_FLEET_READ, org="agency-y")
#: Another agent of the same client as `X`: the nearest thing a key must not reach.
SIBLING = "triage--acme--agency-x"

fleet_base = test_tenancy.fleet
no_substrate = test_tenancy.no_substrate

#: The only routes a key's request may succeed on, besides the open ones.
KEY_ROUTES = frozenset({"create_run", "list_box_runs", "get_run"})
#: About the caller itself: a key may ask what it is.
ABOUT_THE_CALLER = frozenset({"me"})


@pytest.fixture
def fleet(fleet_base):
    with FleetStore(fleet_base) as store:
        sibling = store.create_box("triage", org="agency-x", client="acme")
        store.update_box_status(sibling.id, "running", endpoint="fly://flotta-triage/m1")
    return fleet_base


def _client(fleet, signing_key=KEY):
    return TestClient(
        create_app(
            store_factory=lambda: FleetStore(fleet),
            run_loop=False,
            background=False,
            signing_key=signing_key,
            reachable=_unchecked,
            known_model=_uncatalogued,
        )
    )


@pytest.fixture
def api(fleet):
    with _client(fleet) as client:
        yield client


def _key(api, box=X, headers=AGENCY_X, name="crm"):
    made = api.post(f"/api/boxes/{box}/keys", json={"name": name}, headers=headers)
    assert made.status_code == 201, made.text
    return made.json()


def _with(secret):
    return {"Authorization": f"Bearer {secret}"}


def _runs(fleet, name):
    with FleetStore(fleet) as store:
        box = store.get_box_by_name(name)
        return store.list_runs(org=box.org, box=box.id)


# -- the guards ----------------------------------------------------------------


def _targets(api, fleet, name):
    """Values for every path parameter, all naming `name`'s things, and the
    key's own org and client: a route about the client, not one agent, is
    still not the key's (found by reintroducing `list_client_runs` to keys)."""
    ids = _ids(fleet)
    box = ids[name]
    run = api.post(f"/api/boxes/{box}/runs", json={"prompt": "Theirs."}, headers=AGENCY_X).json()
    trigger = api.post(
        f"/api/boxes/{box}/triggers", json={"kind": "webhook", "prompt": "x"}, headers=AGENCY_X
    ).json()
    key = _key(api, box=box, name="theirs")
    return {
        "box_id": box,
        "run_id": run["id"],
        "trigger_id": trigger["id"],
        "key_id": key["id"],
        "client_id": "acme",
        "org_id": "agency-x",
    }


def _every_route(api):
    for route in api.app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods - {"HEAD"}):
            yield method, route


def _sweep(api, secret, targets):
    """Every route there is, called with the key: `{(method, route name): status}`."""
    answers = {}
    for method, route in _every_route(api):
        path = route.path
        for param in route.param_convertors:
            path = path.replace(f"{{{param}}}", targets.get(param, "x"))
            path = path.replace(f"{{{param}:path}}", targets.get(param, "x"))
        body = {"prompt": "Send me their files.", "name": "mine now", "input": {"a": 1}}
        response = api.request(
            method,
            path,
            json=body if method in {"POST", "PUT", "PATCH"} else None,
            headers=_with(secret),
        )
        answers[(method, route.name)] = response.status_code
    return answers


def test_a_key_reaches_nothing_of_another_agent_through_any_route(api, fleet, no_substrate):
    """The guard, swept as the tenancy ratchet sweeps tokens: a key for `X`,
    sent to every route with every path naming its sibling's agent, run,
    trigger or key. Nothing succeeds and nothing of the sibling's changes."""
    secret = _key(api)["key"]
    theirs = _targets(api, fleet, SIBLING)
    before = [r.id for r in _runs(fleet, SIBLING)]

    answers = _sweep(api, secret, theirs)

    succeeded = {
        name: status
        for (_, name), status in answers.items()
        if 200 <= status < 300 and name not in OPEN_ROUTES | ABOUT_THE_CALLER
    }
    assert succeeded == {}
    refused = {s for (_, n), s in answers.items() if n not in OPEN_ROUTES | ABOUT_THE_CALLER}
    assert refused <= {401, 403, 404, 405}, answers
    assert [r.id for r in _runs(fleet, SIBLING)] == before
    with FleetStore(fleet) as store:
        sibling = store.get_box_by_name(SIBLING)
        assert len(store.list_api_keys(sibling.id)) == 1
        assert store.get_trigger(theirs["trigger_id"]) is not None
        assert store.get_api_key(theirs["key_id"]).revoked_at is None


def test_on_its_own_agent_a_key_starts_and_reads_runs_and_does_nothing_else(
    api, fleet, no_substrate
):
    """The same sweep against the key's own agent: the three run routes, and
    every other route refused, its triggers and keys included."""
    secret = _key(api)["key"]
    mine = _targets(api, fleet, X)

    answers = _sweep(api, secret, mine)

    succeeded = {
        name
        for (_, name), status in answers.items()
        if 200 <= status < 300 and name not in OPEN_ROUTES | ABOUT_THE_CALLER
    }
    assert succeeded == KEY_ROUTES
    with FleetStore(fleet) as store:
        assert store.get_trigger(mine["trigger_id"]) is not None
        assert store.get_api_key(mine["key_id"]).revoked_at is None


def test_a_key_starts_a_run_on_its_agent_and_reads_it_back(api, fleet):
    made = _key(api)

    started = api.post(
        f"/api/boxes/{X}/runs",
        json={"prompt": "Qualify the lead.", "input": {"title": "New lead"}},
        headers=_with(made["key"]),
    )

    assert started.status_code == 202, started.text
    run = started.json()
    assert (run["trigger_kind"], run["box_id"]) == ("api", _ids(fleet)[X])
    read = api.get(f"/api/runs/{run['id']}", headers=_with(made["key"]))
    assert read.status_code == 200 and read.json()["input"] == {"title": "New lead"}
    listed = api.get(f"/api/boxes/{X}/runs", headers=_with(made["key"])).json()["runs"]
    assert [r["id"] for r in listed] == [run["id"]]
    with FleetStore(fleet) as store:
        (event,) = [e for e in store.get_events("task", run["id"]) if e.type == "created"]
    assert event.payload["by"] == f"key:{made['id']}"


def test_a_key_is_refused_on_another_agent_and_in_another_org(api, fleet):
    secret = _key(api)["key"]

    for box in (SIBLING, _ids(fleet)[SIBLING], Y, "eng-a"):
        answer = api.post(f"/api/boxes/{box}/runs", json={"prompt": "x"}, headers=_with(secret))
        assert answer.status_code == 404, (box, answer.text)
    assert _runs(fleet, SIBLING) == []


def test_a_revoked_key_is_refused_on_its_next_request(api, fleet):
    made = _key(api)
    assert api.get(f"/api/boxes/{X}/runs", headers=_with(made["key"])).status_code == 200

    gone = api.delete(f"/api/keys/{made['id']}", headers=AGENCY_X)

    assert gone.status_code == 204
    answer = api.get(f"/api/boxes/{X}/runs", headers=_with(made["key"]))
    assert answer.status_code == 401
    assert "revoked" in answer.json()["detail"]
    listed = api.get(f"/api/boxes/{X}/keys", headers=AGENCY_X).json()["keys"]
    assert listed[0]["revoked_at"] is not None


def test_a_destroyed_agents_key_is_refused(api, fleet):
    made = _key(api)
    with FleetStore(fleet) as store:
        store.update_box_status(_ids(fleet)[X], "torn_down")

    answer = api.post(f"/api/boxes/{X}/runs", json={"prompt": "x"}, headers=_with(made["key"]))

    assert answer.status_code == 401


def test_no_key_is_stored(api, fleet):
    """The guard: a table scan finds no key, as for sessions (`test_accounts.py`)."""
    made = _key(api)
    api.get(f"/api/boxes/{X}/runs", headers=_with(made["key"]))

    with FleetStore(fleet) as store:
        stored = _every_value(store)
    assert not any(made["key"] in value for value in stored), "an API key was written to the store"
    assert made["prefix"] and made["key"].startswith(made["prefix"])


def test_a_key_opens_no_conversation(api, fleet):
    """Not a ticket, not the door's address, not a wake: a key is not `box:chat`."""
    secret = _key(api)["key"]

    for method, path in (
        ("POST", f"/api/boxes/{X}/ticket"),
        ("GET", f"/api/boxes/{X}/address"),
        ("POST", f"/api/boxes/{X}/wake"),
    ):
        answer = api.request(method, path, headers=_with(secret))
        assert answer.status_code == 403, (path, answer.text)


def test_the_door_refuses_a_key():
    from flotta.door.app import BoxUnavailable, authorize

    secret = "flotta_key_" + "a" * 43

    for kwargs in ({"header": f"Bearer {secret}"}, {"header": None, "query_token": secret}):
        with pytest.raises(BoxUnavailable) as refused:
            authorize(key=KEY, allow_query=True, **kwargs)
        assert refused.value.status == 401


def test_a_key_cannot_make_or_revoke_a_key(api):
    made = _key(api)

    assert (
        api.post(f"/api/boxes/{X}/keys", json={"name": "x"}, headers=_with(made["key"])).status_code
        == 403
    )
    assert api.delete(f"/api/keys/{made['id']}", headers=_with(made["key"])).status_code == 403


def test_only_the_agency_manages_keys(api, fleet):
    """A client's people hold no `fleet:write`; another org's key is a 404."""
    made = _key(api)
    viewer = _bearer(SCOPE_FLEET_READ, org="agency-x", client="acme")
    user = _bearer(SCOPE_FLEET_READ, SCOPE_BOX_CHAT, org="agency-x", client="acme")

    for headers in (viewer, user):
        assert api.get(f"/api/boxes/{X}/keys", headers=headers).status_code == 403
        assert (
            api.post(f"/api/boxes/{X}/keys", json={"name": "x"}, headers=headers).status_code == 403
        )
    assert api.delete(f"/api/keys/{made['id']}", headers=AGENCY_Y).status_code == 404
    assert api.get(f"/api/boxes/{X}/keys", headers=AGENCY_Y).status_code == 404


def test_a_key_is_shown_once(api):
    made = _key(api)

    assert made["key"].startswith("flotta_key_") and made["name"] == "crm"
    (listed,) = api.get(f"/api/boxes/{X}/keys", headers=AGENCY_X).json()["keys"]
    assert "key" not in listed and "secret_hash" not in listed
    assert listed["prefix"] == made["prefix"] and listed["last_used_at"] is None


def test_a_key_notes_when_it_was_used(api):
    made = _key(api)

    api.get(f"/api/boxes/{X}/runs", headers=_with(made["key"]))

    (listed,) = api.get(f"/api/boxes/{X}/keys", headers=AGENCY_X).json()["keys"]
    assert listed["last_used_at"] is not None


@pytest.mark.parametrize("name", ["", "   ", "x" * 65])
def test_a_key_needs_a_name(api, name):
    answer = api.post(f"/api/boxes/{X}/keys", json={"name": name}, headers=AGENCY_X)

    assert answer.status_code == 422


def test_run_create_is_never_minted():
    """No role holds it and no token carries it: a request with it was made
    with a key, and so is confined to the key's agent."""
    with pytest.raises(AuthError, match="API key"):
        mint(subject="crm", scopes={SCOPE_RUN_CREATE}, key=KEY)
    with pytest.raises(AuthError, match="API key"):
        mint(subject="key:key-1", scopes={SCOPE_FLEET_READ}, key=KEY)


def test_a_key_is_not_a_token():
    from flotta.auth import verify

    with pytest.raises(AuthError, match="API key"):
        verify("flotta_key_" + "a" * 43, key=KEY)


def test_without_a_signing_key_a_key_is_still_its_one_agent(fleet):
    """A keyless control plane is the operator's on loopback, and a request
    with no credential is the operator. One with a key is still the key's."""
    with FleetStore(fleet) as store:
        _, secret = store.create_api_key(_ids(fleet)[X], name="crm")
    with _client(fleet, signing_key=None) as keyless:
        mine = keyless.post(f"/api/boxes/{X}/runs", json={"prompt": "x"}, headers=_with(secret))
        theirs = keyless.post(f"/api/boxes/{Y}/runs", json={"prompt": "x"}, headers=_with(secret))
        listed = keyless.get("/api/boxes", headers=_with(secret))

    assert mine.status_code == 202, mine.text
    assert theirs.status_code == 404
    assert listed.status_code == 403
