"""Emailing the people who can answer an approval that is waiting (FLOTTA-167).

A run's approval waits for a person for as long as the run may last (FLOTTA-158),
and then it is denied. A list on Home only helps someone who opens Home, so the
control plane also sends an email: one per approval, to everyone who may answer it.

## Who is emailed

The agency's people who can answer: its owners and members. And, when the agency
lets the agent's client answer its approvals (`clients_answer_approvals`), that
client's people who talk to agents (`client_user`). A client viewer can't answer,
so isn't told. Nobody disabled, and each address once.

## When

Not at once. An approval answered within `GRACE_S`, by someone watching, needs no
email. After that, once: `notified_at` is claimed before the send, so two control
planes overlapping on a deploy can't both send it, and cleared if the send fails,
so the next sweep tries again. Only what Home would list is emailed: still waiting,
not past its time, and held by a delivery that can carry the answer to the agent.

## What it says

The agent, its client, what it wants in the agent's words and the command, as
Hermes sent them (Hermes already redacts credential-shaped values), when it runs
out, and a link to the web app, whose Home (or portal, for a client's person)
lists it with Allow once and Deny. No secret, so the email can be forwarded.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from flotta.mail import Mailer, Message
from flotta.store import FleetStore, RunApproval

_log = logging.getLogger(__name__)

#: How long an approval waits before anyone is emailed: someone watching the
#: conversation answers well inside it.
GRACE_S = 60

#: How often to look.
SWEEP_S = 30.0

#: The agency's roles that can answer (`fleet:write`).
AGENCY_ANSWERERS = frozenset({"owner", "member"})
#: A client's role that can answer, on an agent that allows it (`box:chat`).
CLIENT_ANSWERER = "client_user"


@dataclass(frozen=True, slots=True)
class Recipient:
    email: str
    #: One of the client's people rather than the agency's: the words differ.
    client: bool


def recipients(store: FleetStore, approval: RunApproval) -> list[Recipient]:
    """Everyone who may answer `approval`, each address once, the agency first."""
    clients_answer = store.clients_answer_approvals(approval.box_id)
    agency: list[str] = []
    theirs: list[str] = []
    for membership in store.members_of(approval.org):
        if membership.role in AGENCY_ANSWERERS:
            into = agency
        elif (
            clients_answer
            and membership.role == CLIENT_ANSWERER
            and membership.client == approval.client
        ):
            into = theirs
        else:
            continue
        user = store.get_user(membership.user_id)
        if user is None or user.disabled_at or not user.email:
            continue
        into.append(user.email)
    seen: set[str] = set()
    out: list[Recipient] = []
    for emails, client in ((agency, False), (theirs, True)):
        for email in emails:
            key = email.lower()
            if key not in seen:
                seen.add(key)
                out.append(Recipient(email, client))
    return out


def _when(instant: str) -> str:
    """`2026-11-08T21:04:00+00:00` as `21:04 UTC on 2026-11-08`."""
    try:
        moment = datetime.fromisoformat(instant).astimezone(UTC)
    except ValueError:
        return instant
    return f"{moment:%H:%M} UTC on {moment:%Y-%m-%d}"


def approval_message(
    to: Recipient, approval: RunApproval, *, agent: str, client_name: str, app_url: str
) -> Message:
    """The email telling `to` that `agent` is waiting for their OK."""
    link = f"{app_url.rstrip('/')}/"
    subject = f"{agent} is waiting for your OK"
    wants = (approval.description or "").strip()
    command = (approval.command or "").strip()
    where = "your assistants page" if to.client else "Home, under Needs you"
    who = "" if to.client else f" for {client_name}"
    lines = [f"{agent}{who} wants your OK before it goes on."]
    if wants:
        lines.append(f"What it wants: {wants}")
    if command and command != wants:
        lines.append(f"The command: {command}")
    lines.append(
        f"It waits until {_when(approval.expires_at)}. With no answer by then, it is not done."
    )
    lines.append(f"Answer it on {where}: {link}")
    footer = (
        "Flotta sends this for every approval nobody has answered within a minute, "
        "to everyone who can answer it. Once someone has, the rest have nothing to do."
    )
    text = "\n\n".join([*lines, footer])
    parts = [f"<p>{html.escape(lines[0])}</p>"]
    if wants:
        parts.append(f"<p>What it wants: <strong>{html.escape(wants)}</strong></p>")
    if command and command != wants:
        parts.append(f"<p>The command:</p><pre>{html.escape(command)}</pre>")
    parts.append(f"<p>{html.escape(lines[-2])}</p>")
    parts.append(
        f'<p><a href="{html.escape(link, quote=True)}">Answer it on {html.escape(where)}</a></p>'
    )
    parts.append(f'<p style="color:#666;font-size:12px">{html.escape(footer)}</p>')
    return Message(
        to=to.email,
        subject=subject,
        text=text,
        html="".join(parts),
        link=link,
        kind=subject,
    )


def _names(store: FleetStore, approval: RunApproval) -> tuple[str, str]:
    """The agent's name as people see it, and its client's."""
    meta = store.meta_for_box(approval.box_id)
    box = store.get_box(approval.box_id)
    agent = (meta.display_name if meta else None) or (box.name if box else approval.box_id)
    client = store.get_client(approval.org, approval.client)
    return agent, client.name if client else approval.client


def notify_waiting(
    store: FleetStore,
    *,
    mailer: Mailer | None,
    app_url: str,
    now: datetime | None = None,
    grace_s: int = GRACE_S,
) -> int:
    """One sweep: email each approval waiting past the grace. Returns how many were told.

    Without a mailer nothing is claimed, so turning one on later still tells
    what is waiting then.
    """
    if mailer is None:
        return 0
    moment = now or datetime.now(UTC)
    told = 0
    for approval in store.approvals_to_notify(
        older_than=moment - timedelta(seconds=grace_s), now=moment
    ):
        if not store.claim_approval_notice(approval.id, now=moment):
            continue
        sent = 0
        try:
            people = recipients(store, approval)
            agent, client_name = _names(store, approval)
            for person in people:
                mailer(
                    approval_message(
                        person, approval, agent=agent, client_name=client_name, app_url=app_url
                    )
                )
                sent += 1
        except Exception as exc:  # noqa: BLE001 - one approval can't stop the others
            # The type only: a mailer's message can carry a URL or a key.
            _log.warning("approval %s not emailed: %s", approval.id, type(exc).__name__)
            if sent == 0:
                # Nobody was told: the next sweep tries again. Once somebody
                # was, trying again would tell them twice, so it stays told.
                store.release_approval_notice(approval.id)
            continue
        if sent:
            told += 1
    return told


async def run_approval_mail_loop(
    store_factory: Callable[[], FleetStore],
    *,
    mailer: Mailer | None,
    app_url: str,
    interval_s: float = SWEEP_S,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    """Email waiting approvals until cancelled. A sweep that fails is logged."""

    def sweep() -> int:
        with store_factory() as store:
            return notify_waiting(store, mailer=mailer, app_url=app_url)

    while True:
        try:
            await asyncio.to_thread(sweep)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad sweep must not end the loop
            _log.warning("approval email sweep failed: %s", type(exc).__name__)
        await sleep(interval_s)
