"""Founding Studio, and the sign-up funnel PostHog records.

Five founding agencies, ever, and two redeems cannot both take the last place
(the race itself is in `test_store_postgres.py`). A trial caps model use at
EUR 20 and leaves it off the invoice. `signup_completed`, `agent_created` and
`request_filed` carry the properties the funnel asks for, and a test address
is marked `is_test`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from flotta import analytics, budgets
from flotta.auth import SCOPE_BOX_PEER, SCOPE_ORG_GOVERN, box_subject, mint
from flotta.control.test_sign_in import KEY, Outbox, _app
from flotta.founding import (
    FOUNDING_CAP,
    TRIAL_LIMIT_EUR_NANO,
    Attribution,
    parse_attribution,
)
from flotta.judge import COMPLEXITY_NAMES, Verdict
from flotta.pricing import NANO
from flotta.store import FleetStore, FoundingClosedError
from flotta.test_usage import MODEL, _price

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
RATE = Decimal("1")
VISITOR = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def recorded(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(analytics, "sink", lambda event: seen.append(event))
    monkeypatch.setenv("FLOTTA_TEST_EMAILS", "pat@example.com")
    monkeypatch.setenv("FLOTTA_TEST_EMAIL_DOMAINS", "test.flotta.dev")
    return seen


def _events(seen, name):
    return [event for event in seen if event["event"] == name]


def _founding(store, email, org_id, **utm):
    store.sign_up(
        email,
        org_id,
        org_id,
        attribution=Attribution(plan="founding", interval="month", **utm),
    )


def test_five_founding_agencies_and_then_no_more(tmp_path, recorded):
    with FleetStore(tmp_path / "fleet.db") as store:
        for index in range(FOUNDING_CAP):
            _founding(store, f"owner{index}@example.com", f"org-{index}")
        assert store.founding_available() is False
        secret = store.create_login_link(
            "sixth@example.com",
            purpose="sign_up",
            org_id="org-sixth",
            org_name="Sixth",
            attribution=Attribution(plan="founding", interval="month"),
        )
        with pytest.raises(FoundingClosedError):
            store.sign_up_from_link(secret)
        assert store.login_link(secret) is not None, "a refused founding link stays usable"
        assert store.get_org("org-sixth") is None
        # A studio sign-up does not take a founding place, and still gets in.
        store.sign_up(
            "studio@example.com",
            "studio-six",
            "Studio Six",
            attribution=Attribution(plan="studio", interval="year"),
        )
        assert store.get_org("studio-six").plan == "studio"
        assert store._founding_count() == FOUNDING_CAP
        first = store.get_org("org-0")
        assert (first.max_clients, first.max_seats, first.max_agents) == (5, 1, 15)
        assert first.status == "pending"
        assert first.trial_from is None, "the trial starts when the org is approved"


def test_the_trial_caps_model_use_and_leaves_it_off_the_invoice(tmp_path):
    with FleetStore(tmp_path / "fleet.db") as store:
        _founding(store, "ada@example.com", "ada")
        store.refresh_prices([_price()], now=T0 - timedelta(days=1))
        store.decide_org("ada", approve=True, now=T0)
        org = store.get_org("ada")
        assert org.status == "active"
        assert org.trial_until == (T0 + timedelta(days=30)).isoformat(timespec="microseconds")
        cap = next(budget for budget in store.budgets_for_org("ada") if budget.trial_cap)
        assert (cap.limit_eur_nano, cap.action) == (TRIAL_LIMIT_EUR_NANO, "pause")
        store.create_client("ada", "acme", "Acme")
        box = store.create_box("support", org="ada", client="acme")
        during = T0 + timedelta(days=1)

        def call(key, when, euros):
            store.record_usage(
                box.id,
                model=MODEL,
                upstream="openrouter",
                tokens_in=1,
                tokens_out=1,
                idempotency_key=key,
                eur_per_usd=RATE,
                reported_cost_nano=euros * NANO,
                now=when,
            )

        call("under", during, 19)
        assert budgets.check_budgets(store, box, model=MODEL, eur_per_usd=RATE, now=during) is None
        call("over", during, 2)
        refusal = budgets.check_budgets(store, box, model=MODEL, eur_per_usd=RATE, now=during)
        assert isinstance(refusal, budgets.Refusal)
        assert refusal.budget["trial_cap"] is True
        assert refusal.budget["limit_eur"] == "20"

        window = {"since": T0, "until": during + timedelta(days=1)}
        owed = store.usage_summary("ada", **window, billed_only=True)[0]
        shown = store.usage_summary("ada", **window)[0]
        assert owed.eur_nano == 0, "a trial's model use is not what an invoice sums"
        assert shown.eur_nano == 21 * NANO

        after = T0 + timedelta(days=31)
        assert (
            budgets.check_budgets(store, box, model=MODEL, eur_per_usd=RATE, now=after) is None
        ), "the trial cap does not keep pausing once the trial has ended"
        call("later", after, 5)
        billed = store.usage_summary(
            "ada", since=after, until=after + timedelta(days=1), billed_only=True
        )[0]
        assert billed.eur_nano == 5 * NANO


def test_signup_completed_carries_the_plan_and_the_campaign(tmp_path, recorded):
    with FleetStore(tmp_path / "fleet.db") as store:
        secret = store.create_login_link(
            "pat@example.com",
            purpose="sign_up",
            org_id="pat",
            org_name="Pat",
            attribution=Attribution(
                plan="founding",
                interval="month",
                utm_source="flotta.dev",
                utm_medium="cpc",
                utm_campaign="founding",
                visitor_id=VISITOR,
            ),
        )
        created = store.sign_up_from_link(secret)
        assert created is not None
        (event,) = _events(recorded, "signup_completed")
        assert event["distinct_id"] == "pat@example.com"
        assert event["properties"]["org_id"] == "pat"
        assert event["properties"]["plan_intent"] == "founding"
        assert event["properties"]["utm_source"] == "flotta.dev"
        assert event["properties"]["utm_medium"] == "cpc"
        assert event["properties"]["utm_campaign"] == "founding"
        assert event["properties"]["$anon_distinct_id"] == VISITOR
        assert event["properties"]["$set"] == {"email": "pat@example.com", "is_test": True}
        # A second sign-up with no campaign still emits, and is not a test account.
        store.sign_up("ada@example.com", "ada", "Ada")
        plain = _events(recorded, "signup_completed")[1]
        assert plain["properties"]["plan_intent"] is None
        assert plain["properties"]["utm_source"] is None
        assert plain["properties"]["$set"]["is_test"] is False
        assert "$anon_distinct_id" not in plain["properties"]


def test_a_test_domain_is_marked_and_a_plus_tag_is_not(monkeypatch):
    monkeypatch.setenv("FLOTTA_TEST_EMAILS", "pat@example.com")
    monkeypatch.setenv("FLOTTA_TEST_EMAIL_DOMAINS", "test.flotta.dev")
    assert analytics.is_test_email("Pat@Example.com") is True
    assert analytics.is_test_email("ada@test.flotta.dev") is True
    assert analytics.is_test_email("pat+trial@example.com") is False
    assert analytics.is_test_email("ada@example.com") is False


def test_agent_created_and_request_filed_name_the_owner(tmp_path, recorded):
    with FleetStore(tmp_path / "fleet.db") as store:
        _founding(store, "ada@test.flotta.dev", "ada")
        store.decide_org("ada", approve=True, now=T0)
        store.create_client("ada", "acme", "Acme")
        box = store.create_box("support", org="ada", client="acme")
        analytics.agent_created(store, box)
        analytics.request_filed(store, box, size="small", auto_started=False)
        (agent,) = _events(recorded, "agent_created")
        (filed,) = _events(recorded, "request_filed")
        assert agent["distinct_id"] == "ada@test.flotta.dev"
        assert agent["properties"]["org_id"] == "ada"
        assert agent["properties"]["box_id"] == box.id
        assert agent["properties"]["$set"]["is_test"] is True
        assert filed["properties"]["size"] == "small"
        assert filed["properties"]["auto_started"] is False


def test_the_price_note_goes_once_from_14_days_before(tmp_path):
    from flotta.founding import send_price_notices

    outbox = Outbox()
    with FleetStore(tmp_path / "fleet.db") as store:
        _founding(store, "ada@example.com", "ada")
        store.decide_org("ada", approve=True, now=T0)
        paid = T0 + timedelta(days=30)
        store.note_founding_paid("ada", now=paid)
        until = store.get_org("ada").price_until
        assert until.startswith("2027-12-02")
        # A second invoice does not move the date.
        store.note_founding_paid("ada", now=paid + timedelta(days=40))
        assert store.get_org("ada").price_until == until
        early = datetime.fromisoformat(until) - timedelta(days=15)
        assert send_price_notices(store, now=early, mailer=outbox) == []
        assert outbox == []
        due = datetime.fromisoformat(until) - timedelta(days=14)
        assert send_price_notices(store, now=due, mailer=outbox) == ["ada"]
        assert send_price_notices(store, now=due, mailer=outbox) == []
    (message,) = outbox
    assert message.to == "ada@example.com"
    assert "EUR 149" in message.text
    assert "\u2014" not in message.text and "\u2013" not in message.text
    assert "ends on 2027-12-02" in message.text


def test_a_post_failure_does_not_raise(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("FLOTTA_POSTHOG_KEY", "phc_test")

    def boom(*_args, **_kwargs):
        raise OSError("down")

    monkeypatch.setattr(analytics.urllib.request, "urlopen", boom)
    analytics.capture("ada@example.com", "signup_completed", {"org_id": "ada"})


def test_a_configured_key_posts_to_the_eu_project(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("FLOTTA_POSTHOG_KEY", "phc_test")
    seen = {}

    def fake(request, timeout=0):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        seen["timeout"] = timeout

        class _Response:
            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        return _Response()

    monkeypatch.setattr(analytics.urllib.request, "urlopen", fake)
    analytics.capture("ada@example.com", "signup_completed", {"org_id": "ada"})
    assert seen["url"] == "https://eu.i.posthog.com/capture/"
    assert seen["timeout"] == 2
    assert seen["body"]["api_key"] == "phc_test"
    assert seen["body"]["distinct_id"] == "ada@example.com"
    assert seen["body"]["event"] == "signup_completed"


def test_founding_is_monthly_only():
    with pytest.raises(ValueError, match="monthly"):
        parse_attribution({"plan": "founding", "interval": "year"})
    parsed = parse_attribution(
        {"plan": "founding", "utm_source": "site", "visitor": VISITOR.upper()}
    )
    assert (parsed.plan, parsed.interval, parsed.visitor_id) == ("founding", "month", VISITOR)


# -- the routes ------------------------------------------------------------------


@pytest.fixture
def fleet(tmp_path):
    return tmp_path / "fleet.db"


@pytest.fixture
def outbox():
    return Outbox()


@pytest.fixture
def api(fleet, outbox):
    with TestClient(_app(fleet, mailer=outbox)) as client:
        yield client


def _governor():
    token = mint(subject="billing", scopes={SCOPE_ORG_GOVERN}, key=KEY)
    return {"Authorization": f"Bearer {token}"}


def test_the_sign_up_route_keeps_the_campaign_and_stops_at_five(api, fleet, outbox, recorded):
    body = {
        "email": "pat@example.com",
        "org": "pat",
        "org_name": "Pat Studio",
        "plan": "founding",
        "interval": "month",
        "utm_source": "flotta.dev",
        "utm_medium": "cpc",
        "utm_campaign": "founding",
        "visitor": VISITOR,
    }
    sent = api.post("/api/auth/sign-up", json=body)
    assert sent.status_code == 202, sent.text
    message = outbox[-1]
    assert "Pat Studio" not in message.text
    assert "flotta.dev" not in message.text
    assert "trial" in message.text
    assert "\u2014" not in message.text and "\u2013" not in message.text
    redeemed = api.post("/api/auth/redeem", json={"secret": outbox.secret()})
    assert redeemed.status_code == 200, redeemed.text
    (event,) = _events(recorded, "signup_completed")
    assert event["properties"]["plan_intent"] == "founding"
    assert event["properties"]["utm_campaign"] == "founding"
    with FleetStore(fleet) as store:
        org = store.get_org("pat")
        assert (org.max_clients, org.max_seats, org.max_agents) == (5, 1, 15)
        for index in range(FOUNDING_CAP - 1):
            _founding(store, f"o{index}@example.com", f"org-{index}")
    year = api.post("/api/auth/sign-up", json={**body, "org": "other", "interval": "year"})
    assert year.status_code == 422, year.text
    full = api.post(
        "/api/auth/sign-up",
        json={**body, "email": "more@example.com", "org": "more", "org_name": "More"},
    )
    assert full.status_code == 409, full.text
    assert full.json()["detail"] == "Founding Studio is full"


def test_billing_records_checkout_and_the_first_paid_invoice(api, fleet, recorded):
    with FleetStore(fleet) as store:
        _founding(store, "ada@example.com", "ada")
        store.decide_org("ada", approve=True, now=T0)
    started = api.post(
        "/api/orgs/ada/funnel",
        headers=_governor(),
        json={"event": "checkout_started", "plan": "founding", "interval": "month"},
    )
    assert started.status_code == 200, started.text
    paid = api.post(
        "/api/orgs/ada/funnel",
        headers=_governor(),
        json={
            "event": "subscription_started",
            "plan": "founding",
            "interval": "month",
            "amount_eur": "149.00",
        },
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["price_until"]
    again = api.post(
        "/api/orgs/ada/funnel",
        headers=_governor(),
        json={
            "event": "subscription_started",
            "plan": "founding",
            "interval": "month",
            "amount_eur": 149,
        },
    )
    assert again.status_code == 200, again.text
    assert again.json()["recorded"] is False
    assert again.json()["price_until"] == paid.json()["price_until"]
    (checkout,) = _events(recorded, "checkout_started")
    (subscription,) = _events(recorded, "subscription_started")
    assert checkout["distinct_id"] == "ada@example.com"
    assert checkout["properties"]["plan"] == "founding"
    assert checkout["properties"]["interval"] == "month"
    assert subscription["properties"]["amount_eur"] == 149
    owner = mint(subject="ada", scopes={"org:admin"}, key=KEY, org="ada")
    denied = api.post(
        "/api/orgs/ada/funnel",
        headers={"Authorization": f"Bearer {owner}"},
        json={"event": "checkout_started", "plan": "founding", "interval": "month"},
    )
    assert denied.status_code == 403


def test_filing_a_request_records_its_size_and_whether_it_starts(fleet, recorded):
    from flotta.control.app import create_app
    from flotta.control.test_app import _uncatalogued, _unchecked

    def judge(_title, _details):
        return Verdict(
            kind="feature",
            complexity=1,
            complexity_p=(0.0, 1.0, 0.0, 0.0, 0.0),
            priority=1,
            priority_p=(0.0, 1.0, 0.0, 0.0),
        )

    with FleetStore(fleet) as store:
        _founding(store, "ada@example.com", "ada")
        store.decide_org("ada", approve=True, now=T0)
        store.create_client("ada", "acme", "Acme")
        box = store.create_box("support", org="ada", client="acme")
        token = mint(
            subject=box_subject(box.id),
            scopes={SCOPE_BOX_PEER},
            key=KEY,
            org="ada",
            client="acme",
            box=box.id,
        )
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
        judge_request=judge,
    )
    with TestClient(app) as client:
        filed = client.post(
            f"/api/boxes/{box.id}/requests",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": "Rename the button", "details": "The one on the homepage."},
        )
    assert filed.status_code == 201, filed.text
    (event,) = _events(recorded, "request_filed")
    assert event["properties"]["size"] == COMPLEXITY_NAMES[1]
    assert event["properties"]["auto_started"] is filed.json()["starts"]
    assert event["distinct_id"] == "ada@example.com"
