"""Server-side product events for the sign-up and payment funnel.

Posted to PostHog when `$FLOTTA_POSTHOG_KEY` is set, and nowhere otherwise.
The host defaults to `https://eu.i.posthog.com`, the EU region project
347686 that flotta.dev already uses; `$FLOTTA_POSTHOG_HOST` overrides it.
A failure is logged and never raised: sign-up, an agent and a request do
not wait on analytics.

The distinct id is the org owner's email, the same address the hosted app
should send for checkout and the subscription, so a visit, a sign-up and a
payment are one person. When the sign-up carried the website's anonymous id
(`visitor`, the `?ph=` value), the event also sends `$anon_distinct_id`,
which is how PostHog merges that visit into the person. The public site
currently sends events with person profiles off, so the merge only lands
once the site asks for profiles on identified events. See the pull request
for that change.

Test accounts are marked with the person property `is_test`. An address is a
test account when it is listed in `$FLOTTA_TEST_EMAILS` (comma-separated,
compared after the same trim and lowercase an account is stored with) or its
domain is listed in `$FLOTTA_TEST_EMAIL_DOMAINS` (comma-separated, without
the @). Nothing else is: a plus-tag on a real address is not a test account,
so one has to be named.

The suite never posts. Tests record events by installing `sink`.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

_log = logging.getLogger(__name__)

POSTHOG_KEY = "FLOTTA_POSTHOG_KEY"
POSTHOG_HOST = "FLOTTA_POSTHOG_HOST"
TEST_EMAILS = "FLOTTA_TEST_EMAILS"
TEST_DOMAINS = "FLOTTA_TEST_EMAIL_DOMAINS"
DEFAULT_HOST = "https://eu.i.posthog.com"

#: A test installs this and reads what would have been posted. Production
#: leaves it empty.
sink: Callable[[dict[str, Any]], None] | None = None


def is_test_email(email: str, env: Mapping[str, str] | None = None) -> bool:
    """Whether `email` is a test account, by the allowlists in `env`."""
    env = os.environ if env is None else env
    address = email.strip().lower()
    listed = {part.strip().lower() for part in env.get(TEST_EMAILS, "").split(",") if part.strip()}
    if address in listed:
        return True
    domains = {
        part.strip().lower().lstrip("@")
        for part in env.get(TEST_DOMAINS, "").split(",")
        if part.strip()
    }
    return address.rsplit("@", 1)[-1] in domains


def person_properties(
    email: str, visitor: str | None = None, *, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """The identify fields every funnel event carries."""
    properties: dict[str, Any] = {
        "$set": {"email": email, "is_test": is_test_email(email, env)},
        # The site sends profiles off. A server event opts in, so the owner
        # becomes a person the visit can merge into.
        "$process_person_profile": True,
    }
    if visitor:
        properties["$anon_distinct_id"] = visitor
    return properties


def capture(
    distinct_id: str,
    event: str,
    properties: dict[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Record `event` for `distinct_id`. No key, or a test run, records nothing.

    `sink`, when a test has installed one, receives the event instead, and
    nothing is posted.
    """
    env = os.environ if env is None else env
    payload = {"event": event, "distinct_id": distinct_id, "properties": properties}
    if sink is not None:
        sink(payload)
        return
    if env.get("PYTEST_CURRENT_TEST"):
        return
    key = env.get(POSTHOG_KEY, "").strip()
    if not key:
        return
    host = env.get(POSTHOG_HOST, "").strip() or DEFAULT_HOST
    body = json.dumps({"api_key": key, **payload}).encode()
    request = urllib.request.Request(
        host.rstrip("/") + "/capture/",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
            response.read()
    except Exception:
        _log.warning("could not record %s", event, exc_info=True)


def signup_completed(org: Any, email: str) -> None:
    """An org now exists, because its owner confirmed the sign-up link."""
    capture(
        email,
        "signup_completed",
        {
            "org_id": org.id,
            "plan_intent": org.plan,
            "utm_source": org.utm_source,
            "utm_medium": org.utm_medium,
            "utm_campaign": org.utm_campaign,
            **person_properties(email, org.visitor_id),
        },
    )


def agent_created(store: Any, box: Any) -> None:
    """An agent was provisioned. No owner means nobody to attribute it to."""
    email = store.owner_email(box.org)
    if not email:
        return
    capture(
        email,
        "agent_created",
        {"org_id": box.org, "box_id": box.id, **person_properties(email)},
    )


def request_filed(store: Any, box: Any, *, size: str | None, auto_started: bool) -> None:
    """A request was filed. `size` is Jev's complexity, or None if it was not judged."""
    email = store.owner_email(box.org)
    if not email:
        return
    capture(
        email,
        "request_filed",
        {
            "org_id": box.org,
            "box_id": box.id,
            "size": size,
            "auto_started": bool(auto_started),
            **person_properties(email),
        },
    )


def funnel(
    email: str,
    event: str,
    *,
    plan: str,
    interval: str,
    org_id: str,
    amount_eur: int | str | None = None,
    visitor: str | None = None,
) -> None:
    """`checkout_started` or `subscription_started`, attributed to the owner."""
    properties: dict[str, Any] = {
        "plan": plan,
        "interval": interval,
        "org_id": org_id,
        **person_properties(email, visitor),
    }
    if amount_eur is not None:
        properties["amount_eur"] = amount_eur
    capture(email, event, properties)
