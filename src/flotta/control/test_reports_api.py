"""A client's monthly report, as the runtime keeps it (M16.4).

Where it goes, the agency's note, and its send, recorded once. The agency's,
all of it: a client's people read none of it here, and only an owner says
where a report goes.
"""

from __future__ import annotations

import pytest

from flotta.auth import ROLES, scopes_for_role
from flotta.control import test_tenancy
from flotta.control.test_tenancy import _bearer
from flotta.store import FleetStore

fleet = test_tenancy.fleet
api = test_tenancy.api

OWNER = _bearer(*ROLES["owner"], org="agency-x")
MEMBER = _bearer(*ROLES["member"], org="agency-x")
VIEWER = _bearer(*scopes_for_role("client_viewer", client="acme"), org="agency-x", client="acme")
STRANGER = _bearer(*ROLES["owner"], org="agency-y")
CLIENT_MEMBER = _bearer(*ROLES["member"], org="agency-x", client="acme")

SETTINGS = "/api/orgs/agency-x/clients/acme/report-settings"
REPORT = "/api/orgs/agency-x/clients/acme/reports/2026-10"


def _note(api, note, version, headers=MEMBER):
    return api.put(REPORT, json={"note": note, "version": version}, headers=headers)


def _send(api, headers=MEMBER, version=None, fleet=None):
    if version is None:
        version = api.get(REPORT, headers=MEMBER).json()["version"]
    return api.post(f"{REPORT}/sent", json={"version": version}, headers=headers)


@pytest.fixture(autouse=True)
def recipients(api):
    """Every report here goes somewhere: an owner said where."""
    api.put(SETTINGS, json={"recipients": ["ceo@acme.example"]}, headers=OWNER)


# -- settings -------------------------------------------------------------------


def test_a_client_with_no_settings_goes_to_nobody_and_never_by_itself(api, fleet):
    with FleetStore(fleet) as store:
        store.create_client("agency-x", "globex", "Globex")
    unset = "/api/orgs/agency-x/clients/globex/report-settings"

    assert api.get(unset, headers=MEMBER).json() == {
        "recipients": [],
        "auto_send_after_days": None,
    }


def test_an_owner_says_where_a_report_goes(api):
    answer = api.put(
        SETTINGS,
        json={
            "recipients": ["CEO@Acme.example", "ops@acme.example", "ceo@acme.example"],
            "auto_send_after_days": 3,
        },
        headers=OWNER,
    )

    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "recipients": ["ceo@acme.example", "ops@acme.example"],
        "auto_send_after_days": 3,
    }


@pytest.mark.parametrize(
    "body",
    [
        {"recipients": ["not an email"]},
        {"recipients": [f"p{i}@acme.example" for i in range(6)]},
        {"recipients": "ceo@acme.example"},
        {"recipients": [], "auto_send_after_days": 29},
        {"recipients": [], "auto_send_after_days": -1},
        {"recipients": [], "auto_send_after_days": True},
    ],
)
def test_settings_that_are_not_ones_are_refused(api, body):
    assert api.put(SETTINGS, json=body, headers=OWNER).status_code == 422


def test_only_an_owner_changes_settings(api):
    body = {"recipients": ["ceo@acme.example"]}

    assert api.put(SETTINGS, json=body, headers=MEMBER).status_code == 403
    assert api.put(SETTINGS, json=body, headers=STRANGER).status_code == 404


# -- the note -------------------------------------------------------------------


def test_a_report_nobody_wrote_is_a_blank_draft(api):
    report = api.get(REPORT, headers=MEMBER).json()

    assert (report["note"], report["version"], report["status"]) == ("", 0, "draft")


def test_the_note_is_written_against_the_version_read(api):
    first = _note(api, "We added Saturday hours.", 0)
    stale = _note(api, "Someone else's edit", 0)
    second = _note(api, "We added Saturday hours and a FAQ.", 1)

    assert first.status_code == 200 and first.json()["version"] == 1
    assert stale.status_code == 409, "two editors don't overwrite each other"
    assert second.json()["note"] == "We added Saturday hours and a FAQ."
    assert api.get(REPORT, headers=MEMBER).json()["version"] == 2


def test_a_note_has_a_limit(api):
    assert _note(api, "x" * 2001, 0).status_code == 422


@pytest.mark.parametrize("period", ["2026-13", "october", "2026-1", "9999-12"])
def test_a_period_is_a_month(api, period):
    path = f"/api/orgs/agency-x/clients/acme/reports/{period}"

    assert api.get(path, headers=MEMBER).status_code == 422


# -- the send -------------------------------------------------------------------


def test_a_report_is_sent_once(api):
    _note(api, "We added Saturday hours.", 0)

    first = _send(api)
    second = _send(api)
    edit = _note(api, "Too late", 1)

    assert first.status_code == 200
    sent = first.json()
    assert sent["status"] == "sent"
    assert sent["recipients_sent"] == ["ceo@acme.example"]
    assert sent["note"] == "We added Saturday hours."
    assert second.status_code == 409, "two senders never both send it"
    assert edit.status_code == 409, "a sent report's note is what was sent"


def test_a_send_that_could_not_go_is_put_back(api):
    _send(api)

    undone = api.post(f"{REPORT}/sent", json={"undo": True}, headers=MEMBER)
    again = _send(api)

    assert undone.json()["status"] == "draft"
    assert again.status_code == 200


def test_a_send_goes_to_the_owners_list_and_nobody_else(api):
    forged = api.post(
        f"{REPORT}/sent",
        json={"version": 0, "recipients": ["someone@elsewhere.example"]},
        headers=MEMBER,
    )

    assert forged.status_code == 200
    assert forged.json()["recipients_sent"] == ["ceo@acme.example"], "never the sender's list"


def test_a_client_nobody_named_is_sent_to_nobody(api):
    api.put(SETTINGS, json={"recipients": []}, headers=OWNER)

    assert _send(api).status_code == 422


def test_a_send_is_of_the_version_read(api):
    _note(api, "First", 0)
    _note(api, "Second", 1)

    stale = _send(api, version=1)
    current = _send(api, version=2)

    assert stale.status_code == 409, "the email rendered version 1; version 2 is what is there"
    assert current.json()["note"] == "Second"


def test_an_address_that_is_two_is_refused(api):
    for address in (
        "ceo@acme.example,x@evil.example",
        "a@b.c;d@e.f",
        '"x"@acme.example',
        "<a@b.c>",
    ):
        answer = api.put(SETTINGS, json={"recipients": [address]}, headers=OWNER)
        assert answer.status_code == 422, address


def test_every_clients_report_is_listed_for_a_month(api, fleet):
    with FleetStore(fleet) as store:
        store.create_client("agency-x", "globex", "Globex")
    _send(api)

    listed = api.get("/api/orgs/agency-x/reports?period=2026-10", headers=MEMBER).json()

    assert {r["client"]: r["status"] for r in listed["reports"]} == {
        "acme": "sent",
        "globex": "draft",
    }


def test_the_owners_are_told_once_a_month(api):
    path = "/api/orgs/agency-x/reports/2026-10/notice"

    assert api.post(path, headers=OWNER).json()["first"] is True
    assert api.post(path, headers=OWNER).json()["first"] is False
    other = api.post("/api/orgs/agency-x/reports/2026-11/notice", headers=OWNER)
    assert other.json()["first"] is True


def test_a_telling_that_did_not_go_is_taken_back(api):
    path = "/api/orgs/agency-x/reports/2026-10/notice"
    api.post(path, headers=OWNER)

    api.post(path, json={"undo": True}, headers=OWNER)

    assert api.post(path, headers=OWNER).json()["first"] is True


def test_a_member_does_not_spend_the_months_notice(api):
    assert api.post("/api/orgs/agency-x/reports/2026-10/notice", headers=MEMBER).status_code == 403


# -- whose it is ----------------------------------------------------------------


@pytest.mark.parametrize("who", [VIEWER, CLIENT_MEMBER], ids=["client_viewer", "client_member"])
def test_a_clients_people_read_and_write_none_of_it(api, who):
    assert api.get(REPORT, headers=who).status_code == 403
    assert api.get(SETTINGS, headers=who).status_code == 403
    assert api.get("/api/orgs/agency-x/reports?period=2026-10", headers=who).status_code == 403


def test_another_org_reaches_none_of_it(api):
    stranger = _bearer(*ROLES["member"], org="agency-y")

    assert api.get(REPORT, headers=stranger).status_code == 404
    assert _send(api, headers=stranger).status_code == 404
    assert (
        api.get("/api/orgs/agency-x/clients/nobody/reports/2026-10", headers=MEMBER).status_code
        == 404
    )


def test_only_the_sender_takes_a_send_back_and_only_soon(api, fleet):
    from datetime import UTC, datetime, timedelta

    from flotta.store import ReportConflictError

    _send(api)
    someone_else = _bearer(*ROLES["member"], org="agency-x", subject="bo")
    other = api.post(f"{REPORT}/sent", json={"undo": True}, headers=someone_else)
    with FleetStore(fleet) as store:
        report = store.client_report("agency-x", "acme", "2026-10")
        late = datetime.fromisoformat(report.sent_at) + timedelta(minutes=16)
        with pytest.raises(ReportConflictError):
            store.unmark_report_sent("agency-x", "acme", "2026-10", by=report.sent_by, now=late)

    assert other.status_code == 409, "not someone reopening a report that went"
    assert datetime.now(UTC) < late
