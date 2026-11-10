"""Self-serve approval with a starting cap on model spend (FLOTTA-185, M15.3).

The guard first: **billing never approves an org without its cap.** Every
route is sent with a governing token at a pending org, and afterwards the org
is either still pending or active with its cap; and the one route that approves
is checked for the cap directly, including when the setting says nothing.

Then: the cap is its own budget, which an owner can neither change nor remove
and which holds beside the org's own; it refuses model calls at its limit;
lifting it is billing's or the operator's, evented, and idempotent; and the
operator's approvals are as they were.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from flotta import budgets
from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN, mint
from flotta.control.test_sign_in import KEY, Outbox, _app
from flotta.settings import STARTING_CAP_EUR_DEFAULT, starting_cap_eur_nano
from flotta.store import FleetStore
from flotta.test_usage import EARLIER, MODEL, RATE, T0, _price

FIFTY = 50 * 10**9


def _bearer(scopes, **claims):
    return {"Authorization": f"Bearer {mint(subject='t', scopes=scopes, key=KEY, **claims)}"}


OPERATOR = _bearer({SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN})
OWNER = _bearer({SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN}, org="agency-x")
GOVERNOR = {"Authorization": f"Bearer {mint(subject='billing', scopes={'org:govern'}, key=KEY)}"}


@pytest.fixture
def fleet(tmp_path):
    """agency-x signed up and waiting, with an owner and a budget of its own."""
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.create_org("agency-x", "Agency X", status="pending")
        store.create_client("agency-x", "acme", "Acme")
        ana = store.create_user("ana@example.com")
        store.add_membership(ana.id, "agency-x", "owner")
        store.set_budget("agency-x", period="month", limit_eur_nano=500 * 10**9, action="notify")
        store.refresh_prices([_price()], now=EARLIER)
    return path


@pytest.fixture
def api(fleet):
    with TestClient(_app(fleet, mailer=Outbox())) as client:
        yield client


def _cap(fleet):
    with FleetStore(fleet) as store:
        return store.starting_cap("agency-x")


def _status(fleet):
    with FleetStore(fleet) as store:
        return store.get_org("agency-x").status


def _events(fleet):
    with FleetStore(fleet) as store:
        return [e.type for e in store.budget_events("agency-x")]


# -- the guard: billing never approves without the cap ---------------------------


def _routes():
    from flotta.control.app import create_app

    app = create_app(store_factory=lambda: None, run_loop=False, background=False)
    return [
        (method, route.path)
        for route in app.routes
        for method in sorted(getattr(route, "methods", None) or ())
        if method not in ("HEAD", "OPTIONS")
    ]


@pytest.mark.parametrize(("method", "path"), _routes())
def test_no_route_lets_billing_approve_an_org_without_its_cap(api, fleet, method, path):
    url = re.sub(
        r"\{(\w+)(?::\w+)?\}",
        lambda m: {"org_id": "agency-x", "client_id": "acme"}.get(m.group(1), "x"),
        path,
    )
    api.request(method, url, headers=GOVERNOR, json={"starting_cap": False})
    if _status(fleet) != "pending":
        assert _status(fleet) == "active", (method, path)
        cap = _cap(fleet)
        assert cap is not None, f"{method} {path} approved agency-x without its starting cap"
        assert (cap.limit_eur_nano, cap.action, cap.period) == (FIFTY, "pause", "month")


def test_billing_approves_with_the_cap_even_when_it_asks_for_none(api, fleet):
    answer = api.post("/api/orgs/agency-x/approve", headers=GOVERNOR, json={"starting_cap": False})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["org"]["status"] == "active"
    assert body["starting_cap"]["starting_cap"] is True
    assert body["starting_cap"]["limit_eur"] == "50"
    assert _events(fleet) == ["starting_cap_set"]


@pytest.mark.parametrize("stored", ["0", "-5", "nonsense", "", "inf"])
def test_a_setting_that_is_no_cap_falls_back_to_the_default(stored):
    assert starting_cap_eur_nano(_StoreWith(stored), env={}) == (
        int(STARTING_CAP_EUR_DEFAULT) * 10**9
    )
    assert starting_cap_eur_nano(_StoreWith("12.5"), env={}) == 12_500_000_000


class _StoreWith:
    """A store whose only setting is the cap, as `layered` reads it."""

    def __init__(self, value):
        self.value = value

    def all_settings(self):
        return {"FLOTTA_STARTING_CAP_EUR": self.value}


def test_the_setting_decides_the_cap(api, fleet, monkeypatch):
    monkeypatch.setenv("FLOTTA_STARTING_CAP_EUR", "20")
    api.post("/api/orgs/agency-x/approve", headers=GOVERNOR)
    assert _cap(fleet).limit_eur_nano == 20 * 10**9


# -- what the cap is, and what an owner can do about it ---------------------------


def _approved(api):
    answer = api.post("/api/orgs/agency-x/approve", headers=GOVERNOR)
    assert answer.status_code == 200, answer.text


def test_it_is_its_own_budget_beside_the_orgs_own(api, fleet):
    _approved(api)
    listed = api.get("/api/orgs/agency-x/budgets", headers=OWNER).json()
    rows = listed["budgets"] if isinstance(listed, dict) else listed
    month = [b for b in rows if b["scope"] == "org" and b["period"] == "month"]
    assert sorted((b["limit_eur"], b["starting_cap"]) for b in month) == [
        ("50", True),
        ("500", False),
    ]


def test_an_owner_can_neither_change_nor_remove_it(api, fleet):
    _approved(api)
    before = _cap(fleet)
    raised = api.put(
        "/api/orgs/agency-x/budgets",
        headers=OWNER,
        json={"period": "month", "limit_eur": "10000", "action": "notify"},
    )
    assert raised.status_code == 200, raised.text
    removed = api.delete("/api/orgs/agency-x/budgets?period=month", headers=OWNER)
    assert removed.status_code == 200, removed.text
    assert _cap(fleet) == before
    lifted = api.delete("/api/orgs/agency-x/starting-cap", headers=OWNER)
    assert lifted.status_code == 403, lifted.text
    assert _cap(fleet) == before


def test_it_refuses_model_calls_at_its_limit(api, fleet):
    _approved(api)
    with FleetStore(fleet) as store:
        box = store.create_box("support", org="agency-x", client="acme")
        # Six calls of nine euros: past the cap's fifty, well short of the org's 500.
        for i in range(6):
            store.record_usage(
                box.id,
                model=MODEL,
                upstream="openrouter",
                tokens_in=1_000_000,
                tokens_out=1_000_000,
                idempotency_key=f"k{i}",
                eur_per_usd=RATE,
                now=T0,
            )
        refusal = budgets.check_budgets(store, box, model=MODEL, eur_per_usd=RATE, now=T0)
        assert isinstance(refusal, budgets.Refusal)
        assert refusal.budget["starting_cap"] is True
        assert budgets.holding(store, box, T0), "the sweep would not pause it"

        assert store.lift_starting_cap("agency-x", by="test") is True
        assert budgets.check_budgets(store, box, model=MODEL, eur_per_usd=RATE, now=T0) is None
        assert budgets.holding(store, box, T0) == []


# -- lifting it -------------------------------------------------------------------


@pytest.mark.parametrize("who", [GOVERNOR, OPERATOR], ids=["billing", "operator"])
def test_lifting_it_is_explicit_evented_and_idempotent(api, fleet, who):
    _approved(api)
    first = api.delete("/api/orgs/agency-x/starting-cap", headers=who)
    assert first.status_code == 200, first.text
    assert first.json() == {"org": "agency-x", "lifted": True}
    assert _cap(fleet) is None
    # A renewal webhook delivered again.
    again = api.delete("/api/orgs/agency-x/starting-cap", headers=who)
    assert again.json()["lifted"] is False
    assert _events(fleet) == ["starting_cap_set", "starting_cap_lifted"]
    # The org's own budget is still there.
    with FleetStore(fleet) as store:
        assert [b.limit_eur_nano for b in store.budgets_for_org("agency-x")] == [500 * 10**9]


def test_lifting_an_unknown_orgs_cap_is_404(api):
    assert api.delete("/api/orgs/nope/starting-cap", headers=GOVERNOR).status_code == 404


def test_lifting_it_alerts_nobody(api, fleet):
    """New budget event types, and only the thresholds are alerts."""
    from flotta.store import ALERT_THRESHOLDS

    assert {"starting_cap_set", "starting_cap_lifted"}.isdisjoint(ALERT_THRESHOLDS)


# -- the operator's approvals -----------------------------------------------------


def test_the_operator_approves_without_a_cap_as_before(api, fleet):
    answer = api.post("/api/orgs/agency-x/approve", headers=OPERATOR)
    assert answer.status_code == 200, answer.text
    assert answer.json()["starting_cap"] is None
    assert _cap(fleet) is None
    assert _events(fleet) == []


def test_the_operator_may_ask_for_one(api, fleet):
    answer = api.post("/api/orgs/agency-x/approve", headers=OPERATOR, json={"starting_cap": True})
    assert answer.status_code == 200, answer.text
    assert _cap(fleet).limit_eur_nano == FIFTY


def test_an_owner_cannot_approve_its_own_org(api, fleet):
    assert api.post("/api/orgs/agency-x/approve", headers=OWNER).status_code == 403
    assert _status(fleet) == "pending"


def test_the_cli_approves_with_or_without_it(fleet, monkeypatch):
    from typer.testing import CliRunner

    from flotta.cli import app

    monkeypatch.delenv("FLOTTA_CONTROL_URL", raising=False)
    monkeypatch.setenv("FLOTTA_STORE", str(fleet))
    with FleetStore(fleet) as store:
        store.create_org("agency-y", "Agency Y", status="pending")
    plain = CliRunner().invoke(app, ["org", "approve", "agency-y"])
    assert plain.exit_code == 0, plain.output
    capped = CliRunner().invoke(app, ["org", "approve", "agency-x", "--starting-cap"])
    assert capped.exit_code == 0, capped.output
    with FleetStore(fleet) as store:
        assert store.starting_cap("agency-y") is None
        assert store.starting_cap("agency-x").limit_eur_nano == FIFTY
    lifted = CliRunner().invoke(app, ["org", "lift-cap", "agency-x"])
    assert "is lifted" in lifted.output
    assert _cap(fleet) is None


def test_a_suspended_org_keeps_its_cap(api, fleet):
    """Suspension (FLOTTA-184) is not a renewal: nothing about it lifts the cap."""
    _approved(api)
    with FleetStore(fleet) as store:
        store.suspend_org("agency-x")
        store.resume_org("agency-x")
    assert _cap(fleet) is not None
