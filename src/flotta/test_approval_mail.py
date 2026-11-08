"""Emailing who can answer a waiting approval (FLOTTA-167).

The guards first: nobody is told before the grace, nobody twice, and only
people who may answer it are told.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from flotta.approval_mail import (
    GRACE_S,
    Recipient,
    approval_message,
    notify_waiting,
    recipients,
    run_approval_mail_loop,
)
from flotta.store import FleetStore
from flotta.test_migrations import target  # noqa: F401
from flotta.test_usage import T0, seed_agency

ASKED = T0
AFTER_GRACE = T0 + timedelta(seconds=GRACE_S + 1)


@pytest.fixture
def agency(target):  # noqa: F811
    """Studio Nord, with an owner, a member, people at two of its clients, and
    an owner of another agency."""
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
        yield store


def _ask(store, box="b-one", request_id="srq-1", at=ASKED):
    run = store.create_run(box, "Tidy up", trigger_kind="api")
    store.start_run(run.id, now=at)
    return store.ask_approval(
        run.id,
        request_id=request_id,
        command="rm -rf /tmp/old-exports",
        description="delete in root path",
        choices=["once", "session", "always", "deny"],
        expires_at=at + timedelta(minutes=30),
        now=at,
    )


def _hold(store, approval, at):
    """The delivery says it still holds the socket, as the runner does."""
    store._conn.execute(
        "UPDATE run_approvals SET held_at = ? WHERE id = ?", (at.isoformat(), approval.id)
    )


class Outbox:
    def __init__(self, fail: int = 0) -> None:
        self.sent: list = []
        self.fail = fail

    def __call__(self, message) -> None:
        if self.fail:
            self.fail -= 1
            raise ConnectionError("Resend is down, key=secret")
        self.sent.append(message)

    @property
    def to(self) -> list[str]:
        return sorted(m.to for m in self.sent)


def _sweep(store, outbox, at):
    return notify_waiting(store, mailer=outbox, app_url="https://app.flotta.dev", now=at)


def test_nobody_is_told_within_the_grace(agency):
    approval = _ask(agency)
    outbox = Outbox()

    _sweep(agency, outbox, ASKED + timedelta(seconds=GRACE_S - 5))

    assert outbox.sent == []
    assert agency.get_approval(approval.id).status == "waiting"


def test_an_approval_answered_within_the_grace_is_never_emailed(agency):
    approval = _ask(agency)
    agency.answer_approval(approval.id, "once", by="user:owner", now=ASKED + timedelta(seconds=10))
    outbox = Outbox()

    _hold(agency, approval, AFTER_GRACE)
    _sweep(agency, outbox, AFTER_GRACE)

    assert outbox.sent == []


def test_the_agencys_people_are_told_once(agency):
    approval = _ask(agency)
    outbox = Outbox()
    _hold(agency, approval, AFTER_GRACE)

    assert _sweep(agency, outbox, AFTER_GRACE) == 1
    assert outbox.to == ["member@nord.test", "owner@nord.test"]

    _hold(agency, approval, AFTER_GRACE + timedelta(minutes=1))
    _sweep(agency, outbox, AFTER_GRACE + timedelta(minutes=1))
    assert len(outbox.sent) == 2


def test_the_clients_people_are_told_only_when_the_agent_lets_them_answer(agency):
    agency.set_clients_answer_approvals("b-one", True)
    approval = _ask(agency)

    told = recipients(agency, approval)

    assert told == [
        Recipient("owner@nord.test", client=False),
        Recipient("member@nord.test", client=False),
        Recipient("ana@acme.test", client=True),
    ]


def test_without_client_approvals_no_client_person_is_told(agency):
    approval = _ask(agency)

    told = {r.email for r in recipients(agency, approval)}

    assert told == {"owner@nord.test", "member@nord.test"}


def test_a_disabled_person_is_not_told(agency):
    member = agency.get_user_by_email("member@nord.test")
    agency.disable_user(member.id)
    approval = _ask(agency)

    assert [r.email for r in recipients(agency, approval)] == ["owner@nord.test"]


def test_without_a_mailer_nothing_is_sent_and_nothing_claimed(agency):
    approval = _ask(agency)
    _hold(agency, approval, AFTER_GRACE)

    assert notify_waiting(agency, mailer=None, app_url="x", now=AFTER_GRACE) == 0

    outbox = Outbox()
    _sweep(agency, outbox, AFTER_GRACE)
    assert len(outbox.sent) == 2


def test_a_send_that_fails_is_tried_again_next_sweep(agency, caplog):
    approval = _ask(agency)
    outbox = Outbox(fail=1)
    _hold(agency, approval, AFTER_GRACE)

    assert _sweep(agency, outbox, AFTER_GRACE) == 0
    assert outbox.sent == []
    assert "secret" not in caplog.text

    _sweep(agency, outbox, AFTER_GRACE)
    assert outbox.to == ["member@nord.test", "owner@nord.test"]


def test_one_failing_approval_does_not_hold_up_the_next(agency):
    first = _ask(agency, request_id="srq-1")
    second = _ask(agency, box="b-three", request_id="srq-2")
    for approval in (first, second):
        _hold(agency, approval, AFTER_GRACE)
    outbox = Outbox(fail=1)

    assert _sweep(agency, outbox, AFTER_GRACE) == 1
    assert len(outbox.sent) == 2


def test_an_approval_nothing_holds_is_not_emailed(agency):
    """Home doesn't list it, and an answer would have nowhere to go."""
    _ask(agency)
    outbox = Outbox()

    _sweep(agency, outbox, ASKED + timedelta(minutes=5))

    assert outbox.sent == []


def test_the_email_says_who_wants_what_and_where_to_answer(agency):
    approval = _ask(agency)

    message = approval_message(
        Recipient("owner@nord.test", client=False),
        approval,
        agent="Front desk",
        client_name="Acme Dental",
        app_url="https://app.flotta.dev/",
    )

    assert message.subject == "Front desk is waiting for your OK"
    assert "Front desk for Acme Dental" in message.text
    assert "delete in root path" in message.text
    assert "rm -rf /tmp/old-exports" in message.text
    assert "12:30 UTC on 2026-11-02" in message.text
    assert "https://app.flotta.dev/" in message.text
    assert "every approval" in message.text
    assert "<pre>rm -rf /tmp/old-exports</pre>" in message.html


def test_a_clients_person_is_not_told_the_clients_name(agency):
    approval = _ask(agency)

    message = approval_message(
        Recipient("ana@acme.test", client=True),
        approval,
        agent="Front desk",
        client_name="Acme Dental",
        app_url="https://app.flotta.dev",
    )

    assert "Acme Dental" not in message.text
    assert "your assistants page" in message.text


def test_the_loop_survives_a_sweep_that_fails():
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("database away")

    async def stop_after_two(_s):
        if len(calls) >= 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_approval_mail_loop(broken, mailer=print, app_url="x", sleep=stop_after_two))
    assert len(calls) == 2
