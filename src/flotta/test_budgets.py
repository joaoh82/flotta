"""Budgets in the store (FLOTTA-89, M13.3), on every engine the suite has."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from flotta.budgets import eur_text, parse_limit, period_start, resets_at
from flotta.pricing import NANO, Price
from flotta.store import FleetStore, InvalidBudgetError, UnknownEntityError
from flotta.test_migrations import target  # noqa: F401


@pytest.fixture
def fleet(target):  # noqa: F811
    with FleetStore(target) as store:
        store.create_org("studio-nord", "Studio Nord")
        store.create_client("studio-nord", "acme", "Acme Dental")
        store.create_client("studio-nord", "bravo", "Bravo Legal")
        store.create_box("one", box_id="b-one", org="studio-nord", client="acme")
        store.create_box("three", box_id="b-three", org="studio-nord", client="bravo")
        store.create_org("other", "Other Agency")
        store.create_client("other", "acme", "Another Acme")
        store.create_box("four", box_id="b-four", org="other", client="acme")
        yield store


def _set(store, org="studio-nord", **overrides):
    values = {"period": "day", "limit_eur_nano": NANO, "action": "pause", **overrides}
    return store.set_budget(org, **values)


def test_a_budget_is_stored_and_read_back(fleet):
    budget = _set(fleet, client="acme", soft_pct=0.5)
    assert fleet.budgets_for_org("studio-nord") == [budget]
    assert budget.org == "studio-nord"
    assert budget.client == "acme"
    assert budget.box is None
    assert budget.limit_eur_nano == NANO
    assert budget.soft_pct == 0.5
    assert budget.action == "pause"


def test_soft_pct_defaults_to_four_fifths(fleet):
    assert _set(fleet).soft_pct == 0.8


def test_setting_the_same_scope_and_period_again_replaces_it(fleet):
    first = _set(fleet, client="acme")
    second = _set(fleet, client="acme", limit_eur_nano=2 * NANO)
    assert second.id == first.id
    assert second.limit_eur_nano == 2 * NANO
    assert fleet.budgets_for_org("studio-nord") == [second]


def test_each_scope_and_period_is_its_own_budget(fleet):
    _set(fleet)
    _set(fleet, period="month")
    _set(fleet, client="acme")
    _set(fleet, box="b-one")
    assert len(fleet.budgets_for_org("studio-nord")) == 4


def test_a_box_budget_takes_its_client_from_the_box(fleet):
    budget = _set(fleet, box="b-one")
    assert budget.client == "acme"


def test_a_box_can_be_named_by_its_address(fleet):
    assert _set(fleet, box="one--acme--studio-nord").box == "b-one"


def test_budgets_are_listed_by_org_only(fleet):
    _set(fleet)
    assert fleet.budgets_for_org("other") == []


def test_covering_finds_org_client_and_box_and_nothing_else(fleet):
    org_wide = _set(fleet)
    client = _set(fleet, client="acme")
    own = _set(fleet, box="b-one")
    _set(fleet, client="bravo")
    _set(fleet, box="b-three")
    _set(fleet, org="other")
    _set(fleet, org="other", client="acme")
    box = fleet.get_box("b-one")
    assert {b.id for b in fleet.budgets_covering(box)} == {org_wide.id, client.id, own.id}


def test_deleting_removes_only_that_budget(fleet):
    _set(fleet)
    keep = _set(fleet, client="acme")
    assert fleet.delete_budget("studio-nord", period="day") is True
    assert fleet.budgets_for_org("studio-nord") == [keep]


def test_deleting_what_is_not_there_says_so(fleet):
    assert fleet.delete_budget("studio-nord", client="acme", period="day") is False


def test_deleting_a_scope_that_cannot_be_one_says_nothing_was_there(fleet):
    assert fleet.delete_budget("studio-nord", client="bravo", box="b-one", period="day") is False


def test_deleting_does_not_reach_another_org(fleet):
    _set(fleet, org="other", client="acme")
    assert fleet.delete_budget("studio-nord", client="acme", period="day") is False
    assert len(fleet.budgets_for_org("other")) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"period": "week"},
        {"period": "run"},
        {"action": "explode"},
        {"limit_eur_nano": 0},
        {"limit_eur_nano": -1},
        {"soft_pct": 0.0},
        {"soft_pct": 1.5},
        {"action": "downgrade"},
        {"action": "downgrade", "fallback_model": "two words"},
    ],
)
def test_what_cannot_be_a_budget_is_refused(fleet, bad):
    with pytest.raises(InvalidBudgetError):
        _set(fleet, **bad)
    assert fleet.budgets_for_org("studio-nord") == []


def test_a_downgrade_takes_a_fallback(fleet):
    fleet.refresh_prices([Price("fake/cheap", NANO, NANO, None, "USD", "test")])
    budget = _set(fleet, action="downgrade", fallback_model="fake/cheap")
    assert budget.fallback_model == "fake/cheap"


@pytest.mark.parametrize(
    ("scope", "error"),
    [
        ({"client": "nobody"}, UnknownEntityError),
        ({"box": "b-nobody"}, UnknownEntityError),
        # Another org's agent is the same as no agent.
        ({"box": "b-four"}, UnknownEntityError),
        # The agent is real, and not that client's.
        ({"client": "bravo", "box": "b-one"}, InvalidBudgetError),
    ],
)
def test_a_budget_cannot_name_what_is_not_this_orgs(fleet, scope, error):
    with pytest.raises(error):
        _set(fleet, **scope)


def test_a_budget_for_an_org_that_does_not_exist_is_refused(fleet):
    with pytest.raises(UnknownEntityError):
        _set(fleet, org="nobody")


NOW = datetime(2026, 11, 17, 15, 30, tzinfo=UTC)


def test_a_day_starts_at_midnight_utc():
    assert period_start("day", NOW) == datetime(2026, 11, 17, tzinfo=UTC)
    assert resets_at("day", NOW) == datetime(2026, 11, 18, tzinfo=UTC)


def test_a_month_starts_on_the_first():
    assert period_start("month", NOW) == datetime(2026, 11, 1, tzinfo=UTC)
    assert resets_at("month", NOW) == datetime(2026, 12, 1, tzinfo=UTC)


def test_december_rolls_into_january():
    assert resets_at("month", datetime(2026, 12, 31, tzinfo=UTC)) == datetime(
        2027, 1, 1, tzinfo=UTC
    )


def test_a_period_nobody_knows_is_an_error_not_all_time():
    with pytest.raises(ValueError):
        period_start("week", NOW)


@pytest.mark.parametrize(
    ("text", "nano"),
    [("20", 20 * NANO), ("0.01", NANO // 100), (20, 20 * NANO), (0.01, NANO // 100)],
)
def test_a_limit_becomes_exact_nanos(text, nano):
    assert parse_limit(text) == nano


@pytest.mark.parametrize("bad", ["", "abc", "nan", "inf", None, True, "0.0000000001", "1e30"])
def test_what_cannot_be_a_limit_is_refused(bad):
    with pytest.raises(InvalidBudgetError):
        parse_limit(bad)


def test_money_reads_back_without_float_noise():
    assert eur_text(NANO // 100) == "0.01"
    assert eur_text(20 * NANO) == "20"
    assert eur_text(1_220_000) == "0.00122"
    assert Decimal(eur_text(123_456_789)) == Decimal("0.123456789")
