"""Adding a library connector writes a normal catalogue entry (FLOTTA-225)."""

from __future__ import annotations

import logging

from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN, mint
from flotta.control.test_mcp_api import (  # noqa: F401  (fixtures)
    CATALOGUE,
    api,
    machine,
    provider,
)
from flotta.control.test_mcp_catalogue import _operator, _save
from flotta.control.test_sign_in import KEY
from flotta.control.test_tickets import _as, fleet  # noqa: F401  (fixture)
from flotta.mcp_library import entry

SECRET = "sk_live_supersecretvalue"


def _member():
    token = mint(
        subject="ana",
        scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN},
        key=KEY,
        org="agency-x",
    )
    return {"Authorization": f"Bearer {token}"}


def test_the_library_is_readable_and_only_the_operator_can_add(api, fleet):  # noqa: F811
    listed = api.get("/api/mcp/library", headers=_as(fleet, "ana@example.com"))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["connectors"]] == [
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
    ]
    refused = api.post("/api/mcp/library/notion", json={"params": {}}, headers=_member())
    assert refused.status_code == 403
    missing = api.post("/api/mcp/library/no-such", json={"params": {}}, headers=_operator())
    assert missing.status_code == 404


def test_add_from_library_writes_a_catalogue_entry_and_keeps_custom_ones(api, fleet):  # noqa: F811
    added = api.post("/api/mcp/library/notion", json={"params": {}}, headers=_operator())
    assert added.status_code == 200, added.text
    body = added.json()
    assert body["added"] is True
    server = body["server"]
    assert server["name"] == "notion"
    assert server["auth"] == "oauth"
    assert server["url"] == "https://mcp.notion.com/mcp"
    assert server["ask_first"] is True
    assert server["has_client_secret"] is False
    assert "client_secret" not in server
    notion = entry("notion")
    assert notion is not None
    assert server["tools"] == [*notion.read_tools, *notion.write_tools]
    assert server["oauth"]["scopes"] == ["default"]
    assert "client_id" not in server["oauth"]

    catalogue = api.get("/api/mcp/catalogue", headers=_operator()).json()
    names = [item["name"] for item in catalogue["servers"]]
    assert names == [item["name"] for item in CATALOGUE] + ["notion"]
    assert catalogue["source"] == "store"

    again = api.post("/api/mcp/library/notion", json={"params": {}}, headers=_operator())
    assert again.status_code == 200 and again.json()["added"] is False
    again_names = [
        item["name"]
        for item in api.get("/api/mcp/catalogue", headers=_operator()).json()["servers"]
    ]
    assert again_names == names


def test_a_custom_entry_of_the_same_name_is_not_overwritten(api, fleet):  # noqa: F811
    custom = {
        "name": "notion",
        "title": "Marvin notes",
        "url": "https://marvin.example.com/mcp",
        "auth": "bearer",
    }
    assert _save(api, [*CATALOGUE, custom]).status_code == 200
    answer = api.post("/api/mcp/library/notion", json={"params": {}}, headers=_operator())
    assert answer.status_code == 409
    kept = api.get("/api/mcp/catalogue", headers=_operator()).json()["servers"]
    found = next(item for item in kept if item["name"] == "notion")
    assert found["url"] == "https://marvin.example.com/mcp"
    assert found["title"] == "Marvin notes"


def test_a_template_is_checked_and_a_secret_in_it_is_not_logged_or_answered(api, fleet, caplog):  # noqa: F811
    good = api.post(
        "/api/mcp/library/n8n",
        json={"params": {"host": "automation.example.com"}},
        headers=_operator(),
    )
    assert good.status_code == 200, good.text
    assert good.json()["server"]["url"] == "https://automation.example.com/mcp-server/http"
    assert good.json()["server"]["auth"] == "bearer"

    region = api.post(
        "/api/mcp/library/intercom",
        json={"params": {"host": "mcp.eu.intercom.com"}},
        headers=_operator(),
    )
    assert region.status_code == 200, region.text
    assert region.json()["server"]["url"] == "https://mcp.eu.intercom.com/mcp"

    with caplog.at_level(logging.DEBUG):
        refused = api.post(
            "/api/mcp/library/n8n",
            json={"params": {"host": f"automation.example.com/?token={SECRET}"}},
            headers=_operator(),
        )
    assert refused.status_code == 422
    assert SECRET not in refused.text
    assert SECRET not in caplog.text
    for host in ("127.0.0.1", "localhost", "10.0.0.5", "8.8.8.8", "metadata.google.internal"):
        answer = api.post(
            "/api/mcp/library/n8n", json={"params": {"host": host}}, headers=_operator()
        )
        assert answer.status_code == 422, host
        assert host not in answer.text
    wrong = api.post(
        "/api/mcp/library/intercom",
        json={"params": {"host": "mcp.au.intercom.com"}},
        headers=_operator(),
    )
    assert wrong.status_code == 422


def test_a_grant_defaults_to_read_tools_and_a_write_tool_is_named(api, fleet, machine):  # noqa: F811
    added = api.post("/api/mcp/library/notion", json={"params": {}}, headers=_operator())
    assert added.status_code == 200
    ana = _as(fleet, "ana@example.com")
    notion = entry("notion")
    assert notion is not None
    defaulted = api.put(
        "/api/boxes/b-support/mcp",
        json={"servers": [{"server": "notion"}]},
        headers=ana,
    )
    assert defaulted.status_code == 200, defaulted.text
    stored = next(item for item in defaulted.json()["servers"] if item["server"] == "notion")
    assert stored["tools"] == list(notion.read_tools)
    assert "notion-create-pages" not in stored["tools"]

    opted = api.put(
        "/api/boxes/b-support/mcp",
        json={"servers": [{"server": "notion", "tools": ["notion-search", "notion-create-pages"]}]},
        headers=ana,
    )
    assert opted.status_code == 200, opted.text
    stored = next(item for item in opted.json()["servers"] if item["server"] == "notion")
    assert stored["tools"] == ["notion-search", "notion-create-pages"]
