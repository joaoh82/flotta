"""The HQ connector library (FLOTTA-225): the manifest, its grants, and its URLs."""

from __future__ import annotations

import inspect
import json

import pytest

from flotta.mcp import Grant, McpError, parse_catalogue, parse_grants, parse_server
from flotta.mcp_library import (
    LibraryError,
    _host_allowed,
    catalogue_entry,
    classify_tool,
    entries,
    entry,
    parse_library,
    resolve_url,
)
from flotta.store import FleetStore, McpCall

V1 = (
    "notion",
    "linear",
    "atlassian",
    "airtable",
    "monday",
    "calendly",
    "stripe",
    "zapier",
    "make",
    "n8n",
    "intercom",
)
OUT_OF_SCOPE = ("hubspot", "canva", "google", "slack", "microsoft")


def test_the_v1_library_is_exactly_these_connectors():
    assert tuple(item.id for item in entries()) == V1
    ids = set(V1)
    assert not ids & set(OUT_OF_SCOPE)
    for item in entries():
        assert item.transport == "streamable-http"
        assert item.auth in ("oauth", "api_key")
        assert item.icon == item.id
        assert "\u2014" not in item.summary
        assert not (set(item.read_tools) & set(item.write_tools))


def test_a_library_document_that_breaks_the_schema_is_refused():
    raw = json.loads(Path_text())
    raw[0].pop("auth")
    with pytest.raises(LibraryError, match="missing a field"):
        parse_library(json.dumps(raw))
    raw = json.loads(Path_text())
    raw[0]["read_tools"] = raw[0]["write_tools"][:1]
    raw[0]["write_tools"] = raw[0]["read_tools"]
    with pytest.raises(LibraryError, match="both read and write"):
        parse_library(json.dumps(raw))


def Path_text() -> str:
    from pathlib import Path

    return Path(__file__).with_name("mcp_library.json").read_text()


def test_adding_a_fixed_connector_is_a_normal_catalogue_entry():
    notion = entry("notion")
    assert notion is not None
    built = catalogue_entry(notion, {})
    server = parse_server(built)
    assert server.name == "notion"
    assert server.url == "https://mcp.notion.com/mcp"
    assert server.auth == "oauth" and server.ask_first is True
    assert server.oauth is not None and server.oauth.client_id is None
    assert server.oauth.scopes == ("default",)
    assert server.tools == (*notion.read_tools, *notion.write_tools)
    assert "notion-search" in server.tools
    assert "notion-create-pages" in server.tools


def test_an_api_key_connector_is_stored_as_bearer_and_names_no_secret():
    n8n = entry("n8n")
    assert n8n is not None and n8n.auth == "api_key"
    built = catalogue_entry(n8n, {"host": "automation.example.com"})
    server = parse_server(built)
    assert server.auth == "bearer" and server.header is None
    assert server.url == "https://automation.example.com/mcp-server/http"
    assert built["url"].endswith("/mcp-server/http")
    assert "token" not in json.dumps(built)


def test_an_omitted_grant_is_the_read_tools_and_a_write_tool_is_opt_in():
    notion = entry("notion")
    assert notion is not None
    server = parse_server(catalogue_entry(notion, {}))
    servers = {server.name: server}
    defaulted = parse_grants({"servers": [{"server": "notion"}]}, servers)
    assert defaulted == (Grant("notion", notion.read_tools, True),)
    assert "notion-create-pages" not in defaulted[0].tools
    opted = parse_grants(
        {"servers": [{"server": "notion", "tools": ["notion-search", "notion-create-pages"]}]},
        servers,
    )
    assert opted[0].tools == ("notion-search", "notion-create-pages")
    with pytest.raises(McpError, match="doesn't offer"):
        parse_grants(
            {"servers": [{"server": "notion", "tools": ["notion-delete-workspace"]}]},
            servers,
        )


def test_a_hand_written_entry_keeps_every_tool_it_already_allowed():
    """The existing `notion` fixture points at Notion and lists no tools: still every tool."""
    custom = parse_catalogue(
        json.dumps(
            [
                {"name": "notion", "url": "https://mcp.notion.com/mcp", "auth": "bearer"},
                {"name": "marvin", "url": "https://marvin.example.com/mcp", "auth": "bearer"},
            ]
        )
    )
    grants = parse_grants({"servers": [{"server": "notion"}, {"server": "marvin"}]}, custom)
    assert grants == (Grant("notion", None, True), Grant("marvin", None, True))


def test_linear_and_make_say_no_tools_are_enabled_yet():
    by_id = {item.id: item.public() for item in entries()}
    assert by_id["linear"]["tools_enabled"] is False
    assert by_id["linear"]["read_tools"] == []
    assert by_id["linear"]["dynamic_tools"] is True
    assert by_id["make"]["tools_enabled"] is False
    assert by_id["notion"]["tools_enabled"] is True
    n8n = entry("n8n")
    assert n8n is not None
    for name in ("list_credentials", "get_workflow_execution", "explore_node_resources"):
        assert name in n8n.write_tools
        assert name not in n8n.read_tools


def test_a_name_like_nip_io_is_left_to_the_resolver():
    item = entry("n8n")
    assert item is not None
    assert resolve_url(item, {"host": "127.0.0.1.nip.io"}) == (
        "https://127.0.0.1.nip.io/mcp-server/http"
    )


def test_a_spec_that_matches_the_library_is_one_even_without_the_marker():
    """An entry added before `library: true` existed is still that connector."""
    linear = entry("linear")
    assert linear is not None
    built = catalogue_entry(linear, {})
    built.pop("library")
    server = parse_server(built)
    assert server.library is False
    grants = parse_grants({"servers": [{"server": "linear"}]}, {"linear": server})
    assert grants == (Grant("linear", (), True),)


def test_a_hand_written_linear_and_n8n_still_grant_every_tool_when_tools_are_omitted():
    """The example in `mcp.py`, and any n8n entry written by hand: no library marker."""
    linear = parse_server(
        {
            "name": "linear",
            "title": "Linear",
            "url": "https://mcp.linear.app/mcp",
            "auth": "oauth",
            "tools": ["list_issues", "create_issue"],
            "oauth": {"scopes": ["read", "write"]},
        }
    )
    n8n = parse_server(
        {
            "name": "n8n",
            "title": "n8n",
            "url": "https://automation.example.com/mcp-server/http",
            "auth": "bearer",
        }
    )
    assert not linear.library and not n8n.library
    grants = parse_grants(
        {"servers": [{"server": "linear"}, {"server": "n8n"}]},
        {"linear": linear, "n8n": n8n},
    )
    assert grants == (Grant("linear", None, True), Grant("n8n", None, True))
    assert grants[0].allows(linear, "create_issue")
    assert grants[1].allows(n8n, "list_credentials")


def test_a_dynamic_connector_grants_nothing_until_a_tool_is_named():
    linear = entry("linear")
    assert linear is not None and linear.dynamic_tools
    server = parse_server(catalogue_entry(linear, {}))
    assert server.tools is None
    defaulted = parse_grants({"servers": [{"server": "linear"}]}, {"linear": server})
    assert defaulted == (Grant("linear", (), True),)
    assert not defaulted[0].allows(server, "create_issue")
    named = parse_grants(
        {"servers": [{"server": "linear", "tools": ["list_issues"]}]}, {"linear": server}
    )
    assert named[0].tools == ("list_issues",)
    assert named[0].allows(server, "list_issues")


def test_a_same_name_pointing_somewhere_else_is_not_the_library_connector():
    other = parse_server(
        {"name": "notion", "url": "https://notes.example.test/mcp", "auth": "bearer"}
    )
    assert parse_grants({"servers": [{"server": "notion"}]}, {"notion": other}) == (
        Grant("notion", None, True),
    )


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "localhost",
        "10.0.0.5",
        "8.8.8.8",
        "169.254.169.254",
        "[::1]",
        "metadata.google.internal",
        "printer.local",
        "n8n.localhost",
        "printer.lan",
        "nas.home.arpa",
        "n8n.default.svc",
        "files.corp",
        "box.internal",
        "user:secret@automation.example.com",
        "automation.example.com/?token=sk_live_supersecretvalue",
        "https://automation.example.com",
        "automation.example.com:8443",
    ],
)
def test_a_template_host_that_is_not_a_public_name_is_refused(host):
    item = entry("n8n")
    assert item is not None
    secret = "sk_live_supersecretvalue"
    with pytest.raises(LibraryError, match="public DNS name") as caught:
        resolve_url(item, {"host": host})
    assert secret not in str(caught.value)
    assert host not in str(caught.value)


def test_an_allowed_host_pattern_is_exact_and_a_wildcard_cannot_reach_a_neighbour():
    assert _host_allowed("mcp.intercom.com", ("mcp.intercom.com", "mcp.eu.intercom.com"))
    assert not _host_allowed("mcp.intercom.com.evil.com", ("mcp.intercom.com",))
    assert not _host_allowed("evil.mcp.intercom.com", ("mcp.intercom.com",))
    assert not _host_allowed("mcp.au.intercom.com", ("mcp.intercom.com", "mcp.eu.intercom.com"))
    assert _host_allowed("automation.example.com", ("*",))
    assert _host_allowed("a.example.com", ("*.example.com",))
    assert not _host_allowed("example.com", ("*.example.com",))
    assert not _host_allowed("a.example.com.evil.com", ("*.example.com",))


def test_intercom_accepts_its_two_regions_and_no_other_host():
    item = entry("intercom")
    assert item is not None
    assert resolve_url(item, {"host": "mcp.eu.intercom.com"}) == "https://mcp.eu.intercom.com/mcp"
    with pytest.raises(LibraryError, match="not allowed"):
        resolve_url(item, {"host": "mcp.au.intercom.com"})


def test_an_annotation_classifies_a_tool_and_an_unknown_one_is_write():
    known = frozenset({"search_workflows"})
    assert classify_tool("search_workflows", known_read=known) == "read"
    assert classify_tool("execute_workflow", read_only_hint=False, known_read=known) == "write"
    assert classify_tool("search_workflows", read_only_hint=False, known_read=known) == "write"
    assert classify_tool("brand_new_tool", read_only_hint=True, known_read=known) == "read"
    assert classify_tool("brand_new_tool", known_read=known) == "write"


def test_an_mcp_call_records_the_tool_and_not_what_was_sent():
    assert set(McpCall.__dataclass_fields__) == {
        "id",
        "box_id",
        "org_id",
        "client_id",
        "server",
        "tool",
        "status",
        "at",
        "account",
    }
    source = inspect.getsource(FleetStore.record_mcp_call)
    assert "argument" not in source
    assert "INSERT INTO mcp_calls" in source
