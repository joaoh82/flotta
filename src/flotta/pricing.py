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

**The rate looks after itself (FLOTTA-111).** The gateway fetches the European Central
Bank's daily reference rate, free and with no key. `FLOTTA_EUR_PER_USD` is an override.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

_log = logging.getLogger(__name__)

NANO = 10**9
_PER_MILLION = 10**6

#: Euros to one dollar, at which a dollar-priced call is converted when written.
EUR_RATE_ENV = "FLOTTA_EUR_PER_USD"

#: The ECB's reference rates, updated on working days at around 16:00 CET.
ECB_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ECB_SOURCE = "ecb"
_ECB_DAY = re.compile(r"""time=['"](\d{4}-\d{2}-\d{2})['"]""")
_ECB_USD = re.compile(r"""currency=['"]USD['"]\s+rate=['"]([0-9.]+)['"]""")
#: Outside this, the source is broken rather than the dollar: keep the last rate.
_PLAUSIBLE_EUR_PER_USD = (Decimal("0.2"), Decimal("5"))


@dataclass(frozen=True, slots=True)
class Price:
    """One model's price, in nanos per million tokens.

    `cached_nano_per_m` is `None` when the upstream names no cached price; those
    tokens are billed as input, never as free.

    For a model on OpenRouter this is what its catalogue lists: its cheapest
    provider, not the one a call lands on. So it only prices a call whose reply
    didn't say what it cost, and it can be too low (FLOTTA-115).
    """

    model: str
    input_nano_per_m: int
    output_nano_per_m: int
    cached_nano_per_m: int | None
    currency: str
    source: str
    valid_from: str = ""


@dataclass(frozen=True, slots=True)
class EurRate:
    """A fetched rate: euros to one dollar, and the day its source quoted it for."""

    eur_per_usd: Decimal
    as_of: str
    source: str


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


def dollars_to_nano(amount: Any) -> int | None:
    """`amount` dollars in nanos, or `None` if that isn't an amount."""
    number = _positive(amount)
    if number is None:
        return None
    return int((number * NANO).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _positive(text: Any) -> Decimal | None:
    try:
        number = Decimal(str(text).strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def rate_in_force(store: Any, env: Mapping[str, str] | None) -> Decimal | None:
    """The override if there is one, otherwise the latest rate fetched.

    Pass `settings.layered(store)` as `env`.
    """
    override = resolve_eur_per_usd(env or {})
    if override is not None:
        return override
    fetched = store.fetched_eur_rate()
    return fetched.eur_per_usd if fetched else None


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


def eur_rate_from_ecb(text: str) -> EurRate:
    """The rate in the ECB's daily file, or `ValueError` if it does not hold a sane one.

    Two patterns rather than an XML parser, since nothing else in that file is wanted
    and a parser would be one more thing to feed a download. The file quotes dollars
    per euro, so this inverts it.
    """
    day, usd = _ECB_DAY.search(text), _ECB_USD.search(text)
    quoted = _positive(usd.group(1)) if usd else None
    if day is None or not quoted:
        raise ValueError("the ECB's file does not hold a dollar rate and a date")
    rate = (1 / quoted).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    if not _PLAUSIBLE_EUR_PER_USD[0] <= rate <= _PLAUSIBLE_EUR_PER_USD[1]:
        raise ValueError(f"the ECB's file implies {rate} euros per dollar, which is not plausible")
    return EurRate(rate, day.group(1), ECB_SOURCE)


def fetch_eur_rate() -> EurRate:
    """Today's rate from the ECB. Raises on any failure; the caller keeps the last one."""
    import httpx

    response = httpx.get(ECB_RATES_URL, timeout=10.0, trust_env=False, follow_redirects=True)
    response.raise_for_status()
    return eur_rate_from_ecb(response.text)


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


# -- machines and volumes (FLOTTA-92) ---------------------------------------
#
# Fly's published prices, from docs.fly.io/about/pricing on 3 Nov 2026, which quotes
# 30 days of a machine in `iad` and a markup per region. A settings override is an
# operator's own figure, for what this table lacks or a price Fly has since changed.

MACHINE_PRICES_ENV = "FLOTTA_MACHINE_PRICES"
VOLUME_PRICE_ENV = "FLOTTA_VOLUME_USD_GB_MONTH"
#: Fly's month, which its monthly prices assume.
_MONTH_S = 30 * 24 * 60 * 60
_LOCAL = "machine:local"
UNKNOWN_MACHINE = "machine:unknown"
_FLY_MACHINE_USD_MONTH = {
    "shared-1x-256": Decimal("2.19"),
    "shared-1x-512": Decimal("3.69"),
    "shared-1x-1024": Decimal("6.70"),
}
_FLY_REGION_MARKUP = {
    "ams": Decimal("1.038461538"),
    "arn": Decimal("1.038461538"),
    "cdg": Decimal("1.134615385"),
    "dfw": Decimal("1.25"),
    "ewr": Decimal("1"),
    "fra": Decimal("1.153846154"),
    "gru": Decimal("1.615384615"),
    "iad": Decimal("1"),
    "jnb": Decimal("1.302884615"),
    "lax": Decimal("1.199519231"),
    "lhr": Decimal("1.134615385"),
    "nrt": Decimal("1.307692308"),
    "ord": Decimal("1.25"),
    "sin": Decimal("1.269230769"),
    "sjc": Decimal("1.192307692"),
    "syd": Decimal("1.269230769"),
    "yyz": Decimal("1.115384615"),
}
_FLY_VOLUME_USD_GB_MONTH = Decimal("0.15")


def machine_label(info: Any) -> str:
    """What a machine is, as `usage.model` and the prices name it."""
    if info.region == "local":
        return _LOCAL
    if info.cpu_kind and info.cpus and info.memory_mb:
        return f"machine:{info.cpu_kind}-{info.cpus}x-{info.memory_mb}"
    return UNKNOWN_MACHINE


def _configured_machine_prices(env: Mapping[str, str] | None) -> dict[str, Decimal]:
    """`shared-1x-1024=6.70, shared-1x-512=3.69`. An entry that makes no sense is ignored."""
    prices: dict[str, Decimal] = {}
    for entry in ((env or {}).get(MACHINE_PRICES_ENV) or "").split(","):
        size, _, text = entry.partition("=")
        price = _positive(text) if text.strip() else None
        if size.strip() and price is not None:
            prices[size.strip()] = price
        elif entry.strip():
            _log.warning("%s has %r, which is not a size and a price", MACHINE_PRICES_ENV, entry)
    return prices


def machine_usd_per_month(
    label: str, region: str | None, env: Mapping[str, str] | None
) -> Decimal | None:
    """What this machine costs for Fly's month, or `None` if nothing says."""
    if label == _LOCAL:
        return Decimal(0)
    size = label.removeprefix("machine:")
    configured = _configured_machine_prices(env).get(size)
    if configured is not None:
        return configured
    published = _FLY_MACHINE_USD_MONTH.get(size)
    if published is None:
        return None
    return published * _FLY_REGION_MARKUP.get(region or "", Decimal(1))


def volume_usd_per_gb_month(env: Mapping[str, str] | None) -> Decimal:
    configured = _positive((env or {}).get(VOLUME_PRICE_ENV) or "")
    return configured if configured is not None else _FLY_VOLUME_USD_GB_MONTH


def monthly_cost_nano(
    usd_per_month: Decimal | None, seconds: int, *, quantity: int = 1
) -> int | None:
    """`seconds` of something priced by Fly's month, in nanos of a dollar."""
    if usd_per_month is None:
        return None
    amount = usd_per_month * quantity * seconds * NANO / _MONTH_S
    return int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))
