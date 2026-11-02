"""What a model call costs, in whole numbers (FLOTTA-87, M13.1).

**Money here is an integer count of nanos**, a billionth of a unit of currency.
SQLite has no decimal type and `REAL` sums drift, and a budget compares its sum
against a limit exactly: spend at the limit is refused and spend just under it
is not. A cent is too coarse, since at $0.10 per million tokens one token costs
$0.0000001. A nano still holds nine billion in a `BIGINT`.

**Prices are in the upstream's currency, and euros are worked out once** (D19).
OpenRouter prices in dollars and the product bills in euros, so a usage row
keeps what the call really cost, the rate it was converted at, and the euros.
There is no default rate. A made-up one would go stale and misprice every budget
without saying so, whereas no rate leaves a row unpriced, which a budget can see.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

_log = logging.getLogger(__name__)

NANO = 10**9
_PER_MILLION = 10**6

#: Euros to one dollar, at which a dollar-priced call is converted when written.
EUR_RATE_ENV = "FLOTTA_EUR_PER_USD"


@dataclass(frozen=True, slots=True)
class Price:
    """One model's price, in nanos per million tokens.

    `cached_nano_per_m` is `None` when the upstream names no cached price; those
    tokens are billed as input, never as free.
    """

    model: str
    input_nano_per_m: int
    output_nano_per_m: int
    cached_nano_per_m: int | None
    currency: str
    source: str
    valid_from: str = ""


def cost_nano(price: Price, *, tokens_in: int, tokens_out: int, tokens_cached: int = 0) -> int:
    """What a call cost, in the price's currency.

    `tokens_in` includes `tokens_cached`, the way OpenAI-compatible upstreams
    report them. Summed before dividing, so rounding happens once per call.
    """
    cached = price.input_nano_per_m if price.cached_nano_per_m is None else price.cached_nano_per_m
    total = (
        max(tokens_in - tokens_cached, 0) * price.input_nano_per_m
        + tokens_cached * cached
        + tokens_out * price.output_nano_per_m
    )
    return (total + _PER_MILLION // 2) // _PER_MILLION


def to_eur_nano(amount_nano: int, currency: str, eur_per_usd: Decimal | None) -> int | None:
    """`amount_nano` in euros, or `None` if it cannot be converted. Never zero
    for that: a zero reads as free, and a budget would let it through."""
    if currency == "EUR":
        return amount_nano
    if currency == "USD" and eur_per_usd is not None:
        return int((amount_nano * eur_per_usd).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return None


def _positive(text: Any) -> Decimal | None:
    try:
        number = Decimal(str(text).strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def resolve_eur_per_usd(env: Mapping[str, str]) -> Decimal | None:
    """The rate in force, or `None` if there is none that could be one.

    Pass `settings.layered(store)` as `env`, as with `resolve_idle_after`.
    """
    text = (env.get(EUR_RATE_ENV) or "").strip()
    if not text:
        return None
    rate = _positive(text)
    if rate is None or rate == 0:
        _log.warning("%s is %r, which is not a rate: calls stay unpriced", EUR_RATE_ENV, text)
        return None
    return rate


def _per_million(per_token: Any) -> int | None:
    number = _positive(per_token)
    if number is None:
        return None
    return int((number * NANO * _PER_MILLION).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def prices_from_catalogue(entries: Iterable[Any]) -> tuple[list[Price], list[str]]:
    """OpenRouter's catalogue as prices, and the ids of models it gives none for.

    Its prices are strings, in dollars per token. `openrouter/auto` says `-1`,
    since its price depends on where it routes: that is not a price, so it is
    skipped and named rather than stored as a zero that would make it free.
    """
    prices: list[Price] = []
    skipped: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or "id" not in entry:
            continue
        model = str(entry["id"])
        pricing = entry.get("pricing")
        pricing = pricing if isinstance(pricing, dict) else {}
        input_price = _per_million(pricing.get("prompt"))
        output_price = _per_million(pricing.get("completion"))
        if input_price is None or output_price is None:
            skipped.append(model)
            continue
        prices.append(
            Price(
                model=model,
                input_nano_per_m=input_price,
                output_nano_per_m=output_price,
                cached_nano_per_m=_per_million(pricing.get("input_cache_read")),
                currency="USD",
                source="openrouter",
            )
        )
    return prices, skipped
