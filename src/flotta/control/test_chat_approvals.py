"""What an agent asks a person in a conversation, kept by the control plane (FLOTTA-166).

The door records each approval the box asks in a turn against the turn's chat
run, so `GET /api/approvals` lists it beside a delivered run's and
`POST /api/runs/{id}/approval` answers it; the door carries the answer to the
box. What the door hears and sends is in `door/test_turns.py`.
"""

from __future__ import annotations

from flotta.auth import SCOPE_BOX_CHAT, SCOPE_FLEET_READ, SCOPE_FLEET_ROUTE, SCOPE_FLEET_WRITE
from flotta.control import test_tenancy
from flotta.control.test_tenancy import X, Y, _bearer, _ids
from flotta.store import FleetStore

DOOR = _bearer(SCOPE_FLEET_ROUTE, subject="door")
AGENCY_X = _bearer(SCOPE_FLEET_WRITE, SCOPE_FLEET_READ, org="agency-x")
CLIENT_X = _bearer(SCOPE_FLEET_READ, SCOPE_BOX_CHAT, org="agency-x", client="acme")

fleet = test_tenancy.fleet
api = test_tenancy.api

ASKED = {
    "request_id": "srq-1",
    "command": "rm -rf /tmp/old-exports",
    "description": "delete in root path",
    "choices": ["once", "session", "always", "deny"],
}


def _turn(api, fleet, box=X, by=None):
    """A client person's turn on `box`, as the door opens it."""
    if by is None:
        with FleetStore(fleet) as store:
            user = store.create_user("ana@acme.test", name="Ana")
        by = f"user:{user.id}"
    body = {"prompt": "Clean up.", "session_id": "live-1", "by": by, "client": True}
    opened = api.post(f"/api/boxes/{box}/chat-runs", json=body, headers=DOOR)
    assert opened.status_code == 201, opened.text
    return opened.json()["id"]


def _ask(api, run_id, box=X, headers=DOOR, **body):
    return api.post(
        f"/api/boxes/{box}/runs/{run_id}/approvals", json={**ASKED, **body}, headers=headers
    )


def _listed(api, headers=AGENCY_X):
    answer = api.get("/api/approvals", headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()["approvals"]


# -- the guards ----------------------------------------------------------------


def test_only_the_door_records_an_approval_and_only_on_that_boxs_turn(api, fleet):
    run_id = _turn(api, fleet)

    assert _ask(api, run_id, headers=AGENCY_X).status_code == 403
    assert _ask(api, run_id, box=Y).status_code == 404
    other_org_door = _bearer(SCOPE_FLEET_ROUTE, org="agency-y")
    assert _ask(api, run_id, headers=other_org_door).status_code == 404
    assert _listed(api) == []


def test_a_delivered_run_is_not_the_doors_to_record_on(api, fleet):
    """The control plane holds a delivered run's socket and records its own."""
    delivered = api.post(f"/api/boxes/{X}/runs", json={"prompt": "Tidy."}, headers=AGENCY_X)

    assert _ask(api, delivered.json()["id"]).status_code == 404


def test_an_approval_needs_the_boxs_request_id(api, fleet):
    run_id = _turn(api, fleet)

    assert _ask(api, run_id, request_id="").status_code == 422
    assert _ask(api, run_id, request_id=None).status_code == 422


def test_from_outside_a_conversation_takes_only_once_or_deny(api, fleet):
    """Even the agency: `always` and `session` outlive the question it saw, and
    the door carries nothing else to the box."""
    run_id = _turn(api, fleet)
    _ask(api, run_id)

    for choice in ("always", "session", "all"):
        refused = api.post(
            f"/api/runs/{run_id}/approval", json={"choice": choice}, headers=AGENCY_X
        )
        assert refused.status_code == 422, choice
    allowed = api.post(f"/api/runs/{run_id}/approval", json={"choice": "once"}, headers=AGENCY_X)
    assert allowed.status_code == 200, allowed.text


def test_a_client_person_sees_and_answers_only_where_the_agency_allows(api, fleet):
    run_id = _turn(api, fleet)
    _ask(api, run_id)

    assert _listed(api, CLIENT_X) == []
    refused = api.post(f"/api/runs/{run_id}/approval", json={"choice": "deny"}, headers=CLIENT_X)
    assert refused.status_code == 403

    with FleetStore(fleet) as store:
        store.set_clients_answer_approvals(_ids(fleet)[X], True)
    assert [a["run_id"] for a in _listed(api, CLIENT_X)] == [run_id]
    answer = api.post(f"/api/runs/{run_id}/approval", json={"choice": "deny"}, headers=CLIENT_X)
    assert answer.status_code == 200, answer.text


def test_another_agency_never_sees_it(api, fleet):
    run_id = _turn(api, fleet)
    _ask(api, run_id)

    assert _listed(api, _bearer(SCOPE_FLEET_WRITE, SCOPE_FLEET_READ, org="agency-y")) == []
    answer = api.post(
        f"/api/runs/{run_id}/approval",
        json={"choice": "once"},
        headers=_bearer(SCOPE_FLEET_WRITE, org="agency-y"),
    )
    assert answer.status_code == 404


# -- what is listed ------------------------------------------------------------


def test_the_agency_sees_whose_conversation_is_waiting(api, fleet):
    run_id = _turn(api, fleet)

    recorded = _ask(api, run_id)

    assert recorded.status_code == 201, recorded.text
    (listed,) = _listed(api)
    assert listed["id"] == recorded.json()["id"]
    assert (listed["kind"], listed["session_id"], listed["whose"]) == ("chat", "live-1", "Ana")
    assert (listed["command"], listed["description"]) == (ASKED["command"], ASKED["description"])
    assert listed["status"] == "waiting"


def test_whose_is_nobody_for_a_subject_that_is_not_a_person(api, fleet):
    run_id = _turn(api, fleet, by="operator")
    _ask(api, run_id)

    (listed,) = _listed(api)
    assert listed["kind"] == "chat" and listed["whose"] is None


def test_asked_twice_it_is_one_question(api, fleet):
    run_id = _turn(api, fleet)

    first, again = _ask(api, run_id), _ask(api, run_id)

    assert first.json()["id"] == again.json()["id"]
    assert len(_listed(api)) == 1


# -- how it ends ----------------------------------------------------------------


def test_answered_from_outside_the_door_reads_it_and_says_it_was_carried(api, fleet):
    run_id = _turn(api, fleet)
    approval_id = _ask(api, run_id).json()["id"]

    api.post(f"/api/runs/{run_id}/approval", json={"choice": "once"}, headers=AGENCY_X)
    held = api.get(f"/api/boxes/{X}/runs/{run_id}/approvals", headers=DOOR).json()["approvals"]

    assert [(a["id"], a["status"], a["choice"]) for a in held] == [
        (approval_id, "answered", "once")
    ]
    assert _listed(api) == []
    carried = api.post(
        f"/api/boxes/{X}/runs/{run_id}/approvals/{approval_id}/delivered", headers=DOOR
    )
    assert carried.status_code == 200
    # Carried, so the turn ending leaves it answered.
    api.post(
        f"/api/boxes/{X}/runs/{run_id}/steps", json={"steps": [], "status": "done"}, headers=DOOR
    )
    with FleetStore(fleet) as store:
        assert store.get_approval(approval_id).status == "answered"


def test_answered_in_the_conversation_it_leaves_the_list(api, fleet):
    run_id = _turn(api, fleet)
    _ask(api, run_id)

    settled = api.post(
        f"/api/boxes/{X}/runs/{run_id}/approvals/settled",
        json={"request_id": "srq-1", "choice": "once", "by": "user:u-1"},
        headers=DOOR,
    )

    assert settled.status_code == 200, settled.text
    assert (settled.json()["status"], settled.json()["choice"]) == ("answered", "once")
    assert _listed(api) == []
    late = api.post(f"/api/runs/{run_id}/approval", json={"choice": "deny"}, headers=AGENCY_X)
    assert late.status_code == 409


def test_withdrawn_by_the_box_it_expires(api, fleet):
    run_id = _turn(api, fleet)
    _ask(api, run_id)

    settled = api.post(
        f"/api/boxes/{X}/runs/{run_id}/approvals/settled",
        json={"request_id": "srq-1"},
        headers=DOOR,
    )

    assert settled.json()["status"] == "expired"
    assert _listed(api) == []


def test_an_answer_from_outside_that_got_there_first_stands(api, fleet):
    run_id = _turn(api, fleet)
    _ask(api, run_id)
    api.post(f"/api/runs/{run_id}/approval", json={"choice": "deny"}, headers=AGENCY_X)

    settled = api.post(
        f"/api/boxes/{X}/runs/{run_id}/approvals/settled",
        json={"request_id": "srq-1", "choice": "once"},
        headers=DOOR,
    )

    assert settled.json()["choice"] == "deny"


def test_settling_what_was_never_asked_is_404(api, fleet):
    run_id = _turn(api, fleet)

    settled = api.post(
        f"/api/boxes/{X}/runs/{run_id}/approvals/settled",
        json={"request_id": "srq-unknown"},
        headers=DOOR,
    )

    assert settled.status_code == 404


def test_the_turn_ending_expires_what_still_waits(api, fleet):
    run_id = _turn(api, fleet)
    approval_id = _ask(api, run_id).json()["id"]

    ended = api.post(
        f"/api/boxes/{X}/runs/{run_id}/steps", json={"steps": [], "status": "done"}, headers=DOOR
    )

    assert ended.status_code == 200
    assert _listed(api) == []
    with FleetStore(fleet) as store:
        assert store.get_approval(approval_id).status == "expired"
    assert _ask(api, run_id, request_id="srq-2").status_code == 409


def test_a_delivered_runs_approval_is_listed_as_a_run(api, fleet):
    from datetime import UTC, datetime, timedelta

    with FleetStore(fleet) as store:
        run = store.create_run(_ids(fleet)[X], "Tidy up", trigger_kind="api")
        store.start_run(run.id)
        store.ask_approval(
            run.id,
            request_id="srq-1",
            command="rm -rf /tmp/x",
            description=None,
            choices=["once", "deny"],
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )

    (listed,) = _listed(api)
    assert listed["kind"] == "run" and "session_id" not in listed
