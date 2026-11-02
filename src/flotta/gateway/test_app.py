"""The model gateway (FLOTTA-88, M13.2).

The first tests are the guards: a call is metered against the box whose token
made it and no other, a provider key a box sends never reaches the upstream, and
a stream cut part-way still leaves a row. No network: the upstream is injected.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_BOX_CHAT, SCOPE_MODEL_CALL, box_subject, mint
from flotta.gateway.app import (
    UpstreamKey,
    _Events,
    _Ledger,
    _MeteredStream,
    _relay,
    create_gateway,
    resolve_key,
)
from flotta.gateway.fake import FAKE_MODEL, FAKE_PRICE, FAKE_UPSTREAM_URL, FakeUpstream
from flotta.pricing import NANO, Price
from flotta.store import FleetStore

SIGNING_KEY = "a-signing-key-for-these-tests"
FLEET_KEY = "fleet-provider-key"
THEIR_KEY = "a-key-the-box-brought-along"
ENV = {
    "FLOTTA_SIGNING_KEY": SIGNING_KEY,
    "FLOTTA_API_KEY": FLEET_KEY,
    "FLOTTA_MODEL_BASE_URL": FAKE_UPSTREAM_URL,
    "FLOTTA_EUR_PER_USD": "0.5",
}
EARLIER = datetime(2026, 1, 1, tzinfo=UTC)
CALL = {"model": FAKE_MODEL, "messages": [{"role": "user", "content": "hello"}]}


@pytest.fixture
def path(tmp_path):
    db = str(tmp_path / "fleet.db")
    with FleetStore(db) as fleet:
        fleet.create_org("studio-nord", "Studio Nord")
        fleet.create_client("studio-nord", "acme", "Acme Dental")
        fleet.create_box("one", box_id="b-one", org="studio-nord", client="acme")
        fleet.create_box("two", box_id="b-two", org="studio-nord", client="acme")
        fleet.refresh_prices([FAKE_PRICE], now=EARLIER)
    return db


@pytest.fixture
def upstream():
    return FakeUpstream()


def _token(box="b-one", scopes=(SCOPE_MODEL_CALL,), org="studio-nord", client="acme"):
    return mint(
        subject=box_subject(box), scopes=set(scopes), key=SIGNING_KEY, org=org, client=client
    )


def _auth(token=None):
    return {"Authorization": f"Bearer {token or _token()}"}


def _client(path, upstream, env=ENV, **kwargs):
    app = create_gateway(
        store_factory=lambda: FleetStore(path), transport=upstream, env=env, **kwargs
    )
    return TestClient(app)


def _rows(path):
    with FleetStore(path) as fleet:
        rows = fleet._conn.execute("SELECT * FROM usage ORDER BY at").fetchall()
    return [dict(row) for row in rows]


def _spent(path, box):
    with FleetStore(path) as fleet:
        return fleet.spend(org="studio-nord", box=box, since=EARLIER).eur_nano


# -- the guards --------------------------------------------------------------


def test_a_call_is_metered_against_the_box_whose_token_made_it(path, upstream):
    with _client(path, upstream) as client:
        response = client.post(
            "/v1/chat/completions",
            json={**CALL, "user": "b-two", "metadata": {"box": "b-two"}},
            headers={**_auth(_token("b-one")), "X-Flotta-Box": "b-two"},
        )
    assert response.status_code == 200, response.text
    assert [row["box_id"] for row in _rows(path)] == ["b-one"]
    assert _spent(path, "b-two") == 0


@pytest.mark.parametrize(
    "header",
    [
        "x-api-key",
        "api-key",
        "anthropic-api-key",
        "x-goog-api-key",
        "openai-organization",
        "cookie",
        "x-some-provider-key-nobody-has-heard-of",
    ],
)
def test_a_provider_key_a_box_sends_never_reaches_the_upstream(path, upstream, header):
    with _client(path, upstream) as client:
        client.post("/v1/chat/completions", json=CALL, headers={**_auth(), header: THEIR_KEY})
    sent = upstream.requests[0]
    assert THEIR_KEY not in json.dumps(dict(sent.headers))
    assert sent.headers["authorization"] == f"Bearer {FLEET_KEY}"


def test_the_boxs_own_token_never_reaches_the_upstream(path, upstream):
    token = _token()
    with _client(path, upstream) as client:
        client.post("/v1/chat/completions", json=CALL, headers=_auth(token))
    assert token not in json.dumps(dict(upstream.requests[0].headers))


def test_the_query_string_does_not_reach_the_upstream(path, upstream):
    with _client(path, upstream) as client:
        client.post("/v1/chat/completions?key=" + THEIR_KEY, json=CALL, headers=_auth())
    assert THEIR_KEY not in str(upstream.requests[0].url)


def test_a_stream_the_upstream_cuts_still_writes_a_row(path):
    cut = FakeUpstream(cut_after=2)
    with _client(path, cut) as client:
        client.post("/v1/chat/completions", json={**CALL, "stream": True}, headers=_auth())
    rows = _rows(path)
    assert len(rows) == 1
    assert rows[0]["estimated"] == 1
    assert rows[0]["tokens_out"] > 0
    assert rows[0]["cost_eur_nano"] > 0


def test_a_stream_the_box_abandons_still_writes_a_row_and_closes_the_upstream():
    """Directly on the generator, because a test client reads a stream to its end.
    `just gateway-smoke` does this over a real socket."""
    upstream = FakeUpstream()
    written = []
    ledger = _Ledger(lambda **row: written.append(row))

    async def abandon():
        async with httpx.AsyncClient(transport=upstream) as client:
            request = client.build_request(
                "POST", f"{FAKE_UPSTREAM_URL}/chat/completions", json={**CALL, "stream": True}
            )
            response = await client.send(request, stream=True)
            relay = _relay(response, _Events(strip_usage=False), lambda **end: ledger.write(**end))
            assert await anext(relay)
            await relay.aclose()
        await ledger.drain()

    asyncio.run(abandon())
    assert len(written) == 1
    assert written[0]["usage"] is None
    assert written[0]["chars"] > 0
    assert upstream.closed == 1


def test_a_stream_whose_generator_never_ran_still_writes_a_row():
    """A client gone before the first chunk: a generator never started has no
    `finally`, so the response has to do it."""
    finished = []

    async def never_runs():
        yield b"never"

    async def go():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            raise OSError("the client has gone")

        generator = never_runs()
        response = _MeteredStream(generator, lambda: finished.append(True))
        with contextlib.suppress(Exception):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        await generator.aclose()

    asyncio.run(go())
    assert finished == [True]


def test_no_network_in_this_suite(path, upstream):
    """The upstream is the fake's, and the fake is what received the call."""
    with _client(path, upstream) as client:
        client.post("/v1/chat/completions", json=CALL, headers=_auth())
    assert upstream.requests[0].url.host == httpx.URL(FAKE_UPSTREAM_URL).host


# -- done when: streams back unchanged, one correctly priced row -----------------


def test_a_stream_comes_back_unchanged_and_leaves_one_priced_row(path, upstream):
    body = {**CALL, "stream": True, "stream_options": {"include_usage": True}}
    with _client(path, upstream) as client:
        response = client.post("/v1/chat/completions", json=body, headers=_auth())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == upstream.last_stream

    rows = _rows(path)
    assert len(rows) == 1
    row = rows[0]
    usage = upstream.usage
    assert (row["tokens_in"], row["tokens_out"], row["tokens_cached"]) == (
        usage["prompt_tokens"],
        usage["completion_tokens"],
        usage["prompt_tokens_details"]["cached_tokens"],
    )
    # FAKE_PRICE: $2 per million in, $1 per million cached, $8 per million out.
    dollars = (
        (usage["prompt_tokens"] - usage["prompt_tokens_details"]["cached_tokens"]) * 2
        + usage["prompt_tokens_details"]["cached_tokens"] * 1
        + usage["completion_tokens"] * 8
    )
    assert row["upstream_cost_nano"] == dollars * NANO // 1_000_000
    assert row["cost_eur_nano"] == row["upstream_cost_nano"] // 2
    assert row["eur_per_usd"] == "0.5"
    assert row["currency"] == "USD"
    assert row["model"] == FAKE_MODEL
    assert row["upstream"] == "custom"
    assert row["key_scope"] == "fleet"
    assert row["status"] == 200
    assert row["estimated"] == 0
    assert row["latency_ms"] is not None
    assert (row["org_id"], row["client_id"], row["box_id"]) == ("studio-nord", "acme", "b-one")


def test_a_request_that_asked_for_usage_is_forwarded_byte_for_byte(path, upstream):
    """Hermes asks. Re-serialising would risk what the gateway has no opinion on."""
    raw = (
        b'{"model":  "fake/model", "stream":true, "stream_options":{"include_usage":true},'
        b' "messages":[{"role":"user","content":"caf\xc3\xa9"}], "cache_control":{"type":"x"}}'
    )
    with _client(path, upstream) as client:
        client.post(
            "/v1/chat/completions",
            content=raw,
            headers={**_auth(), "Content-Type": "application/json"},
        )
    assert upstream.requests[0].content == raw


def test_a_stream_that_did_not_ask_for_usage_is_metered_and_does_not_see_it(path, upstream):
    """Asked for on the box's behalf, and kept from a client that does not expect
    a chunk with no choices in it."""
    with _client(path, upstream) as client:
        response = client.post(
            "/v1/chat/completions", json={**CALL, "stream": True}, headers=_auth()
        )
    assert json.loads(upstream.requests[0].content)["stream_options"]["include_usage"] is True
    assert b'"usage"' not in response.content
    assert b"data: [DONE]" in response.content
    row = _rows(path)[0]
    assert row["tokens_in"] == upstream.usage["prompt_tokens"]
    assert row["estimated"] == 0


def test_a_call_that_does_not_stream_is_metered(path, upstream):
    with _client(path, upstream) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth())
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"]
    row = _rows(path)[0]
    assert row["tokens_out"] == upstream.usage["completion_tokens"]
    assert row["estimated"] == 0


def test_a_reply_with_no_usage_in_it_is_estimated_and_says_so(path):
    silent = FakeUpstream(reports_usage=False)
    with _client(path, silent) as client:
        client.post("/v1/chat/completions", json=CALL, headers=_auth())
    row = _rows(path)[0]
    assert row["estimated"] == 1
    assert row["tokens_in"] > 0
    assert row["tokens_out"] > 0


@pytest.mark.parametrize("status", [400, 402, 429, 500, 503])
def test_an_upstream_error_comes_back_as_it_was_and_writes_a_row_at_no_cost(path, status):
    failing = FakeUpstream(status=status)
    with _client(path, failing) as client:
        response = client.post(
            "/v1/chat/completions", json={**CALL, "stream": True}, headers=_auth()
        )
    assert response.status_code == status
    assert response.json()["error"]["message"] == "the fake upstream says no"
    row = _rows(path)[0]
    assert row["status"] == status
    assert row["cost_eur_nano"] == 0
    assert row["estimated"] == 0


def test_an_error_on_a_model_with_no_price_is_not_unpriced(path):
    """Nothing was spent, and it must not stop a budget check that fails closed
    on unpriced rows."""
    failing = FakeUpstream(status=400)
    with _client(path, failing) as client:
        client.post("/v1/chat/completions", json={**CALL, "model": "nobody/knows"}, headers=_auth())
    with FleetStore(path) as fleet:
        assert fleet.spend(org="studio-nord", since=EARLIER).unpriced == 0


def test_an_upstream_nobody_can_reach_is_a_502_and_a_row_at_no_cost(path):
    down = FakeUpstream(unreachable=True)
    with _client(path, down) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth())
    assert response.status_code == 502
    assert FLEET_KEY not in response.text
    row = _rows(path)[0]
    assert row["status"] == 502
    assert row["cost_eur_nano"] == 0


def test_a_stream_options_that_is_not_an_object_is_not_a_crash(path, upstream):
    with _client(path, upstream) as client:
        response = client.post(
            "/v1/chat/completions",
            json={**CALL, "stream": True, "stream_options": "yes"},
            headers=_auth(),
        )
    assert response.status_code == 200
    assert json.loads(upstream.requests[0].content)["stream_options"]["include_usage"] is True


def test_a_provider_complaint_that_quotes_the_key_does_not_reach_the_box(path):
    def complains(request):
        return httpx.Response(401, json={"error": {"message": f"bad key: {FLEET_KEY}"}})

    with _client(path, httpx.MockTransport(complains)) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth())
    assert response.status_code == 401
    assert FLEET_KEY not in response.text
    assert _rows(path)[0]["cost_eur_nano"] == 0


class _CutBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"choices":'
        raise httpx.ReadError("gone")


def test_a_reply_cut_part_way_when_not_streaming_is_a_502_and_an_estimate(path):
    def cut(request):
        return httpx.Response(200, stream=_CutBody())

    with _client(path, httpx.MockTransport(cut)) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth())
    assert response.status_code == 502
    row = _rows(path)[0]
    assert row["estimated"] == 1
    assert row["tokens_in"] > 0


def test_an_error_reply_cut_part_way_costs_nothing(path):
    def cut(request):
        return httpx.Response(500, stream=_CutBody())

    with _client(path, httpx.MockTransport(cut)) as client:
        assert client.post("/v1/chat/completions", json=CALL, headers=_auth()).status_code == 502
    assert _rows(path)[0]["cost_eur_nano"] == 0


def test_a_gateway_with_no_signing_key_refuses_every_call(path, upstream):
    env = {k: v for k, v in ENV.items() if k != "FLOTTA_SIGNING_KEY"}
    with _client(path, upstream, env=env) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth())
    assert response.status_code == 503
    assert upstream.requests == []


def _raises(error):
    def handler(request):
        raise error("the upstream")

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_a_request_that_never_got_out_costs_nothing(path, error):
    with _client(path, _raises(error)) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth())
    assert response.status_code == 502
    row = _rows(path)[0]
    assert row["cost_eur_nano"] == 0
    assert row["estimated"] == 0


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout,
        httpx.ReadError,
        httpx.RemoteProtocolError,
        httpx.WriteTimeout,
        httpx.WriteError,
    ],
)
def test_a_failure_after_the_request_may_have_got_out_is_not_called_free(path, error):
    """The provider may have the prompt and be generating, so no proof of zero."""
    with _client(path, _raises(error)) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth())
    assert response.status_code == 502
    row = _rows(path)[0]
    assert row["status"] == 502
    assert row["estimated"] == 1
    assert row["tokens_in"] > 0
    assert row["cost_eur_nano"] > 0


def test_a_provider_complaint_that_quotes_the_key_does_not_reach_the_box_from_the_model_list(path):
    def complains(request):
        return httpx.Response(401, json={"error": {"message": f"bad key: {FLEET_KEY}"}})

    with _client(path, httpx.MockTransport(complains)) as client:
        response = client.get("/v1/models", headers=_auth())
    assert response.status_code == 401
    assert FLEET_KEY not in response.text
    assert "bad key" in response.text


def test_a_run_id_is_recorded_and_not_forwarded(path, upstream):
    with _client(path, upstream) as client:
        client.post(
            "/v1/chat/completions", json=CALL, headers={**_auth(), "X-Flotta-Run": "run-42"}
        )
    assert _rows(path)[0]["run_id"] == "run-42"
    assert "x-flotta-run" not in upstream.requests[0].headers


def test_a_run_id_that_cannot_be_one_is_dropped(path, upstream):
    with _client(path, upstream) as client:
        client.post(
            "/v1/chat/completions", json=CALL, headers={**_auth(), "X-Flotta-Run": "a b" * 40}
        )
    assert _rows(path)[0]["run_id"] is None


def test_a_rate_set_in_the_fleets_settings_wins_over_the_environment(path, upstream):
    with FleetStore(path) as fleet:
        fleet.set_setting("FLOTTA_EUR_PER_USD", "1")
    with _client(path, upstream) as client:
        client.post("/v1/chat/completions", json=CALL, headers=_auth())
    row = _rows(path)[0]
    assert row["eur_per_usd"] == "1"
    assert row["cost_eur_nano"] == row["upstream_cost_nano"]


def test_with_no_rate_the_call_is_unpriced_rather_than_guessed(path, upstream):
    env = {k: v for k, v in ENV.items() if k != "FLOTTA_EUR_PER_USD"}
    with _client(path, upstream, env=env) as client:
        client.post("/v1/chat/completions", json=CALL, headers=_auth())
    row = _rows(path)[0]
    assert row["upstream_cost_nano"] > 0
    assert row["cost_eur_nano"] is None


# -- who may call ------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer nonsense"},
        {"Authorization": "Basic abc"},
        {"Authorization": f"Bearer {FLEET_KEY}"},
    ],
)
def test_no_token_is_refused_before_anything_is_spent(path, upstream, headers):
    with _client(path, upstream) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["message"]
    assert upstream.requests == []
    assert _rows(path) == []


def test_a_token_signed_by_another_key_is_refused(path, upstream):
    forged = mint(subject=box_subject("b-one"), scopes={SCOPE_MODEL_CALL}, key="another-key")
    with _client(path, upstream) as client:
        assert (
            client.post("/v1/chat/completions", json=CALL, headers=_auth(forged)).status_code == 401
        )
    assert upstream.requests == []


def test_a_box_token_without_the_scope_is_refused(path, upstream):
    token = _token(scopes=(SCOPE_BOX_CHAT,))
    with _client(path, upstream) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth(token))
    assert response.status_code == 403
    assert upstream.requests == []


def test_a_person_or_tool_with_the_scope_is_refused(path, upstream):
    """A call is metered against a box, so it must come from one."""
    token = mint(subject="cli", scopes={SCOPE_MODEL_CALL}, key=SIGNING_KEY, org="studio-nord")
    with _client(path, upstream) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth(token))
    assert response.status_code == 403
    assert upstream.requests == []


def test_a_box_that_does_not_exist_is_refused(path, upstream):
    with _client(path, upstream) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth(_token("b-gone")))
    assert response.status_code == 403
    assert upstream.requests == []


def test_a_destroyed_box_is_refused_though_its_token_has_not_expired(path, upstream):
    with FleetStore(path) as fleet:
        fleet.update_box_status("b-two", "torn_down")
    with _client(path, upstream) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth(_token("b-two")))
    assert response.status_code == 403
    assert upstream.requests == []


def test_a_token_naming_another_org_is_refused(path, upstream):
    token = _token(org="other", client=None)
    with _client(path, upstream) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth(token))
    assert response.status_code == 403
    assert upstream.requests == []


def test_a_store_that_cannot_be_read_refuses_rather_than_forwards(upstream):
    """The fail-closed rule M13.3 rests on: no check, no call."""

    def down():
        raise OSError("the database is away")

    app = create_gateway(store_factory=down, transport=upstream, env=ENV)
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth())
    assert response.status_code == 402
    assert upstream.requests == []


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b"{}",
        b'{"model": ""}',
        b'{"model": 7}',
        b'{"model": "two words"}',
    ],
)
def test_a_request_that_names_no_model_is_refused_before_anything_is_spent(path, upstream, body):
    with _client(path, upstream) as client:
        response = client.post(
            "/v1/chat/completions",
            content=body,
            headers={**_auth(), "Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert upstream.requests == []
    assert _rows(path) == []


def test_no_upstream_key_is_refused_before_anything_is_spent(path, upstream):
    env = {k: v for k, v in ENV.items() if k != "FLOTTA_API_KEY"}
    with _client(path, upstream, env=env) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth())
    assert response.status_code == 503
    assert upstream.requests == []


# -- whose key -----------------------------------------------------------------


def _stored(keys):
    return lambda provider, org, client: keys.get((provider, org, client))


def test_the_clients_key_comes_first():
    keys = {("openrouter", "o", "c"): "client", ("openrouter", "o", None): "org"}
    found = resolve_key("openrouter", org="o", client="c", env=ENV, stored=_stored(keys))
    assert found == UpstreamKey("client", "client")


def test_then_the_orgs():
    keys = {("openrouter", "o", None): "org"}
    found = resolve_key("openrouter", org="o", client="c", env=ENV, stored=_stored(keys))
    assert found == UpstreamKey("org", "org")


def test_then_the_fleets():
    found = resolve_key("openrouter", org="o", client="c", env=ENV)
    assert found == UpstreamKey(FLEET_KEY, "fleet")


def test_a_key_for_another_provider_is_not_used():
    keys = {("anthropic", "o", "c"): "wrong"}
    found = resolve_key("openrouter", org="o", client="c", env=ENV, stored=_stored(keys))
    assert found == UpstreamKey(FLEET_KEY, "fleet")


def test_no_key_at_any_level_is_none():
    assert resolve_key("openrouter", org="o", client="c", env={}) is None


def test_the_level_that_paid_is_on_the_row(path, upstream):
    keys = {("custom", "studio-nord", "acme"): "the-clients-own"}
    with _client(path, upstream, stored_keys=_stored(keys)) as client:
        client.post("/v1/chat/completions", json=CALL, headers=_auth())
    assert upstream.requests[0].headers["authorization"] == "Bearer the-clients-own"
    assert _rows(path)[0]["key_scope"] == "client"


# -- the other route -----------------------------------------------------------


def test_the_model_list_is_passed_through_on_the_fleets_key_and_is_not_metered(path, upstream):
    """Hermes reads it for a custom endpoint's context length."""
    with _client(path, upstream) as client:
        response = client.get("/v1/models", headers={**_auth(), "x-api-key": THEIR_KEY})
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == FAKE_MODEL
    assert upstream.requests[0].headers["authorization"] == f"Bearer {FLEET_KEY}"
    assert THEIR_KEY not in json.dumps(dict(upstream.requests[0].headers))
    assert _rows(path) == []


def test_the_model_list_needs_a_token_too(path, upstream):
    with _client(path, upstream) as client:
        assert client.get("/v1/models").status_code == 401
    assert upstream.requests == []


def test_a_route_hermes_does_not_call_is_a_404_and_reaches_nothing(path, upstream):
    with _client(path, upstream) as client:
        for route in ("/v1/embeddings", "/v1/messages", "/v1/responses"):
            assert client.post(route, json=CALL, headers=_auth()).status_code == 404
    assert upstream.requests == []


def test_health_says_nothing_about_the_fleet(path, upstream):
    with _client(path, upstream) as client:
        response = client.get("/health")
    assert response.json() == {"status": "ok"}


def test_what_the_upstream_sets_as_a_cookie_does_not_reach_the_box(path, upstream):
    with _client(path, upstream) as client:
        response = client.post("/v1/chat/completions", json=CALL, headers=_auth())
    assert "set-cookie" not in response.headers


# -- the ledger and the parser -------------------------------------------------


def test_prices_are_refreshed_before_the_first_call(tmp_path, upstream):
    db = str(tmp_path / "fleet.db")
    FleetStore(db).close()
    price = Price("fresh/model", NANO, NANO, None, "USD", "test")
    with _client(db, upstream, price_source=lambda: [price]), FleetStore(db) as fleet:
        assert fleet.price_of("fresh/model", at=datetime.now(UTC)) is not None


def test_a_price_source_that_fails_does_not_stop_the_gateway(path, upstream):
    def down():
        raise OSError("no catalogue")

    with _client(path, upstream, price_source=down) as client:
        assert client.post("/v1/chat/completions", json=CALL, headers=_auth()).status_code == 200


def test_a_write_that_fails_is_retried_with_the_same_key():
    attempts = []

    def flaky(**row):
        attempts.append(row)
        if len(attempts) < 3:
            raise OSError("the database blinked")

    ledger = _Ledger(flaky, backoff_s=0.0)
    ledger.write(idempotency_key="k")
    ledger.close()
    assert [row["idempotency_key"] for row in attempts] == ["k", "k", "k"]


def test_a_write_that_never_succeeds_is_logged_and_does_not_raise(caplog):
    def down(**row):
        raise OSError("the database is away")

    ledger = _Ledger(down, backoff_s=0.0)
    ledger.write(idempotency_key="k", tokens_in=7)
    ledger.close()
    assert "could not be written" in caplog.text


def test_events_split_across_chunks_come_out_whole_and_unchanged():
    raw = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
    events = _Events(strip_usage=False)
    out = b"".join(events.feed(raw[i : i + 7]) for i in range(0, len(raw), 7)) + events.flush()
    assert out == raw
    assert events.chars == 2


def test_comments_and_crlf_pass_through():
    raw = b": OPENROUTER PROCESSING\r\n\r\ndata: [DONE]\r\n\r\n"
    events = _Events(strip_usage=True)
    assert events.feed(raw) + events.flush() == raw


def test_a_usage_chunk_with_choices_in_it_is_read_and_kept():
    raw = b'data: {"choices":[{"delta":{"content":"x"}}],"usage":{"prompt_tokens":1}}\n\n'
    events = _Events(strip_usage=True)
    assert events.feed(raw) == raw
    assert events.usage == {"prompt_tokens": 1}


def test_a_chunk_that_is_not_json_is_passed_on_not_fatal():
    raw = b"data: {broken\n\n"
    events = _Events(strip_usage=True)
    assert events.feed(raw) == raw
    assert events.usage is None


def test_text_of_every_kind_counts_towards_the_estimate():
    chunk = {
        "choices": [
            {
                "delta": {
                    "role": "assistant",
                    "content": "abc",
                    "reasoning": "de",
                    "tool_calls": [{"function": {"name": "f", "arguments": "gh"}}],
                }
            }
        ]
    }
    events = _Events(strip_usage=False)
    events.feed(b"data: " + json.dumps(chunk).encode() + b"\n\n")
    assert events.chars == len("abcdefgh")


def test_the_rate_setting_is_refused_at_zero():
    from flotta.settings import validate

    with pytest.raises(ValueError, match="greater than zero"):
        validate("FLOTTA_EUR_PER_USD", "0")
    assert validate("FLOTTA_EUR_PER_USD", "0.92") == "0.92"


def test_the_fake_price_is_what_these_numbers_assume():
    assert FAKE_PRICE.input_nano_per_m == 2 * NANO
    assert FAKE_PRICE.cached_nano_per_m == NANO
    assert FAKE_PRICE.output_nano_per_m == 8 * NANO
    assert Decimal(ENV["FLOTTA_EUR_PER_USD"]) == Decimal("0.5")
