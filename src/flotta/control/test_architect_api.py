"""The Architect's own model calls (FLOTTA-250).

A signed-in person of an agency asks the Architect something in the web app,
and the web app's server makes the model calls here with their session. The
fleet pays, with a key of its own, up to a monthly allowance per org; none of
it is in `usage`, which is what an agency's budgets and invoice add up.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from flotta import architect
from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN, mint
from flotta.control import test_tickets
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.control.test_sign_in import KEY
from flotta.control.test_tickets import _as
from flotta.gateway.fake import FAKE_MODEL, FAKE_PRICE, FAKE_UPSTREAM_URL, FakeUpstream
from flotta.pricing import NANO
from flotta.store import FleetStore

ORG = "agency-x"
URL = "/api/architect/completions"
ASK = {"messages": [{"role": "user", "content": "What did Acme spend this month?"}]}
ARCHITECT_KEY = "a-provider-key-for-the-architect-alone"
EARLIER = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def fleet(tmp_path):
    """`test_tickets`' two agencies and their people, and a price for the fake model."""
    path = test_tickets.fleet.__wrapped__(tmp_path)
    with FleetStore(path) as store:
        store.refresh_prices([FAKE_PRICE], now=EARLIER)
    return path


@pytest.fixture(autouse=True)
def set_up(monkeypatch):
    """A fleet whose operator has set the Architect up."""
    monkeypatch.setenv(architect.KEY_ENV, ARCHITECT_KEY)
    monkeypatch.setenv("FLOTTA_MODEL_BASE_URL", FAKE_UPSTREAM_URL)
    monkeypatch.setenv("FLOTTA_MODEL", FAKE_MODEL)
    monkeypatch.setenv("FLOTTA_EUR_PER_USD", "0.5")


@pytest.fixture
def upstream():
    return FakeUpstream()


def _api(fleet, upstream):
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
        architect_transport=upstream,
    )
    return TestClient(app)


def _owner(fleet):
    return _as(fleet, "ana@example.com")


def _user_id(fleet, email="ana@example.com"):
    with FleetStore(fleet) as store:
        return store.get_user_by_email(email).id


def _calls(fleet):
    with FleetStore(fleet) as store:
        rows = store._conn.execute("SELECT * FROM architect_usage ORDER BY at").fetchall()
    return [dict(row) for row in rows]


def _agents_usage(fleet):
    with FleetStore(fleet) as store:
        return store._conn.execute("SELECT COUNT(*) AS n FROM usage").fetchone()["n"]


def _spend(fleet, org=ORG, *, euros, at=None, priced=True):
    """A call already made that cost `euros`."""
    with FleetStore(fleet) as store:
        store.record_architect_usage(
            org=org,
            user_id="u-someone",
            model=FAKE_MODEL if priced else "nobody/priced-this",
            upstream="custom",
            tokens_in=10,
            tokens_out=10,
            idempotency_key=f"seed-{org}-{euros}-{at}-{priced}",
            eur_per_usd=Decimal("1"),
            reported_cost_nano=int(Decimal(str(euros)) * NANO) if priced else None,
            now=at,
        )


# -- a call goes out, and is counted -------------------------------------------------


def test_a_call_is_made_with_the_architects_own_key_and_the_operators_model(fleet, upstream):
    with _api(fleet, upstream) as api:
        answer = api.post(URL, json=ASK, headers=_owner(fleet))

    assert answer.status_code == 200, answer.text
    assert answer.json()["choices"][0]["message"]["role"] == "assistant"
    sent = upstream.requests[0]
    assert str(sent.url) == f"{FAKE_UPSTREAM_URL}/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {ARCHITECT_KEY}"
    body = json.loads(sent.content)
    assert body["model"] == FAKE_MODEL
    assert body["messages"] == ASK["messages"]
    assert body["max_tokens"] == architect.MAX_OUTPUT


def test_the_persons_session_never_reaches_the_provider(fleet, upstream):
    headers = _owner(fleet)
    with _api(fleet, upstream) as api:
        api.post(URL, json=ASK, headers={**headers, "cookie": "flotta_session=abc"})

    sent = json.dumps(dict(upstream.requests[0].headers))
    assert headers["Authorization"].split()[1] not in sent
    assert "flotta_session" not in sent


def test_it_is_written_to_the_architects_own_ledger_and_not_the_agencys(fleet, upstream):
    with _api(fleet, upstream) as api:
        api.post(URL, json=ASK, headers=_owner(fleet))

    (call,) = _calls(fleet)
    assert (call["org_id"], call["user_id"]) == (ORG, _user_id(fleet))
    assert (call["model"], call["upstream"]) == (FAKE_MODEL, "custom")
    assert (call["tokens_in"], call["tokens_out"], call["tokens_cached"]) == (1200, 30, 200)
    assert (call["status"], call["estimated"]) == (200, 0)
    # The fake model's price, at the rate in force: priced as an agent's call is.
    with FleetStore(fleet) as store:
        from flotta.pricing import cost_nano, to_eur_nano

        price = store.price_of(FAKE_MODEL, at=datetime.now(UTC))
        cost = cost_nano(price, tokens_in=1200, tokens_out=30, tokens_cached=200)
        assert call["cost_eur_nano"] == to_eur_nano(cost, price.currency, Decimal("0.5")) > 0
        spent = store.architect_spend(ORG, since=EARLIER)
        assert (spent.eur_nano, spent.unpriced) == (call["cost_eur_nano"], 0)
        # An agency's budgets and invoice add up `usage`: this is not in it.
        assert store.spend(org=ORG, since=EARLIER).eur_nano == 0
    assert _agents_usage(fleet) == 0


def test_the_caller_does_not_choose_the_model_or_how_long_the_answer_is(
    fleet, upstream, monkeypatch
):
    monkeypatch.setenv("FLOTTA_MODEL", "some/other-model")
    monkeypatch.setenv("FLOTTA_ARCHITECT_MODEL", FAKE_MODEL)
    asked = {**ASK, "model": "expensive/model", "max_completion_tokens": 999_999}
    with _api(fleet, upstream) as api:
        answer = api.post(URL, json=asked, headers=_owner(fleet))

    assert answer.status_code == 200, answer.text
    body = json.loads(upstream.requests[0].content)
    assert body["model"] == FAKE_MODEL
    assert body["max_tokens"] == architect.MAX_OUTPUT
    assert "max_completion_tokens" not in body
    assert _calls(fleet)[0]["model"] == FAKE_MODEL


def test_a_shorter_answer_may_be_asked_for_and_tools_are_passed_on(fleet, upstream):
    tools = [{"type": "function", "function": {"name": "list_clients", "parameters": {}}}]
    with _api(fleet, upstream) as api:
        api.post(URL, json={**ASK, "max_tokens": 200, "tools": tools}, headers=_owner(fleet))

    body = json.loads(upstream.requests[0].content)
    assert (body["max_tokens"], body["tools"]) == (200, tools)


def test_a_streamed_answer_is_relayed_and_counted(fleet, upstream):
    with _api(fleet, upstream) as api:
        answer = api.post(URL, json={**ASK, "stream": True}, headers=_owner(fleet))

    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"].startswith("text/event-stream")
    assert "data: [DONE]" in answer.text
    # The usage asked for on the caller's behalf is not passed on.
    assert '"usage"' not in answer.text
    (call,) = _calls(fleet)
    assert (call["tokens_in"], call["tokens_out"], call["estimated"]) == (1200, 30, 0)


def test_a_call_the_provider_refused_is_recorded_as_costing_nothing(fleet):
    failing = FakeUpstream(status=500)
    with _api(fleet, failing) as api:
        answer = api.post(URL, json=ASK, headers=_owner(fleet))

    assert answer.status_code == 500
    assert ARCHITECT_KEY not in answer.text
    (call,) = _calls(fleet)
    assert (call["status"], call["tokens_in"], call["cost_eur_nano"]) == (500, 0, 0)


def test_a_provider_that_cannot_be_reached_is_said_and_costs_nothing(fleet):
    with _api(fleet, FakeUpstream(unreachable=True)) as api:
        answer = api.post(URL, json=ASK, headers=_owner(fleet))

    assert answer.status_code == 502
    (call,) = _calls(fleet)
    assert (call["status"], call["cost_eur_nano"]) == (502, 0)


# -- who may ask -------------------------------------------------------------------


def test_a_builder_may_ask_and_a_clients_people_may_not(fleet, upstream):
    with _api(fleet, upstream) as api:
        assert api.post(URL, json=ASK, headers=_as(fleet, "mo@example.com")).status_code == 200
        for email in ("vi@example.com", "cu@example.com"):
            refused = api.post(URL, json=ASK, headers=_as(fleet, email))
            assert refused.status_code == 403, (email, refused.text)
        assert api.post(URL, json=ASK).status_code == 401

    assert len(upstream.requests) == 1
    assert [c["user_id"] for c in _calls(fleet)] == [_user_id(fleet, "mo@example.com")]


def test_a_token_is_not_a_person_and_makes_no_call(fleet, upstream):
    """An owner's token for a script: the allowance is an agency's people's."""
    token = mint(
        subject="a-script",
        scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN},
        key=KEY,
        org=ORG,
    )
    with _api(fleet, upstream) as api:
        answer = api.post(URL, json=ASK, headers={"Authorization": f"Bearer {token}"})

    assert answer.status_code == 403, answer.text
    assert "signed-in person" in answer.json()["detail"]
    assert upstream.requests == []


def test_each_agencys_calls_are_its_own(fleet, upstream):
    with FleetStore(fleet) as store:
        user = store.create_user("yan@example.com")
        store.add_membership(user.id, "agency-y", "owner")
    with _api(fleet, upstream) as api:
        api.post(URL, json=ASK, headers=_owner(fleet))
        api.post(URL, json=ASK, headers=_as(fleet, "yan@example.com", org="agency-y"))

    assert [c["org_id"] for c in _calls(fleet)] == ["agency-x", "agency-y"]


# -- the allowance -----------------------------------------------------------------


def test_an_org_that_used_its_allowance_is_refused_before_anything_is_sent(fleet, upstream):
    _spend(fleet, euros="10")

    with _api(fleet, upstream) as api:
        answer = api.post(URL, json=ASK, headers=_owner(fleet))

    assert answer.status_code == 402, answer.text
    body = answer.json()
    assert body["code"] == "architect_allowance_used"
    assert "€10" in body["detail"]
    assert body["allowance"]["used_up"] is True
    assert body["allowance"]["spent_eur_nano"] == body["allowance"]["allowance_eur_nano"]
    assert upstream.requests == []
    assert len(_calls(fleet)) == 1


def test_the_allowance_is_the_operators_to_set_and_each_orgs_own(fleet, upstream, monkeypatch):
    with FleetStore(fleet) as store:
        user = store.create_user("yan@example.com")
        store.add_membership(user.id, "agency-y", "owner")
    _spend(fleet, euros="2.50")

    monkeypatch.setenv("FLOTTA_ARCHITECT_MONTHLY_EUR", "2.51")
    with _api(fleet, upstream) as api:
        assert api.post(URL, json=ASK, headers=_owner(fleet)).status_code == 200
    monkeypatch.setenv("FLOTTA_ARCHITECT_MONTHLY_EUR", "2.50")
    with _api(fleet, upstream) as api:
        assert api.post(URL, json=ASK, headers=_owner(fleet)).status_code == 402
        # Another agency's allowance is its own.
        theirs = _as(fleet, "yan@example.com", org="agency-y")
        assert api.post(URL, json=ASK, headers=theirs).status_code == 200


def test_last_months_calls_do_not_count_against_this_months(fleet, upstream):
    last_month = datetime.now(UTC).replace(day=1) - timedelta(days=1)
    _spend(fleet, euros="500", at=last_month)

    with _api(fleet, upstream) as api:
        assert api.post(URL, json=ASK, headers=_owner(fleet)).status_code == 200


def test_a_call_nobody_could_price_leaves_the_allowance_unknown_and_refuses(fleet, upstream):
    _spend(fleet, euros="0", priced=False)

    with _api(fleet, upstream) as api:
        answer = api.post(URL, json=ASK, headers=_owner(fleet))

    assert (answer.status_code, answer.json()["code"]) == (402, "architect_unpriced"), answer.text
    assert upstream.requests == []


@pytest.mark.parametrize(
    ("env", "status", "code"),
    [
        ({"FLOTTA_EUR_PER_USD": ""}, 402, "architect_no_rate"),
        ({"FLOTTA_MODEL": ""}, 503, "architect_no_model"),
        ({"FLOTTA_MODEL": "a-model/with-no-price"}, 402, "architect_no_price"),
    ],
)
def test_a_fleet_that_cannot_count_a_call_makes_none(
    fleet, upstream, monkeypatch, env, status, code
):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with _api(fleet, upstream) as api:
        answer = api.post(URL, json=ASK, headers=_owner(fleet))

    assert (answer.status_code, answer.json()["code"]) == (status, code), answer.text
    assert upstream.requests == []
    assert _calls(fleet) == []


@pytest.mark.parametrize("missing", [architect.KEY_ENV, "FLOTTA_MODEL_BASE_URL"])
def test_a_fleet_whose_operator_has_not_set_it_up_says_so(fleet, upstream, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with _api(fleet, upstream) as api:
        answer = api.post(URL, json=ASK, headers=_owner(fleet))

    assert answer.status_code == 503, answer.text
    assert "not set the Architect up" in answer.json()["detail"]
    assert upstream.requests == []


def test_the_fleets_own_provider_key_is_not_the_architects(fleet, upstream, monkeypatch):
    """The key agents' calls are made with is the gateway's. This route never uses it."""
    monkeypatch.delenv(architect.KEY_ENV)
    monkeypatch.setenv("FLOTTA_API_KEY", "the-fleets-key-for-agents")
    with _api(fleet, upstream) as api:
        assert api.post(URL, json=ASK, headers=_owner(fleet)).status_code == 503

    assert upstream.requests == []


@pytest.mark.parametrize("status", ["pending", "suspended", "closed", "declined"])
def test_an_org_that_is_not_active_makes_no_call(fleet, upstream, status):
    # Signed in while the agency was active; a session is not made for one that is closed.
    headers = _owner(fleet)
    with FleetStore(fleet) as store:
        store._conn.execute("UPDATE orgs SET status = ? WHERE id = ?", (status, ORG))

    with _api(fleet, upstream) as api:
        answer = api.post(URL, json=ASK, headers=headers)

    # Refused somewhere on the way in, by whichever guard meets it first.
    assert answer.status_code in (401, 402, 403), answer.text
    assert upstream.requests == []
    assert _calls(fleet) == []


def test_the_allowance_check_itself_refuses_an_org_that_is_not_active(fleet):
    """Whatever lets a request in, `admit` is asked last and says no."""
    now = datetime.now(UTC)
    for status in ("pending", "suspended", "closed", "declined"):
        with FleetStore(fleet) as store:
            store._conn.execute("UPDATE orgs SET status = ? WHERE id = ?", (status, ORG))
            verdict = architect.admit(store, ORG, now=now, cost_is_reported=False)
            nobody = architect.admit(store, "no-such-org", now=now, cost_is_reported=False)
        assert isinstance(verdict, architect.Refusal), status
        assert (verdict.status, verdict.code) == (403, "architect_org_not_active"), status
        assert isinstance(nobody, architect.Refusal) and nobody.status == 403


def test_a_check_that_fails_is_a_refusal_never_a_call(fleet, upstream, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("the store is away")

    monkeypatch.setattr(architect, "admit", broken)
    with _api(fleet, upstream) as api:
        answer = api.post(URL, json=ASK, headers=_owner(fleet))

    assert (answer.status_code, answer.json()["code"]) == (402, "architect_unchecked")
    assert upstream.requests == []


# -- what a call may carry ---------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ([], "expected a JSON object"),
        ({}, "needs the conversation's messages"),
        ({"messages": []}, "needs the conversation's messages"),
        ({**ASK, "plugins": [{"id": "web"}]}, "does not send 'plugins'"),
        ({**ASK, "models": ["a", "b"]}, "does not send 'models'"),
        ({**ASK, "user": "someone"}, "does not send 'user'"),
        ({**ASK, "max_tokens": 0}, "max_tokens is a whole number"),
        ({**ASK, "max_tokens": True}, "max_tokens is a whole number"),
        ({**ASK, "tools": [{"type": "openrouter:web_search"}]}, "adds nothing to a call"),
    ],
)
def test_a_call_carries_only_what_this_route_sends(fleet, upstream, body, says):
    with _api(fleet, upstream) as api:
        answer = api.post(URL, json=body, headers=_owner(fleet))

    assert answer.status_code == 400, answer.text
    assert says in answer.json()["detail"]
    assert upstream.requests == []


def test_a_body_that_names_a_key_twice_is_refused(fleet, upstream):
    twice = b'{"messages": [{"role": "user", "content": "a"}], "messages": []}'
    with _api(fleet, upstream) as api:
        answer = api.post(
            URL, content=twice, headers={**_owner(fleet), "content-type": "application/json"}
        )

    assert answer.status_code == 400, answer.text
    assert upstream.requests == []


# -- where the allowance stands, and what it costs the operator ------------------------


def test_an_agency_is_told_where_its_allowance_stands(fleet, upstream):
    _spend(fleet, euros="2.50")
    _spend(fleet, org="agency-y", euros="9")

    with _api(fleet, upstream) as api:
        answer = api.get("/api/architect/allowance", headers=_owner(fleet))

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["org"], body["used_up"], body["unpriced"]) == (ORG, False, 0)
    assert (body["allowance_eur_nano"], body["spent_eur_nano"]) == (10 * NANO, 2_500_000_000)
    now = datetime.now(UTC)
    assert body["since"].startswith(f"{now.year:04d}-{now.month:02d}-01T00:00:00")
    assert datetime.fromisoformat(body["resets_at"]) > now


def test_a_clients_people_are_not_told_what_the_architect_costs(fleet, upstream):
    with _api(fleet, upstream) as api:
        for email in ("vi@example.com", "cu@example.com"):
            refused = api.get("/api/architect/allowance", headers=_as(fleet, email))
            assert refused.status_code == 403, (email, refused.text)
        assert api.get("/api/architect/allowance").status_code == 401
        assert (
            api.get("/api/architect/allowance", headers=_as(fleet, "mo@example.com")).status_code
            == 200
        )


def test_the_operator_reads_what_every_agencys_architect_cost_this_month(fleet, upstream):
    _spend(fleet, euros="2.50")
    _spend(fleet, euros="1")
    _spend(fleet, org="agency-y", euros="9")
    _spend(fleet, org="agency-y", euros="500", at=datetime.now(UTC).replace(day=1) - timedelta(1))
    operator = {
        "Authorization": "Bearer "
        + mint(subject="josh", scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE}, key=KEY)
    }

    with _api(fleet, upstream) as api:
        answer = api.get("/api/architect/spend", headers=operator)
        refused = api.get("/api/architect/spend", headers=_owner(fleet))

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["allowance_eur_nano"], body["model"]) == (10 * NANO, FAKE_MODEL)
    assert body["orgs"] == [
        {"org": "agency-x", "spent_eur_nano": 3_500_000_000, "unpriced": 0, "calls": 2},
        {"org": "agency-y", "spent_eur_nano": 9_000_000_000, "unpriced": 0, "calls": 1},
    ]
    assert refused.status_code == 403


def test_a_closed_orgs_calls_go_when_it_is_purged(fleet):
    _spend(fleet, euros="1")
    _spend(fleet, org="agency-y", euros="1")
    with FleetStore(fleet) as store:
        for box in store.list_boxes_in("agency-x", None):
            store.update_box_status(box.id, "torn_down")
        store.close_org("agency-x", by="operator")
        gone = store.purge_org("agency-x")

    assert gone.get("architect_usage") == 1, gone
    assert [c["org_id"] for c in _calls(fleet)] == ["agency-y"]
