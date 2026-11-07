"""The schedules API (FLOTTA-148, M14.3): an agent's triggers, for its agency.

The 404 for another org's agent is the tenancy sweep's (`BOX_ROUTES` in
`test_tenancy.py`, `test_route_scope.py`); these are the answers particular to
triggers: a schedule is checked as the store checks it, it says when it fires
next, and a client's people neither see nor change one.
"""

from __future__ import annotations

import pytest

from flotta.auth import SCOPE_BOX_CHAT, SCOPE_FLEET_READ, SCOPE_FLEET_WRITE
from flotta.control import test_tenancy
from flotta.control.test_tenancy import X, Y, _bearer, _ids

AGENCY = {"org": "agency-x"}
WRITER = _bearer(SCOPE_FLEET_WRITE, SCOPE_FLEET_READ, **AGENCY)

fleet = test_tenancy.fleet
api = test_tenancy.api


def _add(api, box=X, headers=None, **body):
    body = {"cron": "0 8 * * 1-5", "tz": "Europe/Lisbon", "prompt": "Morning summary.", **body}
    return api.post(f"/api/boxes/{box}/triggers", json=body, headers=headers or WRITER)


def test_a_schedule_is_added_and_says_when_it_fires_next(api, fleet):
    added = _add(api)

    assert added.status_code == 201, added.text
    trigger = added.json()
    assert (trigger["kind"], trigger["cron"], trigger["tz"], trigger["enabled"]) == (
        "schedule",
        "0 8 * * 1-5",
        "Europe/Lisbon",
        True,
    )
    assert trigger["box_id"] == _ids(fleet)[X]
    assert trigger["created_by"] == "test"
    assert len(trigger["next"]) == 3
    assert trigger["next"] == sorted(trigger["next"])


def test_an_agents_schedules_are_listed(api):
    trigger = _add(api).json()

    listed = api.get(f"/api/boxes/{X}/triggers", headers=WRITER).json()["triggers"]

    assert [t["id"] for t in listed] == [trigger["id"]]
    assert len(listed[0]["next"]) == 3


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"cron": "*/2 * * * *"}, "5 minutes"),
        ({"cron": "0 8 * *"}, "five fields"),
        ({"tz": "Lisbon"}, "time zone"),
        ({"prompt": ""}, "prompt"),
        ({"kind": "email"}, "schedule or a webhook"),
    ],
)
def test_a_schedule_that_cannot_be_kept_is_a_422(api, body, message):
    answer = _add(api, **body)

    assert answer.status_code == 422
    assert message in answer.json()["detail"]


def test_a_schedule_is_changed_and_turned_off(api):
    trigger = _add(api).json()

    edited = api.patch(
        f"/api/triggers/{trigger['id']}",
        json={"cron": "30 7 * * *", "enabled": False},
        headers=WRITER,
    )

    assert edited.status_code == 200, edited.text
    assert (edited.json()["cron"], edited.json()["enabled"], edited.json()["next"]) == (
        "30 7 * * *",
        False,
        [],
    )
    assert (
        api.patch(
            f"/api/triggers/{trigger['id']}", json={"cron": "* * * * *"}, headers=WRITER
        ).status_code
        == 422
    )


def test_a_schedule_is_removed(api):
    trigger = _add(api).json()

    assert api.delete(f"/api/triggers/{trigger['id']}", headers=WRITER).status_code == 204
    assert api.get(f"/api/boxes/{X}/triggers", headers=WRITER).json()["triggers"] == []
    assert api.delete(f"/api/triggers/{trigger['id']}", headers=WRITER).status_code == 404


def test_another_orgs_schedule_is_not_there(api):
    trigger = _add(api).json()
    stranger = _bearer(SCOPE_FLEET_WRITE, SCOPE_FLEET_READ, org="agency-y")

    for method in ("patch", "delete"):
        answer = getattr(api, method)(
            f"/api/triggers/{trigger['id']}",
            **({"json": {"enabled": False}} if method == "patch" else {}),
            headers=stranger,
        )
        assert answer.status_code == 404, method
    assert _add(api, box=Y, headers=stranger).status_code == 201, "its own agent is fine"


def test_a_sibling_clients_token_does_not_reach_the_schedule(api, fleet):
    from flotta.store import FleetStore

    with FleetStore(fleet) as store:
        store.create_client("agency-x", "globex", "Globex")
    trigger = _add(api).json()
    sibling = _bearer(SCOPE_FLEET_WRITE, SCOPE_FLEET_READ, org="agency-x", client="globex")

    answer = api.patch(f"/api/triggers/{trigger['id']}", json={"enabled": False}, headers=sibling)

    assert answer.status_code == 404


@pytest.mark.parametrize(
    "scopes",
    [{SCOPE_FLEET_READ, SCOPE_BOX_CHAT}, {SCOPE_FLEET_READ}],
    ids=["client_user", "client_viewer"],
)
def test_a_clients_people_neither_see_nor_change_schedules(api, scopes):
    _add(api)
    person = _bearer(*scopes, org="agency-x", client="acme")

    assert _add(api, headers=person).status_code == 403
    assert api.get(f"/api/boxes/{X}/triggers", headers=person).status_code == 403
