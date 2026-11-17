"""A client's history (M16.3): its audit trail, and its report's numbers.

The trail is the agency's and a client viewer's (D29), the viewer's without
the agency's words and without conversations; a client user reads neither.
The report's numbers are the agency's alone: the hosted product decides
what of them a client is shown.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from flotta.auth import ROLES, SCOPE_FLEET_READ, scopes_for_role
from flotta.client_audit import BadWindow, month_window
from flotta.control import test_tenancy
from flotta.control.test_tenancy import X, _bearer, _ids
from flotta.store import FleetStore

fleet = test_tenancy.fleet
api = test_tenancy.api

AGENCY = _bearer(*ROLES["member"], org="agency-x")
VIEWER = _bearer(*scopes_for_role("client_viewer", client="acme"), org="agency-x", client="acme")
CLIENT_USER = _bearer(*scopes_for_role("client_user", client="acme"), org="agency-x", client="acme")
TRAIL = "/api/clients/acme/audit"


@pytest.fixture
def history(fleet):
    """A month of work for agency-x's `acme`: two runs, a conversation, an
    approval answered, a request, a pull request and an event on the agent."""
    box = _ids(fleet)[X]
    with FleetStore(fleet) as store:
        store.set_box_meta(box, display_name="Support", instructions="Never mention refunds.")
        store.add_event(
            "box", box, "instructions_set", {"instructions": "Never mention refunds.", "by": "ana"}
        )
        work = store.create_run(box, "Close stale tickets", trigger_kind="schedule")
        store.start_run(work.id)
        approval = store.ask_approval(
            work.id,
            request_id="srq-1",
            command="gh issue close 7",
            description="close an issue",
            choices=["once", "deny"],
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        store.answer_approval(approval.id, "once", by="user:ana")
        store.finish_run(work.id, "done", output="Closed 4 tickets")
        broken = store.create_run(box, "Sync the CRM", trigger_kind="webhook")
        store.start_run(broken.id)
        store.finish_run(broken.id, "failed", error_class="timeout")
        chat = store.create_run(box, "hello, it's Bo", trigger_kind="chat")
        store.start_run(chat.id)
        store.finish_run(chat.id, "done", output="Hi Bo")
        request = store.file_request(
            box,
            title="Add a FAQ page",
            details="Bo's private notes on it",
            verdict=None,
            judge_error=None,
            within=None,
            approved=False,
            why="waits for the agency",
        )
        store.decide_request(request.id, approve=True, by="user:ana")
        store.record_pull_request(
            box,
            repo="acme/site",
            number=12,
            url="https://github.com/acme/site/pull/12",
            title="FAQ page",
            author_login="flotta-bot",
            head_ref="faq",
            base_ref="main",
            state="open",
            draft=False,
            opened_at=datetime.now(UTC).isoformat(),
        )
    return {"work": work, "broken": broken, "chat": chat}


def _entries(api, headers, **query):
    answer = api.get(TRAIL, headers=headers, params=query)
    assert answer.status_code == 200, answer.text
    return answer.json()["entries"]


def test_the_agency_reads_the_whole_trail(api, history):
    entries = _entries(api, AGENCY)
    kinds = [e["kind"] for e in entries]

    assert {"run.started", "run.finished", "approval.answered", "request.made"} <= set(kinds)
    assert "request.approved" in kinds and "pull.opened" in kinds
    assert any(e.get("run") == history["chat"].id for e in entries), "conversations included"
    assert any("payload" in e for e in entries if e["kind"].startswith("agent.")), (
        "the agency reads what was set on its agent"
    )
    assert [e["at"] for e in entries] == sorted(e["at"] for e in entries)


def test_a_viewer_reads_its_trail_without_the_agencys_words(api, history):
    entries = _entries(api, VIEWER)
    text = repr(entries)

    assert entries
    assert all(e.get("run") != history["chat"].id for e in entries), "no conversations"
    assert "Never mention refunds" not in text, "an agent's instructions are the agency's"
    assert "Close stale tickets" not in text, "a run's prompt is the agency's"
    assert "Bo's private notes" not in text
    assert all("payload" not in e for e in entries)
    actors = {e["actor"] for e in entries if not e["kind"].startswith("run.")}
    assert actors <= {None}, f"who did it is the agency's: {actors}"
    assert "flotta-bot" not in text and "user:ana" not in text


def test_a_client_user_reads_no_trail(api, history):
    assert api.get(TRAIL, headers=CLIENT_USER).status_code == 403


def test_another_client_or_org_reads_none_of_it(api, fleet, history):
    with FleetStore(fleet) as store:
        store.create_client("agency-x", "globex", "Globex")
    globex = _bearer(
        *scopes_for_role("client_viewer", client="globex"), org="agency-x", client="globex"
    )
    stranger = _bearer(SCOPE_FLEET_READ, org="agency-y")

    assert api.get(TRAIL, headers=globex).status_code == 404
    assert api.get(TRAIL, headers=stranger).json()["entries"] == [], (
        "agency-y's own acme, which has done nothing"
    )
    assert api.get("/api/clients/nobody/audit", headers=AGENCY).status_code == 404


def test_a_window_is_at_most_a_quarter(api, history):
    long = api.get(TRAIL, headers=AGENCY, params={"from": "2026-01-01", "to": "2026-06-01"})
    backwards = api.get(TRAIL, headers=AGENCY, params={"from": "2026-06-01", "to": "2026-05-01"})
    garbage = api.get(TRAIL, headers=AGENCY, params={"from": "last tuesday"})

    assert long.status_code == backwards.status_code == garbage.status_code == 422


def test_a_window_before_anything_happened_is_empty(api, history):
    assert _entries(api, AGENCY, **{"from": "2020-01-01", "to": "2020-02-01"}) == []


# -- the report's numbers -----------------------------------------------------------


def test_the_report_adds_up_the_month(api, history):
    period = datetime.now(UTC).strftime("%Y-%m")

    data = api.get(
        "/api/clients/acme/report-data", headers=AGENCY, params={"period": period}
    ).json()

    assert data["runs"] == {"total": 2, "done": 1, "failed": 1}, "conversations are not runs"
    assert data["conversations"] == 1
    assert data["approvals_answered"] == 1
    assert data["requests"] == {"made": 1, "approved": 1, "declined": 0}
    assert [p["number"] for p in data["pull_requests"]["opened"]] == [12]
    assert [f["error_class"] for f in data["failures"]] == ["timeout"]
    assert data["agents"][0]["label"] == "Support"
    assert data["agents"][0]["runs"] == {"done": 1, "failed": 1}
    assert data["spend_eur_nano"] == 0
    assert data["limit_eur_nano"] is None


def test_another_month_is_empty(api, history):
    data = api.get(
        "/api/clients/acme/report-data", headers=AGENCY, params={"period": "2020-01"}
    ).json()

    assert data["runs"] == {"total": 0}
    assert data["conversations"] == 0
    assert data["failures"] == []


def test_a_clients_people_read_no_report_data(api, history):
    for who in (VIEWER, CLIENT_USER):
        answer = api.get("/api/clients/acme/report-data", headers=who, params={"period": "2026-11"})
        assert answer.status_code == 403


@pytest.mark.parametrize("period", ["2026-13", "november", "2026-1", ""])
def test_a_period_that_is_not_a_month_is_refused(api, period):
    answer = api.get("/api/clients/acme/report-data", headers=AGENCY, params={"period": period})

    assert answer.status_code == 422


def test_a_month_is_its_first_moment_to_the_next_months():
    assert month_window("2026-12") == (
        datetime(2026, 12, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(BadWindow):
        month_window("2026-00")


def test_a_busy_month_is_counted_not_capped(api, fleet, monkeypatch):
    """The report's numbers come from counts, so a month past the row cap is
    still its own; the trail says it was cut short."""
    import flotta.store as store_module

    monkeypatch.setattr(store_module, "WINDOW_ROWS", 2)
    box = _ids(fleet)[X]
    with FleetStore(fleet) as store:
        for _ in range(5):
            run = store.create_run(box, "Sync", trigger_kind="webhook")
            store.start_run(run.id)
            store.finish_run(run.id, "done")
    period = datetime.now(UTC).strftime("%Y-%m")

    data = api.get(
        "/api/clients/acme/report-data", headers=AGENCY, params={"period": period}
    ).json()
    trail = api.get(TRAIL, headers=AGENCY).json()

    assert data["runs"] == {"total": 5, "done": 5}
    assert data["truncated"] is True
    assert trail["truncated"] is True


# -- what a client calls an agent (M16.9) --------------------------------------------


def _report(api):
    period = datetime.now(UTC).strftime("%Y-%m")
    answer = api.get("/api/clients/acme/report-data", headers=AGENCY, params={"period": period})
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_an_agent_without_a_display_name_is_called_by_its_own_name(api, history, fleet):
    """Not by its address, which names the agency's org: found on staging, where
    a client's export and report said `spike--acme--agency-test`."""
    with FleetStore(fleet) as store:
        store.set_box_meta(_ids(fleet)[X], display_name=None)

    assert {e["agent_label"] for e in _entries(api, VIEWER)} == {"support"}
    assert [a["label"] for a in _report(api)["agents"]] == ["support"]
    assert {f["agent_label"] for f in _report(api)["failures"]} == {"support"}


def test_a_removed_agent_says_so_and_never_shows_its_tombstone(api, history, fleet):
    """Its replacement often has the same name: staging's report listed two
    `Probe`s, and its export `probe--acme--agency-test@b-c09db73d7f8c`."""
    box = _ids(fleet)[X]
    with FleetStore(fleet) as store:
        store.update_box_status(box, "torn_down")
        store.release_name(box)

    labels = {e["agent_label"] for e in _entries(api, VIEWER)}
    report = _report(api)

    assert labels == {"Support (no longer here)"}
    assert [a["label"] for a in report["agents"]] == ["Support (no longer here)"]
    shown = [
        *labels,
        *(a["label"] for a in report["agents"]),
        *(f["agent_label"] for f in report["failures"]),
    ]
    assert not [s for s in shown if "--" in s or "@" in s or "agency-x" in s]
