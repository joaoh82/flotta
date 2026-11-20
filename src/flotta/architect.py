"""The Architect's model calls: who pays, how much, and with which model (FLOTTA-250).

The Architect is an assistant for an agency's own people, in the web app: it
answers questions about the account by calling the fleet API's tools (`POST
/mcp`). Its loop runs in the web app's server. What it needs from here is one
thing, a model to think with, and this module is the rules for that.

**The fleet pays, up to an allowance.** Each org may use
`FLOTTA_ARCHITECT_MONTHLY_EUR` of these calls in a calendar month (UTC). They
are recorded in `architect_usage`, never in `usage`: an agency's budgets and its
invoice are sums over `usage`, and the Architect is neither's to pay for.

**The check is before every call and fails closed**, as a budget's does: an org
that used its allowance, an org that is held, a model with no price, no
exchange rate, a call this month that nobody could price, or a store that
cannot be read is a refusal. None of them is a call that goes out anyway.
A few calls already in flight are not counted, so an org can overshoot by what
those cost.

**The caller does not choose the model.** It is the operator's setting, or the
fleet's model for new agents when that is empty.

Shared by the gateway, which makes the calls, and the control plane, which
tells an agency where its allowance stands.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from flotta.budgets import eur_text, period_start, resets_at
from flotta.inference import InvalidModel, validate_model
from flotta.pricing import rate_in_force
from flotta.settings import architect_monthly_eur_nano, layered

#: The most a single answer may be, in tokens. A cap, so one call cannot be
#: made to cost what the allowance is meant to bound across a month.
MAX_OUTPUT = 4096

#: What a call may carry. Anything else is refused: the gateway calls one model
#: with the messages and tools it is given, and adds nothing to them.
FIELDS = frozenset(
    {
        "model",  # accepted so an OpenAI client can be used, and replaced
        "messages",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "stream",
        "stream_options",
        "temperature",
        "max_tokens",
        "max_completion_tokens",
    }
)


class BadCall(ValueError):
    """A body the Architect's route will not send on, with why."""


def model_for(store: Any, env: Mapping[str, str] | None = None) -> str | None:
    """The model the Architect answers with, or `None` when the fleet has none."""
    settings = layered(store, env)
    for key in ("FLOTTA_ARCHITECT_MODEL", "FLOTTA_MODEL"):
        raw = (settings.get(key) or "").strip()
        if not raw:
            continue
        try:
            return validate_model(raw)
        except InvalidModel:
            continue
    return None


def shaped(payload: Any, model: str) -> dict[str, Any]:
    """`payload` as it goes to the model: the operator's model, an answer no
    longer than `MAX_OUTPUT`, and nothing this route does not send. Or `BadCall`."""
    if not isinstance(payload, dict):
        raise BadCall("expected a JSON object with the conversation's messages")
    unknown = sorted(set(payload) - FIELDS)
    if unknown:
        raise BadCall(f"the Architect's route does not send {unknown[0]!r}")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BadCall("a call needs the conversation's messages")
    out = {key: value for key, value in payload.items() if key != "max_completion_tokens"}
    asked = payload.get("max_tokens", payload.get("max_completion_tokens"))
    if asked is not None and (isinstance(asked, bool) or not isinstance(asked, int) or asked < 1):
        raise BadCall("max_tokens is a whole number, at least 1")
    out["max_tokens"] = min(asked, MAX_OUTPUT) if asked is not None else MAX_OUTPUT
    out["model"] = model
    return out


@dataclass(frozen=True, slots=True)
class Standing:
    """Where an org's allowance stands this month."""

    org: str
    since: datetime
    resets_at: datetime
    allowance_eur_nano: int
    spent_eur_nano: int
    #: Calls this month with no euro cost: the sum is short by them.
    unpriced: int

    @property
    def used_up(self) -> bool:
        return self.spent_eur_nano >= self.allowance_eur_nano

    def as_dict(self) -> dict[str, Any]:
        return {
            "org": self.org,
            "since": self.since.isoformat(),
            "resets_at": self.resets_at.isoformat(),
            "allowance_eur_nano": self.allowance_eur_nano,
            "spent_eur_nano": self.spent_eur_nano,
            "unpriced": self.unpriced,
            "used_up": self.used_up,
        }


def standing(
    store: Any, org: str, *, now: datetime, env: Mapping[str, str] | None = None
) -> Standing:
    since = period_start("month", now)
    spent = store.architect_spend(org, since=since)
    return Standing(
        org=org,
        since=since,
        resets_at=resets_at("month", now),
        allowance_eur_nano=architect_monthly_eur_nano(store, env),
        spent_eur_nano=spent.eur_nano,
        unpriced=spent.unpriced,
    )


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why a call does not go out. `status` is the HTTP answer: 402 for the
    allowance and anything about money, 403 for an org that may not ask, 503
    for a fleet that is not set up for it."""

    status: int
    code: str
    message: str
    standing: Standing | None = None

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "message": self.message,
            "type": "flotta_architect",
            "code": self.code,
        }
        if self.standing is not None:
            error["allowance"] = self.standing.as_dict()
        return {"error": error}


def unchecked() -> Refusal:
    """The check itself failed. No check, no call."""
    return Refusal(
        402,
        "architect_unchecked",
        "The Architect's allowance could not be checked, so this call was refused.",
    )


@dataclass(frozen=True, slots=True)
class Admitted:
    """A call that may go out: with which model, and the rate its cost is turned to euros at."""

    model: str
    eur_per_usd: Decimal


def admit(
    store: Any,
    org_id: str,
    *,
    now: datetime,
    cost_is_reported: bool,
    env: Mapping[str, str] | None = None,
) -> Admitted | Refusal:
    """Whether `org_id`'s Architect may make a call now, and with what.

    `cost_is_reported` is whether the upstream says what each call cost
    (OpenRouter does). Without that the model must have a price here, or the
    call could not be counted against the allowance afterwards.
    """
    org = store.get_org(org_id)
    if org is None or org.status != "active":
        return Refusal(
            403,
            "architect_org_not_active",
            "The Architect is for an agency that is active: this one isn't.",
        )
    model = model_for(store, env)
    if model is None:
        return Refusal(
            503,
            "architect_no_model",
            "The fleet's operator has chosen no model for the Architect.",
        )
    rate = rate_in_force(store, layered(store, env))
    if rate is None:
        return Refusal(
            402,
            "architect_no_rate",
            "There is no exchange rate to price this call in euros, so it was refused.",
        )
    if not cost_is_reported and store.price_of(model, at=now) is None:
        return Refusal(
            402,
            "architect_no_price",
            f"There is no price for {model}, so this call could not be counted and was refused.",
        )
    where = standing(store, org_id, now=now, env=env)
    if where.unpriced:
        return Refusal(
            402,
            "architect_unpriced",
            "A call this month could not be priced, so what is left of the Architect's "
            "allowance is unknown and this call was refused.",
            where,
        )
    if where.used_up:
        return Refusal(
            402,
            "architect_allowance_used",
            f"This agency has used its Architect allowance for the month "
            f"(€{eur_text(where.allowance_eur_nano)}). It answers again on "
            f"{where.resets_at.date().isoformat()}.",
            where,
        )
    return Admitted(model=model, eur_per_usd=rate)
