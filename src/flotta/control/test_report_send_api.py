"""The hosted product's report schedule (M16.8): `report:send`, alone.

It lists the reports due and who they go to, reads a report's numbers and an
agency's branding, and records a send and the owners' notice, for any org;
and reaches nothing else, not one agent, run, setting or conversation.
"""

from __future__ import annotations

import pytest

from flotta.auth import ROLES, SCOPE_FLEET_READ, SCOPE_REPORT_SEND, AuthError, mint
from flotta.control import test_tenancy
from flotta.control.test_tenancy import KEY, X, _bearer
from flotta.store import FleetStore

fleet = test_tenancy.fleet
api = test_tenancy.api

SENDER = _bearer(SCOPE_REPORT_SEND, subject="reports")
OWNER = _bearer(*ROLES["owner"], org="agency-x")
MEMBER = _bearer(*ROLES["member"], org="agency-x")
REPORT = "/api/orgs/agency-x/clients/acme/reports/2026-10"


def _settings(api):
    api.put(
        "/api/orgs/agency-x/clients/acme/report-settings",
        json={"recipients": ["ceo@acme.example"], "auto_send_after_days": 3},
        headers=OWNER,
    )


# -- the scope ------------------------------------------------------------------


@pytest.mark.parametrize(
    "claims",
    [
        {"scopes": {SCOPE_REPORT_SEND, SCOPE_FLEET_READ}},
        {"scopes": {SCOPE_REPORT_SEND}, "org": "agency-x"},
        {"scopes": {SCOPE_REPORT_SEND}, "client": "acme"},
    ],
)
def test_report_send_is_held_alone_and_for_every_org(claims):
    with pytest.raises(AuthError, match="report"):
        mint(key=KEY, subject="reports", **claims)


def test_no_role_holds_it():
    assert all(SCOPE_REPORT_SEND not in scopes for scopes in ROLES.values())


# -- what it reaches -----------------------------------------------------------


def test_it_lists_the_reports_due_with_where_they_go(api, fleet):
    _settings(api)
    with FleetStore(fleet) as store:
        store.add_person("ana@agency-x.example", "agency-x", "owner")
        store.add_person("bo@agency-x.example", "agency-x", "member")

    runs = api.get("/api/report-runs?period=2026-10", headers=SENDER)

    assert runs.status_code == 200, runs.text
    orgs = {o["org"]: o for o in runs.json()["orgs"]}
    assert set(orgs) == {"agency-x", "agency-y"}, (
        "every agency with a client, not the operator's own"
    )
    assert orgs["agency-x"]["owners"] == ["ana@agency-x.example"], "owners, not members"
    acme = orgs["agency-x"]["clients"][0]
    assert acme["id"] == "acme"
    assert acme["report"]["status"] == "draft"
    assert acme["report"]["settings"] == {
        "recipients": ["ceo@acme.example"],
        "auto_send_after_days": 3,
    }
    assert acme["spend_shown"] is False


def test_it_reads_the_numbers_and_the_branding_of_any_org(api):
    data = api.get("/api/clients/acme/report-data?period=2026-10&org=agency-y", headers=SENDER)
    branding = api.get("/api/orgs/agency-y/branding", headers=SENDER)
    report = api.get("/api/orgs/agency-y/clients/acme/reports/2026-10", headers=SENDER)

    assert data.status_code == 200 and data.json()["org"] == "agency-y"
    assert branding.status_code == 200
    assert report.status_code == 200


def test_it_names_the_org_it_reads(api):
    assert (
        api.get("/api/clients/acme/report-data?period=2026-10", headers=SENDER).status_code == 422
    )


def test_it_sends_a_report_once_and_tells_owners_once(api):
    _settings(api)

    first = api.post(f"{REPORT}/sent", json={"version": 0}, headers=SENDER)
    again = api.post(f"{REPORT}/sent", json={"version": 0}, headers=SENDER)
    notice = api.post("/api/orgs/agency-x/reports/2026-10/notice", headers=SENDER)

    assert first.status_code == 200
    assert first.json()["sent_by"] == "reports"
    assert again.status_code == 409
    assert notice.json()["first"] is True


# -- what it doesn't ------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/boxes", None),
        ("GET", f"/api/boxes/{X}", None),
        ("GET", "/api/clients/acme/runs", None),
        ("GET", "/api/clients/acme/audit", None),
        ("GET", "/api/orgs/agency-x/budgets", None),
        ("GET", "/api/orgs/agency-x/clients", None),
        ("GET", "/api/orgs/agency-x/members", None),
        ("PUT", REPORT, {"note": "mine now", "version": 0}),
        (
            "PUT",
            "/api/orgs/agency-x/clients/acme/report-settings",
            {"recipients": ["me@elsewhere.example"]},
        ),
        ("PUT", "/api/orgs/agency-x/branding", {"accent": "#000000"}),
        ("POST", "/api/orgs", {"id": "mine", "name": "Mine"}),
        ("POST", f"/api/boxes/{X}/runs", {"prompt": "do things"}),
    ],
)
def test_it_reaches_nothing_else(api, method, path, body):
    answer = api.request(method, path, json=body, headers=SENDER)

    assert answer.status_code in (403, 404), (method, path, answer.status_code)


def test_the_agency_does_not_read_the_schedule(api):
    assert api.get("/api/report-runs?period=2026-10", headers=OWNER).status_code == 403
    assert api.get("/api/report-runs?period=2026-10", headers=MEMBER).status_code == 403


def test_the_operator_names_the_org_too(api):
    """The schedule against a local control plane runs as the operator."""
    operator = _bearer(SCOPE_FLEET_READ)

    data = api.get("/api/clients/acme/report-data?period=2026-10&org=agency-y", headers=operator)

    assert data.status_code == 200 and data.json()["org"] == "agency-y"


def test_an_agency_cannot_name_another_org(api):
    member = _bearer(*ROLES["member"], org="agency-x")

    data = api.get("/api/clients/acme/report-data?period=2026-10&org=agency-y", headers=member)

    assert data.json()["org"] == "agency-x", "?org= is the schedule's and the operator's alone"
