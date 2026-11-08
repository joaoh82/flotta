"""Requests (FLOTTA-173): an agent files one, Jev judges it, the agency decides.

Jev is injected (`judge_request`), so nothing here reaches TypeSafe.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_BOX_PEER, SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, box_subject, mint
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.judge import JudgeError, Verdict
from flotta.store import FleetStore

KEY = "a-signing-key-for-request-tests"


def _verdict(complexity_p=(0.1, 0.8, 0.1, 0, 0)):
    best = max(range(5), key=lambda i: complexity_p[i])
    return Verdict(
        kind="feature",
        complexity=best,
        complexity_p=tuple(complexity_p),
        priority=2,
        priority_p=(0, 0.2, 0.8, 0),
        confidence={"complexity": 0.8},
    )


class FakeJev:
    def __init__(self):
        self.verdict: Verdict | None = _verdict()
        self.error: str | None = None
        self.asked: list[tuple[str, str]] = []

    def __call__(self, title, details):
        self.asked.append((title, details))
        if self.error:
            raise JudgeError(self.error)
        return self.verdict


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        box = store.create_box("eng-a")
        store.update_box_status(box.id, "running", endpoint="fly://app/eng-a")
        store.create_box("eng-b")
    return path


@pytest.fixture
def jev():
    return FakeJev()


@pytest.fixture
def api(fleet, jev):
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
        judge_request=jev,
    )
    with TestClient(app) as c:
        yield c


def _id(fleet, name="eng-a"):
    with FleetStore(fleet) as store:
        return store.get_box_by_name(name).id


def _as_box(fleet, name="eng-a"):
    token = mint(subject=box_subject(_id(fleet, name)), scopes={SCOPE_BOX_PEER}, key=KEY, ttl_s=300)
    return {"Authorization": f"Bearer {token}"}


def _as(*scopes, **claims):
    token = mint(subject="olivia", scopes=set(scopes), key=KEY, ttl_s=300, **claims)
    return {"Authorization": f"Bearer {token}"}


def _file(api, fleet, title="Add a CSV export", details="On the orders page", name="eng-a"):
    return api.post(
        f"/api/boxes/{_id(fleet, name)}/requests",
        json={"title": title, "details": details},
        headers=_as_box(fleet, name),
    )


def test_small_work_starts_at_once(api, fleet, jev):
    filed = _file(api, fleet)
    assert filed.status_code == 201, filed.text
    body = filed.json()
    assert body["starts"] is True
    assert body["request"]["status"] == "approved" and body["request"]["auto"] is True
    assert body["request"]["complexity_name"] == "small"
    assert body["request"]["priority_name"] == "high"
    assert jev.asked == [("Add a CSV export", "On the orders page")]


def test_large_work_waits_for_the_agency(api, fleet, jev):
    jev.verdict = _verdict((0, 0, 0.1, 0.3, 0.6))
    body = _file(api, fleet, title="Rebuild the billing system").json()
    assert body["starts"] is False
    assert body["request"]["status"] == "waiting"
    assert "project" in body["why"]
    listed = api.get("/api/requests", headers=_as(SCOPE_FLEET_READ)).json()["requests"]
    assert [r["title"] for r in listed] == ["Rebuild the billing system"]


def test_a_request_that_cannot_be_judged_waits_and_says_why(api, fleet, jev):
    jev.error = "no $TYPESAFE_API_KEY is set, so nothing judges requests"
    body = _file(api, fleet).json()
    assert body["starts"] is False
    assert body["request"]["judge_error"].startswith("no $TYPESAFE_API_KEY")
    assert body["request"]["kind"] is None


def test_the_agency_sets_the_level_and_the_agent_reads_it(api, fleet, jev):
    box = _id(fleet)
    read = api.get(f"/api/boxes/{box}/request-policy", headers=_as_box(fleet))
    assert read.json()["auto_level"] == 1 and read.json()["auto_level_name"] == "small"
    put = api.put(
        f"/api/boxes/{box}/request-policy", json={"auto_level": -1}, headers=_as(SCOPE_FLEET_WRITE)
    )
    assert put.json()["auto_level"] == -1 and put.json()["auto_level_name"] is None
    assert _file(api, fleet).json()["starts"] is False, "always ask means always"

    by_agent = api.put(
        f"/api/boxes/{box}/request-policy", json={"auto_level": 4}, headers=_as_box(fleet)
    )
    assert by_agent.status_code == 403, "an agent must not raise its own level"
    for bad in ({"auto_level": 9}, {"auto_level": "large"}, {"min_confidence": 0.1}, {"x": 1}):
        assert (
            api.put(
                f"/api/boxes/{box}/request-policy", json=bad, headers=_as(SCOPE_FLEET_WRITE)
            ).status_code
            == 422
        )


def test_approving_starts_the_work_as_a_run_with_the_note(api, fleet, jev):
    jev.verdict = _verdict((0, 0, 0.1, 0.8, 0.1))
    filed = _file(api, fleet, title="Rebuild the dashboard").json()["request"]
    decided = api.post(
        f"/api/requests/{filed['id']}/decision",
        json={"approve": True, "note": "Keep the old URLs"},
        headers=_as(SCOPE_FLEET_WRITE),
    )
    assert decided.status_code == 200, decided.text
    after = decided.json()["request"]
    assert after["status"] == "approved" and after["auto"] is False
    assert after["note"] == "Keep the old URLs"
    assert after["work_status"] == "pending"
    with FleetStore(fleet) as store:
        run = store.get_run(after["work_run_id"])
    assert run.trigger_kind == "request" and run.trigger_id == filed["id"]
    assert "Keep the old URLs" in run.prompt and "Rebuild the dashboard" in run.prompt

    again = api.post(
        f"/api/requests/{filed['id']}/decision",
        json={"approve": False},
        headers=_as(SCOPE_FLEET_WRITE),
    )
    assert again.status_code == 409, "answered once"


def test_declining_starts_nothing_and_the_agent_can_see_why(api, fleet, jev):
    jev.verdict = _verdict((0, 0, 0, 0.2, 0.8))
    filed = _file(api, fleet).json()["request"]
    api.post(
        f"/api/requests/{filed['id']}/decision",
        json={"approve": False, "note": "Let's scope this on a call first"},
        headers=_as(SCOPE_FLEET_WRITE),
    )
    mine = api.get(f"/api/boxes/{_id(fleet)}/requests", headers=_as_box(fleet)).json()["requests"]
    assert mine[0]["status"] == "declined"
    assert mine[0]["note"] == "Let's scope this on a call first"
    assert mine[0]["work_run_id"] is None


def test_filed_in_a_conversation_it_names_the_conversation_and_the_person(api, fleet, jev):
    with FleetStore(fleet) as store:
        store.open_chat_run(
            _id(fleet),
            "Can you add an export?",
            input={"session_id": "s-42", "by": "user:u-7", "client": True},
            started_at=datetime.now(UTC),
        )
    filed = _file(api, fleet).json()["request"]
    assert (filed["session_id"], filed["asked_by"]) == ("s-42", "user:u-7")
    assert filed["whose"] is None, "u-7 is nobody this store knows"


def test_who_asked_is_named_when_they_sign_in(api, fleet, jev):
    with FleetStore(fleet) as store:
        user = store.create_user("ana@acme.example", name="Ana")
        store.open_chat_run(
            _id(fleet),
            "Can you add an export?",
            input={"session_id": "s-1", "by": f"user:{user.id}", "client": True},
            started_at=datetime.now(UTC),
        )
    assert _file(api, fleet).json()["request"]["whose"] == "Ana"


def test_who_may_do_what(api, fleet, jev):
    jev.verdict = _verdict((0, 0, 0, 0.2, 0.8))
    filed = _file(api, fleet).json()["request"]
    decide = f"/api/requests/{filed['id']}/decision"
    assert (
        api.post(decide, json={"approve": True}, headers=_as(SCOPE_FLEET_READ)).status_code == 403
    )
    assert api.post(decide, json={"approve": True}, headers=_as_box(fleet)).status_code == 403
    elsewhere = api.post(
        decide, json={"approve": True}, headers=_as(SCOPE_FLEET_WRITE, org="agency-y")
    )
    assert elsewhere.status_code == 404
    assert api.get("/api/requests", headers=_as(SCOPE_FLEET_READ, org="agency-y")).json() == {
        "requests": []
    }
    other = api.post(
        f"/api/boxes/{_id(fleet)}/requests", json={"title": "x"}, headers=_as_box(fleet, "eng-b")
    )
    assert other.status_code in (403, 404), "one agent cannot file for another"
    person = api.post(
        f"/api/boxes/{_id(fleet)}/requests", json={"title": "x"}, headers=_as(SCOPE_FLEET_WRITE)
    )
    assert person.status_code == 403, "an agent files; a person decides"


def test_a_request_needs_a_title(api, fleet):
    assert _file(api, fleet, title="  ").status_code == 422
    assert _file(api, fleet, title="x" * 201).status_code == 422
