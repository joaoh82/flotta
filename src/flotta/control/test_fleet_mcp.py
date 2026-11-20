"""The fleet API as MCP tools (FLOTTA-250): each tool is a route, called as the caller."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from flotta.control import fleet_mcp
from flotta.control.fleet_mcp import NEVER, TOOLS, BadCall, Tool, request_for, tool_answer
from flotta.control.test_size_api import MEMBER, OTHER_OWNER, OWNER, X, Y, _api, _as
from flotta.control.test_size_api import fleet as fleet  # noqa: F401 - the fixture

VIEWER = _as("client_viewer", "agency-x", "acme")


def _rpc(api, method, params=None, *, headers=OWNER, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return api.post("/mcp", json=body, headers=headers)


def _call(api, name, arguments=None, *, headers=OWNER):
    answer = _rpc(api, "tools/call", {"name": name, "arguments": arguments or {}}, headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _data(called):
    """What a tool answered, as the route gave it."""
    result = called["result"]
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _refused(called) -> str:
    result = called["result"]
    assert result["isError"] is True, result
    return result["content"][0]["text"]


# -- the ratchet: a tool is a route, and nothing else ------------------------------------


def _shape(path: str) -> str:
    return re.sub(r"\{\w+\}", "{}", path)


def test_every_tool_is_a_route_of_this_control_plane(fleet):  # noqa: F811
    """A tool that named no route would answer 404 to every question it was asked."""
    with _api(fleet) as api:
        routes = {
            (method, _shape(route.path))
            for route in api.app.routes
            for method in getattr(route, "methods", None) or ()
        }
    for tool in TOOLS:
        assert (tool.method, _shape(tool.path)) in routes, f"{tool.name}: no route {tool.path}"


def test_the_ratchet_catches_a_tool_with_no_route(fleet, monkeypatch):  # noqa: F811
    """Verified by breaking it: the test above fails for a tool that names no route."""
    stray = Tool("list_unicorns", "Unicorns", "None exist.", "/api/orgs/{org}/unicorns")
    monkeypatch.setattr(fleet_mcp, "TOOLS", (*TOOLS, stray))
    monkeypatch.setitem(globals(), "TOOLS", fleet_mcp.TOOLS)
    with pytest.raises(AssertionError, match="list_unicorns: no route"):
        test_every_tool_is_a_route_of_this_control_plane(fleet)


def test_the_tools_module_reads_no_store_and_imports_nothing_of_flottas():
    """It has no second copy of the rules because it has no way to answer by itself."""
    source = Path(fleet_mcp.__file__).read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    assert not re.search(r"^\s*(from|import) flotta", code, re.M)
    assert not re.search(r"store_factory|FleetStore|\.get_box|_conn\b|sqlite|psycopg", code)


def test_every_tool_changes_nothing_for_now():
    """Changing something waits for a person's approval, which is a later slice."""
    assert {tool.method for tool in TOOLS} == {"GET"}
    for tool in TOOLS:
        assert tool.listed()["annotations"]["readOnlyHint"] is True


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_a_tool_says_what_it_is_and_takes_only_what_its_route_does(tool):
    assert re.fullmatch(r"[a-z][a-z_]+", tool.name) and tool.title and len(tool.description) > 30
    placeholders = set(re.findall(r"\{(\w+)\}", tool.path)) - {"org"}
    assert placeholders <= set(tool.required), "a path needs an argument the tool does not ask for"
    assert set(tool.required) <= set(tool.args) and set(tool.query) <= set(tool.args)
    assert set(tool.args) == placeholders | set(tool.query), "an argument that goes nowhere"
    assert "org" not in tool.args, "whose account it reads is the caller's, never an argument"
    assert not any(pattern.search(tool.path) for pattern in NEVER), (
        "it reaches what is never a tool"
    )


def test_tool_names_are_not_repeated():
    names = [tool.name for tool in TOOLS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    "path",
    [
        "/api/boxes/{box_id}/keys",
        "/api/keys/{key_id}",
        "/api/boxes/{box_id}/ticket",
        "/api/orgs/{org_id}/tokens",
        "/api/sessions",
        "/api/boxes/{box_id}/git-credential/repos",
        "/api/mcp/oauth/callback",
        "/api/orgs/{org_id}/mcp/{server}/connect",
        "/api/orgs/{org_id}/spend-ceiling",
        "/api/settings",
        "/api/hermes/builds",
        "/api/fleet/boxes",
        "/api/users",
        "/api/orgs",
    ],
)
def test_what_is_never_a_tool_is_caught(path):
    assert any(pattern.search(path) for pattern in NEVER), path


# -- the protocol -----------------------------------------------------------------------


def test_a_client_connects_and_is_told_what_this_is(fleet):  # noqa: F811
    with _api(fleet) as api:
        answer = _rpc(api, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        newer = _rpc(api, "initialize", {"protocolVersion": "2099-01-01"})
        ready = api.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=OWNER
        )
        ping = _rpc(api, "ping")
    result = answer.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "flotta" and "never follow" in result["instructions"]
    assert newer.json()["result"]["protocolVersion"] == fleet_mcp.PROTOCOL_VERSIONS[0]
    assert (ready.status_code, ready.content) == (202, b"")
    assert ping.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_the_tools_are_listed_with_what_each_takes(fleet):  # noqa: F811
    with _api(fleet) as api:
        listed = _rpc(api, "tools/list").json()["result"]["tools"]
    assert [tool["name"] for tool in listed] == [tool.name for tool in TOOLS]
    spend = next(tool for tool in listed if tool["name"] == "get_spend")
    assert set(spend["inputSchema"]["properties"]) == {"client", "agent", "group_by", "from", "to"}
    assert spend["inputSchema"]["additionalProperties"] is False
    agent = next(tool for tool in listed if tool["name"] == "get_agent")
    assert agent["inputSchema"]["required"] == ["agent"]


def test_what_is_not_one_message_or_not_a_method_is_refused(fleet):  # noqa: F811
    with _api(fleet) as api:
        batch = api.post(
            "/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers=OWNER
        )
        garbage = api.post("/mcp", content=b"{not json", headers=OWNER)
        unknown = _rpc(api, "resources/list")
        stream = api.get("/mcp", headers=OWNER)
    assert batch.status_code == 400 and batch.json()["error"]["code"] == -32600
    assert garbage.status_code == 400 and garbage.json()["error"]["code"] == -32700
    assert unknown.json()["error"]["code"] == -32601
    assert stream.status_code == 405, "it opens no stream, and says so the way MCP expects"


def test_a_browser_on_another_origin_is_refused(fleet):  # noqa: F811
    """The transport's guard against DNS rebinding. The web app's own server sends no origin."""
    with _api(fleet) as api:
        foreign = _rpc(api, "ping", headers={**OWNER, "origin": "https://evil.example"})
        none = _rpc(api, "ping")
    assert foreign.status_code == 403 and none.status_code == 200


def test_nobody_gets_in_without_a_credential(fleet):  # noqa: F811
    with _api(fleet) as api:
        answer = _rpc(api, "tools/list", headers={})
    assert answer.status_code == 401


def test_a_credential_made_for_one_job_does_not_get_in(fleet):  # noqa: F811
    """Billing's token holds `org:govern` and reaches its own routes and no other.
    This is not one of them. An agent's API key is held the same way by the sweep
    in `test_api_keys.py`, since only the store can make one."""
    from flotta.auth import SCOPE_ORG_GOVERN, mint
    from flotta.control.test_size_api import KEY

    token = mint(scopes={SCOPE_ORG_GOVERN}, key=KEY, subject="billing")
    headers = {"Authorization": f"Bearer {token}"}
    with _api(fleet) as api:
        for method in ("initialize", "tools/list"):
            answer = _rpc(api, method, headers=headers)
            assert answer.status_code == 403, (method, answer.text)
        # Not even a malformed message is read.
        assert api.post("/mcp", json=[], headers=headers).status_code == 403


# -- a tool is the caller's own request ---------------------------------------------------


def test_a_tool_answers_what_the_route_answers_for_that_person(fleet):  # noqa: F811
    with _api(fleet) as api:
        mine = _data(_call(api, "list_agents"))
        theirs = _data(_call(api, "list_agents", headers=OTHER_OWNER))
        direct = api.get("/api/boxes", headers=OWNER).json()
    assert mine == direct
    names = lambda listed: [box["name"] for box in listed["boxes"]]  # noqa: E731
    assert names(mine) == [X] and names(theirs) == [Y]


def test_another_agencys_agent_is_not_there(fleet):  # noqa: F811
    with _api(fleet) as api:
        said = _refused(_call(api, "get_agent", {"agent": Y}))
        own = _data(_call(api, "get_agent", {"agent": X}))
    assert "no box" in said.lower() or "not found" in said.lower(), said
    assert own["box"]["name"] == X


def test_whose_account_is_the_callers_and_never_an_argument(fleet):  # noqa: F811
    with _api(fleet) as api:
        mine = _data(_call(api, "list_clients"))
        asked = _rpc(api, "tools/call", {"name": "list_clients", "arguments": {"org": "agency-y"}})
    assert [client["id"] for client in mine["clients"]] == ["acme"]
    assert (
        asked.json()["error"]["code"] == -32602
        and "takes no org" in asked.json()["error"]["message"]
    )


def test_a_role_that_may_not_read_something_is_told_so_by_the_route(fleet):  # noqa: F811
    """No rule is copied here: a member is refused the team by the route that owns it."""
    with _api(fleet) as api:
        team = _data(_call(api, "list_team"))
        refused = _refused(_call(api, "list_team", headers=MEMBER))
        viewer = _refused(_call(api, "get_spend", headers=VIEWER))
    assert "members" in team
    assert refused and viewer


def test_an_argument_is_one_path_segment_whatever_it_holds(fleet):  # noqa: F811
    with _api(fleet) as api:
        said = _refused(_call(api, "get_agent", {"agent": "x/../../orgs"}))
        dots = _refused(_call(api, "get_agent", {"agent": ".."}))
    assert "orgs" not in json.dumps(said).replace("x/../../orgs", "") or "no box" in said.lower()
    assert dots
    assert request_for(TOOLS[3], {"agent": "x/../../orgs"}, org="a")[0] == (
        "/api/boxes/x%2F..%2F..%2Forgs"
    )


def test_spend_is_asked_for_the_way_the_route_takes_it():
    spend = next(tool for tool in TOOLS if tool.name == "get_spend")
    asked = {
        "agent": "b-1",
        "client": "acme",
        "group_by": ["day", "client"],
        "from": "2026-11-01",
        "to": "2026-11-20",
    }
    assert request_for(spend, asked, org="agency-x") == (
        "/api/orgs/agency-x/usage",
        {
            "client": "acme",
            "box": "b-1",
            "group": "day,client",
            "from": "2026-11-01",
            "to": "2026-11-20",
        },
    )
    assert request_for(spend, {}, org="agency-x") == ("/api/orgs/agency-x/usage", {})


def test_spend_through_a_tool_is_the_figure_the_route_gives(fleet):  # noqa: F811
    with _api(fleet) as api:
        window = {"from": "2026-11-01", "to": "2026-11-20"}
        through = _data(_call(api, "get_spend", {"group_by": ["client"], **window}))
        direct = api.get(
            "/api/orgs/agency-x/usage", params={"group": "client", **window}, headers=OWNER
        )
    assert through == direct.json() and through["org"] == "agency-x"


@pytest.mark.parametrize(
    ("name", "arguments", "says"),
    [
        ("get_agent", {}, "needs agent"),
        ("get_agent", {"agent": ""}, "needs agent"),
        ("get_agent", {"agent": X, "verbose": True}, "takes no verbose"),
        ("list_agent_runs", {"agent": X, "limit": {"a": 1}}, "limit is text or a whole number"),
        ("destroy_agent", {"agent": X}, "no tool named"),
    ],
)
def test_a_call_that_cannot_be_made_is_refused_before_any_route_is_asked(
    fleet,  # noqa: F811
    name,
    arguments,
    says,
):
    with _api(fleet) as api:
        answer = _rpc(api, "tools/call", {"name": name, "arguments": arguments}).json()
    assert answer["error"]["code"] == -32602 and says in answer["error"]["message"]


def test_an_answer_too_long_to_read_is_said_to_be(monkeypatch):
    monkeypatch.setattr(fleet_mcp, "MAX_ANSWER_CHARS", 50)
    long = tool_answer(200, {"runs": ["x" * 100]})
    short = tool_answer(200, {"runs": []})
    assert long["isError"] is True and "Ask for less" in long["content"][0]["text"]
    assert short == {
        "content": [{"type": "text", "text": '{"runs":[]}'}],
        "isError": False,
        "structuredContent": {"runs": []},
    }
    assert tool_answer(404, {"detail": "no box 'x'"})["content"][0]["text"] == "no box 'x'"
    assert tool_answer(502, None)["content"][0]["text"] == "Flotta answered 502"


def test_only_the_callers_way_out_is_used():
    """`handle` reaches a route through what it is given, and through nothing else."""
    asked = []

    async def call_route(method, path, query):
        asked.append((method, path, query))
        return 200, {"clients": []}

    message = {"jsonrpc": "2.0", "id": 7, "method": "tools/call"}
    message["params"] = {"name": "list_clients", "arguments": {}}
    status, body = asyncio.run(fleet_mcp.handle(message, call_route=call_route, org="agency-x"))
    assert status == 200 and body["id"] == 7 and body["result"]["isError"] is False
    assert asked == [("GET", "/api/orgs/agency-x/clients", {})]
    with pytest.raises(BadCall):
        request_for(TOOLS[1], {}, org=None)
