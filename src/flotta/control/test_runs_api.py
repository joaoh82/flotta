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


def test_listed_runs_say_what_each_cost(api, fleet):
    """The web app's runs list shows a cost per run, and asking each run for
    it would be a request per row (M14.7)."""
    from decimal import Decimal

    run = _ask(api).json()
    with FleetStore(fleet) as store:
        store.record_usage(
            _ids(fleet)[X],
            model="some/model",
            upstream="openrouter",
            tokens_in=10,
            tokens_out=5,
            idempotency_key="call-1",
            eur_per_usd=Decimal("1"),
            reported_cost_nano=3_000_000,
            run_id=run["id"],
        )
    reader = _bearer(SCOPE_FLEET_READ, **AGENCY)
    one = api.get(f"/api/runs/{run['id']}", headers=reader).json()["cost"]

    for path in (f"/api/boxes/{X}/runs", "/api/clients/acme/runs"):
        listed = api.get(path, headers=reader).json()["runs"]
        assert listed[0]["cost"] == one, path
    assert one["eur_nano"] == 3_000_000 and one["calls"] == 1


def test_runs_are_listed_from_a_moment_on(api, fleet):
    from datetime import UTC, datetime

    with FleetStore(fleet) as store:
        old = store.create_run(
            _ids(fleet)[X], "Last month's", trigger_kind="api", now=datetime(2026, 1, 5, tzinfo=UTC)
        )
    new = _ask(api).json()
    reader = _bearer(SCOPE_FLEET_READ, **AGENCY)

    for path in (f"/api/boxes/{X}/runs", "/api/clients/acme/runs"):
        every = [r["id"] for r in api.get(path, headers=reader).json()["runs"]]
        since_day = api.get(f"{path}?since=2026-02-01", headers=reader).json()["runs"]
        since_moment = api.get(f"{path}?since=2026-01-05T00:00:01%2B00:00", headers=reader).json()[
            "runs"
        ]
        assert every == [new["id"], old.id], path
        assert [r["id"] for r in since_day] == [new["id"]], path
        assert [r["id"] for r in since_moment] == [new["id"]], path


@pytest.mark.parametrize("since", ["last week", "2026-02-01T08:00"])
def test_a_moment_that_is_not_one_is_refused(api, since):
    """No zone is no particular moment, as in the usage report."""
    reader = _bearer(SCOPE_FLEET_READ, **AGENCY)

    for path in (f"/api/boxes/{X}/runs", "/api/clients/acme/runs"):
        answer = api.get(path, params={"since": since}, headers=reader)
        assert answer.status_code == 422, (path, answer.text)
        assert "since" in answer.json()["detail"]


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


# -- answering a run's approval (FLOTTA-124's rules) ---------------------------


def _waiting(fleet, choices=("once", "session", "always", "deny")):
    """A run of agency-x's `support` agent, waiting on one approval."""
    from datetime import UTC, datetime, timedelta

    with FleetStore(fleet) as store:
        run = store.create_run(_ids(fleet)[X], "Tidy up", trigger_kind="api")
        store.start_run(run.id)
        approval = store.ask_approval(
            run.id,
            request_id="srq-1",
            command="rm -rf /tmp/flotta-standin-probe",
            description="delete in root path",
            choices=list(choices),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
    return run, approval


def _allow_clients(fleet):
    with FleetStore(fleet) as store:
        store.set_clients_answer_approvals(_ids(fleet)[X], True)


CLIENT_USER = (SCOPE_FLEET_READ, SCOPE_BOX_CHAT)


def _answer(api, run, choice, headers=None, **extra):
    return api.post(
        f"/api/runs/{run.id}/approval",
        json={"choice": choice, **extra},
        headers=headers or _bearer(SCOPE_FLEET_WRITE, **AGENCY),
    )


def test_the_agency_answers_with_any_choice_the_agent_offered(api, fleet):
    run, approval = _waiting(fleet)

    answer = _answer(api, run, "always")

    assert answer.status_code == 200, answer.text
    assert (answer.json()["status"], answer.json()["choice"]) == ("answered", "always")


def test_a_run_is_read_as_waiting_until_it_is_answered(api, fleet):
    run, approval = _waiting(fleet)
    reader = _bearer(SCOPE_FLEET_READ, **AGENCY)

    before = api.get(f"/api/runs/{run.id}", headers=reader).json()
    _answer(api, run, "once")
    after = api.get(f"/api/runs/{run.id}", headers=reader).json()

    assert before["waiting_approval"] is True
    assert before["approvals"][0]["command"] == "rm -rf /tmp/flotta-standin-probe"
    assert after["waiting_approval"] is False


def test_an_approval_is_answered_once(api, fleet):
    run, _ = _waiting(fleet)

    assert _answer(api, run, "once").status_code == 200
    assert _answer(api, run, "deny").status_code == 409


def test_a_choice_the_agent_did_not_offer_is_refused(api, fleet):
    run, _ = _waiting(fleet, choices=("once", "deny"))

    answer = _answer(api, run, "always")

    assert answer.status_code == 422
    assert "once" in answer.json()["detail"]


def test_a_clients_person_answers_only_where_the_agency_allows_it(api, fleet):
    run, _ = _waiting(fleet)
    person = _bearer(*CLIENT_USER, org="agency-x", client="acme")

    refused = _answer(api, run, "once", headers=person)
    _allow_clients(fleet)
    always = _answer(api, run, "always", headers=person)
    once = _answer(api, run, "once", headers=person)

    assert refused.status_code == 403
    assert always.status_code == 422, "never 'always' from a client's people"
    assert once.status_code == 200


def test_a_client_viewer_answers_nothing(api, fleet):
    run, _ = _waiting(fleet)
    _allow_clients(fleet)

    answer = _answer(api, run, "once", headers=_bearer(SCOPE_FLEET_READ, **AGENCY, client="acme"))

    assert answer.status_code == 403


def test_another_orgs_approval_is_not_there(api, fleet):
    run, _ = _waiting(fleet)

    answer = _answer(api, run, "once", headers=_bearer(SCOPE_FLEET_WRITE, org="agency-y"))

    assert answer.status_code == 404


def test_an_agents_own_run_token_answers_nothing(api, fleet):
    """The run's token holds `box:chat` on that agent: still not a person."""
    from flotta import runner

    run, _ = _waiting(fleet)
    with FleetStore(fleet) as store:
        box = store.get_box(run.box_id)
    token = runner.run_token(run, box, key=KEY, ttl_s=300)
    _allow_clients(fleet)

    answer = _answer(api, run, "once", headers={"Authorization": f"Bearer {token}"})

    assert answer.status_code == 403


def test_nothing_waiting_is_said(api, fleet):
    run = _ask(api).json()

    answer = api.post(
        f"/api/runs/{run['id']}/approval",
        json={"choice": "once"},
        headers=_bearer(SCOPE_FLEET_WRITE, **AGENCY),
    )

    assert answer.status_code == 409


def test_waiting_approvals_are_listed_for_whoever_may_answer_them(api, fleet):
    run, approval = _waiting(fleet)
    agency = _bearer(SCOPE_FLEET_WRITE, **AGENCY)
    person = _bearer(*CLIENT_USER, org="agency-x", client="acme")
    stranger = _bearer(SCOPE_FLEET_WRITE, org="agency-y")

    def listed(headers):
        return [a["id"] for a in api.get("/api/approvals", headers=headers).json()["approvals"]]

    assert listed(agency) == [approval.id]
    assert listed(person) == [], "not until the agency lets the client answer"
    assert listed(stranger) == []
    _allow_clients(fleet)
    assert listed(person) == [approval.id]
    _answer(api, run, "once")
    assert listed(agency) == []


def test_only_those_who_may_answer_list_what_waits(api, fleet):
    """The list is of what the caller can answer: a reader who can't, sees none."""
    _waiting(fleet)

    answer = api.get("/api/approvals", headers=_bearer(SCOPE_FLEET_READ, **AGENCY))

    assert answer.status_code == 403


def test_a_run_that_has_ended_takes_no_answer(api, fleet):
    """Its delivery is gone, so an answer would reach nobody."""
    run, _ = _waiting(fleet)
    with FleetStore(fleet) as store:
        store.finish_run(run.id, "failed", error_class="interrupted")

    assert _answer(api, run, "once").status_code == 409
