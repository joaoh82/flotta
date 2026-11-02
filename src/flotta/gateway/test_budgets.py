"""Budgets, checked in the gateway before every call (FLOTTA-89, M13.3).

The first tests are the guards. **A budget check that fails open is the bug this
milestone exists not to have:** anything that stops the check from answering has
to end in a refusal, with the upstream never called.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from flotta import budgets as budgets_module
from flotta.auth import SCOPE_MODEL_CALL, box_subject, mint
from flotta.gateway.app import create_gateway
from flotta.gateway.fake import FAKE_MODEL, FAKE_PRICE, FAKE_UPSTREAM_URL, FakeUpstream
from flotta.pricing import NANO
from flotta.store import FleetStore

SIGNING_KEY = "a-signing-key-for-these-tests"
ENV = {
    "FLOTTA_SIGNING_KEY": SIGNING_KEY,
    "FLOTTA_API_KEY": "fleet-provider-key",
    "FLOTTA_MODEL_BASE_URL": FAKE_UPSTREAM_URL,
    "FLOTTA_EUR_PER_USD": "0.5",
}
EARLIER = datetime(2026, 1, 1, tzinfo=UTC)
CALL = {"model": FAKE_MODEL, "messages": [{"role": "user", "content": "hello"}]}
#: What one call costs the fake: 1000 in at $2, 200 cached at $1, 30 out at $8,
#: per million, at 0.5 euros to the dollar.
ONE_CALL = 1_220_000


@pytest.fixture
def path(tmp_path):
    db = str(tmp_path / "fleet.db")
    with FleetStore(db) as fleet:
        fleet.create_org("studio-nord", "Studio Nord")
        fleet.create_client("studio-nord", "acme", "Acme Dental")
        fleet.create_client("studio-nord", "bravo", "Bravo Legal")
        fleet.create_box("one", box_id="b-one", org="studio-nord", client="acme")
        fleet.create_box("two", box_id="b-two", org="studio-nord", client="acme")
        fleet.create_box("three", box_id="b-three", org="studio-nord", client="bravo")
        fleet.create_org("other", "Other Agency")
        fleet.create_client("other", "acme", "Another Acme")
        fleet.create_box("four", box_id="b-four", org="other", client="acme")
        fleet.refresh_prices([FAKE_PRICE], now=EARLIER)
    return db


@pytest.fixture
def upstream():
    return FakeUpstream()


def _auth(box="b-one", org="studio-nord", client="acme"):
    token = mint(
        subject=box_subject(box), scopes={SCOPE_MODEL_CALL}, key=SIGNING_KEY, org=org, client=client
    )
    return {"Authorization": f"Bearer {token}"}


def _client(path, upstream, env=ENV):
    app = create_gateway(store_factory=lambda: FleetStore(path), transport=upstream, env=env)
    return TestClient(app)


def _budget(path, *, org="studio-nord", client=None, box=None, nano=NANO, period="day"):
    with FleetStore(path) as fleet:
        return fleet.set_budget(
            org, client=client, box=box, period=period, limit_eur_nano=nano, action="pause"
        )


def _call(http, **kwargs):
    return http.post("/v1/chat/completions", json=CALL, headers=_auth(**kwargs))


def _events(path, box="b-one"):
    with FleetStore(path) as fleet:
        return [e for e in fleet.get_events("box", box) if e.type == "budget_refused"]


# -- the guards ------------------------------------------------------------------


@pytest.mark.parametrize("name", ["budgets_covering", "spend", "price_of"])
def test_a_check_that_raises_refuses_the_call_and_never_forwards_it(
    path, upstream, monkeypatch, name
):
    """The guard. Reintroducing a `try/except: forward` fails this."""
    _budget(path)

    def broken(self, *args, **kwargs):
        raise RuntimeError("the secret reason")

    monkeypatch.setattr(FleetStore, name, broken)
    with _client(path, upstream) as client:
        response = _call(client)
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "budget_unchecked"
    assert "could not be checked" in response.json()["error"]["message"]
    assert "secret" not in response.text
    assert upstream.requests == []


def test_a_store_that_cannot_be_opened_refuses_with_a_402(upstream):
    def down():
        raise OSError("the database is away")

    app = create_gateway(store_factory=down, transport=upstream, env=ENV)
    with TestClient(app) as client:
        response = _call(client)
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "budget_unchecked"
    assert upstream.requests == []


def test_a_store_that_fails_only_for_the_agent_lookup_refuses(path, upstream, monkeypatch):
    """With the budgets readable, the check would pass on whatever box it was given."""

    def broken(self, *args, **kwargs):
        raise OSError("the database blinked")

    monkeypatch.setattr(FleetStore, "get_box", broken)
    with _client(path, upstream) as client:
        response = _call(client)
    assert response.status_code == 402
    assert upstream.requests == []


def test_a_store_that_fails_only_for_the_check_refuses(path, upstream, monkeypatch):
    """The box can be read and the budgets cannot: a real partial outage."""
    _budget(path)

    def broken(*args, **kwargs):
        raise OSError("no such table")

    monkeypatch.setattr(budgets_module, "check_budgets", broken)
    with _client(path, upstream) as client:
        assert _call(client).status_code == 402
    assert upstream.requests == []


def test_a_failed_check_leaves_an_event_when_it_can(path, upstream, monkeypatch):
    _budget(path)

    def broken(*args, **kwargs):
        raise OSError("no such table")

    monkeypatch.setattr(budgets_module, "check_budgets", broken)
    with _client(path, upstream) as client:
        _call(client)
    assert _events(path)[0].payload["code"] == "budget_unchecked"


def test_spend_exactly_at_the_limit_is_refused(path, upstream):
    _budget(path, box="b-one", nano=ONE_CALL)
    with _client(path, upstream) as client:
        assert _call(client).status_code == 200
        assert _call(client).status_code == 402
    assert len(upstream.requests) == 1


def test_spend_just_under_the_limit_is_allowed(path, upstream):
    _budget(path, box="b-one", nano=ONE_CALL + 1)
    with _client(path, upstream) as client:
        assert _call(client).status_code == 200
        assert _call(client).status_code == 200
        assert _call(client).status_code == 402


def test_another_orgs_budget_is_never_consulted(path, upstream):
    """The same client id, in another org, with a budget already used up."""
    _budget(path, org="other", nano=1)
    _budget(path, org="other", client="acme", nano=1)
    with _client(path, upstream) as client:
        _call(client, box="b-four", org="other")
        assert _call(client).status_code == 200


def test_another_clients_spend_does_not_count_against_a_client_budget(path, upstream):
    _budget(path, client="acme", nano=ONE_CALL + 1)
    with _client(path, upstream) as client:
        _call(client, box="b-three", client="bravo")
        _call(client, box="b-three", client="bravo")
        assert _call(client).status_code == 200


def test_another_agents_spend_does_not_count_against_an_agents_budget(path, upstream):
    _budget(path, box="b-one", nano=ONE_CALL + 1)
    with _client(path, upstream) as client:
        _call(client, box="b-two")
        _call(client, box="b-two")
        assert _call(client).status_code == 200


def test_every_agents_spend_counts_against_their_clients_budget(path, upstream):
    _budget(path, client="acme", nano=ONE_CALL + 1)
    with _client(path, upstream) as client:
        assert _call(client, box="b-two").status_code == 200
        assert _call(client).status_code == 200
        assert _call(client, box="b-two").status_code == 402


def test_another_orgs_spend_does_not_count(path, upstream):
    _budget(path, client="acme", nano=ONE_CALL + 1)
    with _client(path, upstream) as client:
        _call(client, box="b-four", org="other")
        _call(client, box="b-four", org="other")
        assert _call(client).status_code == 200


# -- which budgets cover a box ---------------------------------------------------


def test_an_org_budget_covers_every_client(path, upstream):
    _budget(path, nano=ONE_CALL)
    with _client(path, upstream) as client:
        assert _call(client).status_code == 200
        assert _call(client, box="b-three", client="bravo").status_code == 402


def test_a_client_budget_covers_its_agents_and_no_other_clients(path, upstream):
    _budget(path, client="acme", nano=ONE_CALL)
    with _client(path, upstream) as client:
        assert _call(client).status_code == 200
        assert _call(client, box="b-two").status_code == 402
        assert _call(client, box="b-three", client="bravo").status_code == 200


def test_an_agents_budget_covers_only_that_agent(path, upstream):
    _budget(path, box="b-one", nano=ONE_CALL)
    with _client(path, upstream) as client:
        assert _call(client).status_code == 200
        assert _call(client).status_code == 402
        assert _call(client, box="b-two").status_code == 200


def test_the_tightest_of_several_budgets_decides(path, upstream):
    _budget(path, nano=100 * NANO)
    _budget(path, client="acme", nano=ONE_CALL)
    _budget(path, box="b-one", nano=100 * NANO)
    with _client(path, upstream) as client:
        assert _call(client).status_code == 200
        response = _call(client)
    assert response.status_code == 402
    assert response.json()["error"]["budget"]["scope"] == "client"


def test_no_budget_means_no_check_of_the_price_either(path, upstream):
    """A fleet with no budgets runs as before, including on an unpriced model."""
    with _client(path, upstream) as client:
        response = client.post(
            "/v1/chat/completions", json={**CALL, "model": "nobody/knows"}, headers=_auth()
        )
    assert response.status_code == 200


def test_a_day_budget_does_not_count_yesterday(path, upstream):
    _budget(path, box="b-one", nano=ONE_CALL)
    with FleetStore(path) as fleet:
        fleet.record_usage(
            "b-one",
            model=FAKE_MODEL,
            upstream="custom",
            tokens_in=1200,
            tokens_cached=200,
            tokens_out=30,
            idempotency_key="old",
            eur_per_usd=None,
            now=EARLIER,
        )
    with _client(path, upstream) as client:
        assert _call(client).status_code == 200


# -- what cannot be priced -------------------------------------------------------


def test_a_model_with_no_price_is_refused_when_a_budget_covers_the_box(path, upstream):
    _budget(path)
    with _client(path, upstream) as client:
        response = client.post(
            "/v1/chat/completions", json={**CALL, "model": "nobody/knows"}, headers=_auth()
        )
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "budget_no_price"
    assert "nobody/knows" in response.json()["error"]["message"]
    assert upstream.requests == []


def test_no_exchange_rate_is_refused_when_a_budget_covers_the_box(path, upstream):
    _budget(path)
    env = {k: v for k, v in ENV.items() if k != "FLOTTA_EUR_PER_USD"}
    with _client(path, upstream, env=env) as client:
        response = _call(client)
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "budget_no_rate"
    assert upstream.requests == []


def test_an_unpriced_call_this_period_stops_the_budget_trusting_its_sum(path, upstream):
    _budget(path, client="acme")
    with FleetStore(path) as fleet:
        fleet.record_usage(
            "b-two",
            model="nobody/knows",
            upstream="custom",
            tokens_in=10,
            tokens_out=10,
            idempotency_key="unpriced",
            eur_per_usd=None,
        )
    with _client(path, upstream) as client:
        response = _call(client)
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "budget_unpriced"
    assert upstream.requests == []


# -- what the box is told --------------------------------------------------------


def test_the_refusal_names_the_budget_its_spend_and_its_limit(path, upstream):
    _budget(path, client="acme", nano=NANO // 100_000)
    with _client(path, upstream) as client:
        _call(client)
        response = _call(client)
    assert response.status_code == 402
    error = response.json()["error"]
    assert error["type"] == "flotta_budget"
    assert error["code"] == "budget_exceeded"
    assert error["budget"]["scope"] == "client"
    assert error["budget"]["client"] == "acme"
    assert error["budget"]["period"] == "day"
    assert error["budget"]["limit_eur"] == "0.00001"
    assert error["budget"]["spent_eur"] == "0.00122"
    assert error["budget"]["resets_at"].endswith("00:00:00+00:00")
    assert "0.00001" in error["message"]
    assert "0.00122" in error["message"]


def test_a_refused_call_writes_no_usage(path, upstream):
    _budget(path, box="b-one", nano=ONE_CALL)
    with _client(path, upstream) as client:
        _call(client)
        _call(client)
    with FleetStore(path) as fleet:
        rows = fleet._conn.execute("SELECT COUNT(*) AS n FROM usage").fetchone()
    assert rows["n"] == 1


def test_a_refusal_leaves_an_event_on_the_box(path, upstream):
    budget = _budget(path, box="b-one", nano=ONE_CALL)
    with _client(path, upstream) as client:
        _call(client)
        _call(client)
    event = _events(path)[0]
    assert event.payload["code"] == "budget_exceeded"
    assert event.payload["budget"]["id"] == budget.id


def test_a_looping_agent_leaves_one_event_not_thousands(path, upstream):
    _budget(path, box="b-one", nano=ONE_CALL)
    with _client(path, upstream) as client:
        for _ in range(5):
            _call(client)
    assert len(_events(path)) == 1


def test_an_event_that_cannot_be_written_still_refuses(path, upstream, monkeypatch):
    _budget(path, box="b-one", nano=ONE_CALL)

    def broken(self, *args, **kwargs):
        raise OSError("no room")

    with _client(path, upstream) as client:
        _call(client)
        monkeypatch.setattr(FleetStore, "add_event", broken)
        response = _call(client)
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "budget_exceeded"


def test_the_previous_call_is_counted_though_its_row_is_written_on_a_thread(path, upstream):
    """A loop's next call must see the last one, or a budget runs over by more."""
    _budget(path, box="b-one", nano=ONE_CALL)
    with _client(path, upstream) as client:
        outcomes = [_call(client).status_code for _ in range(4)]
    assert outcomes == [200, 402, 402, 402]


def test_the_model_list_is_not_budgeted(path, upstream):
    _budget(path, box="b-one", nano=1)
    with _client(path, upstream) as client:
        _call(client)
        assert client.get("/v1/models", headers=_auth()).status_code == 200


def test_a_refusal_reads_like_an_openai_error(path, upstream):
    _budget(path, box="b-one", nano=ONE_CALL)
    with _client(path, upstream) as client:
        _call(client)
        response = _call(client)
    assert response.headers["content-type"].startswith("application/json")
    assert isinstance(json.loads(response.text)["error"]["message"], str)
