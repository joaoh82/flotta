"""The spend ceiling's routes (FLOTTA-241).

The guard: **only the operator and billing decide a ceiling.** An org's own
owner reads it and cannot change it, a client's people cannot see it, and the
default billing applies never overwrites a ceiling somebody decided.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN, mint
from flotta.control.test_sign_in import KEY, Outbox, _app
from flotta.store import FleetStore


def _bearer(scopes, **claims):
    return {"Authorization": f"Bearer {mint(subject='t', scopes=scopes, key=KEY, **claims)}"}


OPERATOR = _bearer({SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN})
OWNER = _bearer({SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN}, org="agency-x")
CLIENT = _bearer({SCOPE_FLEET_READ}, org="agency-x", client="acme")
STRANGER = _bearer({SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN}, org="agency-y")
GOVERNOR = {"Authorization": f"Bearer {mint(subject='billing', scopes={'org:govern'}, key=KEY)}"}
URL = "/api/orgs/agency-x/spend-ceiling"


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.create_org("agency-x", "Agency X")
        store.create_org("agency-y", "Agency Y")
        store.create_client("agency-x", "acme", "Acme")
    return path


@pytest.fixture
def api(fleet):
    with TestClient(_app(fleet, mailer=Outbox())) as client:
        yield client


def _ceiling(fleet):
    with FleetStore(fleet) as store:
        return store.get_org("agency-x").spend_ceiling_eur_nano


def test_an_undecided_org_has_none(api):
    view = api.get(URL, headers=OWNER).json()
    assert view["limit_eur"] is None
    assert view["decided"] is False
    assert view["reached"] is False


@pytest.mark.parametrize("who", [OPERATOR, GOVERNOR], ids=["operator", "billing"])
def test_the_operator_and_billing_decide_it(api, fleet, who):
    answer = api.put(URL, headers=who, json={"limit_eur": "750"})
    assert answer.status_code == 200, answer.text
    assert answer.json()["limit_eur"] == "750"
    assert answer.json()["decided"] is True
    none = api.put(URL, headers=who, json={"limit_eur": None})
    assert none.json()["limit_eur"] is None
    assert none.json()["decided"] is True
    assert _ceiling(fleet) is None


def test_an_owner_reads_it_and_cannot_change_it(api, fleet):
    api.put(URL, headers=OPERATOR, json={"limit_eur": "750"})
    assert api.get(URL, headers=OWNER).json()["limit_eur"] == "750"
    for answer in (
        api.put(URL, headers=OWNER, json={"limit_eur": None}),
        api.post(f"{URL}/default", headers=OWNER),
        api.put(
            "/api/orgs/agency-x/billing-period",
            headers=OWNER,
            json={"from": "2026-01-01T00:00:00Z"},
        ),
    ):
        assert answer.status_code == 403, answer.text
    assert _ceiling(fleet) == 750 * 10**9


@pytest.mark.parametrize("who", [CLIENT, STRANGER], ids=["client", "another-org"])
def test_nobody_else_sees_it(api, who):
    assert api.get(URL, headers=who).status_code == 404


def test_the_default_is_given_once(api, fleet, monkeypatch):
    monkeypatch.setenv("FLOTTA_SPEND_CEILING_EUR", "300")
    first = api.post(f"{URL}/default", headers=GOVERNOR)
    assert first.status_code == 200, first.text
    assert first.json()["set"] is True
    assert first.json()["limit_eur"] == "300"
    api.put(URL, headers=OPERATOR, json={"limit_eur": "5000"})
    again = api.post(f"{URL}/default", headers=GOVERNOR).json()
    assert again["set"] is False
    assert again["limit_eur"] == "5000"


def test_a_default_that_is_no_amount_is_the_fleets(api, monkeypatch):
    monkeypatch.setenv("FLOTTA_SPEND_CEILING_EUR", "0")
    assert api.post(f"{URL}/default", headers=GOVERNOR).json()["limit_eur"] == "500"


@pytest.mark.parametrize("bad", [{"limit_eur": "0"}, {"limit_eur": "-3"}, {"limit_eur": "x"}, {}])
def test_a_ceiling_that_is_no_amount_is_refused(api, fleet, bad):
    assert api.put(URL, headers=OPERATOR, json=bad).status_code == 422
    assert _ceiling(fleet) is None


def test_billing_sets_the_window(api):
    answer = api.put(
        "/api/orgs/agency-x/billing-period",
        headers=GOVERNOR,
        json={"from": "2026-11-05T14:00:00Z"},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["billing_period_from"] == "2026-11-05T14:00:00+00:00"


@pytest.mark.parametrize("bad", ["yesterday", "2026-11-05T14:00:00", 17, None])
def test_a_window_that_is_no_moment_is_refused(api, bad):
    answer = api.put("/api/orgs/agency-x/billing-period", headers=GOVERNOR, json={"from": bad})
    assert answer.status_code == 422


def test_an_unknown_org_is_404(api):
    assert (
        api.put(
            "/api/orgs/nope/spend-ceiling", headers=GOVERNOR, json={"limit_eur": "1"}
        ).status_code
        == 404
    )
    assert api.post("/api/orgs/nope/spend-ceiling/default", headers=GOVERNOR).status_code == 404


def test_the_cli_reads_and_sets_it(fleet, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from flotta.cli import app

    # From a directory with no `.env`, so the CLI uses this store (see conftest).
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLOTTA_CONTROL_URL", raising=False)
    monkeypatch.setenv("FLOTTA_STORE", str(fleet))
    read = CliRunner().invoke(app, ["org", "ceiling", "agency-x"])
    assert "no spend ceiling" in read.output, read.output
    set_ = CliRunner().invoke(app, ["org", "ceiling", "agency-x", "--eur", "1"])
    assert "of a EUR 1 ceiling" in set_.output, set_.output
    assert _ceiling(fleet) == 10**9
    CliRunner().invoke(app, ["org", "ceiling", "agency-x", "--none"])
    assert _ceiling(fleet) is None
    both = CliRunner().invoke(app, ["org", "ceiling", "agency-x", "--eur", "1", "--none"])
    assert both.exit_code != 0
