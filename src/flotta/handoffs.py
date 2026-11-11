"""Handoffs: an agent needs a person on its screen, and the agency hears it (FLOTTA-213).

## The gap

Hermes has no tool that pauses for a person. On a sign-in, a 2FA prompt, a
CAPTCHA or a payment, the agent says so in its reply and ends its turn. That
reaches whoever is reading that conversation, and for a run, which is a trigger
with nobody watching, it reaches nobody.

## The path

1. The agent runs `flotta-handoff "<why>" [--url <page>]` on its box
   (`flotta.box.handoff`, taught by the `flotta-screen` skill), then ends its
   turn. The control plane records it (`POST /api/boxes/{id}/handoffs`), one
   open per agent, naming the run it was filed in.
2. It is on the agency's Home and the agent's card as "Needs you on the
   screen", with **Open screen** (`GET /api/handoffs`), and the people who may
   drive the screen are emailed (`notify_open`).
3. It is cleared by the next hand-back of the screen from a person to the
   agent after it was filed (`FleetStore.hand_back_handoffs`), or dismissed by
   the agency (`POST /api/handoffs/{id}/dismiss`). A hand-back counts only when
   the door saw a person's driving ticket send the release (`door.app.LeaseWatch`):
   the agent has root, and its own word that the screen came back is none.

## A run waits for it, parked

A run's agent that hands over ends its turn, which would end the run as done
with the work not done. So the run is parked (`flotta.runner`): `running`, which
holds its agent awake (`provision.idle_boxes`), but holding no delivery slot,
for as long as runs may last (`runs.RUN_TIMEOUT_S`). Handed back, a sweep picks
it up and tells it to carry on in the same conversation (`continue_message`);
dismissed, it ends `cancelled`; out of time, `failed`. A handoff still open
when its run ends is `expired`.

## Email

After `GRACE_S`, and at most one per agent in `NOTICE_QUIET_S`, whatever the
agent does. A handoff reused for another run (a conversation's, left open, then
a run's) is due again. The page the agent named is written as text, never as
a link; a mail client may still turn a bare address into one.

## Who sees it, and who is told

What the screen's tickets grant decides it (`flotta.auth.screen_grant`): the
agency's people drive any screen, and a client's people only an agent whose
agency lets them (`client_screen_control`). A client's people who may not
drive are not shown it, and a client viewer never is.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from flotta.mail import Mailer, Message, esc, layout, paragraph
from flotta.store import FleetStore, Handoff

_log = logging.getLogger(__name__)

#: How long a handoff waits before anyone is emailed: someone reading the
#: conversation it was filed in takes the screen well inside it.
GRACE_S = 60

#: How often to look.
SWEEP_S = 30.0

#: At most one email about any one agent in this long. An agent has root on its
#: box and can ask as often as it likes; the agency's inbox is not its to fill.
NOTICE_QUIET_S = 20 * 60

#: The agency's roles that drive any of its agents' screens.
AGENCY_DRIVERS = frozenset({"owner", "member"})
#: A client's role that may drive, on an agent whose agency allows it.
CLIENT_DRIVER = "client_user"


def screen_path(box_id: str) -> str:
    """Where the web app shows an agent's screen, from its base URL: its page,
    whose conversation tab carries the screen beside the conversation."""
    from urllib.parse import quote

    return f"/agents/{quote(box_id, safe='')}#screen"


def view(store: FleetStore, handoff: Handoff, *, names: dict[str, Any] | None = None) -> dict:
    """A handoff as the API says it, with its agent's name and address."""
    names = names if names is not None else {}
    if handoff.box_id not in names:
        box = store.get_box(handoff.box_id)
        meta = store.meta_for_box(handoff.box_id)
        names[handoff.box_id] = (
            box.name if box else handoff.box_id,
            (meta.display_name if meta else None) or (box.name if box else handoff.box_id),
        )
    name, shown = names[handoff.box_id]
    out = asdict(handoff)
    # What a parked run needs to carry on is the runner's, not a page's.
    for kept in ("run_session", "run_reply", "run_steps"):
        out.pop(kept, None)
    out["box_name"] = name
    out["agent"] = shown
    out["screen_path"] = screen_path(handoff.box_id)
    # A run, not a conversation's turn, waiting on it to carry on (`flotta.runner`).
    out["run_waits"] = handoff.status == "open" and _in_run(store, handoff)
    return out


def views(store: FleetStore, found: list[Handoff]) -> list[dict]:
    names: dict[str, Any] = {}
    return [view(store, h, names=names) for h in found]


def continue_message(handoff: Handoff) -> str:
    """What a run's agent is told once the screen is handed back to it."""
    return (
        "The person you handed your screen to has handed it back. You asked them to: "
        f"{handoff.why}\n\n"
        "Look at the screen before you go on: they may have done it, or not managed "
        "to. Then carry on with your work from where you stopped. If you still need "
        "a person, run `flotta-handoff` again and end your turn."
    )


# -- the email ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Recipient:
    email: str
    #: One of the client's people rather than the agency's: the words differ.
    client: bool


def recipients(store: FleetStore, handoff: Handoff) -> list[Recipient]:
    """Everyone who may drive the agent's screen, each address once, the agency first."""
    clients_drive = store.clients_control_screen(handoff.box_id)
    agency: list[str] = []
    theirs: list[str] = []
    for membership in store.members_of(handoff.org):
        if membership.role in AGENCY_DRIVERS:
            into = agency
        elif (
            clients_drive
            and membership.role == CLIENT_DRIVER
            and membership.client == handoff.client
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


def handoff_message(
    to: Recipient,
    handoff: Handoff,
    *,
    agent: str,
    client_name: str,
    app_url: str,
    in_run: bool,
) -> Message:
    """The email telling `to` that `agent` needs a person on its screen."""
    link = f"{app_url.rstrip('/')}{screen_path(handoff.box_id)}"
    subject = f"{agent} needs you on its screen"
    who = "" if to.client else f" for {client_name}"
    lines = [f"{agent}{who} needs a person on its screen before it can go on."]
    lines.append(f"What it needs: {handoff.why}")
    if handoff.url:
        lines.append(f"The page: {handoff.url}")
    run_line = (
        "It is in the middle of a run, which waits for you and carries on by itself "
        "once you hand the screen back."
    )
    if in_run:
        lines.append(run_line)
    lines.append(
        "Open its screen, take over, do it there, then hand the screen back. Type any "
        "password or code on the screen, never in the conversation: whatever you sign "
        "in to there, the agent keeps."
    )
    lines.append(f"Open the screen: {link}")
    footer = (
        "Flotta sends this when an agent asks for a person on its screen and nobody has "
        "taken it within a minute, to everyone who may drive that screen."
    )
    text = "\n\n".join([*lines, footer])
    body = [
        paragraph(esc(lines[0])),
        paragraph(f"What it needs: <strong>{esc(handoff.why)}</strong>"),
    ]
    if handoff.url:
        # As text, never a link: an agent's word for a page is not ours to send people to.
        body.append(paragraph(f"The page: {esc(handoff.url)}"))
    if in_run:
        body.append(paragraph(esc(run_line)))
    body.append(paragraph(esc(lines[-2])))
    page = layout(
        title=subject,
        eyebrow="Needs you on the screen",
        heading=subject,
        preheader=lines[0],
        body=body,
        action=("Open screen", link),
        note="Hand the screen back when you are done.",
        footer=footer,
    )
    return Message(to=to.email, subject=subject, text=text, html=page, link=link, kind=subject)


def _in_run(store: FleetStore, handoff: Handoff) -> bool:
    if not handoff.run_id:
        return False
    run = store.get_run(handoff.run_id)
    return run is not None and run.trigger_kind != "chat" and run.status == "running"


def notify_open(
    store: FleetStore,
    *,
    mailer: Mailer | None,
    app_url: str,
    now: datetime | None = None,
    grace_s: int = GRACE_S,
) -> int:
    """One sweep: email each handoff open past the grace. Returns how many were told.

    Claimed before the send and released if nobody could be told, as approvals
    are (`flotta.approval_mail`). At most one email per agent in
    `NOTICE_QUIET_S`: a later one waits for the quiet. A handoff reused for
    another run is due again. Without a mailer nothing is claimed.
    """
    if mailer is None:
        return 0
    moment = now or datetime.now(UTC)
    quiet_since = moment - timedelta(seconds=NOTICE_QUIET_S)
    told = 0
    for handoff in store.handoffs_to_notify(
        older_than=moment - timedelta(seconds=grace_s), quiet_since=quiet_since
    ):
        before = store.claim_handoff_notice(handoff.id, now=moment, quiet_since=quiet_since)
        if before is None:
            continue
        sent = 0
        try:
            people = recipients(store, handoff)
            box = store.get_box(handoff.box_id)
            meta = store.meta_for_box(handoff.box_id)
            box_name = box.name if box else handoff.box_id
            agent = (meta.display_name if meta else None) or box_name
            client = store.get_client(handoff.org, handoff.client)
            in_run = _in_run(store, handoff)
            for person in people:
                mailer(
                    handoff_message(
                        person,
                        handoff,
                        agent=agent,
                        client_name=client.name if client else handoff.client,
                        app_url=app_url,
                        in_run=in_run,
                    )
                )
                sent += 1
        except Exception as exc:  # noqa: BLE001 - one handoff can't stop the others
            # The type only: a mailer's message can carry a URL or a key.
            _log.warning("handoff %s not emailed: %s", handoff.id, type(exc).__name__)
            if sent == 0:
                store.release_handoff_notice(handoff.id, before)
            continue
        if sent:
            told += 1
    return told


async def run_handoff_mail_loop(
    store_factory: Callable[[], FleetStore],
    *,
    mailer: Mailer | None,
    app_url: str,
    interval_s: float = SWEEP_S,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    """Email open handoffs until cancelled. A sweep that fails is logged."""

    def sweep() -> int:
        with store_factory() as store:
            return notify_open(store, mailer=mailer, app_url=app_url)

    while True:
        try:
            await asyncio.to_thread(sweep)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad sweep must not end the loop
            _log.warning("handoff email sweep failed: %s", type(exc).__name__)
        await sleep(interval_s)
