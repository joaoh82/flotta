"""GET /api/orgs/{org}/mcp: the library marker and the connection enum (FLOTTA-225).

Both are added beside the fields the list already returned. A hand-written
server is not a library connector because of its name or its host, and a
broken connection is not `expired` because its detail says so.
"""

from __future__ import annotations

import json
import time

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

HAND_LINEAR = {
    "name": "linear",
    "title": "Linear",
    "url": "https://mcp.linear.app/mcp",
    "auth": "oauth",
    "tools": ["list_issues", "create_issue"],
    "oauth": {"scopes": ["read", "write"]},
}
HAND_N8N = {
    "name": "n8n",
    "title": "n8n",
    "url": "https://automation.example.com/mcp-server/http",
    "auth": "bearer",
}
CONNECTION_FIELDS = {
    "kind",
    "status",
    "detail",
    "connected_by",
    "connected_by_name",
    "connected_at",
    "updated_at",
}


def _servers(client, db, catalogue=None, monkeypatch=None):
    if catalogue is not None:
        assert monkeypatch is not None
        monkeypatch.setenv("FLOTTA_MCP_SERVERS", json.dumps(catalogue))
    answer = client.get("/api/orgs/agency-x/mcp", headers=_as(db, "ana@example.com"))
    assert answer.status_code == 200, answer.text
    return {server["name"]: server for server in answer.json()["servers"]}


def test_a_library_server_names_its_manifest_id(api, fleet, monkeypatch):  # noqa: F811
    linear = entry("linear")
    assert linear is not None
    servers = _servers(api, fleet, [catalogue_entry(linear, {})], monkeypatch)
    assert servers["linear"] == {
        "name": "linear",
        "title": "Linear",
        "url": "https://mcp.linear.app/mcp",
        "auth": "oauth",
        "ask_first": True,
        "tools": None,
        "library": True,
        "library_id": "linear",
        "status": "not_connected",
        "connection": None,
        "clients": {},
    }


def test_a_hand_written_linear_and_n8n_are_not_library_connectors(api, fleet, monkeypatch):  # noqa: F811
    """The same name and host as the library, and no marker."""
    servers = _servers(api, fleet, [HAND_LINEAR, HAND_N8N], monkeypatch)
    assert servers["linear"]["library"] is False
    assert "library_id" not in servers["linear"]
    assert servers["linear"]["tools"] == ["list_issues", "create_issue"]
    assert servers["linear"]["url"] == "https://mcp.linear.app/mcp"
    assert servers["n8n"]["library"] is False
    assert "library_id" not in servers["n8n"]
    assert servers["n8n"]["status"] == "not_connected"
    assert servers["n8n"]["connection"] is None


def test_a_matching_spec_without_the_marker_is_still_hand_written(api, fleet, monkeypatch):  # noqa: F811
    linear = entry("linear")
    assert linear is not None
    built = catalogue_entry(linear, {})
    built.pop("library")
    servers = _servers(api, fleet, [built], monkeypatch)
    assert servers["linear"]["library"] is False
    assert "library_id" not in servers["linear"]


def test_each_connection_status_and_the_old_fields(api, fleet):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    before = _servers(api, fleet)["notes"]
    assert before["status"] == "not_connected"
    assert before["connection"] is None
    assert before["library"] is False
    assert before["name"] == "notes"
    assert before["auth"] == "bearer"
    assert before["tools"] == ["read_note", "write_note"]

    connected = api.put("/api/orgs/agency-x/mcp/notes", json={"key": "notes-secret"}, headers=ana)
    assert connected.status_code == 200, connected.text
    notes = _servers(api, fleet)["notes"]
    assert notes["status"] == "connected"
    assert notes["connection"]["status"] == "ok"
    assert notes["connection"]["detail"] is None
    assert notes["connection"]["kind"] == "bearer"
    assert set(notes["connection"]) == CONNECTION_FIELDS
    assert "notes-secret" not in json.dumps(notes)
    # What the list returned before this change is still there, unchanged.
    assert notes["url"] == "https://notes.example.test/mcp"
    assert notes["ask_first"] is True
    assert notes["title"] == "notes"
    assert notes["clients"] == {}

    with FleetStore(fleet) as store:
        store.break_mcp_connection("agency-x", "notes", "expired, with no way to refresh")
    refused = _servers(api, fleet)["notes"]
    assert refused["status"] == "error"
    assert refused["connection"]["status"] == "broken"
    assert refused["connection"]["detail"] == "expired, with no way to refresh"

    body = oauth_credential(
        Endpoints("https://auth.example.test/authorize", "https://auth.example.test/token"),
        Client("dyn-1"),
        Tokens("at-old", None, time.time() - 60),
    )
    sealed = seal(
        json.dumps(body),
        context=connection_context("agency-x", "tracker"),
        env={"FLOTTA_SECRETS_KEY": SECRETS, "FLOTTA_SIGNING_KEY": KEY},
    )
    with FleetStore(fleet) as store:
        store.set_mcp_connection(
            "agency-x",
            "tracker",
            kind="oauth",
            credential=sealed,
            by="ana",
            server_url=MCP_URL,
        )
        store.break_mcp_connection("agency-x", "tracker", "the provider refused")
    expired = _servers(api, fleet)["tracker"]
    assert expired["status"] == "expired"
    assert expired["connection"]["status"] == "broken"
    assert expired["connection"]["detail"] == "the provider refused"
    assert "at-old" not in json.dumps(expired)


def test_the_failure_flag_is_expired_even_before_the_clock(api, fleet):  # noqa: F811
    body = oauth_credential(
        Endpoints("https://auth.example.test/authorize", "https://auth.example.test/token"),
        Client("dyn-1"),
        Tokens("at-old", None, time.time() + 3600),
    )
    body = {**body, "failure": "expired"}
    sealed = seal(
        json.dumps(body),
        context=connection_context("agency-x", "tracker"),
        env={"FLOTTA_SECRETS_KEY": SECRETS, "FLOTTA_SIGNING_KEY": KEY},
    )
    with FleetStore(fleet) as store:
        store.set_mcp_connection(
            "agency-x",
            "tracker",
            kind="oauth",
            credential=sealed,
            by="ana",
            server_url=MCP_URL,
        )
        store.break_mcp_connection("agency-x", "tracker", "could not be renewed")
    tracker = _servers(api, fleet)["tracker"]
    assert tracker["status"] == "expired"
    assert tracker["connection"]["detail"] == "could not be renewed"


def test_a_refresh_token_that_was_refused_is_an_error(api, fleet):  # noqa: F811
    body = oauth_credential(
        Endpoints("https://auth.example.test/authorize", "https://auth.example.test/token"),
        Client("dyn-1"),
        Tokens("at-old", "rt-old", time.time() - 60),
    )
    sealed = seal(
        json.dumps(body),
        context=connection_context("agency-x", "tracker"),
        env={"FLOTTA_SECRETS_KEY": SECRETS, "FLOTTA_SIGNING_KEY": KEY},
    )
    with FleetStore(fleet) as store:
        store.set_mcp_connection(
            "agency-x",
            "tracker",
            kind="oauth",
            credential=sealed,
            by="ana",
            server_url=MCP_URL,
        )
        store.break_mcp_connection("agency-x", "tracker", "expired, with no way to refresh")
    tracker = _servers(api, fleet)["tracker"]
    assert tracker["status"] == "error"
    assert tracker["connection"]["status"] == "broken"
    assert "rt-old" not in json.dumps(tracker)
