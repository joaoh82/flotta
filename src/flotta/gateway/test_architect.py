"""The Architect's own model calls, through the gateway (FLOTTA-250).

A signed-in person of an agency asks the Architect something in the web app;
the web app's server makes the model calls here, with a ticket the control
plane gave it. The fleet pays, up to a monthly allowance per org, and none of
it is in `usage`, which is what an agency's budgets and invoice add up.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from flotta import architect
from flotta.auth import (
    SCOPE_FLEET_READ,
    SCOPE_FLEET_WRITE,
    mint,
    mint_architect_ticket,
    mint_ticket,
)
from flotta.gateway import test_app
from flotta.gateway.fake import FAKE_MODEL, FakeUpstream
from flotta.gateway.test_app import CALL, FLEET_KEY, SIGNING_KEY, _auth, _client, _rows
from flotta.pricing import NANO
from flotta.store import FleetStore

ORG, PERSON = "studio-nord", "u-ana"
URL = "/v1/architect/chat/completions"
ASK = {"messages": [{"role": "user", "content": "What did Acme spend this month?"}]}
ENV = {**test_app.ENV, "FLOTTA_MODEL": FAKE_MODEL}

#: `test_app`'s fleet (an org, a client, two agents, the fake model's price) and its upstream.
path = test_app.path
upstream = test_app.upstream


def _ticket(org=ORG, user=PERSON, **more):
    return {
        "Authorization": "Bearer "
        + mint_architect_ticket(user_id=user, org=org, key=SIGNING_KEY, **more)
    }


def _calls(path):
    with FleetStore(path) as fleet:
        rows = fleet._conn.execute("SELECT * FROM architect_usage ORDER BY at").fetchall()
    return [dict(row) for row in rows]


def _spend(path, org=ORG, *, euros, at=None, priced=True):
    """A call already made this month that cost `euros`."""
    with FleetStore(path) as fleet:
        fleet.record_architect_usage(
            org=org,
            user_id=PERSON,
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


def test_a_call_is_made_on_the_fleets_key_with_the_operators_model(path, upstream):
    with _client(path, upstream, env=ENV) as client:
        answer = client.post(URL, json=ASK, headers=_ticket())

    assert answer.status_code == 200, answer.text
    assert answer.json()["choices"][0]["message"]["role"] == "assistant"
    sent = upstream.requests[0]
    assert sent.headers["authorization"] == f"Bearer {FLEET_KEY}"
    body = json.loads(sent.content)
    assert body["model"] == FAKE_MODEL
    assert body["messages"] == ASK["messages"]
    assert body["max_tokens"] == architect.MAX_OUTPUT


def test_it_is_written_to_the_architects_own_ledger_and_not_the_agencys(path, upstream):
    with _client(path, upstream, env=ENV) as client:
        client.post(URL, json=ASK, headers=_ticket())
        # The same call as an agent makes it, to compare what each is said to cost.
        client.post("/v1/chat/completions", json=CALL, headers=_auth())

    (call,) = _calls(path)
    (agents,) = _rows(path)
    assert (call["org_id"], call["user_id"], call["model"]) == (ORG, PERSON, FAKE_MODEL)
    assert (call["tokens_in"], call["tokens_out"], call["tokens_cached"]) == (1200, 30, 200)
    assert (call["status"], call["estimated"]) == (200, 0)
    # Priced by the same rule as an agent's call, and kept apart from it.
    assert call["cost_eur_nano"] == agents["cost_eur_nano"] > 0
    assert agents["box_id"] == "b-one"
    with FleetStore(path) as fleet:
        spent = fleet.architect_spend(ORG, since=datetime(2026, 1, 1, tzinfo=UTC))
        assert spent.eur_nano == call["cost_eur_nano"] and spent.unpriced == 0
        # An agency's budgets and invoice add up `usage`: this is not in it.
        assert (
            fleet.spend(org=ORG, since=datetime(2026, 1, 1, tzinfo=UTC)).eur_nano
            == (agents["cost_eur_nano"])
        )


def test_the_caller_does_not_choose_the_model_or_how_long_the_answer_is(path, upstream):
    asked = {**ASK, "model": "expensive/model", "max_completion_tokens": 999_999}
    with _client(path, upstream, env={**ENV, "FLOTTA_ARCHITECT_MODEL": FAKE_MODEL}) as client:
        answer = client.post(URL, json=asked, headers=_ticket())

    assert answer.status_code == 200, answer.text
    body = json.loads(upstream.requests[0].content)
    assert body["model"] == FAKE_MODEL
    assert body["max_tokens"] == architect.MAX_OUTPUT
    assert "max_completion_tokens" not in body
    assert _calls(path)[0]["model"] == FAKE_MODEL


def test_a_shorter_answer_may_be_asked_for_and_tools_are_passed_on(path, upstream):
    tools = [{"type": "function", "function": {"name": "list_clients", "parameters": {}}}]
    with _client(path, upstream, env=ENV) as client:
        client.post(URL, json={**ASK, "max_tokens": 200, "tools": tools}, headers=_ticket())

    body = json.loads(upstream.requests[0].content)
    assert (body["max_tokens"], body["tools"]) == (200, tools)


def test_a_streamed_answer_is_relayed_and_counted(path, upstream):
    with _client(path, upstream, env=ENV) as client:
        answer = client.post(URL, json={**ASK, "stream": True}, headers=_ticket())

    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"].startswith("text/event-stream")
    assert "data: [DONE]" in answer.text
    # The usage the gateway asked for on the caller's behalf is not passed on.
    assert '"usage"' not in answer.text
    (call,) = _calls(path)
    assert (call["tokens_in"], call["tokens_out"], call["estimated"]) == (1200, 30, 0)


def test_a_call_the_provider_refused_is_recorded_as_costing_nothing(path):
    failing = FakeUpstream(status=500)
    with _client(path, failing, env=ENV) as client:
        answer = client.post(URL, json=ASK, headers=_ticket())

    assert answer.status_code == 500
    assert FLEET_KEY not in answer.text
    (call,) = _calls(path)
    assert (call["status"], call["tokens_in"], call["cost_eur_nano"]) == (500, 0, 0)


# -- who gets in -------------------------------------------------------------------


def test_only_an_architect_ticket_gets_in(path, upstream):
    agent = _auth()
    person = {
        "Authorization": "Bearer "
        + mint(
            subject="ana", scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE}, key=SIGNING_KEY, org=ORG
        )
    }
    chat = {
        "Authorization": "Bearer "
        + mint_ticket(subject="user:u-ana", box_id="b-one", org=ORG, client="acme", key=SIGNING_KEY)
    }
    forged = {
        "Authorization": "Bearer "
        + mint_architect_ticket(user_id=PERSON, org=ORG, key="another-fleets-signing-key")
    }
    stale = _ticket(now=int((datetime.now(UTC) - timedelta(hours=1)).timestamp()))
    with _client(path, upstream, env=ENV) as client:
        for headers in ({}, agent, person, chat, forged, stale, {"Authorization": "Bearer x"}):
            answer = client.post(URL, json=ASK, headers=headers)
            assert answer.status_code == 401, (headers, answer.text)

    assert upstream.requests == []
    assert _calls(path) == []


def test_a_ticket_opens_no_other_route(path, upstream):
    with _client(path, upstream, env=ENV) as client:
        as_agent = client.post("/v1/chat/completions", json=CALL, headers=_ticket())
        models = client.get("/v1/models", headers=_ticket())

    assert (as_agent.status_code, models.status_code) == (401, 401)
    assert upstream.requests == []
    assert _rows(path) == []


# -- the allowance -----------------------------------------------------------------


def test_an_org_that_used_its_allowance_is_refused_before_anything_is_sent(path, upstream):
    _spend(path, euros="10")

    with _client(path, upstream, env=ENV) as client:
        answer = client.post(URL, json=ASK, headers=_ticket())

    assert answer.status_code == 402, answer.text
    error = answer.json()["error"]
    assert (error["type"], error["code"]) == ("flotta_architect", "architect_allowance_used")
    assert "€10" in error["message"]
    assert error["allowance"]["used_up"] is True
    assert error["allowance"]["spent_eur_nano"] == error["allowance"]["allowance_eur_nano"]
    assert upstream.requests == []
    assert len(_calls(path)) == 1


def test_the_allowance_is_the_operators_to_set_and_each_orgs_own(path, upstream):
    with FleetStore(path) as fleet:
        fleet.create_org("studio-sud", "Studio Sud")
    _spend(path, euros="2.50")
    roomy = {**ENV, "FLOTTA_ARCHITECT_MONTHLY_EUR": "2.51"}
    tight = {**ENV, "FLOTTA_ARCHITECT_MONTHLY_EUR": "2.50"}

    with _client(path, upstream, env=roomy) as client:
        assert client.post(URL, json=ASK, headers=_ticket()).status_code == 200
    with _client(path, upstream, env=tight) as client:
        assert client.post(URL, json=ASK, headers=_ticket()).status_code == 402
        # Another agency's allowance is its own.
        assert client.post(URL, json=ASK, headers=_ticket(org="studio-sud")).status_code == 200


def test_last_months_calls_do_not_count_against_this_months(path, upstream):
    last_month = datetime.now(UTC).replace(day=1) - timedelta(days=1)
    _spend(path, euros="500", at=last_month)

    with _client(path, upstream, env=ENV) as client:
        assert client.post(URL, json=ASK, headers=_ticket()).status_code == 200


def test_a_call_nobody_could_price_leaves_the_allowance_unknown_and_refuses(path, upstream):
    _spend(path, euros="0", priced=False)

    with _client(path, upstream, env=ENV) as client:
        answer = client.post(URL, json=ASK, headers=_ticket())

    assert answer.status_code == 402, answer.text
    assert answer.json()["error"]["code"] == "architect_unpriced"
    assert upstream.requests == []


@pytest.mark.parametrize(
    ("env", "status", "code"),
    [
        ({"FLOTTA_EUR_PER_USD": ""}, 402, "architect_no_rate"),
        ({"FLOTTA_MODEL": ""}, 503, "architect_no_model"),
        ({"FLOTTA_MODEL": "a-model/with-no-price"}, 402, "architect_no_price"),
    ],
)
def test_a_fleet_that_cannot_count_a_call_makes_none(path, upstream, env, status, code):
    with _client(path, upstream, env={**ENV, **env}) as client:
        answer = client.post(URL, json=ASK, headers=_ticket())

    assert (answer.status_code, answer.json()["error"]["code"]) == (status, code), answer.text
    assert upstream.requests == []
    assert _calls(path) == []


@pytest.mark.parametrize("status", ["pending", "suspended", "closed", "declined"])
def test_an_org_that_is_not_active_makes_no_call(path, upstream, status):
    with FleetStore(path) as fleet:
        fleet._conn.execute("UPDATE orgs SET status = ? WHERE id = ?", (status, ORG))

    with _client(path, upstream, env=ENV) as client:
        answer = client.post(URL, json=ASK, headers=_ticket())
        gone = client.post(URL, json=ASK, headers=_ticket(org="no-such-org"))

    assert answer.status_code == 403, answer.text
    assert answer.json()["error"]["code"] == "architect_org_not_active"
    assert gone.status_code == 403
    assert upstream.requests == []


def test_a_check_that_fails_is_a_refusal_never_a_call(path, upstream, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("the store is away")

    monkeypatch.setattr(architect, "admit", broken)
    with _client(path, upstream, env=ENV) as client:
        answer = client.post(URL, json=ASK, headers=_ticket())

    assert answer.status_code == 402, answer.text
    assert answer.json()["error"]["code"] == "architect_unchecked"
    assert upstream.requests == []


def test_a_gateway_with_no_upstream_says_so(path, upstream):
    with _client(path, upstream, env={**ENV, "FLOTTA_API_KEY": ""}) as client:
        answer = client.post(URL, json=ASK, headers=_ticket())

    assert answer.status_code == 503
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
def test_a_call_carries_only_what_this_route_sends(path, upstream, body, says):
    with _client(path, upstream, env=ENV) as client:
        answer = client.post(URL, json=body, headers=_ticket())

    assert answer.status_code == 400, answer.text
    assert says in answer.json()["error"]["message"]
    assert upstream.requests == []


def test_a_body_that_names_a_key_twice_is_refused(path, upstream):
    twice = b'{"messages": [{"role": "user", "content": "a"}], "messages": []}'
    with _client(path, upstream, env=ENV) as client:
        answer = client.post(
            URL, content=twice, headers={**_ticket(), "content-type": "application/json"}
        )

    assert answer.status_code == 400, answer.text
    assert upstream.requests == []
