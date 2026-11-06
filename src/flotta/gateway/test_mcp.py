"""The gateway's MCP half (FLOTTA-125): the grant, the tools and the credential, off the box.

The guards first. A tool not granted is refused before anything goes out, and
taken out of every tool list. A grant taken away stops the next call on the
same running gateway. The agency's credential reaches the server and the box's
token doesn't, and nothing the box sends is passed on as a credential. No
network: the server and its OAuth provider are `fake_mcp`.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_MCP_CALL, SCOPE_MODEL_CALL, box_subject, mint
from flotta.gateway.app import create_gateway
from flotta.gateway.fake_mcp import FakeMcp, catalogue
from flotta.mcp import connection_context
from flotta.mcp_oauth import Client, Endpoints, Tokens, oauth_credential
from flotta.sealed import fresh, seal
from flotta.store import FleetStore

SIGNING_KEY = "a-signing-key-for-these-tests"
SECRETS_KEY = fresh()
ENV = {
    "FLOTTA_SIGNING_KEY": SIGNING_KEY,
    "FLOTTA_SECRETS_KEY": SECRETS_KEY,
    "FLOTTA_MCP_SERVERS": catalogue(),
}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _call(tool, request_id=2):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {"secret": "the client's data"}},
    }


@pytest.fixture(autouse=True)
def public_names(monkeypatch):
    """`.test` names resolve to a public address, as a real provider's would."""
    import flotta.mcp_oauth as mcp_oauth

    monkeypatch.setattr(mcp_oauth, "resolve_host", lambda host, port: ["93.184.216.34"])


@pytest.fixture
def path(tmp_path):
    db = str(tmp_path / "fleet.db")
    with FleetStore(db) as fleet:
        fleet.create_org("studio-nord", "Studio Nord")
        fleet.create_client("studio-nord", "acme", "Acme Dental")
        fleet.create_box("one", box_id="b-one", org="studio-nord", client="acme")
        fleet.create_box("two", box_id="b-two", org="studio-nord", client="acme")
        fleet.create_org("other", "Other")
        fleet.create_client("other", "acme", "Acme")
        fleet.create_box("theirs", box_id="b-theirs", org="other", client="acme")
    return db


def _connect(path, credential, org="studio-nord", kind="bearer"):
    sealed = seal(json.dumps(credential), context=connection_context(org, "tracker"), env=ENV)
    with FleetStore(path) as fleet:
        fleet.set_mcp_connection(org, "tracker", kind=kind, credential=sealed, by="test")


def _grant(path, box="b-one", tools=("read_issue", "create_issue"), ask_first=True):
    with FleetStore(path) as fleet:
        fleet.set_box_mcp_grants(
            box, [("tracker", json.dumps(list(tools)) if tools else None, ask_first)]
        )


@pytest.fixture
def server(path):
    _connect(path, {"kind": "bearer", "value": "agency-key"})
    _grant(path)
    return FakeMcp()


def _token(box="b-one", scopes=(SCOPE_MCP_CALL,), org="studio-nord"):
    return mint(
        subject=box_subject(box), scopes=set(scopes), key=SIGNING_KEY, org=org, client="acme"
    )


def _auth(token=None):
    return {"Authorization": f"Bearer {token or _token()}"}


def _client(path, server, env=ENV):
    app = create_gateway(
        store_factory=lambda: FleetStore(path),
        env=env,
        mcp_transport=server.transport(),
        oauth_transport=server.transport(),
    )
    return TestClient(app)


def _calls(path, box="b-one"):
    with FleetStore(path) as fleet:
        return [(c.tool, c.status) for c in reversed(fleet.mcp_calls(box))]


# -- the guards ------------------------------------------------------------------


def test_a_granted_tool_is_called_with_the_agencys_key_and_not_the_box_token(path, server):
    token = _token()
    with _client(path, server) as client:
        answer = client.post("/mcp/tracker", json=_call("create_issue"), headers=_auth(token))
    assert answer.status_code == 200, answer.text
    assert answer.json()["result"]["content"][0]["text"] == "did create_issue"
    sent = server.requests[-1]
    assert sent.headers["authorization"] == "Bearer agency-key"
    assert token not in json.dumps(dict(sent.headers))
    assert _calls(path) == [("create_issue", "ok")]


def test_a_tool_not_granted_is_refused_before_anything_goes_out(path, server):
    with _client(path, server) as client:
        answer = client.post("/mcp/tracker", json=_call("delete_everything"), headers=_auth())
    assert answer.status_code == 200
    assert answer.json()["id"] == 2
    assert answer.json()["error"]["code"] == -32001
    assert server.requests == []
    assert _calls(path) == [("delete_everything", "refused")]


@pytest.mark.parametrize("stream", [False, True], ids=["json", "event-stream"])
def test_a_tool_list_shows_only_what_was_granted(path, stream):
    _connect(path, {"kind": "bearer", "value": "agency-key"})
    _grant(path)
    server = FakeMcp(stream=stream)
    with _client(path, server) as client:
        answer = client.post("/mcp/tracker", json=LIST, headers=_auth())
    assert answer.status_code == 200, answer.text
    if stream:
        events = [e for e in answer.text.split("\n\n") if e.strip()]
        assert "notifications/progress" in events[0]
        assert events[1].startswith("id: 7\n")
        message = json.loads(events[1].split("data: ", 1)[1])
    else:
        message = answer.json()
    assert [t["name"] for t in message["result"]["tools"]] == ["read_issue", "create_issue"]


def test_the_servers_own_stream_is_filtered_too(path, server):
    with _client(path, server) as client:
        answer = client.get("/mcp/tracker", headers=_auth())
    assert "delete_everything" not in answer.text
    assert "read_issue" in answer.text and "event: message" in answer.text


def test_a_grant_taken_away_stops_the_next_call_on_the_same_gateway(path, server):
    with _client(path, server) as client:
        first = client.post("/mcp/tracker", json=_call("read_issue"), headers=_auth())
        with FleetStore(path) as fleet:
            fleet.set_box_mcp_grants("b-one", [])
        second = client.post("/mcp/tracker", json=_call("read_issue"), headers=_auth())
    assert "result" in first.json()
    assert second.status_code == 403
    assert len(server.requests) == 1


def test_one_agents_grant_is_not_anothers(path, server):
    with _client(path, server) as client:
        answer = client.post(
            "/mcp/tracker", json=_call("read_issue"), headers=_auth(_token("b-two"))
        )
    assert answer.status_code == 403
    assert server.requests == []


def test_another_orgs_connection_is_never_used(path, server):
    """`b-theirs` is granted, but its org has connected nothing: it gets nothing."""
    _grant(path, box="b-theirs")
    with _client(path, server) as client:
        answer = client.post(
            "/mcp/tracker", json=_call("read_issue"), headers=_auth(_token("b-theirs", org="other"))
        )
    assert answer.status_code == 503
    assert "isn't connected" in answer.json()["error"]["message"]
    assert server.requests == []


@pytest.mark.parametrize(
    "token",
    [
        lambda: _token(scopes=(SCOPE_MODEL_CALL,)),
        lambda: mint(subject="a-person", scopes={SCOPE_MCP_CALL}, key=SIGNING_KEY),
        lambda: "not-a-token",
    ],
    ids=["model-token", "not-a-box", "garbage"],
)
def test_only_an_agents_own_mcp_token_is_let_in(path, server, token):
    with _client(path, server) as client:
        answer = client.post("/mcp/tracker", json=_call("read_issue"), headers=_auth(token()))
    assert answer.status_code in (401, 403)
    assert server.requests == []


def test_an_mcp_token_cannot_call_a_model(path, server):
    with _client(path, server) as client:
        answer = client.post("/v1/chat/completions", json={"model": "x"}, headers=_auth())
    assert answer.status_code == 403


def test_nothing_the_box_sends_reaches_the_server_as_a_credential(path, server):
    headers = {
        **_auth(),
        "X-Api-Key": "box-key",
        "Cookie": "session=box",
        "Mcp-Session-Id": "session-1",
        "Mcp-Protocol-Version": "2025-06-18",
    }
    with _client(path, server) as client:
        client.post("/mcp/tracker", json=_call("read_issue"), headers=headers)
    sent = server.requests[-1].headers
    assert "x-api-key" not in sent and "cookie" not in sent
    assert sent["mcp-session-id"] == "session-1"
    assert sent["mcp-protocol-version"] == "2025-06-18"


@pytest.mark.parametrize(
    "body",
    [
        json.dumps([_call("read_issue"), _call("delete_everything", 3)]),
        json.dumps([_call("read_issue"), {"jsonrpc": "2.0", "id": 3, "method": "resources/read"}]),
        '{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "method": "tools/call"}',
    ],
    ids=["batch", "batch-with-a-resource-read", "repeated-key"],
)
def test_a_message_that_could_hide_a_call_is_refused(path, server, body):
    with _client(path, server) as client:
        answer = client.post(
            "/mcp/tracker",
            content=body,
            headers={**_auth(), "Content-Type": "application/json"},
        )
    assert answer.status_code == 400
    assert server.requests == []


def test_an_api_key_goes_in_the_header_the_catalogue_names(path):
    env = {**ENV, "FLOTTA_MCP_SERVERS": catalogue(auth="api_key")}
    _connect(path, {"kind": "api_key", "value": "k-1"}, kind="api_key")
    _grant(path)
    server = FakeMcp()
    server.keys = {None}  # this server reads X-Api-Key, not Authorization
    with _client(path, server, env=env) as client:
        client.post("/mcp/tracker", json=_call("read_issue"), headers=_auth())
    assert server.requests[-1].headers["x-api-key"] == "k-1"
    assert "authorization" not in server.requests[-1].headers


def test_an_upstream_401_never_reaches_the_agent(path, server):
    """Its `WWW-Authenticate` would start Hermes's own sign-in, on the box."""
    server.keys = {"Bearer something-else"}
    with _client(path, server) as client:
        answer = client.post("/mcp/tracker", json=_call("read_issue"), headers=_auth())
    assert answer.status_code == 502
    assert "www-authenticate" not in answer.headers
    assert "connecting again" in answer.json()["error"]["message"]
    assert _calls(path) == [("read_issue", "unavailable")]


def test_a_broken_connection_says_connect_again(path, server):
    with FleetStore(path) as fleet:
        fleet.break_mcp_connection("studio-nord", "tracker", "refused")
    with _client(path, server) as client:
        answer = client.post("/mcp/tracker", json=_call("read_issue"), headers=_auth())
    assert answer.status_code == 503 and "connect it again" in answer.json()["error"]["message"]


def test_without_the_secrets_key_nothing_is_sent(path, server):
    env = {k: v for k, v in ENV.items() if k != "FLOTTA_SECRETS_KEY"}
    with _client(path, server, env=env) as client:
        answer = client.post("/mcp/tracker", json=_call("read_issue"), headers=_auth())
    assert answer.status_code == 503
    assert server.requests == []


def test_a_server_not_in_the_catalogue_is_a_404(path, server):
    with _client(path, server) as client:
        assert client.post("/mcp/elsewhere", json=LIST, headers=_auth()).status_code == 404


def test_every_tool_the_catalogue_allows_when_the_grant_names_none(path, server):
    _grant(path, tools=None)
    with _client(path, server) as client:
        answer = client.post("/mcp/tracker", json=_call("delete_everything"), headers=_auth())
    assert "result" in answer.json()


def test_a_tool_error_and_a_stream_answer_are_recorded_as_what_they_were(path):
    _connect(path, {"kind": "bearer", "value": "agency-key"})
    _grant(path)
    server = FakeMcp(stream=True)
    with _client(path, server) as client:
        client.post("/mcp/tracker", json=_call("read_issue"), headers=_auth())
    deadline = time.monotonic() + 2
    while not _calls(path) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _calls(path) == [("read_issue", "ok")]


def test_no_row_holds_the_arguments_or_the_result(path, server):
    with _client(path, server) as client:
        client.post("/mcp/tracker", json=_call("read_issue"), headers=_auth())
    with FleetStore(path) as fleet:
        rows = fleet._conn.execute("SELECT * FROM mcp_calls").fetchall()
    assert "the client's data" not in json.dumps([dict(r) for r in rows])
    assert "did read_issue" not in json.dumps([dict(r) for r in rows])


# -- OAuth -------------------------------------------------------------------------

OAUTH_ENV = {**ENV, "FLOTTA_MCP_SERVERS": catalogue(auth="oauth")}
ENDPOINTS = Endpoints(
    authorization="https://auth.example.test/authorize",
    token="https://auth.example.test/token",
    revocation="https://auth.example.test/revoke",
)


def _oauth(path, server, *, expires_at):
    tokens = server._issue()
    credential = oauth_credential(
        ENDPOINTS,
        Client("dyn-1", "dyn-secret"),
        Tokens(tokens["access_token"], tokens["refresh_token"], expires_at),
    )
    _connect(path, credential, kind="oauth")
    _grant(path)
    return tokens


def test_an_expired_token_is_refreshed_before_the_call(path):
    server = FakeMcp(keys=set())
    old = _oauth(path, server, expires_at=time.time() - 10)
    server.keys.discard(f"Bearer {old['access_token']}")
    with _client(path, server, env=OAUTH_ENV) as client:
        answer = client.post("/mcp/tracker", json=_call("read_issue"), headers=_auth())
    assert "result" in answer.json(), answer.text
    assert server.refreshes == 1
    assert server.requests[-1].headers["authorization"] == "Bearer at-2"


def test_a_refused_upstream_token_is_refreshed_once_and_the_call_tried_again(path):
    server = FakeMcp(keys=set())
    old = _oauth(path, server, expires_at=time.time() + 3600)
    server.keys.discard(f"Bearer {old['access_token']}")  # revoked at the provider's end
    with _client(path, server, env=OAUTH_ENV) as client:
        answer = client.post("/mcp/tracker", json=_call("read_issue"), headers=_auth())
    assert "result" in answer.json(), answer.text
    assert server.refreshes == 1 and len(server.requests) == 2


def test_a_refused_refresh_breaks_the_connection(path):
    server = FakeMcp(keys=set())
    _oauth(path, server, expires_at=time.time() - 10)
    server.refresh_tokens.clear()
    with _client(path, server, env=OAUTH_ENV) as client:
        answer = client.post("/mcp/tracker", json=_call("read_issue"), headers=_auth())
        again = client.post("/mcp/tracker", json=_call("read_issue"), headers=_auth())
    assert answer.status_code == 503 and "connect it again" in answer.json()["error"]["message"]
    with FleetStore(path) as fleet:
        assert fleet.mcp_connection("studio-nord", "tracker").status == "broken"
    assert again.status_code == 503
    assert server.requests == []


def test_two_calls_at_once_spend_one_refresh_token(path):
    """Rotation makes the second refresh with the same token a refusal: a broken
    connection for nothing, if both went out."""
    server = FakeMcp(keys=set())
    server.refresh_delay = 0.05
    _oauth(path, server, expires_at=time.time() - 10)
    from flotta.gateway.mcp import Proxy

    proxy = Proxy(
        store_factory=lambda: FleetStore(path),
        env=OAUTH_ENV,
        transport=None,
        oauth_transport=server.transport(),
    )
    with FleetStore(path) as fleet:
        box = fleet.get_box("b-one")
    found, errors = [], []

    def one():
        try:
            found.append(proxy.credential(box, proxy.servers["tracker"])["access_token"])
        except Exception as exc:  # noqa: BLE001 - collected, so a thread's failure is seen
            errors.append(exc)

    threads = [threading.Thread(target=one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert server.refreshes == 1
    assert set(found) == {"at-2"}


# -- tools, and only tools ---------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    ["resources/read", "resources/subscribe", "prompts/get", "completion/complete", "sampling/x"],
)
def test_a_method_that_isnt_tools_is_refused_before_the_credential_is_read(
    path, server, monkeypatch, method
):
    """The agent holds the token, so it can send anything; only tools go out."""
    from flotta.gateway.mcp import Proxy

    read = []
    monkeypatch.setattr(Proxy, "credential", lambda self, *a, **k: read.append(a) or {})
    message = {"jsonrpc": "2.0", "id": 5, "method": method, "params": {"uri": "file:///etc"}}
    with _client(path, server) as client:
        answer = client.post("/mcp/tracker", json=message, headers=_auth())
    assert answer.status_code == 200
    assert answer.json()["id"] == 5 and answer.json()["error"]["code"] == -32601
    assert read == [] and server.requests == []


def test_a_notification_that_isnt_the_handshake_is_refused(path, server):
    note = {"jsonrpc": "2.0", "method": "notifications/roots/list_changed"}
    with _client(path, server) as client:
        answer = client.post("/mcp/tracker", json=note, headers=_auth())
    assert answer.status_code == 400
    assert server.requests == []


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "result": {"action": "accept"}},
    ],
    ids=["initialize", "initialized", "ping", "an-answer-to-the-server"],
)
def test_the_handshake_and_answers_to_the_server_go_through(path, server, message):
    with _client(path, server) as client:
        client.post("/mcp/tracker", json=message, headers=_auth())
    assert len(server.requests) == 1


def test_a_403_is_the_upstreams_refusal_and_never_breaks_the_connection(path):
    server = FakeMcp(keys=set())
    _oauth(path, server, expires_at=time.time() + 3600)
    server.forbidden = {"create_issue"}
    with _client(path, server, env=OAUTH_ENV) as client:
        answer = client.post("/mcp/tracker", json=_call("create_issue"), headers=_auth())
    assert answer.status_code == 403
    assert "www-authenticate" not in answer.headers
    assert server.refreshes == 0 and len(server.requests) == 1
    with FleetStore(path) as fleet:
        assert fleet.mcp_connection("studio-nord", "tracker").status == "ok"
