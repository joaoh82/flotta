"""Founding Studio, the hidden plan for the first agencies.

EUR 149 a month, the same limits as Studio (5 clients, 1 seat, 15 agents), a
30 day trial whose model use on Flotta's keys is capped at EUR 20 and left off
the invoice, and that price for 12 months from the first paid invoice. The
Stripe Price, the Checkout session and the move onto the regular Studio price
live in the hosted product. What lives here is the cap of five, the trial, and
the note sent 14 days before the price ends.

A founding place is taken when the org is approved, which for a self-serve
agency is when its EUR 0 trial invoice is paid. A pending sign-up and a
declined one do not take a place. A later suspension or close keeps it:
five approved founding orgs is the whole offer.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

_log = logging.getLogger(__name__)

FOUNDING_PLAN = "founding"
PLAN_INTENTS = frozenset({FOUNDING_PLAN, "studio", "agency"})
INTERVALS = frozenset({"month", "year"})
FOUNDING_INTERVAL = "month"
FOUNDING_CAP = 5
#: Statuses that hold one of the five places. Pending has not been approved
#: and declined was refused, so neither holds one. Suspended and closed were
#: approved — the trial invoice was paid, or the operator approved them — and
#: keep the place. See `FleetStore._founding_count`.
HELD_STATUSES = ("active", "suspended", "closed")
FOUNDING_LIMITS = {"clients": 5, "seats": 1, "agents": 15}
TRIAL_DAYS = 30
TRIAL_LIMIT_EUR_NANO = 20 * 10**9
LOCK_MONTHS = 12
NOTICE_DAYS = 14
MONTHLY_EUR = Decimal("149")
UTM_MAX = 80

_VISITOR = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class AttributionError(ValueError):
    """A sign-up's plan or campaign fields cannot be stored."""


@dataclass(frozen=True, slots=True)
class Attribution:
    """What the sign-up page passed through from flotta.dev, if anything."""

    plan: str | None = None
    interval: str | None = None
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    visitor_id: str | None = None

    @property
    def founding(self) -> bool:
        return self.plan == FOUNDING_PLAN


def _utm(value: object, name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise AttributionError(f"{name} is text")
    text = value.strip()
    if not text:
        return None
    if len(text) > UTM_MAX or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise AttributionError(f"{name} is at most {UTM_MAX} ordinary characters")
    return text


def _visitor(value: object) -> str | None:
    """The website's anonymous id (`?ph=`), and nothing else.

    It is how a visit and this sign-up become one person. Anything that is not
    that id is dropped rather than stored, so a caller cannot put arbitrary
    text on the org.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str) or _VISITOR.match(value.strip().lower()) is None:
        raise AttributionError("visitor is the id the website passed along")
    return value.strip().lower()


def parse_attribution(body: dict) -> Attribution:
    """The optional plan and campaign fields on `POST /api/auth/sign-up`.

    Absent fields are no plan and no campaign. Founding Studio is monthly.
    """
    plan = body.get("plan")
    interval = body.get("interval")
    if plan is None or plan == "":
        if interval not in (None, ""):
            raise AttributionError("name the plan this interval is for")
        chosen: str | None = None
        every: str | None = None
    else:
        if not isinstance(plan, str) or plan not in PLAN_INTENTS:
            raise AttributionError("plan is founding, studio or agency")
        chosen = plan
        if interval is None or interval == "":
            every = FOUNDING_INTERVAL if chosen == FOUNDING_PLAN else None
        elif not isinstance(interval, str) or interval not in INTERVALS:
            raise AttributionError("interval is month or year")
        elif chosen == FOUNDING_PLAN and interval != FOUNDING_INTERVAL:
            raise AttributionError("Founding Studio is monthly")
        else:
            every = interval
    return Attribution(
        plan=chosen,
        interval=every,
        utm_source=_utm(body.get("utm_source"), "utm_source"),
        utm_medium=_utm(body.get("utm_medium"), "utm_medium"),
        utm_campaign=_utm(body.get("utm_campaign"), "utm_campaign"),
        visitor_id=_visitor(body.get("visitor")),
    )


def parse_amount_eur(value: object) -> str:
    """An invoice amount in euros, as text with no float noise."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise AttributionError("amount_eur is the amount in euros")
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise AttributionError("amount_eur is the amount in euros") from exc
    if not amount.is_finite() or amount < 0:
        raise AttributionError("amount_eur is the amount in euros")
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def amount_value(text: str) -> int | str:
    """`text` as a whole number of euros when it is one, so PostHog can sum it."""
    amount = Decimal(text)
    if amount == amount.to_integral_value():
        return int(amount)
    return text


def trial_id(org_id: str) -> str:
    """The id of an org's trial budget. No owner-set budget has this id."""
    return f"trial:{org_id}"


def parse_instant(text: str) -> datetime:
    """A timestamp the store wrote, as an aware UTC datetime."""
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def in_trial(org: object, moment: datetime) -> bool:
    """Whether `moment` falls in `org`'s founding trial, if it has one."""
    plan = getattr(org, "plan", None)
    start_text = getattr(org, "trial_from", None)
    end_text = getattr(org, "trial_until", None)
    if org is None or plan != FOUNDING_PLAN or not start_text or not end_text:
        return False
    return parse_instant(start_text) <= moment.astimezone(UTC) < parse_instant(end_text)


def send_price_notices(
    store: object, *, now: datetime | None = None, mailer: object = None
) -> list[str]:
    """Email each founding owner once, from 14 days before their price ends.

    Returns the org ids told on this call. No mailer tells nobody and leaves
    them due, so the next sweep tries again. A send that fails is not marked.
    """
    from flotta.mail import founding_price_message, resolve_mailer

    moment = now or datetime.now(UTC)
    due = store.founding_notices_due(moment)
    if not due:
        return []
    if mailer is not None:
        sender = mailer
    else:
        keyless = not os.environ.get("FLOTTA_SIGNING_KEY", "").strip()
        sender = resolve_mailer(keyless=keyless)
    if sender is None:
        return []
    told: list[str] = []
    for org in due:
        email = store.owner_email(org.id)
        if not email or not org.price_until:
            continue
        try:
            sender(founding_price_message(email, until=org.price_until, now=moment))
        except Exception:
            _log.warning("could not tell %s their founding price is ending", org.id, exc_info=True)
            continue
        if store.mark_price_notice(org.id, moment):
            told.append(org.id)
    return told
