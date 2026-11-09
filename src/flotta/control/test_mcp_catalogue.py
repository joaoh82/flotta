"""The MCP catalogue edited in the console (FLOTTA-125).

The list is a fleet setting beside the model list: a stored value wins, the
environment is the fallback, and only the operator writes it. A client secret is
set on its own and never read back. A connection is never sent to an address it
wasn't made for, and a change reaches every agent granted the server.
"""

from __future__ import annotations

import json

from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN, mint
from flotta.control.test_mcp_api import (  # noqa: F401  (fixtures)
    CATALOGUE,
    _callback,
    _carried,
    _connection,
    api,
    machine,
    provider,
    public_names,
)
from flotta.control.test_sign_in import KEY
from flotta.control.test_tickets import _as, fleet  # noqa: F401  (fixture)
from flotta.gateway.fake_mcp import MCP_URL
from flotta.mcp import server_secret_context
from flotta.sealed import unseal
from flotta.store import FleetStore

STORED = [
    {
        "name": "docs",
        "title": "Docs",
        "url": "https://docs.example.test/mcp",
        "auth": "bearer",
        "tools": ["search"],
    },
    {
        "name": "tracker",
        "title": "Tracker",
        "url": MCP_URL,
        "auth": "oauth",
        "oauth": {"client_id": "flotta-registered"},
    },
]


def _operator():
    token = mint(
        subject="josh",
        scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN},
        key=KEY,
    )
    return {"Authorization": f"Bearer {token}"}


def _save(api, servers, headers=None):  # noqa: F811
    value = "" if servers is None else json.dumps(servers)
    return api.put(
        "/api/settings",
        json={"values": {"FLOTTA_MCP_SERVERS": value}},
        headers=headers or _operator(),
    )


def _names(api, fleet):  # noqa: F811
    listed = api.get("/api/mcp/servers", headers=_as(fleet, "ana@example.com")).json()
    return [s["name"] for s in listed["servers"]]


# -- stored wins, the environment is the fallback --------------------------------------


def test_a_saved_catalogue_replaces_the_environments_and_clearing_it_brings_it_back(api, fleet):  # noqa: F811
    assert _names(api, fleet) == [s["name"] for s in CATALOGUE]
    assert api.get("/api/mcp/catalogue", headers=_operator()).json()["source"] == "env"

    assert _save(api, STORED).status_code == 200
    assert _names(api, fleet) == ["docs", "tracker"]
    read = api.get("/api/mcp/catalogue", headers=_operator()).json()
    assert read["source"] == "store"
    assert read["servers"][0] == {**STORED[0], "ask_first": True, "has_client_secret": False}

    assert _save(api, None).status_code == 200
    assert _names(api, fleet) == [s["name"] for s in CATALOGUE]


def test_a_catalogue_that_cant_be_used_is_refused_whole(api, fleet):  # noqa: F811
    bad = [{**STORED[0], "url": "http://docs.example.test/mcp"}]
    answer = _save(api, bad)
    assert answer.status_code == 422 and "https" in answer.text
    assert _names(api, fleet) == [s["name"] for s in CATALOGUE]


def test_only_the_operator_writes_or_reads_it_whole(api, fleet):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    assert _save(api, STORED, headers=ana).status_code == 403
    assert api.get("/api/mcp/catalogue", headers=ana).status_code == 403
    secret = api.put(
        "/api/mcp/servers/tracker/client-secret", json={"client_secret": "s"}, headers=ana
    )
    assert secret.status_code == 403


# -- the client secret, set on its own ------------------------------------------------


def test_a_client_secret_is_sealed_and_never_answered(api, fleet):  # noqa: F811
    _save(api, STORED)
    answer = api.put(
        "/api/mcp/servers/tracker/client-secret",
        json={"client_secret": "operator-secret"},
        headers=_operator(),
    )
    assert answer.status_code == 200 and answer.json() == {
        "server": "tracker",
        "has_client_secret": True,
    }
    for url in ("/api/mcp/catalogue", "/api/settings", "/api/mcp/servers"):
        assert "operator-secret" not in api.get(url, headers=_operator()).text
    with FleetStore(fleet) as store:
        sealed = store.mcp_server_secrets()["tracker"]
    assert "operator-secret" not in sealed
    assert unseal(sealed, context=server_secret_context("tracker")) == "operator-secret"
    read = api.get("/api/mcp/catalogue", headers=_operator()).json()
    assert [s["has_client_secret"] for s in read["servers"]] == [False, True]


def test_a_client_secret_is_only_for_a_server_the_operator_registered(api, fleet):  # noqa: F811
    _save(api, [STORED[0], {**STORED[1], "oauth": {}}])
    for server in ("docs", "tracker"):
        answer = api.put(
            f"/api/mcp/servers/{server}/client-secret",
            json={"client_secret": "s"},
            headers=_operator(),
        )
        assert answer.status_code == 422


def test_the_operators_secret_is_what_the_sign_in_sends(api, fleet, provider):  # noqa: F811
    _save(api, STORED)
    api.put(
        "/api/mcp/servers/tracker/client-secret",
        json={"client_secret": "operator-secret"},
        headers=_operator(),
    )
    ana = _as(fleet, "ana@example.com")
    started = api.post("/api/orgs/agency-x/mcp/tracker/connect", json={}, headers=ana)
    assert started.status_code == 200, started.text
    back = _callback(api, provider, started.json()["url"])
    claimed = api.post("/api/mcp/oauth/claim", json=_carried(back), headers=ana)
    assert claimed.status_code == 200, claimed.text
    assert provider.registered == 0
    form = provider.token_forms[-1]
    assert form["client_id"] == "flotta-registered"
    assert form["client_secret"] == "operator-secret"


def test_a_secret_goes_with_its_server_and_its_registration(api, fleet):  # noqa: F811
    _save(api, STORED)
    api.put(
        "/api/mcp/servers/tracker/client-secret",
        json={"client_secret": "s"},
        headers=_operator(),
    )
    # Same registration, another title: kept.
    _save(api, [STORED[0], {**STORED[1], "title": "Issues"}])
    with FleetStore(fleet) as store:
        assert "tracker" in store.mcp_server_secrets()
    # Another client_id: the secret was that registration's, so it goes.
    _save(api, [STORED[0], {**STORED[1], "oauth": {"client_id": "another"}}])
    with FleetStore(fleet) as store:
        assert "tracker" not in store.mcp_server_secrets()


# -- a connection is only ever sent where it was made for ------------------------------


def test_a_connection_reads_broken_once_its_server_moves(api, fleet):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    _save(api, STORED)
    api.put("/api/orgs/agency-x/mcp/docs", json={"key": "docs-key"}, headers=ana)
    assert _connection(fleet, "docs").server_url == STORED[0]["url"]
    _save(api, [{**STORED[0], "url": "https://elsewhere.example.test/mcp"}, STORED[1]])
    listed = api.get("/api/orgs/agency-x/mcp", headers=ana).json()
    docs = next(s for s in listed["servers"] if s["name"] == "docs")
    assert docs["connection"]["status"] == "broken"
    assert "moved" in docs["connection"]["detail"]


def test_who_connected_a_server_is_named(api, fleet):  # noqa: F811
    with FleetStore(fleet) as store:
        user = store.get_user_by_email("ana@example.com")
        store._conn.execute("UPDATE users SET name = 'Ana Lima' WHERE id = ?", (user.id,))
    ana = _as(fleet, "ana@example.com")
    api.put("/api/orgs/agency-x/mcp/notes", json={"key": "k"}, headers=ana)
    api.put("/api/orgs/agency-x/clients/acme/mcp/notes", json={"key": "k"}, headers=ana)
    listed = api.get("/api/orgs/agency-x/mcp", headers=ana).json()
    notes = next(s for s in listed["servers"] if s["name"] == "notes")
    assert notes["connection"]["connected_by_name"] == "Ana Lima"
    assert notes["clients"]["acme"]["connected_by_name"] == "Ana Lima"


def test_without_a_name_the_email_is_shown(api, fleet):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    api.put("/api/orgs/agency-x/mcp/notes", json={"key": "k"}, headers=ana)
    listed = api.get("/api/orgs/agency-x/mcp", headers=ana).json()
    notes = next(s for s in listed["servers"] if s["name"] == "notes")
    assert notes["connection"]["connected_by_name"] == "ana@example.com"


# -- a change reaches the agents granted it --------------------------------------------


def test_removing_a_server_takes_it_out_of_every_granted_agent(api, fleet, machine):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    api.put("/api/boxes/b-support/mcp", json={"servers": [{"server": "notes"}]}, headers=ana)
    assert "flotta-notes" in machine.config["mcp_servers"]
    machine.commands.clear()

    assert _save(api, [CATALOGUE[0]]).status_code == 200
    assert "flotta-notes" not in machine.config["mcp_servers"]
    assert machine.config["mcp_servers"]["mine"] == {"url": "x"}
    assert any("--flotta-mcp-reload" in c for c in machine.commands)
    # The grant stays, shown as going away; the gateway refuses the server.
    read = api.get("/api/boxes/b-support/mcp", headers=ana).json()
    assert [g["server"] for g in read["servers"]] == ["notes"]
    assert read["accounts"]["notes"] is None


def test_an_unchanged_server_rewrites_nothing(api, fleet, machine):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    api.put("/api/boxes/b-support/mcp", json={"servers": [{"server": "notes"}]}, headers=ana)
    machine.commands.clear()
    assert _save(api, CATALOGUE).status_code == 200
    assert machine.commands == []


def test_a_server_removed_and_restored_is_put_back_in_its_agents_config(api, fleet, machine):  # noqa: F811
    """Its grants were kept while it was gone, so coming back has agents to write it to,
    whether it is saved again or the override is cleared back to the environment's."""
    ana = _as(fleet, "ana@example.com")
    api.put("/api/boxes/b-support/mcp", json={"servers": [{"server": "notes"}]}, headers=ana)
    _save(api, [CATALOGUE[0]])
    assert "flotta-notes" not in machine.config["mcp_servers"]
    _save(api, CATALOGUE)
    assert "flotta-notes" in machine.config["mcp_servers"]
    _save(api, [CATALOGUE[0]])
    assert "flotta-notes" not in machine.config["mcp_servers"]
    _save(api, None)  # back to the environment's list, which has it
    assert "flotta-notes" in machine.config["mcp_servers"]


def test_a_catalogue_change_doesnt_wake_a_sleeping_agent(api, fleet, machine):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    api.put("/api/boxes/b-support/mcp", json={"servers": [{"server": "notes"}]}, headers=ana)
    with FleetStore(fleet) as store:
        store.update_box_status("b-support", "stopped")
    machine.commands.clear()
    _save(api, [CATALOGUE[0]])
    assert machine.commands == []
    with FleetStore(fleet) as store:
        assert store.mcp_config_pending("b-support")
