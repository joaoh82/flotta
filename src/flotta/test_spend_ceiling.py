# ruff: noqa: F811 - the fixtures imported from test_budget_actions are parameters here
"""An org's spend ceiling: what it may owe in model spend between invoices (FLOTTA-241).

The guards first. **An org at its ceiling makes no paid model call**, through
the gateway every path's calls go through, and the sweep pauses its agents.
**Raising it lets the next call through with no restart**, since nothing is
cached. Then: it counts only spend on the fleet's keys, its window is the
billing period billing last recorded, and the default never overwrites a
ceiling somebody decided.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from flotta import budgets
from flotta.budgets import billing_window
from flotta.gateway.fake import FAKE_MODEL, FakeUpstream
from flotta.pricing import NANO
from flotta.store import InvalidBudgetError, Org, ceiling_budget
from flotta.test_budget_actions import (  # noqa: F401 - fixtures
    CALL,
    NOON,
    RATE,
    _box_auth,
    _gateway,
    _spend,
    _sweep,
    _types,
    fleet,
    machine,
    path,
)

ORG = "studio-nord"


def _org(**fields) -> Org:
    return Org(id=ORG, name="Studio Nord", created_at="2026-01-01T00:00:00+00:00", **fields)


# -- the guards ---------------------------------------------------------------


def test_an_org_at_its_ceiling_makes_no_model_call(path, fleet):
    fleet.set_spend_ceiling(ORG, NANO, by="test")
    _spend(fleet, NANO, now=datetime.now(UTC))
    upstream = FakeUpstream()
    with _gateway(path, upstream) as gateway:
        refused = gateway.post("/v1/chat/completions", json=CALL, headers=_box_auth())
        assert refused.status_code == 402, refused.text
        assert upstream.requests == []
        error = refused.json()["error"]
        assert error["budget"]["ceiling"] is True
        assert "spending ceiling" in error["message"]
        assert "Ask Flotta for a higher ceiling" in error["message"]
        # Any agent of the org, whichever client it is for.
        other = gateway.post(
            "/v1/chat/completions", json=CALL, headers=_box_auth("b-three", client="bravo")
        )
        assert other.status_code == 402

        # Raised: the very next call goes out, on the same gateway.
        fleet.set_spend_ceiling(ORG, 10 * NANO, by="test")
        passed = gateway.post("/v1/chat/completions", json=CALL, headers=_box_auth())
        assert passed.status_code == 200, passed.text
        assert len(upstream.requests) == 1


def test_the_sweep_pauses_the_orgs_agents_and_releases_them_when_raised(fleet, machine):
    fleet.set_spend_ceiling(ORG, NANO, by="test")
    _spend(fleet, NANO)
    _sweep(fleet)
    assert {fleet.get_box(b).status for b in ("b-one", "b-two", "b-three")} == {"paused"}
    assert "budget_limit" in _types(fleet)

    fleet.set_spend_ceiling(ORG, None, by="test")
    _sweep(fleet)
    assert "paused" not in {fleet.get_box(b).status for b in ("b-one", "b-two", "b-three")}


def test_the_owner_is_told_at_eighty_percent_once(fleet, machine):
    fleet.set_spend_ceiling(ORG, 10 * NANO, by="test")
    _spend(fleet, 8 * NANO)
    _sweep(fleet)
    _sweep(fleet)
    assert _types(fleet).count("budget_soft_limit") == 1
    assert "budget_limit" not in _types(fleet)
    assert fleet.get_box("b-one").status == "running"
    soft = [e for e in fleet.budget_events(ORG) if e.type == "budget_soft_limit"]
    assert [e.budget for e in soft] == [f"ceiling:{ORG}"]


def test_an_owner_cannot_unpause_past_it(fleet, machine):
    from flotta.provision import BudgetHolds, unpause_box

    fleet.set_spend_ceiling(ORG, NANO, by="test")
    _spend(fleet, NANO)
    _sweep(fleet)
    with pytest.raises(BudgetHolds):
        unpause_box("b-one", store=fleet, now=NOON)


# -- what it counts -------------------------------------------------------------


def test_it_counts_only_spend_on_the_fleets_keys(fleet):
    fleet.set_spend_ceiling(ORG, NANO, by="test")
    box = fleet.get_box("b-one")
    fleet.record_usage(
        box.id,
        model=FAKE_MODEL,
        upstream="openrouter",
        tokens_in=1,
        tokens_out=1,
        idempotency_key="own",
        eur_per_usd=RATE,
        reported_cost_nano=10 * NANO,
        key_scope="org",
        now=NOON,
    )
    assert budgets.holding(fleet, box, NOON) == []
    _spend(fleet, NANO)
    assert budgets.holding(fleet, box, NOON)


def test_a_new_billing_period_resets_it(fleet):
    fleet.set_spend_ceiling(ORG, NANO, by="test")
    fleet.set_billing_period(ORG, NOON - timedelta(days=3))
    _spend(fleet, NANO, now=NOON - timedelta(hours=1))
    box = fleet.get_box("b-one")
    assert budgets.holding(fleet, box, NOON)
    # The renewal: billing records the new period's start.
    fleet.set_billing_period(ORG, NOON - timedelta(minutes=30))
    assert budgets.holding(fleet, box, NOON) == []


def test_an_org_with_no_ceiling_is_unlimited(fleet):
    _spend(fleet, 10**6 * NANO)
    assert ceiling_budget(fleet.get_org(ORG)) is None
    assert budgets.holding(fleet, fleet.get_box("b-one"), NOON) == []


def test_a_ceiling_is_more_than_zero(fleet):
    with pytest.raises(InvalidBudgetError):
        fleet.set_spend_ceiling(ORG, 0, by="test")


# -- the default ------------------------------------------------------------------


def test_the_default_is_given_once_and_never_over_a_decision(fleet):
    assert fleet.default_spend_ceiling(ORG, 500 * NANO, by="billing") is True
    assert fleet.get_org(ORG).spend_ceiling_eur_nano == 500 * NANO
    assert fleet.default_spend_ceiling(ORG, 500 * NANO, by="billing") is False

    fleet.set_spend_ceiling(ORG, 2000 * NANO, by="operator")
    assert fleet.default_spend_ceiling(ORG, 500 * NANO, by="billing") is False
    assert fleet.get_org(ORG).spend_ceiling_eur_nano == 2000 * NANO

    # None, decided, is not "never decided".
    fleet.set_spend_ceiling(ORG, None, by="operator")
    assert fleet.default_spend_ceiling(ORG, 500 * NANO, by="billing") is False
    assert fleet.get_org(ORG).spend_ceiling_eur_nano is None


def test_every_decision_is_on_the_budget_timeline(fleet):
    fleet.default_spend_ceiling(ORG, 500 * NANO, by="billing")
    fleet.set_spend_ceiling(ORG, None, by="operator")
    sets = [e for e in fleet.budget_events(ORG) if e.type == "spend_ceiling_set"]
    assert [(e.payload["limit_eur_nano"], e.payload["by"]) for e in sets] == [
        (500 * NANO, "billing"),
        (None, "operator"),
    ]


# -- the window -------------------------------------------------------------------

NOV_17 = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


def test_no_billing_period_is_the_calendar_month():
    assert billing_window(_org(), NOV_17) == (
        datetime(2026, 11, 1, tzinfo=UTC),
        datetime(2026, 12, 1, tzinfo=UTC),
    )


def test_the_window_runs_from_the_recorded_start_for_a_month():
    org = _org(billing_period_from="2026-11-05T14:00:00+00:00")
    assert billing_window(org, NOV_17) == (
        datetime(2026, 11, 5, 14, tzinfo=UTC),
        datetime(2026, 12, 5, 14, tzinfo=UTC),
    )


def test_a_missed_renewal_still_starts_a_new_window_each_month():
    org = _org(billing_period_from="2026-08-20T00:00:00+00:00")
    assert billing_window(org, NOV_17) == (
        datetime(2026, 10, 20, tzinfo=UTC),
        datetime(2026, 11, 20, tzinfo=UTC),
    )


def test_a_period_anchored_on_the_31st_renews_on_the_months_last_day():
    org = _org(billing_period_from="2026-01-31T00:00:00+00:00")
    assert billing_window(org, datetime(2026, 2, 15, tzinfo=UTC)) == (
        datetime(2026, 1, 31, tzinfo=UTC),
        datetime(2026, 2, 28, tzinfo=UTC),
    )
    assert billing_window(org, datetime(2026, 3, 30, tzinfo=UTC))[0] == datetime(
        2026, 2, 28, tzinfo=UTC
    )
    assert billing_window(org, datetime(2026, 3, 31, 1, tzinfo=UTC))[0] == datetime(
        2026, 3, 31, tzinfo=UTC
    )


def test_a_start_a_little_in_the_future_counts_from_the_month_before():
    org = _org(billing_period_from="2026-11-17T10:00:00+00:00")
    since, resets = billing_window(org, NOV_17)
    assert since == datetime(2026, 10, 17, 10, tzinfo=UTC)
    assert resets == datetime(2026, 11, 17, 10, tzinfo=UTC)
