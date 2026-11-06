"""The runs API (FLOTTA-147, M14.2): ask an agent for work, and read what it did.

Every route here is also in the tenancy sweeps (`BOX_ROUTES` in
`test_tenancy.py`, `test_route_scope.py`), which hold the 404 for another
org's agent. These are the answers that are particular to runs: who may ask
for one, and that a client's people see none (D21).
"""

from __future__ import annotations

import pytest

from flotta.auth import SCOPE_BOX_CHAT, SCOPE_FLEET_READ, SCOPE_FLEET_WRITE
from flotta.control import test_tenancy
from flotta.control.test_tenancy import KEY, X, Y, _bearer, _ids
from flotta.runs import MAX_INPUT
from flotta.store import FleetStore

AGENCY = {"org": "agency-x"}

#: `test_tenancy`'s fleet, two agencies with a `support` agent each, and its app.
fleet = test_tenancy.fleet
api = test_tenancy.api


def _ask(api, box=X, headers=None, **body):
    body.setdefault("prompt", "Summarise yesterday's tickets")
    return api.post(
        f"/api/boxes/{box}/runs",
        json=body,
        headers=headers or _bearer(SCOPE_FLEET_WRITE, **AGENCY),
    )


def test_asking_for_a_run_queues_it(api, fleet):
    answer = _ask(api, input={"ticket": 42})

    assert answer.status_code == 202, answer.text
    run = answer.json()
    assert (run["status"], run["trigger_kind"], run["input"]) == ("pending", "api", {"ticket": 42})
    assert run["box_id"] == _ids(fleet)[X]
    with FleetStore(fleet) as store:
        assert store.get_run(run["id"]).prompt == "Summarise yesterday's tickets"


def test_a_run_is_read_with_what_it_cost(api):
    run = _ask(api).json()

    read = api.get(f"/api/runs/{run['id']}", headers=_bearer(SCOPE_FLEET_READ, **AGENCY))

    assert read.status_code == 200, read.text
    assert read.json()["id"] == run["id"]
    assert read.json()["cost"] == {
        "eur_nano": 0,
        "unpriced": 0,
        "calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
    }


def test_runs_are_listed_by_agent_and_by_client(api):
    run = _ask(api).json()
    reader = _bearer(SCOPE_FLEET_READ, **AGENCY)

    by_box = api.get(f"/api/boxes/{X}/runs", headers=reader)
    by_client = api.get("/api/clients/acme/runs", headers=reader)

    assert [r["id"] for r in by_box.json()["runs"]] == [run["id"]]
    assert [r["id"] for r in by_client.json()["runs"]] == [run["id"]]


def test_another_orgs_run_is_not_there(api):
    """Read by id or listed by a client with the same name in another org."""
    run = _ask(api).json()
    stranger = _bearer(SCOPE_FLEET_READ, org="agency-y")

    assert api.get(f"/api/runs/{run['id']}", headers=stranger).status_code == 404
    assert api.get("/api/clients/acme/runs", headers=stranger).json()["runs"] == []


def test_a_sibling_clients_token_does_not_reach_the_run(api, fleet):
    with FleetStore(fleet) as store:
        store.create_client("agency-x", "globex", "Globex")
    run = _ask(api).json()
    sibling = _bearer(SCOPE_FLEET_WRITE, SCOPE_FLEET_READ, org="agency-x", client="globex")

    assert api.get(f"/api/runs/{run['id']}", headers=sibling).status_code == 404
    assert api.get("/api/clients/acme/runs", headers=sibling).status_code == 404


def test_a_clients_system_asks_for_and_reads_its_own_runs(api):
    """A token for one client's integration, which M14.5 will hand out."""
    own = _bearer(SCOPE_FLEET_WRITE, SCOPE_FLEET_READ, org="agency-x", client="acme")

    run = _ask(api, headers=own).json()

    assert api.get(f"/api/runs/{run['id']}", headers=own).status_code == 200


@pytest.mark.parametrize(
    "scopes",
    [{SCOPE_FLEET_READ, SCOPE_BOX_CHAT}, {SCOPE_FLEET_READ}],
    ids=["client_user", "client_viewer"],
)
def test_a_clients_people_neither_ask_for_runs_nor_see_them(api, scopes):
    run = _ask(api).json()
    person = _bearer(*scopes, org="agency-x", client="acme")

    assert _ask(api, headers=person).status_code == 403
    for path in (f"/api/runs/{run['id']}", f"/api/boxes/{X}/runs", "/api/clients/acme/runs"):
        assert api.get(path, headers=person).status_code == 403, path


def test_a_run_needs_something_to_do(api):
    assert _ask(api, prompt="").status_code == 422
    assert _ask(api, prompt=["not", "text"]).status_code == 422


def test_a_runs_input_has_a_limit(api):
    answer = _ask(api, input={"body": "x" * MAX_INPUT})

    assert answer.status_code == 422
    assert "input" in answer.json()["detail"]


def test_a_run_token_reaches_no_other_agent_through_the_fleet_api(api, fleet):
    """The door is not the only place a `box:chat` token is good for."""
    from flotta import runner

    ids = _ids(fleet)
    with FleetStore(fleet) as store:
        box = store.get_box(ids[X])
        other = store.create_box("intake", org="agency-x", client="acme")
        run = store.create_run(box.id, "hi", trigger_kind="api")
    token = runner.run_token(run, box, key=KEY, ttl_s=300)
    headers = {"Authorization": f"Bearer {token}"}

    assert api.get(f"/api/boxes/{other.id}/address", headers=headers).status_code == 404
    assert api.get(f"/api/boxes/{box.id}/address", headers=headers).status_code == 200


def test_an_agent_in_another_org_is_not_asked(api):
    assert _ask(api, box=Y).status_code == 404
