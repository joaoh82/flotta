"""GET /api/orgs/{org}/mcp/{server}/tools lists a connected server's real tools.

The call uses the sealed credential and the gateway's pin. A missing or expired
connection is a code the console can match to the org list's status, and the
answer never carries the credential.
"""

from __future__ import annotations

import json
import time

import httpx

from flotta.control.test_mcp_api import (  # noqa: F401  (fixtures)
    SECRETS,
    api,
    fleet,
    provider,
)
from flotta.control.test_sign_in import KEY
from flotta.control.test_tickets import _as
from flotta.gateway.fake_mcp import MCP_URL
from flotta.mcp import connection_context
from flotta.mcp_library import catalogue_entry, entry
from flotta.mcp_oauth import Client, Endpoints, Tokens, oauth_credential
from flotta.sealed import seal
from flotta.store import FleetStore

PUBLIC = "93.184.216.34"
ENDPOINTS = Endpoints(
    authorization="https://auth.example.test/authorize",
    token="https://auth.example.test/token",
    revocation="https://auth.example.test/revoke",
)


def _headers(db, email="ana@example.com"):
    return _as(db, email)


def _seal(org: str, server: str, credential: dict) -> str:
    return seal(
        json.dumps(credential),
        context=connection_context(org, server),
        env={"FLOTTA_SECRETS_KEY": SECRETS, "FLOTTA_SIGNING_KEY": KEY},
    )


def test_a_connected_server_lists_its_tools(api, fleet, provider):  # noqa: F811
    provider.tool_annotations["read_issue"] = {"readOnlyHint": True, "title": "Read"}
    ana = _headers(fleet)
    answer = api.put("/api/orgs/agency-x/mcp/notes", json={"key": "agency-key"}, headers=ana)
    assert answer.status_code == 200, answer.text
    listed = api.get("/api/orgs/agency-x/mcp/notes/tools", headers=ana)
    assert listed.status_code == 200, listed.text
    assert listed.json() == {
        "server": "notes",
        "tools": [
            {
                "name": "read_issue",
                "description": "read_issue, for tests",
                "read_only": True,
                "annotations": {"readOnlyHint": True, "title": "Read"},
            },
            {
                "name": "create_issue",
                "description": "create_issue, for tests",
                "read_only": None,
            },
            {
                "name": "delete_everything",
                "description": "delete_everything, for tests",
                "read_only": None,
            },
        ],
    }
    assert "agency-key" not in listed.text
    assert "inputSchema" not in listed.text
    sent = provider.requests[-1]
    assert json.loads(sent.content)["method"] == "tools/list"
    assert sent.headers["authorization"] == "Bearer agency-key"


def test_a_server_that_is_not_connected_is_refused(api, fleet, provider):  # noqa: F811
    ana = _headers(fleet)
    answer = api.get("/api/orgs/agency-x/mcp/notes/tools", headers=ana)
    assert answer.status_code == 409
    assert answer.json() == {"code": "not_connected", "detail": "notes is not connected"}
    assert provider.requests == []
    assert api.get("/api/orgs/agency-x/mcp/notes/tools").status_code == 401
    assert api.get("/api/orgs/agency-x/mcp/nope/tools", headers=ana).status_code == 404


def test_an_expired_sign_in_is_expired_and_not_called(api, fleet, provider):  # noqa: F811
    credential = oauth_credential(
        ENDPOINTS, Client("dyn-1", "dyn-secret"), Tokens("at-old", None, time.time() - 60)
    )
    with FleetStore(fleet) as store:
        store.set_mcp_connection(
            "agency-x",
            "tracker",
            kind="oauth",
            credential=_seal("agency-x", "tracker", credential),
            by="ana",
            server_url=MCP_URL,
        )
        store.break_mcp_connection("agency-x", "tracker", "the provider refused")
    answer = api.get("/api/orgs/agency-x/mcp/tracker/tools", headers=_headers(fleet))
    assert answer.status_code == 424
    assert answer.json()["code"] == "expired"
    assert "at-old" not in answer.text
    assert provider.requests == []


def test_an_upstream_failure_and_a_timeout_are_their_own_codes(api, fleet, provider):  # noqa: F811
    ana = _headers(fleet)
    assert (
        api.put("/api/orgs/agency-x/mcp/notes", json={"key": "agency-key"}, headers=ana).status_code
        == 200
    )
    provider.list_status = 500
    failed = api.get("/api/orgs/agency-x/mcp/notes/tools", headers=ana)
    assert failed.status_code == 502
    assert failed.json() == {"code": "upstream", "detail": "notes answered 500"}
    assert "agency-key" not in failed.text
    provider.list_status = None
    provider.list_error = httpx.ReadTimeout("slow")
    timed = api.get("/api/orgs/agency-x/mcp/notes/tools", headers=ana)
    assert timed.status_code == 504
    assert timed.json()["code"] == "timeout"


def test_a_templated_host_is_pinned_and_a_hand_written_one_is_not(
    api, fleet, provider, monkeypatch  # noqa: F811
):
    import flotta.mcp_oauth as mcp_oauth

    item = entry("n8n")
    assert item is not None
    built = catalogue_entry(item, {"host": "automation.example.com"})
    hand = {
        "name": "custom",
        "title": "Custom",
        "url": built["url"],
        "auth": "bearer",
    }
    monkeypatch.setenv("FLOTTA_MCP_SERVERS", json.dumps([built, hand]))
    lookups: list[str] = []

    def resolve(host, port):
        lookups.append(host)
        return [PUBLIC]

    monkeypatch.setattr(mcp_oauth, "resolve_host", resolve)
    ana = _headers(fleet)
    for name in ("n8n", "custom"):
        assert (
            api.put(
                f"/api/orgs/agency-x/mcp/{name}", json={"key": "agency-key"}, headers=ana
            ).status_code
            == 200
        )
    pinned = api.get("/api/orgs/agency-x/mcp/n8n/tools", headers=ana)
    assert pinned.status_code == 200, pinned.text
    sent = provider.requests[-1]
    assert sent.url.host == PUBLIC
    assert sent.headers["host"] == "automation.example.com"
    assert sent.extensions["sni_hostname"] == "automation.example.com"
    assert lookups == ["automation.example.com"]
    plain = api.get("/api/orgs/agency-x/mcp/custom/tools", headers=ana)
    assert plain.status_code == 200, plain.text
    assert provider.requests[-1].url.host == "automation.example.com"
    assert lookups == ["automation.example.com"]


def test_a_private_address_is_not_dialled(api, fleet, provider, monkeypatch):  # noqa: F811
    import flotta.mcp_oauth as mcp_oauth

    item = entry("n8n")
    assert item is not None
    built = catalogue_entry(item, {"host": "automation.example.com"})
    monkeypatch.setenv("FLOTTA_MCP_SERVERS", json.dumps([built]))
    monkeypatch.setattr(mcp_oauth, "resolve_host", lambda host, port: ["10.1.1.1"])
    ana = _headers(fleet)
    assert (
        api.put("/api/orgs/agency-x/mcp/n8n", json={"key": "agency-key"}, headers=ana).status_code
        == 200
    )
    answer = api.get("/api/orgs/agency-x/mcp/n8n/tools", headers=ana)
    assert answer.status_code == 502
    assert answer.json()["code"] == "upstream"
    assert "10.1.1.1" not in answer.text
    assert provider.requests == []


def test_a_token_inside_its_refresh_window_is_renewed_first(
    api, fleet, provider, monkeypatch  # noqa: F811
):
    import flotta.mcp_oauth as mcp_oauth

    monkeypatch.setattr(mcp_oauth, "resolve_host", lambda host, port: [PUBLIC])
    provider.refresh_tokens.add("rt-old")
    credential = oauth_credential(
        ENDPOINTS, Client("dyn-1", "dyn-secret"), Tokens("at-old", "rt-old", time.time() + 30)
    )
    with FleetStore(fleet) as store:
        store.set_mcp_connection(
            "agency-x",
            "tracker",
            kind="oauth",
            credential=_seal("agency-x", "tracker", credential),
            by="ana",
            server_url=MCP_URL,
        )
    answer = api.get("/api/orgs/agency-x/mcp/tracker/tools", headers=_headers(fleet))
    assert answer.status_code == 200, answer.text
    assert provider.refreshes == 1
    assert provider.requests[-1].headers["authorization"] == "Bearer at-1"
    assert "at-old" not in answer.text and "rt-old" not in answer.text
    assert [tool["name"] for tool in answer.json()["tools"]] == [
        "read_issue",
        "create_issue",
        "delete_everything",
    ]
