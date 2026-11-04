"""Budget alerts (FLOTTA-93, M13.7), on every engine the suite has.

The guards come first. **The same threshold alerts once, a webhook can't reach
anything private, and the signature is the one the docs describe.**
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import flotta
from flotta import alerts
from flotta.alerts import (
    SIGNATURE_HEADER,
    WebhookRefused,
    allow_local_webhooks,
    check_addresses,
    check_webhook_url,
    deliver_due,
    sign,
    verify_signature,
)
from flotta.auth import webhook_secret
from flotta.mail import print_mailer
from flotta.pricing import NANO
from flotta.store import FleetStore
from flotta.test_migrations import target  # noqa: F401

KEY = "a-signing-key-for-these-tests"
HOOK = "https://hooks.example.com/flotta"
START = datetime(2026, 11, 4, tzinfo=UTC)
PUBLIC = ["93.184.216.34"]


@pytest.fixture
def fleet(target):  # noqa: F811
    with FleetStore(target) as store:
        store.create_org("studio-nord", "Studio Nord")
        store.create_client("studio-nord", "acme", "Acme Dental")
        store.create_box("one", box_id="b-one", org="studio-nord", client="acme")
        owner = store.create_user("owner@example.com")
        store.add_membership(owner.id, "studio-nord", role="owner")
        member = store.create_user("member@example.com")
        store.add_membership(member.id, "studio-nord", role="member")
        yield store, target


def _budget(store, **overrides):
    values = {"period": "day", "limit_eur_nano": 20 * NANO, "action": "pause", "client": "acme"}
    return store.set_budget("studio-nord", **{**values, **overrides})


def _cross(store, budget, kind="budget_limit", period=START, spent="20.01", **said):
    from flotta.budgets import budget_dict

    details = {
        **budget_dict(budget),
        "spent_eur": spent,
        "resets_at": "2026-11-05T00:00:00+00:00",
        **said,
    }
    return store.record_budget_event(budget, kind, period_start=period, payload={"budget": details})


def _webhook(store, url=HOOK, nonce="nonce-1"):
    store.set_org_alerts("studio-nord", alert_email=None, webhook_url=url, nonce=nonce)


class _Sends:
    """Everything that would have left the machine."""

    def __init__(self, status=200):
        self.status = status
        self.mail = []
        self.posts = []
        self.now = 1_793_800_000.0

    def mailer(self, message):
        self.mail.append(message)

    async def post(self, url, address, body, headers):
        self.posts.append((url, address, body, headers))
        return self.status

    async def resolve(self, host, port):
        return PUBLIC

    def sweep(self, target, **overrides):  # noqa: F811
        values = {
            "mailer": self.mailer,
            "app_url": "https://app.flotta.dev",
            "signing_key": KEY,
            "post": self.post,
            "resolve": self.resolve,
            "clock": lambda: self.now,
        }
        return asyncio.run(deliver_due(lambda: FleetStore(target), **{**values, **overrides}))


# -- the guards ---------------------------------------------------------------


def test_the_same_threshold_alerts_once_not_once_per_sweep(fleet):
    store, db = fleet
    _webhook(store)
    budget = _budget(store)
    assert _cross(store, budget)
    assert not _cross(store, budget), "the sweep crossing it again writes nothing"

    sends = _Sends()
    for _ in range(4):
        sends.sweep(db)
        sends.now += 8000
    assert [m.to for m in sends.mail] == ["owner@example.com"]
    assert len(sends.posts) == 1


def test_the_soft_threshold_and_the_limit_each_alert_once(fleet):
    store, db = fleet
    _webhook(store)
    budget = _budget(store)
    _cross(store, budget, "budget_soft_limit", spent="16")
    sends = _Sends()
    sends.sweep(db)
    _cross(store, budget, "budget_limit")
    sends.sweep(db)
    sends.sweep(db)
    assert len(sends.mail) == 2
    kinds = [json.loads(post[2])["type"] for post in sends.posts]
    assert kinds == ["budget.soft_limit", "budget.limit"]


def test_the_next_period_alerts_again(fleet):
    store, db = fleet
    budget = _budget(store)
    _cross(store, budget)
    sends = _Sends()
    sends.sweep(db)
    _cross(store, budget, period=datetime(2026, 11, 5, tzinfo=UTC))
    sends.sweep(db)
    assert len(sends.mail) == 2


def test_two_sweeps_at_once_send_it_once(fleet):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    with FleetStore(db) as one, FleetStore(db) as other:
        one.queue_budget_alerts()
        other.queue_budget_alerts()
        due = one.due_alert_deliveries(100, limit=10)
        assert len(due) == 2
        assert [one.claim_alert_delivery(d, next_attempt_at=200) for d in due] == [True, True]
        assert [other.claim_alert_delivery(d, next_attempt_at=200) for d in due] == [False, False]


def test_queuing_is_done_once_however_many_sweeps_look(fleet):
    store, _ = fleet
    _webhook(store)
    _cross(store, _budget(store))
    assert store.queue_budget_alerts() == 2
    assert store.queue_budget_alerts() == 0


def test_a_budget_event_from_before_alerts_says_nothing_after_the_deploy(target):  # noqa: F811
    """Or the first sweep after this ships tells everyone about everything."""
    from flotta.migrations import MIGRATIONS

    assert any("UPDATE budget_events SET alerts_queued_at = ts" in m for m in _steps(MIGRATIONS))
    with FleetStore(target) as store:
        store.create_org("studio-nord", "Studio Nord")
        owner = store.create_user("owner@example.com")
        store.add_membership(owner.id, "studio-nord", role="owner")
        _cross(store, _budget(store, client=None))
        store._conn.execute("UPDATE budget_events SET alerts_queued_at = ts")
        assert store.queue_budget_alerts() == 0


def _steps(migrations):
    return [step for migration in migrations for step in migration.steps]


def test_a_restart_before_a_send_loses_nothing(fleet):
    """Queued and not sent is still due, after the moment its claim moved it to."""
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    store.queue_budget_alerts()
    delivery = store.due_alert_deliveries(100, limit=10)[0]
    store.claim_alert_delivery(delivery, next_attempt_at=200)
    assert delivery.id not in [d.id for d in store.due_alert_deliveries(199, limit=10)]
    assert delivery.id in [d.id for d in store.due_alert_deliveries(200, limit=10)]


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/flotta",
        "ftp://hooks.example.com/flotta",
        "https://127.0.0.1/hook",
        "https://10.0.0.8/hook",
        "https://172.16.0.1/hook",
        "https://192.168.1.1/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://100.64.0.1/hook",
        "https://0.0.0.0/hook",
        "https://[::1]/hook",
        "https://[fe80::1]/hook",
        "https://[fdaa:0:1::2]/hook",
        "https://[::ffff:10.0.0.8]/hook",
        "https://224.0.0.1/hook",
        "https://239.255.255.250/hook",
        "https://[ff02::1]/hook",
        "https://[ff0e::1]/hook",
        "https://[fec0::1]/hook",
        "https://[5f00::1]/hook",
        "https://localhost/hook",
        "https://box.localhost/hook",
        "https://eng-a.internal/hook",
        "https://printer.local/hook",
        "https://user:pass@hooks.example.com/flotta",
        "not a url",
        "https:///nohost",
    ],
)
def test_a_url_that_could_not_be_a_webhook_is_refused_when_it_is_set(url):
    with pytest.raises(WebhookRefused):
        check_webhook_url(url)


def test_a_public_https_url_is_a_webhook():
    assert check_webhook_url(f" {HOOK} ") == HOOK
    assert check_webhook_url("https://93.184.216.34:8443/hook")


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.0.0.8"],
        ["169.254.169.254"],
        ["::1"],
        ["fdaa:0:1::2"],
        ["::ffff:127.0.0.1"],
        ["64:ff9b::a00:8"],
        ["2002:a00:8::"],
        ["::ffff:93.184.216.34"],
        ["64:ff9b::5db8:d822"],
        ["2002:5db8:d822::"],
        ["2001:0:4136:e378:8000:63bf:3fff:fdd2"],
        ["93.184.216.34", "10.0.0.8"],
        ["224.0.0.1"],
        ["ff02::1"],
        ["fec0::1"],
        ["4000::1"],
        ["93.184.216.34", "ff02::1"],
        ["fe80::1%eth0"],
        ["not an address"],
    ],
)
def test_a_name_resolving_to_anything_private_is_refused_when_it_is_sent(addresses):
    async def resolve(host, port):
        return addresses

    with pytest.raises(WebhookRefused):
        asyncio.run(check_addresses(HOOK, resolve=resolve))


def test_a_name_that_resolves_to_public_addresses_goes_to_one_that_was_checked():
    async def resolve(host, port):
        return ["93.184.216.34", "2606:2800:220:1::1"]

    assert asyncio.run(check_addresses(HOOK, resolve=resolve)) == "93.184.216.34"


def test_a_refused_webhook_is_never_called_and_says_so_at_once(fleet):
    """A name that started resolving somewhere private, after it was set."""
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))

    async def private(host, port):
        return ["10.0.0.8"]

    sends = _Sends()
    sends.sweep(db, resolve=private)
    sends.now += 100_000
    sends.sweep(db, resolve=private)
    assert sends.posts == []
    webhook = next(d for d in store.alert_deliveries("studio-nord") if d.channel == "webhook")
    assert webhook.status == "failed"
    assert webhook.attempts == 1
    assert "public" in webhook.last_error


def test_the_signature_is_the_one_the_docs_show():
    docs = pathlib.Path(flotta.__file__).parents[2] / "docs" / "ALERTS.md"
    text = docs.read_text()
    fixture = text.split("checked by src/flotta/test_alerts.py")[1].split("```text\n")[1]
    header = fixture.split("\n")[0]
    body = b'{"id":"be-example","type":"budget.limit"}'
    assert header == sign("whsec-example", 1793800000, body)
    assert verify_signature("whsec-example", header, body, now=1793800000)


def test_the_python_in_the_docs_agrees_with_what_is_sent():
    docs = pathlib.Path(flotta.__file__).parents[2] / "docs" / "ALERTS.md"
    source = docs.read_text().split("```python\n")[1].split("```")[0]
    namespace: dict = {}
    exec(compile(ast.parse(source), "ALERTS.md", "exec"), namespace)  # noqa: S102
    body = b'{"id":"be-1"}'
    good = sign("the-secret", int(time.time()), body)
    assert namespace["verify"]("the-secret", good, body)
    assert not namespace["verify"]("another-secret", good, body)
    assert not namespace["verify"]("the-secret", good, b'{"id":"be-2"}')
    old = sign("the-secret", int(time.time()) - 301, body)
    assert not namespace["verify"]("the-secret", old, body)


@pytest.mark.parametrize(
    ("secret", "body", "now"),
    [
        ("another-secret", b"{}", 1000),
        ("the-secret", b'{"changed":1}', 1000),
        ("the-secret", b"{}", 1000 + 301),
        ("the-secret", b"{}", 1000 - 301),
    ],
)
def test_a_signature_does_not_verify_for_anything_else(secret, body, now):
    header = sign("the-secret", 1000, b"{}")
    assert not verify_signature(secret, header, body, now=now)


@pytest.mark.parametrize("header", ["", "t=abc,v1=00", "v1=00", "garbage", "t=1000"])
def test_a_bad_header_does_not_verify(header):
    assert not verify_signature("the-secret", header, b"{}", now=1000)


def test_what_is_sent_is_signed_under_the_orgs_secret_with_an_id_that_stays(fleet):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    sends = _Sends(status=500)
    sends.sweep(db)
    sends.now += 60
    sends.sweep(db)
    assert len(sends.posts) == 2
    secret = webhook_secret(KEY, "studio-nord", "nonce-1")
    ids = set()
    stamps = set()
    for url, address, body, headers in sends.posts:
        assert url == HOOK
        assert address == PUBLIC[0]
        assert verify_signature(secret, headers[SIGNATURE_HEADER], body, now=sends.now)
        ids.add(headers[alerts.DELIVERY_HEADER])
        stamps.add(headers[SIGNATURE_HEADER])
    assert len(ids) == 1, "a receiver ignores a repeat by this"
    assert len(stamps) == 2, "a retry is signed for when it is sent"


def test_each_orgs_webhook_has_its_own_secret():
    assert webhook_secret(KEY, "a", "n") != webhook_secret(KEY, "b", "n")
    assert webhook_secret(KEY, "a", "n") != webhook_secret(KEY, "a", "m")
    assert webhook_secret(KEY, "a", "n") != webhook_secret("another-key", "a", "n")


# -- a webhook that misbehaves ------------------------------------------------


def test_a_failing_webhook_is_retried_with_waits_and_then_recorded(fleet):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    sends = _Sends(status=500)
    sends.sweep(db)
    assert len(sends.posts) == 1
    sends.sweep(db)
    assert len(sends.posts) == 1, "not before its wait"
    for wait in alerts.RETRY_AFTER_S:
        sends.now += wait - 1
        sends.sweep(db)
        before = len(sends.posts)
        sends.now += 1
        sends.sweep(db)
        assert len(sends.posts) == before + 1
    assert len(sends.posts) == alerts.MAX_ATTEMPTS
    sends.now += 100_000
    sends.sweep(db)
    assert len(sends.posts) == alerts.MAX_ATTEMPTS

    webhook = next(d for d in store.alert_deliveries("studio-nord") if d.channel == "webhook")
    assert webhook.status == "failed"
    assert webhook.attempts == alerts.MAX_ATTEMPTS
    failed = [e for e in store.budget_events("studio-nord") if e.type.startswith("alert_failed")]
    assert [e.type for e in failed] == ["alert_failed:budget_limit:webhook"]
    assert failed[0].payload["error"] == "the endpoint answered 500"


def test_a_webhook_that_recovers_is_marked_sent(fleet):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    sends = _Sends(status=500)
    sends.sweep(db)
    sends.status = 204
    sends.now += alerts.RETRY_AFTER_S[0]
    sends.sweep(db)
    webhook = next(d for d in store.alert_deliveries("studio-nord") if d.channel == "webhook")
    assert (webhook.status, webhook.attempts, webhook.last_error) == ("sent", 2, None)
    assert not [e for e in store.budget_events("studio-nord") if e.type.startswith("alert_failed")]


@pytest.mark.parametrize("status", [301, 302, 307, 400, 404, 500])
def test_only_a_2xx_counts(fleet, status):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    _Sends(status=status).sweep(db)
    webhook = next(d for d in store.alert_deliveries("studio-nord") if d.channel == "webhook")
    assert webhook.status == "pending"
    assert str(status) in webhook.last_error


def test_a_webhook_that_hangs_holds_up_nothing_but_itself(fleet):
    """Its deadline ends it, and the email in the same sweep has already gone."""
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    sends = _Sends()

    async def hang(url, address, body, headers):
        await asyncio.sleep(8)
        return 200

    started = time.monotonic()
    sends.sweep(db, post=hang, deadline_s=0.05)
    assert time.monotonic() - started < 5
    assert [m.to for m in sends.mail] == ["owner@example.com"]
    webhook = next(d for d in store.alert_deliveries("studio-nord") if d.channel == "webhook")
    assert webhook.status == "pending"
    assert "no answer" in webhook.last_error


def test_what_a_client_raised_is_not_written_down_in_full(fleet):
    """An exception's message can carry a URL with a token in it."""
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))

    async def boom(url, address, body, headers):
        raise RuntimeError("https://hooks.example.com/flotta?token=sekret failed")

    _Sends().sweep(db, post=boom)
    webhook = next(d for d in store.alert_deliveries("studio-nord") if d.channel == "webhook")
    assert webhook.last_error == "RuntimeError"


def test_a_webhook_removed_after_it_was_queued_is_not_called(fleet):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    store.queue_budget_alerts()
    store.set_org_alerts("studio-nord", alert_email=None, webhook_url=None, nonce=None)
    sends = _Sends()
    sends.sweep(db)
    assert sends.posts == []


NEW_HOOK = "https://elsewhere.example.com/flotta"


def _webhooks(store):
    return {d.target: d for d in store.alert_deliveries("studio-nord") if d.channel == "webhook"}


def _failures(store):
    events = store.budget_events("studio-nord")
    return [e.type for e in events if e.type.startswith("alert_failed")]


def test_a_webhook_changed_before_its_first_try_gets_the_alert_at_the_new_url(fleet):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    store.queue_budget_alerts()
    _webhook(store, url=NEW_HOOK, nonce="nonce-2")
    sends = _Sends()
    sends.sweep(db)
    assert [post[0] for post in sends.posts] == [NEW_HOOK], "and never the old URL"
    url, _, body, headers = sends.posts[0]
    secret = webhook_secret(KEY, "studio-nord", "nonce-2")
    assert verify_signature(secret, headers["Flotta-Signature"], body, now=sends.now)
    assert json.loads(body)["type"] == "budget.limit"
    found = _webhooks(store)
    assert found[HOOK].status == "replaced"
    assert found[NEW_HOOK].status == "sent"
    assert headers["Flotta-Delivery"] == found[NEW_HOOK].id
    assert _failures(store) == [], "the alert did not fail: it moved"


def test_fixing_a_webhook_after_a_failed_try_delivers_the_alert_straight_away(fleet):
    """The case the retries are for: the endpoint was wrong, and someone fixed it."""
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    broken = _Sends(status=500)
    broken.sweep(db)
    assert _webhooks(store)[HOOK].attempts == 1
    _webhook(store, url=NEW_HOOK, nonce="nonce-2")
    fixed = _Sends()
    fixed.now = broken.now + 1  # Long before the old URL's next try.
    fixed.sweep(db)
    assert [post[0] for post in fixed.posts] == [NEW_HOOK]
    found = _webhooks(store)[NEW_HOOK]
    assert (found.status, found.attempts) == ("sent", 1)
    assert _failures(store) == []


def test_a_fix_that_fails_too_has_all_its_tries(fleet):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    sends = _Sends(status=500)
    sends.sweep(db)
    _webhook(store, url=NEW_HOOK, nonce="nonce-2")
    sends.sweep(db)
    found = _webhooks(store)[NEW_HOOK]
    assert (found.status, found.attempts, found.last_error) == (
        "pending",
        1,
        "the endpoint answered 500",
    )


def test_changing_a_webhook_and_changing_it_back_delivers_once(fleet):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    broken = _Sends(status=500)
    broken.sweep(db)
    _webhook(store, url=NEW_HOOK, nonce="nonce-2")
    _webhook(store, url=HOOK, nonce="nonce-3")
    back = _webhooks(store)[HOOK]
    assert (back.status, back.attempts, back.next_attempt_at) == ("pending", 0, 0)
    assert (back.last_error, back.finished_at) == (None, None)
    sends = _Sends()
    sends.now = broken.now + 1
    sends.sweep(db)
    assert [post[0] for post in sends.posts] == [HOOK]
    secret = webhook_secret(KEY, "studio-nord", "nonce-3")
    assert verify_signature(
        secret, sends.posts[0][3]["Flotta-Signature"], sends.posts[0][2], now=sends.now
    )
    found = _webhooks(store)
    assert found[HOOK].status == "sent"
    assert found[NEW_HOOK].status == "replaced"


def test_an_alert_that_failed_for_good_stays_failed_when_the_webhook_changes(fleet):
    """Its tries are over, and a threshold from last month isn't news. It is on the timeline."""
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    sends = _Sends(status=500)
    for _ in range(alerts.MAX_ATTEMPTS):
        sends.sweep(db)
        sends.now += max(alerts.RETRY_AFTER_S)
    assert _webhooks(store)[HOOK].status == "failed"
    _webhook(store, url=NEW_HOOK, nonce="nonce-2")
    assert NEW_HOOK not in _webhooks(store)


def test_an_alert_already_sent_is_not_sent_again_when_the_webhook_changes(fleet):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    sends = _Sends()
    sends.sweep(db)
    assert len(sends.posts) == 1
    _webhook(store, url=NEW_HOOK, nonce="nonce-2")
    later = _Sends()
    later.sweep(db)
    assert later.posts == []
    assert _webhooks(store)[HOOK].status == "sent"


def test_setting_the_same_url_again_keeps_the_delivery_and_signs_with_the_new_secret(fleet):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    sends = _Sends(status=500)
    sends.sweep(db)
    before = _webhooks(store)[HOOK]
    _webhook(store, nonce="nonce-2")
    assert _webhooks(store)[HOOK] == before, "its tries and its id carry on"
    sends.status = 200
    sends.now += alerts.RETRY_AFTER_S[1]
    sends.sweep(db)
    url, _, body, headers = sends.posts[-1]
    assert url == HOOK
    assert headers["Flotta-Delivery"] == before.id
    secret = webhook_secret(KEY, "studio-nord", "nonce-2")
    assert verify_signature(secret, headers["Flotta-Signature"], body, now=sends.now)


def test_a_webhook_removed_and_set_again_before_a_sweep_keeps_the_alert(fleet):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    store.queue_budget_alerts()
    store.set_org_alerts("studio-nord", alert_email=None, webhook_url=None, nonce=None)
    _webhook(store, url=NEW_HOOK, nonce="nonce-2")
    sends = _Sends()
    sends.sweep(db)
    assert [post[0] for post in sends.posts] == [NEW_HOOK]
    assert _failures(store) == []


def test_changing_only_the_alert_address_leaves_a_queued_webhook_alone(fleet):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    store.queue_budget_alerts()
    before = _webhooks(store)
    store.set_org_alerts(
        "studio-nord", alert_email="ops@example.com", webhook_url=HOOK, nonce="nonce-1"
    )
    assert _webhooks(store) == before


def test_changing_a_webhook_leaves_another_orgs_alerts_alone(fleet):
    store, db = fleet
    store.create_org("other", "Other Agency")
    store.create_client("other", "acme", "Acme")
    theirs = "https://other.example.com/hook"
    store.set_org_alerts("other", alert_email=None, webhook_url=theirs, nonce="theirs")
    _cross(store, store.set_budget("other", period="day", limit_eur_nano=NANO, action="pause"))
    store.queue_budget_alerts()
    _webhook(store, url=NEW_HOOK, nonce="nonce-2")
    assert [(d.target, d.status) for d in store.alert_deliveries("other")] == [(theirs, "pending")]


def test_changing_a_webhook_leaves_a_queued_email_alone(fleet):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    store.queue_budget_alerts()
    _webhook(store, url=NEW_HOOK, nonce="nonce-2")
    emails = [d for d in store.alert_deliveries("studio-nord") if d.channel == "email"]
    assert [(d.target, d.status) for d in emails] == [("owner@example.com", "pending")]
    assert set(_webhooks(store)) == {HOOK, NEW_HOOK}


@pytest.mark.parametrize(
    "answer", [WebhookRefused("this endpoint is refused"), ConnectionError("no answer")]
)
def test_a_webhook_changed_during_a_send_does_not_report_a_failure(fleet, answer):
    """The try that was under way answers for a row that has already moved on."""
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))

    async def changed_meanwhile(url, address, body, headers):
        with FleetStore(db) as other:
            other.set_org_alerts(
                "studio-nord", alert_email=None, webhook_url=NEW_HOOK, nonce="nonce-2"
            )
        raise answer

    _Sends().sweep(db, post=changed_meanwhile)
    found = _webhooks(store)
    assert (found[HOOK].status, found[HOOK].attempts) == ("replaced", 0)
    assert found[NEW_HOOK].status == "pending"
    assert _failures(store) == []
    sends = _Sends()
    sends.sweep(db)
    assert [post[0] for post in sends.posts] == [NEW_HOOK]


def test_a_webhook_changed_between_the_claim_and_the_read_says_nothing_failed(fleet, monkeypatch):
    store, db = fleet
    _webhook(store)
    _cross(store, _budget(store))
    real = FleetStore.claim_alert_delivery

    def claim_then_change(self, delivery, *, next_attempt_at):
        taken = real(self, delivery, next_attempt_at=next_attempt_at)
        if delivery.channel == "webhook":
            self.set_org_alerts(
                "studio-nord", alert_email=None, webhook_url=NEW_HOOK, nonce="nonce-2"
            )
        return taken

    monkeypatch.setattr(FleetStore, "claim_alert_delivery", claim_then_change)
    sends = _Sends()
    sends.sweep(db)
    assert sends.posts == []
    assert _webhooks(store)[HOOK].status == "replaced"
    assert _failures(store) == []


def test_the_gateway_knows_nothing_about_alerts():
    """A misbehaving endpoint can't slow a model call that has no way to reach it."""
    gateway = pathlib.Path(flotta.__file__).parent / "gateway"
    for path in gateway.glob("*.py"):
        if path.name.startswith("test_"):
            continue
        text = path.read_text()
        assert "flotta.alerts" not in text
        assert "record_budget_event" not in text, "the sweep alone says a threshold was crossed"


def test_the_alert_loop_survives_a_sweep_that_fails(monkeypatch):
    sweeps = []

    async def failing(*args, **kwargs):
        sweeps.append(1)
        raise RuntimeError("the store went away")

    async def stop_after_two(_seconds):
        if len(sweeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(alerts, "deliver_due", failing)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            alerts.run_alert_loop(
                lambda: None, mailer=None, app_url="", signing_key=KEY, sleep=stop_after_two
            )
        )
    assert len(sweeps) == 2


# -- an email ----------------------------------------------------------------


def test_it_goes_to_owners_and_the_alert_address_not_members(fleet):
    store, db = fleet
    store.set_org_alerts("studio-nord", alert_email="ops@example.com", webhook_url=None, nonce=None)
    _cross(store, _budget(store))
    sends = _Sends()
    sends.sweep(db)
    assert sorted(m.to for m in sends.mail) == ["ops@example.com", "owner@example.com"]


def test_an_owner_who_is_also_the_alert_address_gets_one(fleet):
    store, db = fleet
    store.set_org_alerts(
        "studio-nord", alert_email="owner@example.com", webhook_url=None, nonce=None
    )
    _cross(store, _budget(store))
    sends = _Sends()
    sends.sweep(db)
    assert [m.to for m in sends.mail] == ["owner@example.com"]


@pytest.mark.parametrize(
    ("kind", "action", "says"),
    [
        ("budget_soft_limit", "pause", "80% of its limit"),
        ("budget_limit", "notify", "only notifies"),
        ("budget_limit", "downgrade", "Calls now go to cheap/model"),
        ("budget_limit", "pause", "paused"),
        ("budget_limit", "kill", "machines have been stopped"),
    ],
)
def test_it_says_which_budget_how_much_and_what_happened(fleet, kind, action, says):
    store, db = fleet
    _cross(store, _budget(store), kind, action=action, fallback_model="cheap/model")
    sends = _Sends()
    sends.sweep(db)
    message = sends.mail[0]
    assert "Client acme" in message.subject
    for part in (message.text, message.html):
        assert says in part
        assert "EUR 20.01 of EUR 20 a day" in part
        assert "https://app.flotta.dev/usage" in part


def test_what_an_email_quotes_is_escaped(fleet):
    store, db = fleet
    event_budget = _budget(store)
    _cross(store, event_budget, spent="<script>1</script>")
    sends = _Sends()
    sends.sweep(db)
    assert "<script>" not in sends.mail[0].html


def test_no_way_to_send_email_is_recorded_rather_than_retried_for_ever(fleet):
    store, db = fleet
    _cross(store, _budget(store))
    sends = _Sends()
    sends.sweep(db, mailer=None)
    email = store.alert_deliveries("studio-nord")[0]
    assert (email.status, email.attempts) == ("failed", 1)


def test_a_mailer_that_fails_is_tried_again(fleet):
    store, db = fleet
    _cross(store, _budget(store))
    tries = []

    def flaky(message):
        tries.append(message)
        if len(tries) == 1:
            raise RuntimeError("Resend answered 429")

    sends = _Sends()
    sends.sweep(db, mailer=flaky)
    sends.now += alerts.RETRY_AFTER_S[0]
    sends.sweep(db, mailer=flaky)
    assert len(tries) == 2
    assert store.alert_deliveries("studio-nord")[0].status == "sent"


def test_printing_an_alert_calls_it_what_it_is(fleet, caplog):
    store, db = fleet
    _cross(store, _budget(store))
    sends = _Sends()
    with caplog.at_level("WARNING", logger="flotta.mail"):
        sends.sweep(db, mailer=print_mailer)
    assert "budget reached its limit for owner@example.com" in caplog.text
    assert "sign-in" not in caplog.text


def test_only_a_laptop_may_have_a_local_webhook():
    assert not allow_local_webhooks(keyless=False, env={})
    assert allow_local_webhooks(keyless=True, env={})
    assert allow_local_webhooks(keyless=False, env={alerts.ALLOW_LOCAL_ENV: "1"})
    assert not allow_local_webhooks(keyless=False, env={alerts.ALLOW_LOCAL_ENV: "0"})


# -- from the sweep to a real socket -------------------------------------------


@pytest.fixture
def receiver():
    got = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.append((self.path, dict(self.headers), body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], got
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_crossing_a_budget_in_the_real_sweep_reaches_a_real_socket_once(fleet, receiver):
    from flotta.provision import enforce_budgets
    from flotta.test_budget_actions import _spend

    store, db = fleet
    port, got = receiver
    url = f"http://localhost:{port}/flotta?x=1"
    _webhook(store, url=url)
    _budget(store, action="notify")
    now = datetime.now(UTC)
    _spend(store, 21 * NANO, now=now)
    enforce_budgets(store, now=now, idle_after_s=10**9)
    enforce_budgets(store, now=now, idle_after_s=10**9)

    sends = _Sends()
    for _ in range(2):
        sends.sweep(
            db,
            allow_local=True,
            post=alerts.post_pinned,
            resolve=check_loopback,
            clock=time.time,
        )

    assert len(got) == 2, "the soft threshold and the limit, once each"
    secret = webhook_secret(KEY, "studio-nord", "nonce-1")
    for path, headers, body in got:
        assert path == "/flotta?x=1"
        assert headers["Host"] == f"localhost:{port}"
        assert verify_signature(secret, headers[SIGNATURE_HEADER], body)
    assert len(sends.mail) == 2


def test_a_request_goes_to_the_address_that_was_checked_not_the_name_again(receiver, monkeypatch):
    """A name can answer differently the second time. No proxy from the environment, either."""
    port, got = receiver
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    status = asyncio.run(
        alerts.post_pinned(
            f"http://nowhere.invalid:{port}/flotta", "127.0.0.1", b"{}", {"X-Test": "1"}
        )
    )
    assert status == 200
    path, headers, _ = got[0]
    assert path == "/flotta"
    assert headers["Host"] == f"nowhere.invalid:{port}"


def test_a_redirect_is_not_followed(monkeypatch):
    got = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status = asyncio.run(
            alerts.post_pinned(f"http://localhost:{port}/first", "127.0.0.1", b"{}", {})
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert status == 302
    assert got == ["/first"]


async def check_loopback(host, port):
    return ["127.0.0.1"]
