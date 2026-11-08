"""The pull-request routes (FLOTTA-171): an agent reports, the agency reads and merges.

On the fleet token, the operator's own path, so the GitHub App is not needed:
the token rules themselves are `repo_tokens`, which the credential route's
tests already hold. GitHub is `test_pulls.FakeGitHub`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flotta.auth import (
    SCOPE_BOX_CHAT,
    SCOPE_FLEET_READ,
    SCOPE_FLEET_WRITE,
    SCOPE_GIT_CREDENTIAL,
    box_subject,
    mint,
)
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.store import FleetStore
from flotta.test_pulls import FakeGitHub

KEY = "a-signing-key-for-pull-request-tests"
PR_URL = "https://github.com/acme/app/pull/7"


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", "tok")
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        for name in ("eng-a", "eng-b"):
            box = store.create_box(name)
            store.update_box_status(box.id, "running", endpoint=f"fly://app/{name}")
        store.grant_repo(store.get_box_by_name("eng-a").id, "acme/app", fleet_token=True)
    return path


@pytest.fixture
def github():
    return FakeGitHub().open(7)


@pytest.fixture
def api(fleet, github):
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
        github_fetch=github,
    )
    with TestClient(app) as c:
        yield c


def _id(fleet, name):
    with FleetStore(fleet) as store:
        return store.get_box_by_name(name).id


def _as_box(fleet, name):
    token = mint(
        subject=box_subject(_id(fleet, name)), scopes={SCOPE_GIT_CREDENTIAL}, key=KEY, ttl_s=300
    )
    return {"Authorization": f"Bearer {token}"}


def _as(*scopes, **claims):
    token = mint(subject="olivia", scopes=set(scopes), key=KEY, ttl_s=300, **claims)
    return {"Authorization": f"Bearer {token}"}


def _report(api, fleet, name="eng-a", url=PR_URL):
    return api.post(
        f"/api/boxes/{_id(fleet, name)}/pull-requests",
        json={"url": url},
        headers=_as_box(fleet, name),
    )


def test_an_agent_reports_its_pull_request_and_the_agency_sees_it(api, fleet):
    reported = _report(api, fleet)
    assert reported.status_code == 200, reported.text
    body = reported.json()
    assert body["created"] is True
    assert body["pull_request"]["title"] == "Change 7"
    assert body["pull_request"]["box_id"] == _id(fleet, "eng-a")

    listed = api.get("/api/pull-requests", headers=_as(SCOPE_FLEET_READ))
    assert [p["number"] for p in listed.json()["pull_requests"]] == [7]
    assert listed.json()["pull_requests"][0]["following"] is None

    again = _report(api, fleet)
    assert again.json()["created"] is False

    with FleetStore(fleet) as store:
        kinds = [e.type for e in store.get_events("box", _id(fleet, "eng-a"))]
    assert kinds.count("pull_request_opened") == 1


def test_an_agent_cannot_report_a_repository_it_was_not_granted(api, fleet, github):
    refused = _report(api, fleet, name="eng-b")
    assert refused.status_code == 403
    assert "not granted" in refused.json()["detail"]


def test_an_agent_cannot_report_for_another_agent(api, fleet):
    refused = api.post(
        f"/api/boxes/{_id(fleet, 'eng-a')}/pull-requests",
        json={"url": PR_URL},
        headers=_as_box(fleet, "eng-b"),
    )
    assert refused.status_code in (403, 404)


def test_a_pull_request_that_does_not_exist_is_refused(api, fleet):
    refused = _report(api, fleet, url="https://github.com/acme/app/pull/99")
    assert refused.status_code == 422
    assert "no pull request acme/app#99" in refused.json()["detail"]


def test_a_url_that_is_not_a_pull_request_is_refused(api, fleet):
    refused = _report(api, fleet, url="https://github.com/acme/app/issues/7")
    assert refused.status_code == 422


def test_a_person_cannot_report_one(api, fleet):
    refused = api.post(
        f"/api/boxes/{_id(fleet, 'eng-a')}/pull-requests",
        json={"url": PR_URL},
        headers=_as(SCOPE_FLEET_WRITE),
    )
    assert refused.status_code == 403


def test_an_agent_reads_only_its_own(api, fleet):
    _report(api, fleet)
    own = api.get(
        f"/api/boxes/{_id(fleet, 'eng-a')}/pull-requests", headers=_as_box(fleet, "eng-a")
    )
    assert [p["number"] for p in own.json()["pull_requests"]] == [7]
    theirs = api.get(
        f"/api/boxes/{_id(fleet, 'eng-a')}/pull-requests", headers=_as_box(fleet, "eng-b")
    )
    assert theirs.status_code in (403, 404)
    everyone = api.get("/api/pull-requests", headers=_as_box(fleet, "eng-a"))
    assert everyone.status_code == 403


def test_another_org_sees_none_of_them(api, fleet):
    _report(api, fleet)
    listed = api.get("/api/pull-requests", headers=_as(SCOPE_FLEET_READ, org="agency-y"))
    assert listed.json()["pull_requests"] == []


def test_the_agency_merges_from_flotta_and_it_says_who(api, fleet, github):
    pr = _report(api, fleet).json()["pull_request"]
    merged = api.post(
        f"/api/pull-requests/{pr['id']}/merge",
        json={"method": "squash"},
        headers=_as(SCOPE_FLEET_WRITE),
    )
    assert merged.status_code == 200, merged.text
    after = merged.json()["pull_request"]
    assert after["state"] == "merged"
    assert after["merged_by"] == "olivia"
    assert after["merged_by_person"]
    assert github.merged == [(7, "squash")]

    again = api.post(f"/api/pull-requests/{pr['id']}/merge", headers=_as(SCOPE_FLEET_WRITE))
    assert again.status_code == 409


def test_githubs_refusal_to_merge_is_said(api, fleet, github):
    pr = _report(api, fleet).json()["pull_request"]
    github.refuse_merge = (405, "At least 1 approving review is required")
    refused = api.post(f"/api/pull-requests/{pr['id']}/merge", headers=_as(SCOPE_FLEET_WRITE))
    assert refused.status_code == 409
    assert "approving review" in refused.json()["detail"]


def test_merging_needs_write_and_the_right_org(api, fleet):
    pr = _report(api, fleet).json()["pull_request"]
    reader = api.post(f"/api/pull-requests/{pr['id']}/merge", headers=_as(SCOPE_FLEET_READ))
    assert reader.status_code == 403
    chatter = api.post(f"/api/pull-requests/{pr['id']}/merge", headers=_as(SCOPE_BOX_CHAT))
    assert chatter.status_code == 403
    elsewhere = api.post(
        f"/api/pull-requests/{pr['id']}/merge", headers=_as(SCOPE_FLEET_WRITE, org="agency-y")
    )
    assert elsewhere.status_code == 404


def test_the_policy_is_read_by_the_agent_and_changed_by_the_agency(api, fleet):
    box = _id(fleet, "eng-a")
    asked = api.get(f"/api/boxes/{box}/pr-policy", headers=_as_box(fleet, "eng-a"))
    assert asked.json() == {"may_merge": False, "follow_reviews": True}

    changed = api.put(
        f"/api/boxes/{box}/pr-policy", json={"may_merge": True}, headers=_as(SCOPE_FLEET_WRITE)
    )
    assert changed.json() == {"may_merge": True, "follow_reviews": True}

    by_agent = api.put(
        f"/api/boxes/{box}/pr-policy", json={"may_merge": True}, headers=_as_box(fleet, "eng-a")
    )
    assert by_agent.status_code == 403, "an agent must not let itself merge"

    for bad in ({"may_merge": "yes"}, {"merge": True}):
        assert (
            api.put(
                f"/api/boxes/{box}/pr-policy", json=bad, headers=_as(SCOPE_FLEET_WRITE)
            ).status_code
            == 422
        )
