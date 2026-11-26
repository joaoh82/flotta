"""An agency's runs in one list, and what its agents are doing now (FLOTTA-250).

Two reads the Architect needs and no route gave a person: runs were listed per
agent or per client, and what an agent is doing was the door's alone. Both keep
to the caller's agency, to one client for someone confined to it, and to what
a client's viewer may read of a run (D29).
"""

from __future__ import annotations

import pytest

from flotta.auth import (
    SCOPE_BOX_CHAT,
    SCOPE_FLEET_READ,
    SCOPE_FLEET_WRITE,
    SCOPE_PORTAL_READ,
)
from flotta.control import test_tenancy
from flotta.control.test_tenancy import X, Y, _bearer, _ids
from flotta.store import FleetStore

api = test_tenancy.api

OWNER = _bearer(SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, org="agency-x")
OTHER_OWNER = _bearer(SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, org="agency-y")
#: A builder the agency confined to one client.
GLOBEX_MEMBER = _bearer(SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, org="agency-x", client="globex")
ACME_VIEWER = _bearer(SCOPE_FLEET_READ, SCOPE_PORTAL_READ, org="agency-x", client="acme")
ACME_USER = _bearer(SCOPE_FLEET_READ, SCOPE_BOX_CHAT, org="agency-x", client="acme")
#: Agency X's second client's agent.
G = "desk--globex--agency-x"


@pytest.fixture
def fleet(tmp_path):
    """`test_tenancy`'s two agencies, and a second client with an agent in the first."""
    path = test_tenancy.fleet.__wrapped__(tmp_path)
    with FleetStore(path) as store:
        store.create_client("agency-x", "globex", "Globex")
        box = store.create_box("desk", org="agency-x", client="globex")
        store.update_box_status(box.id, "running", endpoint="fly://app/desk")
    return path


def _run(fleet, box, prompt="Summarise yesterday's tickets", *, kind="api", running=False, **more):
    with FleetStore(fleet) as store:
        run = store.create_run(_ids(fleet)[box], prompt, trigger_kind=kind, **more)
        if running:
            store.start_run(run.id)
        return run.id


def _listed(api, headers, **params):
    answer = api.get("/api/runs", params=params, headers=headers)
    assert answer.status_code == 200, answer.text
    return [r["id"] for r in answer.json()["runs"]]


def _working(api, headers, **params):
    answer = api.get("/api/working", params=params, headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()["working"]


# -- the agency's runs, in one list -----------------------------------------------


def test_an_agencys_runs_are_one_list_across_its_clients(api, fleet):
    from datetime import UTC, datetime

    first = _run(fleet, X, now=datetime(2026, 11, 1, tzinfo=UTC))
    second = _run(fleet, G, now=datetime(2026, 11, 2, tzinfo=UTC))
    theirs = _run(fleet, Y)

    assert _listed(api, OWNER) == [second, first]
    assert _listed(api, OTHER_OWNER) == [theirs]
    assert _listed(api, OWNER, limit=1) == [second]


def test_each_listed_run_says_whose_it_is_and_what_it_cost(api, fleet):
    run = _run(fleet, G)
    (listed,) = api.get("/api/runs", headers=OWNER).json()["runs"]

    assert (listed["id"], listed["client"], listed["box_id"]) == (run, "globex", _ids(fleet)[G])
    assert listed["prompt"] == "Summarise yesterday's tickets"
    assert listed["cost"]["eur_nano"] == 0


def test_the_list_takes_a_moment_and_how_runs_stand(api, fleet):
    from datetime import UTC, datetime

    old = _run(fleet, X, now=datetime(2026, 1, 5, tzinfo=UTC))
    waiting = _run(fleet, X)
    under_way = _run(fleet, G, running=True)
    with FleetStore(fleet) as store:
        failed = store.create_run(_ids(fleet)[X], "Broke", trigger_kind="api")
        store.start_run(failed.id)
        store.finish_run(failed.id, "failed", error_class="agent")

    assert old not in _listed(api, OWNER, since="2026-02-01")
    assert sorted(_listed(api, OWNER, status="pending,running")) == sorted(
        [old, waiting, under_way]
    )
    assert _listed(api, OWNER, status="failed") == [failed.id]
    assert api.get("/api/runs", params={"status": "nope"}, headers=OWNER).status_code == 422
    assert api.get("/api/runs", params={"since": "soon"}, headers=OWNER).status_code == 422


def test_someone_confined_to_a_client_lists_that_clients_runs_only(api, fleet):
    _run(fleet, X)
    theirs = _run(fleet, G)

    assert _listed(api, GLOBEX_MEMBER) == [theirs]


def test_a_clients_viewer_lists_its_own_without_the_agencys_words_or_any_conversation(api, fleet):
    run = _run(fleet, X)
    _run(fleet, G)
    with FleetStore(fleet) as store:
        store.open_chat_run(_ids(fleet)[X], "Hello.")

    listed = api.get("/api/runs", headers=ACME_VIEWER)

    assert listed.status_code == 200, listed.text
    assert [r["id"] for r in listed.json()["runs"]] == [run]
    assert "prompt" not in listed.json()["runs"][0]
    assert "cost" not in listed.json()["runs"][0]


def test_a_clients_user_and_nobody_at_all_list_no_runs(api, fleet):
    _run(fleet, X)

    assert api.get("/api/runs", headers=ACME_USER).status_code == 403
    assert api.get("/api/runs").status_code == 401
    assert api.get("/api/working", headers=ACME_USER).status_code == 403
    assert api.get("/api/working").status_code == 401


# -- what each agent is doing now -------------------------------------------------


def test_agents_doing_nothing_are_not_listed(api, fleet):
    _run(fleet, X)  # waiting, not under way

    assert _working(api, OWNER) == []


def test_each_agent_with_a_run_under_way_is_listed_with_what_it_is_about(api, fleet):
    run = _run(fleet, X, "\nMorning report: check the orders feed\nand more", running=True)
    other = _run(fleet, G, "Chase the unpaid invoices", running=True)
    _run(fleet, Y, "Another agency's work", running=True)

    working = {w["box"]: w for w in _working(api, OWNER)}

    assert sorted(working) == sorted([G, X])
    assert working[X] == {
        "box_id": _ids(fleet)[X],
        "box": X,
        "client": "acme",
        "run_id": run,
        "trigger_kind": "api",
        "about": "Morning report: check the orders feed",
        "started_at": working[X]["started_at"],
    }
    assert working[X]["started_at"]
    assert (working[G]["run_id"], working[G]["about"]) == (other, "Chase the unpaid invoices")


def test_a_requests_run_is_named_by_the_request(api, fleet):
    with FleetStore(fleet) as store:
        filed = store.file_request(
            _ids(fleet)[X],
            title="Adjust the columnists' banners",
            details="",
            verdict=None,
            judge_error=None,
            within=None,
            approved=True,
            why="small",
        )
    _run(fleet, X, "The agency approved…", kind="request", trigger_id=filed.id, running=True)

    assert [w["about"] for w in _working(api, OWNER)] == ["Adjust the columnists' banners"]


def test_a_conversation_is_said_to_be_one_with_nothing_of_what_was_said(api, fleet):
    with FleetStore(fleet) as store:
        store.open_chat_run(_ids(fleet)[X], "My card number is 4242, can you check it?")

    answer = api.get("/api/working", headers=OWNER)

    (working,) = answer.json()["working"]
    assert (working["box"], working["trigger_kind"], working["about"]) == (X, "chat", "")
    assert "4242" not in answer.text


def test_one_agent_is_asked_about_by_name_or_id(api, fleet):
    run = _run(fleet, X, running=True)
    _run(fleet, G, running=True)

    for ref in (X, _ids(fleet)[X]):
        assert [w["run_id"] for w in _working(api, OWNER, box=ref)] == [run]
    # Another agency's agent is not there to ask about, and neither is one that does not exist.
    for ref in (Y, _ids(fleet)[Y], "no-such-agent"):
        assert api.get("/api/working", params={"box": ref}, headers=OWNER).status_code == 404


def test_someone_confined_to_a_client_is_told_of_that_clients_agents_only(api, fleet):
    _run(fleet, X, running=True)
    theirs = _run(fleet, G, running=True)

    assert [w["run_id"] for w in _working(api, GLOBEX_MEMBER)] == [theirs]
    assert api.get("/api/working", params={"box": X}, headers=GLOBEX_MEMBER).status_code == 404


def test_a_clients_viewer_is_told_an_agent_is_working_and_not_on_what(api, fleet):
    run = _run(fleet, X, "Quietly chase Acme's late payment", running=True)
    _run(fleet, G, running=True)

    answer = api.get("/api/working", headers=ACME_VIEWER)

    assert answer.status_code == 200, answer.text
    (working,) = answer.json()["working"]
    assert (working["run_id"], working["box"], working["about"]) == (run, X, None)
    assert "late payment" not in answer.text


def test_a_clients_viewer_is_told_of_no_conversation(api, fleet):
    with FleetStore(fleet) as store:
        store.open_chat_run(_ids(fleet)[X], "Hello.")

    assert _working(api, ACME_VIEWER) == []
