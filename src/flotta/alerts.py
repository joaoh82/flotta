"""Telling an org what its budgets did (FLOTTA-93, M13.7).

An email and a signed webhook, when a budget reaches its soft threshold and when
it reaches its limit. `docs/ALERTS.md` is what a receiver needs to know.

## Once a period

`budget_events` already fires each threshold once a period. Each one becomes a row
per channel and target in `alert_deliveries`, in one transaction with the mark that
says it has been queued, so a restart can neither lose an alert nor queue it twice.
A delivery is claimed before it is sent. A crash between the send and the note that
it went sends it again, so a webhook carries an id that stays the same across tries.

## Nothing waits on a webhook

The gateway doesn't know this exists: it reads budgets, and only the control plane's
sweep writes `budget_events`. Sending has its own loop, not the reconcile loop, and a
webhook has a deadline for the whole attempt, so an endpoint that drips its answer
holds up nothing but itself.

## A webhook URL is an SSRF surface

An owner names a URL and the control plane calls it, from inside its network. So it
has to be https, and every address the name resolves to **at send time** has to be
public. The request then goes to the address that was checked, not the name again,
or a name that changes its answer between the check and the connect gets past it.
No redirects, no proxy from the environment. What came back is never kept or shown:
only the status, so it can't be used to read an internal service.

`$FLOTTA_ALLOW_LOCAL_WEBHOOKS=1` lifts this for a laptop (`just local`), as does a
keyless control plane, which `check_bind` keeps on loopback.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from flotta.auth import webhook_secret
from flotta.mail import Mailer, Message
from flotta.store import AlertDelivery, BudgetEvent, FleetStore

_log = logging.getLogger(__name__)

ALLOW_LOCAL_ENV = "FLOTTA_ALLOW_LOCAL_WEBHOOKS"

#: How often to look for anything due.
SWEEP_S = 30.0
#: Tries in all, and the wait before each one after the first.
MAX_ATTEMPTS = 5
RETRY_AFTER_S = (60, 300, 1800, 7200)
#: The whole of one attempt: a name, a connection, and the status line.
SEND_DEADLINE_S = 10.0
#: At a time, so one sweep is bounded. The rest are still due next sweep.
BATCH = 50

SIGNATURE_HEADER = "Flotta-Signature"
DELIVERY_HEADER = "Flotta-Delivery"
#: How old a signature a receiver should accept (`verify_signature`).
TOLERANCE_S = 300

#: What each threshold is called to a receiver.
WEBHOOK_TYPES = {"budget_soft_limit": "budget.soft_limit", "budget_limit": "budget.limit"}

#: What an action does at the limit, from `flotta.budgets`.
_AT_THE_LIMIT = {
    "notify": "Nothing has been stopped: this budget only notifies.",
    "downgrade": "Calls now go to {fallback_model} until it resets.",
    "pause": "Its agents are paused, and their model calls are refused until it resets.",
    "kill": "Its agents' machines have been stopped, and their calls are refused until it resets.",
}


class WebhookRefused(ValueError):
    """A webhook URL this control plane won't call."""


# -- which URLs ---------------------------------------------------------------


def allow_local_webhooks(*, keyless: bool, env: dict[str, str] | None = None) -> bool:
    """Whether webhooks may be local, which only a laptop wants. See the module."""
    env = os.environ if env is None else env
    return keyless or (env.get(ALLOW_LOCAL_ENV) or "").strip() == "1"


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _is_public(address: str) -> bool:
    """Whether an address is on the public internet, whatever form it arrives in."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    # An IPv4 address carried in an IPv6 one reaches the IPv4 one, and no webhook needs it.
    # Site-local is private, whatever `is_global` says of it.
    if isinstance(ip, ipaddress.IPv6Address) and (
        ip.ipv4_mapped or ip.sixtofour or ip.teredo or ip in _NAT64 or ip.is_site_local
    ):
        return False
    # `is_global` says yes to these. `ff02::1` is every node on the link.
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def check_webhook_url(url: str, *, allow_local: bool = False) -> str:
    """`url` if it can be a webhook, as far as can be told without resolving it."""
    url = url.strip()
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError as exc:
        raise WebhookRefused(f"not a URL: {exc}") from exc
    if not host:
        raise WebhookRefused("a webhook URL needs a host")
    if parts.username or parts.password:
        raise WebhookRefused("a webhook URL can't carry a password: the signature is the proof")
    if port == 0:
        raise WebhookRefused("a webhook URL can't use port 0")
    if allow_local:
        if parts.scheme not in ("http", "https"):
            raise WebhookRefused("a webhook URL has to be http or https")
        return url
    if parts.scheme != "https":
        raise WebhookRefused("a webhook URL has to be https")
    name = host.rstrip(".").lower()
    if name == "localhost" or name.endswith((".localhost", ".local", ".internal")):
        raise WebhookRefused(f"{host} isn't on the public internet")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return url  # A name, which is checked each time it is resolved.
    if not _is_public(name):
        raise WebhookRefused(f"{host} isn't on the public internet")
    return url


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def check_addresses(
    url: str,
    *,
    allow_local: bool = False,
    resolve: Callable[[str, int], Awaitable[list[str]]] = _resolve,
) -> str:
    """The address to connect to. All of a name's have to be public, not only the first."""
    parts = urlsplit(check_webhook_url(url, allow_local=allow_local))
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = await resolve(host, port)
    except OSError as exc:
        raise ConnectionError(f"can't resolve {host}") from exc
    if not addresses:
        raise ConnectionError(f"can't resolve {host}")
    if not allow_local and not all(_is_public(a) for a in addresses):
        raise WebhookRefused(f"{host} resolves to an address that isn't public")
    return addresses[0]


# -- signing ------------------------------------------------------------------


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """The signature header's value. The timestamp is in what's signed, to stop replays."""
    signed = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(
    secret: str,
    header: str,
    body: bytes,
    *,
    now: float | None = None,
    tolerance_s: int = TOLERANCE_S,
) -> bool:
    """What a receiver does, in Python. `docs/ALERTS.md` has it for any language."""
    fields = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    try:
        timestamp = int(fields["t"])
    except (KeyError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_s:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), header)


# -- what is said -------------------------------------------------------------


def _whose(event: BudgetEvent) -> str:
    if event.box:
        return f"Agent {event.box}'s"
    if event.client:
        return f"Client {event.client}'s"
    return f"Org {event.org}'s"


def what_happened(event: BudgetEvent) -> str:
    """One sentence for what the budget has just done."""
    budget = event.payload["budget"]
    spent = f"EUR {budget['spent_eur']} of EUR {budget['limit_eur']} a {budget['period']}"
    if event.type == "budget_limit":
        action = _AT_THE_LIMIT.get(budget["action"], "").format(
            fallback_model=budget.get("fallback_model")
        )
        return f"{_whose(event)} budget has reached its limit: {spent}. {action}".rstrip()
    percent = round(float(budget["soft_pct"]) * 100)
    return f"{_whose(event)} budget has reached {percent}% of its limit: {spent}."


def alert_message(to: str, event: BudgetEvent, *, app_url: str) -> Message:
    budget = event.payload["budget"]
    limit = event.type == "budget_limit"
    subject = f"{_whose(event)} budget {'reached its limit' if limit else 'is nearly used'}"
    sentence = what_happened(event)
    resets = f"It resets at {budget['resets_at']}."
    link = f"{app_url.rstrip('/')}/usage"
    return Message(
        to=to,
        subject=subject,
        text=f"{sentence}\n\n{resets}\n\nSee it in Flotta: {link}",
        html=(
            f"<p>{html.escape(sentence)}</p><p>{html.escape(resets)}</p>"
            f'<p><a href="{html.escape(link, quote=True)}">See it in Flotta</a></p>'
        ),
        link=link,
        kind=subject,
    )


def webhook_body(event: BudgetEvent) -> bytes:
    return json.dumps(
        {
            "id": event.id,
            "type": WEBHOOK_TYPES[event.type],
            "org": event.org,
            "client": event.client,
            "box": event.box,
            "period_start": event.period_start,
            "occurred_at": event.ts,
            "summary": what_happened(event),
            "budget": event.payload["budget"],
        },
        sort_keys=True,
    ).encode()


# -- sending ------------------------------------------------------------------

#: Sends a body with these headers to a URL, connecting to this address. Returns the status.
Poster = Callable[[str, str, bytes, dict[str, str]], Awaitable[int]]


async def post_pinned(url: str, address: str, body: bytes, headers: dict[str, str]) -> int:
    """POST to `address`, as `url`. The status is all that is read of the answer."""
    import httpx

    parts = urlsplit(url)
    host = parts.hostname or ""
    bracketed = f"[{address.split('%', 1)[0]}]" if ":" in address else address
    netloc = f"{bracketed}:{parts.port}" if parts.port else bracketed
    pinned = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
    # The certificate is still checked against the name: `sni_hostname` is what it is
    # compared with, whatever address the connection went to.
    async with (
        httpx.AsyncClient(trust_env=False, follow_redirects=False) as client,
        client.stream(
            "POST",
            pinned,
            content=body,
            headers={**headers, "Host": parts.netloc.rsplit("@", 1)[-1]},
            extensions={"sni_hostname": host},
        ) as response,
    ):
        return response.status_code


@dataclass(frozen=True, slots=True)
class _Work:
    delivery: AlertDelivery
    event: BudgetEvent
    nonce: str | None


@dataclass(frozen=True, slots=True)
class _Outcome:
    work: _Work
    error: str | None
    #: Trying again won't help.
    final: bool = False


def _claim_due(store: FleetStore, now: float) -> list[_Work]:
    """Queue what's new, and take what is due, moving each one's next try along first."""
    store.queue_budget_alerts()
    claimed = []
    for delivery in store.due_alert_deliveries(int(now), limit=BATCH):
        wait = RETRY_AFTER_S[min(delivery.attempts, len(RETRY_AFTER_S) - 1)]
        if not store.claim_alert_delivery(delivery, next_attempt_at=int(now) + wait):
            continue
        event = store.get_budget_event(delivery.event_id)
        settings = store.org_alerts(delivery.org)
        if event is None:
            store.finish_alert_delivery(delivery.id, status="failed", error="the event is gone")
            continue
        # Moved or removed since this was read: the old URL never saw a new secret.
        current = settings is not None and settings.webhook_url == delivery.target
        claimed.append(_Work(delivery, event, settings.webhook_nonce if current else None))
    return claimed


def _note(store: FleetStore, outcome: _Outcome) -> None:
    delivery = outcome.work.delivery
    if outcome.error is None:
        store.finish_alert_delivery(delivery.id, status="sent", error=None)
        return
    tried = delivery.attempts + 1
    _log.warning(
        "alert %s by %s, try %d of %d: %s",
        delivery.id,
        delivery.channel,
        tried,
        MAX_ATTEMPTS,
        outcome.error,
    )
    if not outcome.final and tried < MAX_ATTEMPTS:
        store.note_alert_failure(delivery.id, outcome.error)
        return
    if not store.finish_alert_delivery(delivery.id, status="failed", error=outcome.error):
        return  # It moved to a new URL while this was under way, so it hasn't failed.
    event = outcome.work.event
    store.record_alert_failure(
        event,
        delivery.channel,
        {"delivery": delivery.id, "channel": delivery.channel, "error": outcome.error},
    )


async def _send_webhook(
    work: _Work,
    *,
    signing_key: str | None,
    allow_local: bool,
    post: Poster,
    resolve: Callable[[str, int], Awaitable[list[str]]],
    now: float,
) -> None:
    delivery = work.delivery
    if work.nonce is None:
        raise WebhookRefused("the webhook has been removed or changed")
    address = await check_addresses(delivery.target, allow_local=allow_local, resolve=resolve)
    body = webhook_body(work.event)
    secret = webhook_secret(signing_key, delivery.org, work.nonce)
    status = await post(
        delivery.target,
        address,
        body,
        {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign(secret, int(now), body),
            DELIVERY_HEADER: delivery.id,
        },
    )
    if not 200 <= status < 300:
        # Redirects included: following one would be a request to a URL nobody checked.
        raise ConnectionError(f"the endpoint answered {status}")


async def _attempt(
    work: _Work,
    *,
    mailer: Mailer | None,
    mail_lock: asyncio.Lock,
    app_url: str,
    signing_key: str | None,
    allow_local: bool,
    post: Poster,
    resolve: Callable[[str, int], Awaitable[list[str]]],
    now: float,
    deadline_s: float,
) -> _Outcome:
    delivery = work.delivery
    try:
        if delivery.channel == "webhook":
            await asyncio.wait_for(
                _send_webhook(
                    work,
                    signing_key=signing_key,
                    allow_local=allow_local,
                    post=post,
                    resolve=resolve,
                    now=now,
                ),
                deadline_s,
            )
        elif mailer is None:
            return _Outcome(work, "this control plane has no way to send email", final=True)
        else:
            message = alert_message(delivery.target, work.event, app_url=app_url)
            # One at a time: Resend limits how fast, and a burst is only a few owners.
            async with mail_lock:
                await asyncio.wait_for(asyncio.to_thread(mailer, message), deadline_s)
    except WebhookRefused as exc:
        return _Outcome(work, str(exc), final=True)
    except TimeoutError:
        return _Outcome(work, f"no answer within {deadline_s:.0f}s")
    except ConnectionError as exc:
        return _Outcome(work, str(exc))
    except Exception as exc:  # noqa: BLE001 - one send can't stop the others
        # The type only. A message from a mailer or a client can carry a URL or a key.
        return _Outcome(work, type(exc).__name__)
    return _Outcome(work, None)


async def deliver_due(
    store_factory: Callable[[], FleetStore],
    *,
    mailer: Mailer | None,
    app_url: str,
    signing_key: str | None,
    allow_local: bool = False,
    post: Poster = post_pinned,
    resolve: Callable[[str, int], Awaitable[list[str]]] = _resolve,
    clock: Callable[[], float] = time.time,
    deadline_s: float = SEND_DEADLINE_S,
) -> int:
    """One sweep: queue what is new and send what is due. Returns how many were tried."""
    now = clock()

    def claim() -> list[_Work]:
        with store_factory() as store:
            return _claim_due(store, now)

    work = await asyncio.to_thread(claim)
    if not work:
        return 0
    mail_lock = asyncio.Lock()
    outcomes = await asyncio.gather(
        *(
            _attempt(
                item,
                mailer=mailer,
                mail_lock=mail_lock,
                app_url=app_url,
                signing_key=signing_key,
                allow_local=allow_local,
                post=post,
                resolve=resolve,
                now=now,
                deadline_s=deadline_s,
            )
            for item in work
        )
    )

    def note() -> None:
        with store_factory() as store:
            for outcome in outcomes:
                _note(store, outcome)

    await asyncio.to_thread(note)
    return len(work)


async def run_alert_loop(
    store_factory: Callable[[], FleetStore],
    *,
    mailer: Mailer | None,
    app_url: str,
    signing_key: str | None,
    allow_local: bool = False,
    interval_s: float = SWEEP_S,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    """Send alerts until cancelled. A sweep that fails is logged, and the next one tries."""
    while True:
        try:
            await deliver_due(
                store_factory,
                mailer=mailer,
                app_url=app_url,
                signing_key=signing_key,
                allow_local=allow_local,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - alerts must not end with one bad sweep
            _log.warning("alert sweep failed: %s", type(exc).__name__)
        await sleep(interval_s)
