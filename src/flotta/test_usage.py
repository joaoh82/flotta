"""The meter's ledger (FLOTTA-87, M13.1): `usage`, `model_prices`, and `spend`.

Run on every engine the suite has, like `test_accounts.py`, because the tables
arrive by migration 6 and a budget has to mean the same on SQLite and Postgres.

The first two tests are the guards. An invoice must not change when a price
does, so a call's cost is worked out once, when it is written. And the gateway
may retry a write, so the same call recorded twice counts once.
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from flotta import store as store_module
from flotta.pricing import NANO, Price
from flotta.store import FleetStore, UnknownEntityError
from flotta.test_migrations import target  # noqa: F401

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
EARLIER = T0 - timedelta(days=1)
RATE = Decimal("0.5")
MODEL = "anthropic/claude-test"


def _price(input_usd: str = "3", output_usd: str = "15", cached_usd: str | None = None) -> Price:
    """A price in dollars per million tokens, as a person would say it."""
    return Price(
        model=MODEL,
        input_nano_per_m=int(Decimal(input_usd) * NANO),
        output_nano_per_m=int(Decimal(output_usd) * NANO),
        cached_nano_per_m=None if cached_usd is None else int(Decimal(cached_usd) * NANO),
        currency="USD",
        source="test",
    )


@pytest.fixture
def fleet(target):  # noqa: F811
    with FleetStore(target) as store:
        yield store


@pytest.fixture
def agency(fleet):
    """Two clients of one org and a client of another, each with an agent."""
    fleet.create_org("studio-nord", "Studio Nord")
    fleet.create_client("studio-nord", "acme", "Acme Dental")
    fleet.create_client("studio-nord", "bravo", "Bravo Legal")
    fleet.create_org("other", "Other Agency")
    # The same client id in another org: a client's id is only unique in its org.
    fleet.create_client("other", "acme", "Another Acme")
    fleet.create_box("one", box_id="b-one", org="studio-nord", client="acme")
    fleet.create_box("two", box_id="b-two", org="studio-nord", client="acme")
    fleet.create_box("three", box_id="b-three", org="studio-nord", client="bravo")
    fleet.create_box("four", box_id="b-four", org="other", client="acme")
    fleet.refresh_prices([_price()], now=EARLIER)
    return fleet


def _call(store: FleetStore, box: str = "b-one", key: str = "k1", **overrides):
    """One million tokens in and out: 3 + 15 = 18 dollars, 9 euros at `RATE`."""
    call = {
        "model": MODEL,
        "upstream": "openrouter",
        "tokens_in": 1_000_000,
        "tokens_out": 1_000_000,
        "idempotency_key": key,
        "eur_per_usd": RATE,
        "now": T0,
    }
    return store.record_usage(box, **{**call, **overrides})


def test_a_stored_cost_is_never_recomputed(agency):
    """The guard: a price change reaches the next call and no call before it."""
    first = _call(agency, key="before")
    assert first.upstream_cost_nano == 18 * NANO
    assert first.cost_eur_nano == 9 * NANO

    agency.refresh_prices([_price("300", "1500")], now=T0 + timedelta(minutes=1))

    assert agency.spend(org="studio-nord", since=EARLIER).eur_nano == 9 * NANO
    same = _call(agency, key="before", now=T0 + timedelta(minutes=2))
    assert same.cost_eur_nano == 9 * NANO

    after = _call(agency, key="after", now=T0 + timedelta(minutes=2))
    assert after.cost_eur_nano == 900 * NANO
    assert agency.spend(org="studio-nord", since=EARLIER).eur_nano == 909 * NANO


def test_a_change_of_exchange_rate_does_not_touch_a_stored_cost(agency):
    first = _call(agency, key="before", eur_per_usd=Decimal("0.5"))
    assert first.eur_per_usd == "0.5"
    _call(agency, key="after", eur_per_usd=Decimal("1"))
    assert agency.spend(org="studio-nord", since=EARLIER).eur_nano == (9 + 18) * NANO


def test_the_same_call_recorded_twice_counts_once(agency):
    """The guard: the gateway may retry a write it did not see succeed."""
    first = _call(agency, key="retry")
    second = _call(agency, key="retry")
    assert second.id == first.id
    assert agency.spend(org="studio-nord", since=EARLIER).eur_nano == 9 * NANO


def test_a_retry_cannot_change_what_was_recorded(agency):
    first = _call(agency, key="retry")
    second = _call(agency, key="retry", tokens_in=5, tokens_out=5)
    assert second.tokens_in == first.tokens_in == 1_000_000


def test_one_box_cannot_use_up_anothers_idempotency_key(agency):
    """A key is one box's. Sharing one must not lose the other box's call."""
    _call(agency, box="b-one", key="same")
    _call(agency, box="b-two", key="same")
    assert agency.spend(org="studio-nord", since=EARLIER).eur_nano == 18 * NANO


def test_a_call_is_attributed_from_the_box_row(agency):
    """No caller says whose call it was, so no caller can say it wrongly."""
    row = _call(agency, box="b-three")
    assert (row.org, row.client, row.box) == ("studio-nord", "bravo", "b-three")
    assert row.kind == "model"
    assert row.key_scope == "fleet"


def test_a_call_for_a_box_that_does_not_exist_is_refused(agency):
    with pytest.raises(UnknownEntityError):
        _call(agency, box="b-nobody")


@pytest.mark.parametrize(
    ("counts", "bad"),
    [
        ({"tokens_in": -1}, "tokens_in"),
        ({"tokens_out": -1}, "tokens_out"),
        ({"tokens_cached": -1}, "tokens_cached"),
        ({"key_scope": "someone"}, "key_scope"),
    ],
)
def test_what_would_lower_or_misfile_spend_is_refused(agency, counts, bad):
    with pytest.raises(ValueError, match=bad):
        _call(agency, **counts)


def test_spend_is_scoped_to_what_was_asked(agency):
    _call(agency, box="b-one", key="a")
    _call(agency, box="b-two", key="b")
    _call(agency, box="b-three", key="c")
    _call(agency, box="b-four", key="d")

    assert agency.spend(org="studio-nord", since=EARLIER).eur_nano == 27 * NANO
    assert agency.spend(org="studio-nord", client="acme", since=EARLIER).eur_nano == 18 * NANO
    assert agency.spend(org="studio-nord", client="bravo", since=EARLIER).eur_nano == 9 * NANO
    assert agency.spend(org="studio-nord", box="b-one", since=EARLIER).eur_nano == 9 * NANO
    # The other org's client has the same id, and its call is not counted above.
    assert agency.spend(org="other", client="acme", since=EARLIER).eur_nano == 9 * NANO
    # A box of another org is not spend of this one.
    assert agency.spend(org="studio-nord", box="b-four", since=EARLIER).eur_nano == 0


def test_nothing_spent_is_zero_not_none(agency):
    spent = agency.spend(org="studio-nord", since=EARLIER)
    assert spent.eur_nano == 0
    assert spent.unpriced == 0


def test_since_includes_the_moment_it_names(agency):
    _call(agency, key="whole-second", now=T0)
    _call(agency, key="fraction", now=T0 + timedelta(microseconds=1))
    _call(agency, key="old", now=T0 - timedelta(microseconds=1))
    assert agency.spend(org="studio-nord", since=T0).eur_nano == 18 * NANO


def test_since_means_the_same_instant_in_any_timezone(agency):
    """Timestamps compare as text, so one written as 07:00-05:00 would sort
    before the 12:00+00:00 it comes a second after."""
    _call(agency, now=T0)
    a_second_later = T0.astimezone(timezone(timedelta(hours=-5))) + timedelta(seconds=1)
    assert a_second_later.isoformat() < T0.isoformat()
    assert agency.spend(org="studio-nord", since=a_second_later).eur_nano == 0


def test_a_time_with_no_timezone_is_refused(agency):
    """A budget's day that starts somewhere unknown is off by hours, silently."""
    with pytest.raises(ValueError, match="timezone"):
        agency.spend(org="studio-nord", since=datetime(2026, 11, 2))


# -- what cannot be priced ---------------------------------------------------


def test_a_model_with_no_price_is_recorded_and_counted_as_unpriced(agency):
    """The money is already spent, so the row is kept, and the budget check can
    see that its sum is short rather than believing it."""
    row = _call(agency, key="unknown", model="nobody/knows")
    assert row.upstream_cost_nano is None
    assert row.cost_eur_nano is None
    assert row.tokens_in == 1_000_000

    _call(agency, key="known")
    spent = agency.spend(org="studio-nord", since=EARLIER)
    assert spent.eur_nano == 9 * NANO
    assert spent.unpriced == 1


def test_no_exchange_rate_keeps_the_upstream_cost_and_counts_as_unpriced(agency):
    row = _call(agency, eur_per_usd=None)
    assert row.upstream_cost_nano == 18 * NANO
    assert row.currency == "USD"
    assert row.cost_eur_nano is None
    assert row.eur_per_usd is None
    assert agency.spend(org="studio-nord", since=EARLIER).unpriced == 1


def test_a_price_after_the_call_does_not_price_it(agency):
    """The price in force at that moment, not the newest."""
    agency.refresh_prices(
        [Price("later/model", NANO, NANO, None, "USD", "test")], now=T0 + timedelta(hours=1)
    )
    assert _call(agency, key="a", model="later/model").cost_eur_nano is None


def test_a_free_model_is_priced_at_zero_not_unpriced(agency):
    agency.refresh_prices([Price("free/model", 0, 0, None, "USD", "test")], now=EARLIER)
    row = _call(agency, model="free/model")
    assert row.cost_eur_nano == 0
    assert agency.spend(org="studio-nord", since=EARLIER).unpriced == 0


def test_a_price_already_in_euros_needs_no_rate(agency):
    agency.refresh_prices([Price("eur/model", NANO, NANO, None, "EUR", "test")], now=EARLIER)
    row = _call(agency, model="eur/model", eur_per_usd=None)
    assert row.upstream_cost_nano == 2 * NANO
    assert row.cost_eur_nano == 2 * NANO


def test_a_call_that_used_no_tokens_cost_nothing_whatever_its_model(agency):
    """An upstream's refusal must not stop a budget that fails closed on unpriced."""
    row = _call(
        agency, model="nobody/knows", tokens_in=0, tokens_out=0, eur_per_usd=None, status=429
    )
    assert row.cost_eur_nano == 0
    assert row.status == 429
    assert agency.spend(org="studio-nord", since=EARLIER).unpriced == 0


def test_an_estimate_is_priced_and_says_it_is_one(agency):
    row = _call(agency, estimated=True, status=200)
    assert row.estimated is True
    assert row.cost_eur_nano == 9 * NANO
    assert _call(agency, key="reported").estimated is False


# -- prices ------------------------------------------------------------------


def test_cached_tokens_are_part_of_the_input_and_billed_at_their_own_price(agency):
    agency.refresh_prices([_price("3", "15", cached_usd="1")], now=EARLIER + timedelta(hours=1))
    row = _call(agency, tokens_in=1_000_000, tokens_cached=400_000, tokens_out=0)
    # 600,000 at $3 per million, 400,000 at $1 per million.
    assert row.upstream_cost_nano == int(Decimal("2.2") * NANO)


def test_cached_tokens_with_no_cached_price_are_billed_as_input(agency):
    row = _call(agency, tokens_in=1_000_000, tokens_cached=400_000, tokens_out=0)
    assert row.upstream_cost_nano == 3 * NANO


def test_a_refresh_records_only_what_changed(agency):
    in_force = agency.price_of(MODEL, at=T0)
    assert agency.refresh_prices([_price()], now=T0) == 0
    assert agency.price_of(MODEL, at=T0) == in_force

    assert agency.refresh_prices([_price("4", "15")], now=T0) == 1
    assert agency.price_of(MODEL, at=T0).input_nano_per_m == 4 * NANO
    # The old price is still the answer for the moment before.
    assert agency.price_of(MODEL, at=T0 - timedelta(seconds=1)).input_nano_per_m == 3 * NANO


def test_a_model_the_catalogue_dropped_keeps_its_last_price(agency):
    agency.refresh_prices([Price("another/model", NANO, NANO, None, "USD", "test")], now=T0)
    assert agency.price_of(MODEL, at=T0) is not None


def test_a_model_never_priced_has_no_price(fleet):
    assert fleet.price_of("nobody/knows", at=T0) is None


# -- the shape of it ---------------------------------------------------------


def test_nothing_in_the_store_rewrites_the_ledger():
    """Read as source: the ledger is appended to. A correction is a new row."""
    source = inspect.getsource(store_module)
    assert not re.search(r"(UPDATE|DELETE FROM)\s+usage\b", source)


@pytest.mark.parametrize(
    ("scope", "index"),
    [
        ({}, "idx_usage_org"),
        ({"client": "acme"}, "idx_usage_client"),
        ({"box": "b-one"}, "idx_usage_box"),
    ],
)
def test_spend_is_one_indexed_query(tmp_path, monkeypatch, scope, index):
    """SQLite's plan for each shape, since a budget check runs before every call.
    Postgres has the same indexes and its planner's choice depends on the data."""
    with FleetStore(tmp_path / "fleet.db") as fleet:
        queries: list[str] = []
        real = fleet._conn.execute

        def counted(sql, *args, **kwargs):
            queries.append(sql)
            return real(sql, *args, **kwargs)

        monkeypatch.setattr(fleet._conn, "execute", counted)
        fleet.spend(org="studio-nord", since=T0, **scope)
        monkeypatch.undo()

        assert len(queries) == 1
        params = ["studio-nord", *scope.values(), T0.isoformat()]
        plan = fleet._conn.execute(f"EXPLAIN QUERY PLAN {queries[0]}", params).fetchall()
        assert index in " ".join(str(row["detail"]) for row in plan)


# -- what the upstream says a call cost (FLOTTA-115) -------------------------


def test_a_reported_cost_beats_the_listed_price(agency):
    row = _call(agency, reported_cost_nano=7 * NANO)
    assert row.upstream_cost_nano == 7 * NANO
    assert row.cost_eur_nano == 3_500_000_000
    assert row.currency == "USD"
    assert row.eur_per_usd == "0.5"
    assert row.cost_reported is True
    assert agency.spend(org="studio-nord", since=EARLIER).eur_nano == 3_500_000_000


def test_a_listed_price_says_it_is_not_a_reported_cost(agency):
    row = _call(agency)
    assert row.upstream_cost_nano == 18 * NANO
    assert row.cost_reported is False


def test_a_reported_cost_prices_a_model_with_no_listed_price(agency):
    row = _call(agency, model="nobody/knows", reported_cost_nano=2 * NANO)
    assert (row.upstream_cost_nano, row.cost_eur_nano) == (2 * NANO, NANO)
    assert row.currency == "USD"
    assert agency.spend(org="studio-nord", since=EARLIER).unpriced == 0


def test_a_reported_cost_is_in_dollars_whatever_the_list_is_in(agency):
    agency.refresh_prices(
        [Price(MODEL, 3 * NANO, 15 * NANO, None, "EUR", "test")], now=EARLIER + timedelta(hours=1)
    )
    row = _call(agency, reported_cost_nano=2 * NANO)
    assert row.currency == "USD"
    assert row.eur_per_usd == "0.5"
    assert row.cost_eur_nano == NANO


def test_a_reported_cost_with_no_rate_is_counted_as_unpriced(agency):
    row = _call(agency, reported_cost_nano=2 * NANO, eur_per_usd=None)
    assert row.upstream_cost_nano == 2 * NANO
    assert row.cost_eur_nano is None
    assert row.eur_per_usd is None
    assert agency.spend(org="studio-nord", since=EARLIER).unpriced == 1


def test_a_retry_keeps_the_cost_first_written(agency):
    first = _call(agency, reported_cost_nano=2 * NANO)
    again = _call(agency, reported_cost_nano=5 * NANO)
    assert again.id == first.id
    assert again.upstream_cost_nano == 2 * NANO


def test_a_reported_zero_is_not_believed(agency):
    row = _call(agency, reported_cost_nano=0)
    assert row.upstream_cost_nano == 18 * NANO
    assert row.cost_reported is False


def test_a_negative_reported_cost_is_refused(agency):
    with pytest.raises(ValueError, match="reported_cost_nano"):
        _call(agency, reported_cost_nano=-1)
