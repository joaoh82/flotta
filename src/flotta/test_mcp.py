"""The MCP catalogue, grants, and what reaches an agent's `config.yaml` (FLOTTA-125)."""

from __future__ import annotations

import json

import pytest
import yaml

from flotta.box import rules as box_rules
from flotta.mcp import (
    Grant,
    McpError,
    config_changes,
    mcp_base,
    parse_catalogue,
    parse_grants,
    parse_key,
)

LINEAR = {
    "name": "linear",
    "title": "Linear",
    "url": "https://mcp.linear.app/mcp",
    "auth": "oauth",
    "tools": ["list_issues", "create_issue"],
}
NOTION = {"name": "notion", "url": "https://mcp.notion.com/mcp", "auth": "bearer"}
SERVERS = parse_catalogue(json.dumps([LINEAR, NOTION]))


def test_the_catalogue_reads_the_operators_servers():
    assert set(SERVERS) == {"linear", "notion"}
    assert SERVERS["linear"].tools == ("list_issues", "create_issue")
    assert SERVERS["notion"].title == "notion" and SERVERS["notion"].ask_first is True
    assert parse_catalogue(None) == {} and parse_catalogue("  ") == {}


@pytest.mark.parametrize(
    ("entry", "says"),
    [
        ({**NOTION, "url": "http://mcp.notion.com/mcp"}, "https"),
        ({**NOTION, "url": "https://user:pw@mcp.notion.com/mcp"}, "user or password"),
        ({**NOTION, "auth": "magic"}, "auth is one of"),
        ({**NOTION, "name": "Notion"}, "lowercase"),
        ({**NOTION, "auth": "api_key"}, "names the header"),
        ({**NOTION, "auth": "api_key", "header": "Cookie"}, "can't go in"),
        ({**NOTION, "header": "X-Key"}, "only an api_key"),
        ({**NOTION, "tools": ["read_*"]}, "tool names"),
        ({**NOTION, "command": "npx thing"}, "unknown fields"),
        ({**LINEAR, "oauth": {"client_secret_env": "FLOTTA_SIGNING_KEY"}}, "FLOTTA_MCP_"),
    ],
)
def test_an_entry_that_cant_be_used_is_refused(entry, says):
    with pytest.raises(McpError, match=says):
        parse_catalogue(json.dumps([entry]))


def test_a_local_server_may_be_plain_http():
    found = parse_catalogue(json.dumps([{**NOTION, "url": "http://127.0.0.1:9000/mcp"}]))
    assert found["notion"].url == "http://127.0.0.1:9000/mcp"


def test_a_name_twice_is_refused():
    with pytest.raises(McpError, match="twice"):
        parse_catalogue(json.dumps([NOTION, NOTION]))


def test_grants_read_from_a_body():
    grants = parse_grants(
        {"servers": [{"server": "linear", "tools": ["list_issues"], "ask_first": False}]}, SERVERS
    )
    assert grants == (Grant("linear", ("list_issues",), False),)
    defaulted = parse_grants({"servers": [{"server": "notion"}]}, SERVERS)
    assert defaulted == (Grant("notion", None, True),)


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ({"servers": [{"server": "github"}]}, "not one of"),
        ({"servers": [{"server": "linear", "tools": ["delete_workspace"]}]}, "doesn't offer"),
        ({"servers": [{"server": "notion", "tools": ["*"]}]}, "tool names"),
        ({"servers": [{"server": "notion", "tools": []}]}, "list of tool names"),
        ({"servers": [{"server": "notion"}, {"server": "notion"}]}, "twice"),
        ({"servers": [{"server": "notion", "trust": "full"}]}, "each grant"),
        ({"grants": []}, "servers"),
    ],
)
def test_a_grant_that_cant_be_kept_is_refused(body, says):
    with pytest.raises(McpError, match=says):
        parse_grants(body, SERVERS)


def test_a_grant_allows_its_tools_and_the_catalogue_bounds_one_that_names_none():
    linear, notion = SERVERS["linear"], SERVERS["notion"]
    assert Grant("linear", ("list_issues",), True).allows(linear, "list_issues")
    assert not Grant("linear", ("list_issues",), True).allows(linear, "create_issue")
    assert Grant("linear", None, True).allows(linear, "create_issue")
    assert not Grant("linear", None, True).allows(linear, "delete_workspace")
    assert Grant("notion", None, True).allows(notion, "anything_it_lists")


def _written(config, changes, tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    box_rules.apply(tmp_path, changes)
    return yaml.safe_load((tmp_path / "config.yaml").read_text())


def test_a_grant_becomes_an_entry_pointing_at_the_gateway_with_the_boxs_own_token(tmp_path):
    changes = config_changes(
        [Grant("linear", ("list_issues",), True)],
        SERVERS,
        base="https://gw.example",
        token="flotta_box_token",
    )
    config = _written({"model": {"default": "x"}}, changes, tmp_path)
    assert config["mcp_servers"] == {
        "flotta-linear": {
            "url": "https://gw.example/mcp/linear",
            "headers": {"Authorization": "Bearer flotta_box_token"},
            "tools": {"include": ["list_issues"]},
            "trust": "untrusted",
        }
    }
    assert config["model"] == {"default": "x"}


def test_a_grant_taken_away_leaves_no_entry_and_the_agents_own_stay(tmp_path):
    before = {
        "mcp_servers": {
            "flotta-notion": {"url": "https://gw.example/mcp/notion"},
            "flotta-retired": {"url": "https://gw.example/mcp/retired"},
            "mine": {"command": "my-server"},
        }
    }
    changes = config_changes(
        [Grant("linear", None, False)],
        SERVERS,
        base="https://gw.example",
        token="t",
        previous=["notion", "retired"],
    )
    config = _written(before, changes, tmp_path)
    assert set(config["mcp_servers"]) == {"flotta-linear", "mine"}
    linear = config["mcp_servers"]["flotta-linear"]
    assert "trust" not in linear
    assert linear["tools"] == {"include": ["list_issues", "create_issue"]}


def test_no_grants_at_all_takes_the_section_away(tmp_path):
    changes = config_changes([], SERVERS, base="https://gw.example", token="t")
    assert "mcp_servers" not in _written({"mcp_servers": {"flotta-linear": {}}}, changes, tmp_path)


def test_nothing_of_the_upstream_reaches_the_config():
    changes = config_changes(
        [Grant("linear", None, True)], SERVERS, base="https://gw.example", token="t"
    )
    assert "linear.app" not in json.dumps(changes)


@pytest.mark.parametrize(
    ("url", "base"),
    [
        ("https://gw.example/v1", "https://gw.example"),
        ("https://gw.example/", "https://gw.example"),
    ],
)
def test_the_gateway_base_drops_the_models_path(url, base):
    assert mcp_base({"FLOTTA_GATEWAY_URL": url}) == base
    assert mcp_base({}) is None


@pytest.mark.parametrize("key", ["", "  ", "a\nb", "x" * 5000])
def test_a_key_that_cant_go_in_a_header_is_refused(key):
    with pytest.raises(McpError):
        parse_key({"key": key}, SERVERS["notion"])


def test_an_oauth_server_takes_no_key():
    with pytest.raises(McpError, match="signing in"):
        parse_key({"key": "k"}, SERVERS["linear"])
