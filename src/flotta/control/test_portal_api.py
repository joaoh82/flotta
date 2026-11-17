"""A client's viewer and the work done for it (M16.1, D29).

`portal:read` is a client viewer's, and only theirs: its own client's runs,
without the agency's words; its usage and limits, once the agency shows the
client what its agents cost; and the approvals of runs the agency lets the
client answer. A client user, who talks to the agents, has none of it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from flotta.auth import (
    ROLES,
    SCOPE_BOX_CHAT,
    SCOPE_FLEET_READ,
    SCOPE_FLEET_WRITE,
    SCOPE_ORG_ADMIN,
    SCOPE_PORTAL_READ,
    AuthError,
    mint,
    scopes_for_role,
)
from flotta.control import test_tenancy
from flotta.control.test_tenancy import KEY, X, _bearer, _ids
from flotta.store import FleetStore

fleet = test_tenancy.fleet
api = test_tenancy.api

AGENCY = _bearer(SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, org="agency-x")
OWNER = _bearer(*ROLES["owner"], org="agency-x")
VIEWER = _bearer(*scopes_for_role("client_viewer", client="acme"), org="agency-x", client="acme")
CLIENT_USER = _bearer(*scopes_for_role("client_user", client="acme"), org="agency-x", client="acme")


def _run(fleet, kind="schedule", prompt="Answer yesterday's tickets", **extra):
    with FleetStore(fleet) as store:
        run = store.create_run(
            _ids(fleet)[X], prompt, trigger_kind=kind, input={"secret": "agency's"}, **extra
        )
        return run


def _show_spend(api, shown=True):
    answer = api.put(
        "/api/orgs/agency-x/clients/acme/spend-shown", json={"shown": shown}, headers=OWNER
    )
    assert answer.status_code == 200, answer.text
    return answer


# -- the scope ----------------------------------------------------------------


def test_a_client_viewer_holds_portal_read_and_a_client_user_does_not():
    assert SCOPE_PORTAL_READ in scopes_for_role("client_viewer", client="acme")
    assert SCOPE_PORTAL_READ not in scopes_for_role("client_user", client="acme")
    assert all(SCOPE_PORTAL_READ not in ROLES[r] for r in ("owner", "member"))


def test_portal_read_is_always_one_clients():
    with pytest.raises(AuthError, match="name the client"):
        mint(scopes={SCOPE_FLEET_READ, SCOPE_PORTAL_READ}, key=KEY, subject="v", org="agency-x")


# -- runs -----------------------------------------------------------------------


def test_a_viewer_reads_its_clients_runs_without_the_agencys_words(api, fleet):
    run = _run(fleet)

    listed = api.get("/api/clients/acme/runs", headers=VIEWER)
    by_box = api.get(f"/api/boxes/{X}/runs", headers=VIEWER)
    one = api.get(f"/api/runs/{run.id}", headers=VIEWER)

    assert listed.status_code == by_box.status_code == one.status_code == 200
    for view in (listed.json()["runs"][0], by_box.json()["runs"][0], one.json()):
        assert view["id"] == run.id
        for withheld in ("prompt", "input", "resolved_by", "resolved_note"):
            assert withheld not in view, withheld
        assert "cost" not in view, "not until the agency shows the client its spend"


def test_the_agency_still_reads_every_word(api, fleet):
    run = _run(fleet)

    view = api.get(f"/api/runs/{run.id}", headers=AGENCY).json()

    assert view["prompt"] == "Answer yesterday's tickets"
    assert view["input"] == {"secret": "agency's"}
    assert "cost" in view


def test_a_viewer_sees_no_ones_conversations(api, fleet):
    chat = _run(fleet, kind="chat", prompt="hello")
    work = _run(fleet)

    listed = [r["id"] for r in api.get("/api/clients/acme/runs", headers=VIEWER).json()["runs"]]

    assert listed == [work.id]
    assert api.get(f"/api/runs/{chat.id}", headers=VIEWER).status_code == 404


def test_a_client_user_still_reads_no_runs(api, fleet):
    run = _run(fleet)

    for path in (f"/api/runs/{run.id}", f"/api/boxes/{X}/runs", "/api/clients/acme/runs"):
        assert api.get(path, headers=CLIENT_USER).status_code == 403, path


def test_a_viewer_reads_no_other_clients_runs(api, fleet):
    with FleetStore(fleet) as store:
        store.create_client("agency-x", "globex", "Globex")
        other = store.create_box("intake", org="agency-x", client="globex")
        theirs = store.create_run(other.id, "Globex's work", trigger_kind="schedule")

    assert api.get(f"/api/runs/{theirs.id}", headers=VIEWER).status_code == 404
    assert api.get("/api/clients/globex/runs", headers=VIEWER).status_code == 404
    assert api.get(f"/api/boxes/{other.id}/runs", headers=VIEWER).status_code == 404


def test_a_viewer_reads_no_triggers(api, fleet):
    """A trigger is its prompt: the agency's words, every one of them."""
    assert api.get(f"/api/boxes/{X}/triggers", headers=VIEWER).status_code == 403


def test_a_runs_cost_is_shown_once_the_agency_shows_spend(api, fleet):
    run = _run(fleet)
    _show_spend(api)

    view = api.get(f"/api/runs/{run.id}", headers=VIEWER).json()
    listed = api.get("/api/clients/acme/runs", headers=VIEWER).json()["runs"][0]

    assert "cost" in view and "cost" in listed
    assert "prompt" not in view


# -- usage and limits ---------------------------------------------------------------


def _budget(fleet):
    with FleetStore(fleet) as store:
        store.set_budget(
            "agency-x",
            period="month",
            limit_eur_nano=60 * 10**9,
            action="pause",
            client="acme",
        )
        store.set_budget("agency-x", period="month", limit_eur_nano=500 * 10**9, action="notify")


def test_spend_is_the_agencys_until_it_shows_the_client(api, fleet):
    _budget(fleet)

    for path in (
        "/api/orgs/agency-x/budgets",
        "/api/orgs/agency-x/usage?client=acme",
        "/api/orgs/agency-x/budget-events",
    ):
        assert api.get(path, headers=VIEWER).status_code == 403, path


def test_a_viewer_reads_its_own_clients_limits_and_usage_once_shown(api, fleet):
    _budget(fleet)
    _show_spend(api)

    budgets = api.get("/api/orgs/agency-x/budgets", headers=VIEWER)
    usage = api.get("/api/orgs/agency-x/usage?client=acme&group=day", headers=VIEWER)
    events = api.get("/api/orgs/agency-x/budget-events", headers=VIEWER)

    assert budgets.status_code == usage.status_code == events.status_code == 200
    seen = budgets.json()["budgets"]
    assert [b["client"] for b in seen] == ["acme"], "never the agency's own org-wide budget"
    assert "fallback_model" not in seen[0], "how the agency runs its agents"


def test_a_viewers_usage_is_its_own_client_by_day_or_agent(api, fleet):
    _show_spend(api)

    assert api.get("/api/orgs/agency-x/usage", headers=VIEWER).status_code == 403
    assert api.get("/api/orgs/agency-x/usage?client=globex", headers=VIEWER).status_code == 403
    for group in ("client", "model", "requested_model", "kind"):
        answer = api.get(f"/api/orgs/agency-x/usage?client=acme&group={group}", headers=VIEWER)
        assert answer.status_code == 403, group
    ok = api.get("/api/orgs/agency-x/usage?client=acme&group=day,box", headers=VIEWER)
    assert ok.status_code == 200


def test_a_client_user_reads_no_spend_even_when_shown(api, fleet):
    _show_spend(api)

    assert api.get("/api/orgs/agency-x/budgets", headers=CLIENT_USER).status_code == 403
    usage = api.get("/api/orgs/agency-x/usage?client=acme", headers=CLIENT_USER)
    assert usage.status_code == 403


def test_alerts_stay_the_agencys(api, fleet):
    _show_spend(api)

    assert api.get("/api/orgs/agency-x/alerts", headers=VIEWER).status_code == 403


def test_only_an_owner_shows_a_client_its_spend(api, fleet):
    member = _bearer(*ROLES["member"], org="agency-x")
    stranger = _bearer(*ROLES["owner"], org="agency-y")
    path = "/api/orgs/agency-x/clients/acme/spend-shown"

    assert api.put(path, json={"shown": True}, headers=member).status_code == 403
    assert api.put(path, json={"shown": True}, headers=VIEWER).status_code == 403
    assert api.put(path, json={"shown": True}, headers=stranger).status_code == 404
    assert api.put(path, json={"shown": "yes"}, headers=OWNER).status_code == 422
    assert (
        api.put(
            "/api/orgs/agency-x/clients/nobody/spend-shown", json={"shown": True}, headers=OWNER
        ).status_code
        == 404
    )


def test_the_client_list_says_whether_spend_is_shown(api, fleet):
    def shown():
        clients = api.get("/api/orgs/agency-x/clients", headers=OWNER).json()["clients"]
        return {c["id"]: c["spend_shown"] for c in clients}

    assert shown() == {"acme": False}
    _show_spend(api)
    assert shown() == {"acme": True}
    _show_spend(api, shown=False)
    assert shown() == {"acme": False}


# -- approvals ------------------------------------------------------------------


def _waiting(fleet, kind="schedule"):
    with FleetStore(fleet) as store:
        run = store.create_run(_ids(fleet)[X], "Tidy up", trigger_kind=kind)
        store.start_run(run.id)
        approval = store.ask_approval(
            run.id,
            request_id="srq-1",
            command="rm -rf /tmp/probe",
            description="delete in root path",
            choices=["once", "session", "always", "deny"],
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
    return run, approval


def _allow_clients(fleet):
    with FleetStore(fleet) as store:
        store.set_clients_answer_approvals(_ids(fleet)[X], True)


def _listed(api, headers):
    answer = api.get("/api/approvals", headers=headers)
    assert answer.status_code == 200, answer.text
    return [a["id"] for a in answer.json()["approvals"]]


def test_a_viewer_answers_a_runs_approval_where_the_agency_allows_it(api, fleet):
    run, approval = _waiting(fleet)
    path = f"/api/runs/{run.id}/approval"

    assert _listed(api, VIEWER) == []
    refused = api.post(path, json={"choice": "once"}, headers=VIEWER)
    _allow_clients(fleet)
    listed = _listed(api, VIEWER)
    always = api.post(path, json={"choice": "always"}, headers=VIEWER)
    once = api.post(path, json={"choice": "once"}, headers=VIEWER)

    assert refused.status_code == 403
    assert listed == [approval.id]
    assert always.status_code == 422, "never 'always' from a client's people"
    assert once.status_code == 200


def test_a_viewer_answers_no_conversations_approval(api, fleet):
    run, _ = _waiting(fleet, kind="chat")
    _allow_clients(fleet)

    answer = api.post(f"/api/runs/{run.id}/approval", json={"choice": "once"}, headers=VIEWER)

    assert answer.status_code == 403
    assert _listed(api, VIEWER) == []


def test_a_client_user_still_answers_in_its_conversation(api, fleet):
    run, approval = _waiting(fleet, kind="chat")
    _allow_clients(fleet)

    assert _listed(api, CLIENT_USER) == [approval.id]
    answer = api.post(f"/api/runs/{run.id}/approval", json={"choice": "once"}, headers=CLIENT_USER)
    assert answer.status_code == 200


def test_a_client_token_that_only_reads_still_answers_nothing(api, fleet):
    """`fleet:read` alone, with no `portal:read`: not a person who may answer."""
    run, _ = _waiting(fleet)
    _allow_clients(fleet)
    reader = _bearer(SCOPE_FLEET_READ, org="agency-x", client="acme")

    answer = api.post(f"/api/runs/{run.id}/approval", json={"choice": "once"}, headers=reader)

    assert answer.status_code == 403
    assert api.get("/api/approvals", headers=reader).status_code == 403


def test_portal_read_admits_no_writes(api, fleet):
    """A viewer's new scope reads; everything that changes an agent stays refused."""
    for method, path, body in (
        ("PUT", f"/api/boxes/{X}/meta", {"display_name": "Mine"}),
        ("POST", f"/api/boxes/{X}/runs", {"prompt": "do things"}),
        ("PUT", f"/api/boxes/{X}/client-approvals", {"allowed": True}),
    ):
        answer = api.request(method, path, json=body, headers=VIEWER)
        assert answer.status_code == 403, (method, path, answer.text)
    assert SCOPE_ORG_ADMIN not in scopes_for_role("client_viewer", client="acme")
    assert SCOPE_BOX_CHAT not in scopes_for_role("client_viewer", client="acme")
