"""Budgets: checked before every model call, and failing closed (FLOTTA-89, M13.3).

A budget is a limit in euros per day or month, on an org, a client or one agent.
The gateway calls `check_budgets` before forwarding, and anything but a clean
answer ends in a 402. **A check that fails open is the bug this exists not to
have**, so the gateway treats an exception here as a refusal too.

## Not cached

The spec allowed a cache of up to a minute. It would let a looping agent run on
for that minute past its limit, to save a few indexed queries in front of a call
that takes seconds and costs money. If an org's ledger ever makes the sum slow,
this one function is where to add it.

## How far past the limit spend can go

A check before a call cannot know what the call will cost, and a stream's row is
written when it ends. So **a budget can be overrun by what is in flight when it
is crossed: at most one call's cost per call running at that moment.** One agent
in a loop overruns by one call. An org-wide budget across many busy agents
overruns by more. Reserving against an estimate would close that, and would
refuse calls that fit. A failed call whose output is unknown can undercount too
(`flotta.gateway.app`).

**A finished call whose row has not reached the store is not overshoot, it is a
sum that cannot be trusted.** Every budget that row would count against refuses
until it is written (`unsettled`), so a store that can be read and not written
cannot wave calls through.

## What cannot be priced

With a budget covering the box, a model with no price, or no exchange rate, is a
refusal before the call rather than a row the sum cannot see. A row already
unpriced this period also stops the check, since the sum is short by an unknown
amount: it clears when the period rolls over or the budget is removed. With no
budget covering the box none of this applies, and the call goes through.

Periods are in UTC.

## What each action does at the limit (FLOTTA-91, M13.5)

The gateway decides about a call, at once, and needs no substrate. The control
plane's sweep (`provision.enforce_budgets`) changes boxes, because it is what can
reach them (D10), so it acts up to one sweep later. By then the gateway has been
refusing for that long, and refusing is what stops the spending.

- **notify:** events only. Calls carry on.
- **downgrade:** the gateway sends calls to `fallback_model` for the rest of the
  period, and the row keeps both models. A fallback that has lost its price is a
  refusal, never the original model.
- **pause:** the gateway refuses, and the sweep marks the covered boxes `paused`.
  A turn in progress ends at its next model call, and the machine sleeps once it
  is quiet.
- **kill:** as pause, and the sweep **stops** the machine at once. A stop and not a
  suspend: a suspended machine keeps the turn in memory and would carry on with it
  on waking. `session.interrupt` would be gentler, but it needs the box's socket,
  and the control plane has no route to one.

A sum with an unpriced call in it refuses for every action (above). It pauses
nobody new unless it is already over, and it releases nobody.

## How a paused box comes back

**Nothing holds it, and that's the only rule.** The sweep releases a box when no
pause or kill budget covering it is reached: the period rolled over, the limit was
raised, or the budget was removed. So a daily limit doesn't need a person every
morning, which matches the "resets at" a refusal already promises. An owner's
unpause does the same at once rather than a sweep later, and is refused while a
budget still holds the box, since the sweep would only pause it again: raise the
limit instead. It comes back asleep, and wakes the usual way.

Every threshold writes an event on each box covered and one on the client, once a
period (`store.budget_events`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from flotta.pricing import NANO, to_eur_nano
from flotta.store import Box, Budget, FleetStore, InvalidBudgetError, suspended_refusal

#: The actions that stop calls at the limit, and so pause the boxes they cover.
STOPPING_ACTIONS = frozenset({"pause", "kill"})

#: Well inside a `BIGINT` of nanos, and more than anyone means.
_MOST_EUR = Decimal(10**9)


def period_start(period: str, now: datetime) -> datetime:
    """When the period holding `now` began, in UTC."""
    moment = now.astimezone(UTC)
    if period == "day":
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"no period {period!r}")


def resets_at(period: str, now: datetime) -> datetime:
    """When the period holding `now` ends."""
    start = period_start(period, now)
    if period == "day":
        return start + timedelta(days=1)
    # The 28th plus four days is always in the next month.
    return (start.replace(day=28) + timedelta(days=4)).replace(day=1)


def parse_limit(value: Any) -> int:
    """A limit in euros, as text or a number from JSON, in exact nanos."""
    if value is None or isinstance(value, bool):
        raise InvalidBudgetError("a budget's limit must be an amount in euros")
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise InvalidBudgetError(f"{value!r} is not an amount in euros") from exc
    if not amount.is_finite():
        raise InvalidBudgetError(f"{value!r} is not an amount in euros")
    if amount > _MOST_EUR:
        raise InvalidBudgetError(f"{value!r} is more than a budget can hold")
    nano = amount * NANO
    if nano != nano.to_integral_value():
        raise InvalidBudgetError(f"{value!r} is finer than a billionth of a euro")
    return int(nano)


def eur_text(nano: int) -> str:
    """Nanos as an exact amount in euros, with no float noise."""
    text = format(Decimal(nano) / NANO, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def budget_dict(budget: Budget) -> dict[str, Any]:
    """A budget as the API and a refusal describe it."""
    scope = "box" if budget.box else "client" if budget.client else "org"
    return {
        "id": budget.id,
        "org": budget.org,
        "client": budget.client,
        "box": budget.box,
        "scope": scope,
        "period": budget.period,
        "limit_eur": eur_text(budget.limit_eur_nano),
        "soft_pct": budget.soft_pct,
        "action": budget.action,
        "fallback_model": budget.fallback_model,
    }


def _whose(budget: Budget) -> str:
    if budget.box:
        return "This agent's"
    if budget.client:
        return f"Client {budget.client}'s"
    return f"Org {budget.org}'s"


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why a call may not go out."""

    code: str
    message: str
    budget: dict[str, Any] | None = None
    #: What makes two refusals the same one, so a loop leaves one event.
    key: tuple[str, ...] = field(default=())

    def body(self) -> dict[str, Any]:
        """The shape an OpenAI client reads, so an agent's log says what happened."""
        error: dict[str, Any] = {
            "message": self.message,
            "type": "flotta_budget",
            "code": self.code,
        }
        if self.budget is not None:
            error["budget"] = self.budget
        return {"error": error}


def unchecked(*key: str) -> Refusal:
    """The refusal for a check that could not answer. Says nothing of why."""
    return Refusal(
        "budget_unchecked",
        "The budget could not be checked, so this call was refused.",
        key=("budget_unchecked", *key),
    )


def counts_against(budget: Budget, box: Box) -> bool:
    """Whether a call by `box` counts against `budget`."""
    return (
        budget.org == box.org
        and (budget.client is None or budget.client == box.client)
        and (budget.box is None or budget.box == box.id)
    )


@dataclass(frozen=True, slots=True)
class Standing:
    """Where a budget is this period."""

    budget: Budget
    spent_nano: int
    unpriced: int
    since: datetime
    resets: datetime

    @property
    def reached(self) -> bool:
        return self.spent_nano >= self.budget.limit_eur_nano

    @property
    def soft_reached(self) -> bool:
        return self.spent_nano >= self.budget.limit_eur_nano * self.budget.soft_pct

    @property
    def stops(self) -> bool:
        """Whether it stops calls now."""
        return self.reached and self.budget.action in STOPPING_ACTIONS

    def details(self) -> dict[str, Any]:
        return {
            **budget_dict(self.budget),
            "spent_eur": eur_text(self.spent_nano),
            "resets_at": self.resets.isoformat(),
        }

    def sentence(self) -> str:
        details = self.details()
        return (
            f"{_whose(self.budget)} budget of EUR {details['limit_eur']} a {self.budget.period} "
            f"is used up: EUR {details['spent_eur']} spent. It resets at {details['resets_at']}."
        )


def standing(store: FleetStore, budget: Budget, now: datetime) -> Standing:
    since = period_start(budget.period, now)
    spent = store.spend(org=budget.org, client=budget.client, box=budget.box, since=since)
    return Standing(budget, spent.eur_nano, spent.unpriced, since, resets_at(budget.period, now))


def holding(store: FleetStore, box: Box, now: datetime) -> list[Standing]:
    """The budgets that keep `box` paused now, most severe first."""
    found = [standing(store, b, now) for b in store.budgets_covering(box)]
    stopping = [s for s in found if s.stops]
    return sorted(stopping, key=lambda s: s.budget.action != "kill")


def paused_message(store: FleetStore, box: Box, now: datetime) -> str:
    """What someone trying to reach a paused box is told."""
    held = holding(store, box, now)
    reason = f" {held[0].sentence()}" if held else ""
    return f"{box.name} is paused by budget, so it was not woken.{reason}"


@dataclass(frozen=True, slots=True)
class Downgrade:
    """Send this call to `model` instead of the one it named."""

    model: str
    budget: dict[str, Any]


def _narrowest_first(found: Standing) -> tuple[bool, bool]:
    return (found.budget.box is None, found.budget.client is None)


def check_budgets(
    store: FleetStore,
    box: Box,
    *,
    model: str,
    eur_per_usd: Decimal | None,
    now: datetime,
    unsettled: Iterable[Box] = (),
) -> Refusal | Downgrade | None:
    """Why this box may not call `model` now, the model to call instead, or `None`.

    `unsettled` is the boxes with a finished call whose row is not in the store.

    A suspended org's agents call nothing (FLOTTA-184), whatever its budgets
    say: this is how an agent mid-turn stops spending the moment it is
    suspended, before the sweep puts its machine to sleep.
    """
    org = store.get_org(box.org)
    if org is not None and org.status == "suspended":
        return Refusal(
            "org_suspended",
            suspended_refusal(org, "call a model"),
            key=("org_suspended", org.id, org.suspended_at or ""),
        )
    covering = store.budgets_covering(box)
    if not covering:
        return None

    missing = list(unsettled)
    for budget in covering:
        if any(counts_against(budget, other) for other in missing):
            return Refusal(
                "budget_unchecked",
                "The budget could not be checked, because the cost of an earlier call has not "
                "been recorded yet, so this call was refused.",
                budget_dict(budget),
                key=("budget_unsettled", budget.id, period_start(budget.period, now).isoformat()),
            )

    standings = [standing(store, budget, now) for budget in covering]
    for found in standings:
        if found.unpriced:
            return Refusal(
                "budget_unpriced",
                f"{_whose(found.budget)} budget cannot be trusted this {found.budget.period}: "
                f"{found.unpriced} call(s) in it have no known cost. It clears at "
                f"{found.resets.isoformat()}.",
                found.details(),
                key=("budget_unpriced", found.budget.id, found.since.isoformat()),
            )
    for found in standings:
        if found.stops or (found.reached and _downgrades_to_nothing(found.budget)):
            return Refusal(
                "budget_exceeded",
                found.sentence(),
                found.details(),
                key=("budget_exceeded", found.budget.id, found.since.isoformat()),
            )

    downgrades = sorted(
        (s for s in standings if s.reached and s.budget.action == "downgrade"),
        key=_narrowest_first,
    )
    fallback = downgrades[0].budget.fallback_model if downgrades else None
    calling = fallback or model

    first = covering[0]
    price = store.price_of(calling, at=now)
    if price is None:
        return Refusal(
            "budget_no_price",
            f"A budget covers this agent and {calling} has no known price, "
            "so the call was refused.",
            budget_dict(first),
            key=("budget_no_price", calling),
        )
    if to_eur_nano(NANO, price.currency, eur_per_usd) is None:
        return Refusal(
            "budget_no_rate",
            f"A budget covers this agent and there is no rate from {price.currency} to euros, "
            "so the call was refused. The operator sets it on the Fleet page.",
            budget_dict(first),
            key=("budget_no_rate", price.currency),
        )
    if fallback and fallback != model:
        return Downgrade(fallback, downgrades[0].details())
    return None


def _downgrades_to_nothing(budget: Budget) -> bool:
    """A downgrade that names no fallback, which `set_budget` refuses to save, has
    nothing to send a call to. Refused, because the original model is the one thing
    it may not send."""
    return budget.action == "downgrade" and not budget.fallback_model
