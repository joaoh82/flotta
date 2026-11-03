"""What a model call costs, as whole numbers, and what the catalogue says (FLOTTA-87)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from flotta import model_catalog
from flotta.pricing import (
    NANO,
    Price,
    cost_nano,
    dollars_to_nano,
    eur_rate_from_ecb,
    prices_from_catalogue,
    resolve_eur_per_usd,
    to_eur_nano,
)


def _price(input_nano: int = 3 * NANO, output_nano: int = 15 * NANO, cached: int | None = None):
    return Price("m/x", input_nano, output_nano, cached, "USD", "test")


def test_a_very_cheap_call_does_not_round_to_nothing():
    """The reason for nanos: at $0.10 per million, one token is $0.0000001."""
    assert cost_nano(_price(input_nano=NANO // 10), tokens_in=1, tokens_out=0) == 100


def test_a_thousand_small_calls_sum_exactly():
    """REAL would drift, and a budget compares its sum against a limit exactly."""
    one = cost_nano(_price(input_nano=NANO // 10), tokens_in=1, tokens_out=0)
    assert one * 1000 == cost_nano(_price(input_nano=NANO // 10), tokens_in=1000, tokens_out=0)


def test_cost_rounds_to_the_nearest_nano():
    # 1 token at 1,500,000 nano per million is 1.5 nano; half rounds up.
    assert cost_nano(_price(input_nano=1_500_000), tokens_in=1, tokens_out=0) == 2
    # 1 token at 1,400,000 nano per million is 1.4 nano.
    assert cost_nano(_price(input_nano=1_400_000), tokens_in=1, tokens_out=0) == 1


def test_more_cached_tokens_than_input_tokens_does_not_lower_the_cost():
    """An upstream that reports them separately must not earn a discount."""
    cost = cost_nano(_price(cached=NANO), tokens_in=10, tokens_out=0, tokens_cached=1_000_000)
    assert cost == NANO


def test_dollars_become_euros_at_the_rate():
    assert to_eur_nano(18 * NANO, "USD", Decimal("0.5")) == 9 * NANO
    assert to_eur_nano(3, "USD", Decimal("0.5")) == 2  # 1.5 rounds up


def test_euros_stay_euros_with_or_without_a_rate():
    assert to_eur_nano(7, "EUR", None) == 7
    assert to_eur_nano(7, "EUR", Decimal("0.5")) == 7


def test_what_cannot_be_converted_is_none_not_zero():
    """A zero would read as free, and a budget would let it through."""
    assert to_eur_nano(7, "USD", None) is None
    assert to_eur_nano(7, "GBP", Decimal("0.5")) is None


@pytest.mark.parametrize("bad", ["", "abc", "0", "-1", "nan", "inf", "Infinity"])
def test_a_rate_that_cannot_be_one_is_no_rate(bad):
    assert resolve_eur_per_usd({"FLOTTA_EUR_PER_USD": bad}) is None


def test_no_rate_is_set_by_default():
    """There is no made-up default: a stale guess would misprice every budget."""
    assert resolve_eur_per_usd({}) is None


def test_a_rate_is_exact():
    assert resolve_eur_per_usd({"FLOTTA_EUR_PER_USD": " 0.92 "}) == Decimal("0.92")


def _entry(model="m/x", prompt="0.000003", completion="0.000015", **more):
    return {"id": model, "pricing": {"prompt": prompt, "completion": completion, **more}}


def test_the_catalogues_per_token_prices_become_per_million_nanos():
    prices, skipped = prices_from_catalogue([_entry(input_cache_read="0.0000003")])
    assert skipped == []
    price = prices[0]
    assert price.model == "m/x"
    assert price.input_nano_per_m == 3 * NANO
    assert price.output_nano_per_m == 15 * NANO
    assert price.cached_nano_per_m == 3 * NANO // 10
    assert (price.currency, price.source) == ("USD", "openrouter")


def test_a_price_too_small_for_floats_is_still_exact():
    # In floats, 0.00000003 times a thousand trillion is 29,999,999.999999996.
    prices, _ = prices_from_catalogue([_entry(prompt="0.00000003", completion="0")])
    assert prices[0].input_nano_per_m == 30_000_000
    assert prices[0].output_nano_per_m == 0


def test_no_cached_price_is_none_not_free():
    prices, _ = prices_from_catalogue([_entry()])
    assert prices[0].cached_nano_per_m is None


@pytest.mark.parametrize(
    "entry",
    [
        # What OpenRouter's router answers: the price depends on where it routes.
        _entry("openrouter/auto", prompt="-1", completion="-1"),
        _entry("m/no-number", prompt="abc"),
        _entry("m/inf", prompt="Infinity"),
        {"id": "m/no-pricing"},
        {"id": "m/odd-pricing", "pricing": "free"},
        {"id": "m/half", "pricing": {"prompt": "0.1"}},
    ],
)
def test_what_is_not_a_price_is_skipped_and_named(entry):
    """Never a zero: this model would then be free on every budget."""
    prices, skipped = prices_from_catalogue([entry])
    assert prices == []
    assert skipped == [entry["id"]]


def test_an_entry_with_no_id_is_dropped():
    prices, skipped = prices_from_catalogue([{"pricing": {}}, "nonsense", _entry()])
    assert [p.model for p in prices] == ["m/x"]
    assert skipped == []


def test_an_empty_catalogue_is_a_broken_answer(monkeypatch):
    monkeypatch.setattr(model_catalog, "_entries", lambda timeout: [])
    with pytest.raises(model_catalog.CatalogueUnavailable, match="empty"):
        model_catalog.fetch_prices()


def test_a_catalogue_that_cannot_be_read_says_so(monkeypatch):
    def down(timeout):
        raise OSError("no route")

    monkeypatch.setattr(model_catalog, "_entries", down)
    with pytest.raises(model_catalog.CatalogueUnavailable, match="no route"):
        model_catalog.fetch_prices()


def test_a_catalogue_with_entries_but_no_usable_price_is_a_broken_answer(monkeypatch):
    monkeypatch.setattr(model_catalog, "_entries", lambda timeout: [{"id": "m/no-pricing"}])
    with pytest.raises(model_catalog.CatalogueUnavailable, match="no usable price"):
        model_catalog.fetch_prices()


# -- the rate looks after itself (FLOTTA-111) ------------------------------------

ECB_FILE = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01">
  <gesmes:subject>Reference rates</gesmes:subject>
  <Cube>
    <Cube time='2026-11-02'>
      <Cube currency='JPY' rate='171.44'/>
      <Cube currency='USD' rate='1.1216'/>
      <Cube currency='GBP' rate='0.8712'/>
    </Cube>
  </Cube>
</gesmes:Envelope>
"""


def test_the_ecb_quotes_dollars_per_euro_and_the_rate_is_the_inverse():
    rate = eur_rate_from_ecb(ECB_FILE)
    assert rate.eur_per_usd == Decimal("0.891583")
    assert rate.as_of == "2026-11-02"
    assert rate.source == "ecb"


@pytest.mark.parametrize(
    "broken",
    [
        "",
        "<html>Service unavailable</html>",
        ECB_FILE.replace("USD", "CHF"),
        ECB_FILE.replace("time='2026-11-02'", ""),
        ECB_FILE.replace("rate='1.1216'", "rate='0'"),
        ECB_FILE.replace("rate='1.1216'", "rate='1.1.2'"),
        ECB_FILE.replace("rate='1.1216'", "rate='112.16'"),
        ECB_FILE.replace("rate='1.1216'", "rate='0.0011216'"),
    ],
)
def test_a_file_without_a_sane_rate_is_refused(broken):
    with pytest.raises(ValueError):
        eur_rate_from_ecb(broken)


def test_dollars_become_nanos_exactly_and_round_half_up():
    assert dollars_to_nano(0.000755174) == 755_174
    assert dollars_to_nano(2) == 2 * NANO
    assert dollars_to_nano(0.0000000015) == 2


@pytest.mark.parametrize("amount", ["soon", None, -1, float("nan"), float("inf")])
def test_what_is_not_an_amount_of_dollars_has_no_nanos(amount):
    assert dollars_to_nano(amount) is None
