"""Handoffs in the store, and who is emailed about one (FLOTTA-213).

On SQLite and, in CI, on Postgres (`target`). The guards first: nobody is told
before the grace or twice, and only people who may drive the screen are told.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from flotta.handoffs import (
    GRACE_S,
    NOTICE_QUIET_S,
    Recipient,
    continue_message,
    handoff_message,
    notify_open,
    recipients,
    run_handoff_mail_loop,
)
from flotta.store import FleetStore
from flotta.test_migrations import target  # noqa: F401
from flotta.test_usage import T0, seed_agency

AFTER_GRACE = T0 + timedelta(seconds=GRACE_S + 1)


@pytest.fixture
def agency(target):  # noqa: F811
    with FleetStore(target) as store:
        seed_agency(store)
        people = {
            "owner@nord.test": ("owner", None),
            "member@nord.test": ("member", None),
            "ana@acme.test": ("client_user", "acme"),
            "viewer@acme.test": ("client_viewer", "acme"),
            "bo@bravo.test": ("client_user", "bravo"),
        }
        for email, (role, client) in people.items():
            user = store.create_user(email)
            store.add_membership(user.id, "studio-nord", role, client_id=client)
        other = store.create_user("owner@other.test")
        store.add_membership(other.id, "other", "owner")
        store.set_box_meta("b-one", display_name="Front desk")
        store.set_box_screen("b-one", True)
        yield store


class Outbox:
    def __init__(self, fail: int = 0) -> None:
        self.sent: list = []
        self.fail = fail

    def __call__(self, message) -> None:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("the mail provider is down")
        self.sent.append(message)


def _file(store, why="Sign in to Shopify", **kwargs):
    handoff, _ = store.file_handoff("b-one", why=why, now=T0, **kwargs)
    return handoff


def test_the_agency_is_told_and_a_client_only_if_it_may_drive(agency):
    handoff = _file(agency)
    assert [r.email for r in recipients(agency, handoff)] == ["owner@nord.test", "member@nord.test"]
    agency.set_clients_control_screen("b-one", True)
    assert recipients(agency, handoff) == [
        Recipient("owner@nord.test", False),
        Recipient("member@nord.test", False),
        Recipient("ana@acme.test", True),
    ]


def test_nobody_is_told_before_the_grace_and_nobody_twice(agency):
    _file(agency)
    outbox = Outbox()
    assert notify_open(agency, mailer=outbox, app_url="https://app.test", now=T0) == 0
    assert notify_open(agency, mailer=outbox, app_url="https://app.test", now=AFTER_GRACE) == 1
    assert notify_open(agency, mailer=outbox, app_url="https://app.test", now=AFTER_GRACE) == 0
    assert sorted(m.to for m in outbox.sent) == ["member@nord.test", "owner@nord.test"]
    message = outbox.sent[0]
    assert message.subject == "Front desk needs you on its screen"
    assert message.link == "https://app.test/agents/b-one#screen"
    assert "Sign in to Shopify" in message.text


def test_filing_again_while_open_tells_nobody_again(agency):
    _file(agency)
    outbox = Outbox()
    notify_open(agency, mailer=outbox, app_url="https://app.test", now=AFTER_GRACE)
    again, new = agency.file_handoff("b-one", why="Now the 2FA code", now=AFTER_GRACE)
    assert new is False
    assert notify_open(agency, mailer=outbox, app_url="https://app.test", now=AFTER_GRACE) == 0
    assert agency.get_handoff(again.id).why == "Now the 2FA code"


def test_an_agent_that_keeps_asking_is_emailed_about_at_most_once_in_the_quiet(agency):
    """The guard for the review of #239: an agent has root, and could hand
    over, clear and ask again every minute; the agency's inbox is not its."""
    outbox = Outbox()
    for n in range(5):
        at = T0 + timedelta(seconds=90 * n)
        handoff = agency.file_handoff("b-one", why=f"Sign in, try {n}", now=at)[0]
        notify_open(
            agency,
            mailer=outbox,
            app_url="https://app.test",
            now=at + timedelta(seconds=GRACE_S + 1),
        )
        agency.dismiss_handoff(handoff.id, by="owner@nord.test")
    assert len({m.subject for m in outbox.sent}) == 1
    assert len(outbox.sent) == 2, "one email, to its two agency people"
    later = T0 + timedelta(seconds=NOTICE_QUIET_S + 600)
    agency.file_handoff("b-one", why="Once more", now=later)
    assert (
        notify_open(
            agency,
            mailer=outbox,
            app_url="https://app.test",
            now=later + timedelta(seconds=GRACE_S + 1),
        )
        == 1
    )


def test_the_quiet_is_per_agent(agency):
    agency.set_box_screen("b-two", True)
    agency.file_handoff("b-one", why="Sign in", now=T0)
    agency.file_handoff("b-two", why="Sign in", now=T0)
    assert notify_open(agency, mailer=Outbox(), app_url="https://app.test", now=AFTER_GRACE) == 2


def test_a_conversations_handoff_reused_by_a_run_is_emailed_again(agency):
    """The review of #239: a stale conversation's handoff, already emailed,
    must not swallow the email a later run waits fifty minutes on."""
    turn = agency.open_chat_run("b-one", "Log in for me", input={"session_id": "s-1"}, now=T0)
    agency.file_handoff("b-one", why="Sign in", now=T0)
    agency.finish_chat_run(turn.id, "b-one", "done")
    outbox = Outbox()
    assert notify_open(agency, mailer=outbox, app_url="https://app.test", now=AFTER_GRACE) == 1
    run = agency.create_run("b-one", "Export", trigger_kind="api")
    agency.start_run(run.id)
    reused, new = agency.file_handoff("b-one", why="Sign in for the export")
    assert (new, reused.run_id) == (False, run.id)
    soon = AFTER_GRACE + timedelta(minutes=5)
    assert notify_open(agency, mailer=outbox, app_url="https://app.test", now=soon) == 0, "quiet"
    after = AFTER_GRACE + timedelta(seconds=NOTICE_QUIET_S + 1)
    assert notify_open(agency, mailer=outbox, app_url="https://app.test", now=after) == 1
    assert "carries on by itself" in outbox.sent[-1].text
    assert notify_open(agency, mailer=outbox, app_url="https://app.test", now=after) == 0


def test_a_handoff_handed_back_or_dismissed_before_the_email_is_not_sent(agency):
    first = _file(agency)
    agency.hand_back_handoffs("b-one", since=T0 + timedelta(seconds=5))
    assert agency.get_handoff(first.id).status == "handed_back"
    second = agency.file_handoff("b-one", why="Pay it", now=T0 + timedelta(seconds=10))[0]
    agency.dismiss_handoff(second.id, by="owner@nord.test")
    outbox = Outbox()
    later = T0 + timedelta(hours=1)
    assert notify_open(agency, mailer=outbox, app_url="https://app.test", now=later) == 0


def test_a_failed_send_is_tried_again(agency):
    _file(agency)
    outbox = Outbox(fail=1)
    assert notify_open(agency, mailer=outbox, app_url="https://app.test", now=AFTER_GRACE) == 0
    assert notify_open(agency, mailer=outbox, app_url="https://app.test", now=AFTER_GRACE) == 1


def test_without_a_mailer_nothing_is_claimed(agency):
    _file(agency)
    assert notify_open(agency, mailer=None, app_url="https://app.test", now=AFTER_GRACE) == 0
    outbox = Outbox()
    assert notify_open(agency, mailer=outbox, app_url="https://app.test", now=AFTER_GRACE) == 1


def test_the_page_is_said_as_text_never_as_a_link(agency):
    handoff = _file(agency, url="https://admin.shopify.com/login")
    message = handoff_message(
        Recipient("owner@nord.test", False),
        handoff,
        agent="Front desk",
        client_name="Acme Dental",
        app_url="https://app.test",
        in_run=True,
    )
    assert "https://admin.shopify.com/login" in message.html
    assert 'href="https://admin.shopify.com' not in message.html
    assert "carries on by itself" in message.text
    assert "never in the conversation" in message.text


def test_a_run_ending_expires_its_handoff_and_a_conversations_turn_does_not(agency):
    run = agency.create_run("b-one", "Export", trigger_kind="api")
    agency.start_run(run.id)
    in_run = _file(agency)
    assert in_run.run_id == run.id
    agency.finish_run(run.id, "failed", error_class="timeout")
    assert agency.get_handoff(in_run.id).status == "expired"
    turn = agency.open_chat_run("b-one", "Log in for me", input={"session_id": "s-1"})
    in_turn = agency.file_handoff("b-one", why="Sign in")[0]
    assert in_turn.run_id == turn.id
    agency.finish_chat_run(turn.id, "b-one", "done")
    assert agency.get_handoff(in_turn.id).status == "open"


def test_the_carry_on_names_what_was_asked():
    from flotta.store import Handoff

    handoff = Handoff(
        "ho-1", "b-one", "o", "c", "Sign in", None, None, "handed_back", "", None, None
    )
    said = continue_message(handoff)
    assert "handed it back" in said and "Sign in" in said and "flotta-handoff" in said


def test_the_loop_survives_a_bad_sweep():
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("no store")

    async def go():
        async def sleep(_):
            if len(calls) >= 2:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await run_handoff_mail_loop(broken, mailer=Outbox(), app_url="x", sleep=sleep)

    asyncio.run(go())
    assert len(calls) == 2
