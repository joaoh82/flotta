"""MCP servers over the fleet API (FLOTTA-125): connect, sign in, grant, and who may.

An org's owners connect a catalogue server, with a key or by signing in; the
agency's people grant it to agents; a client's people do neither. The sign-in
has to be finished by whoever started it, and nothing a connection holds is
ever answered back. The provider is `fake_mcp`'s, and the machine a stand-in
that runs the real config writer into a directory.
"""

from __future__ import annotations

import base64
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from fastapi.testclient import TestClient

import flotta.provision as provision
from flotta import mcp_oauth
from flotta.auth import verify
from flotta.backend import ExecResult
from flotta.box import rules as box_rules
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.control.test_sign_in import APP_URL, KEY
from flotta.control.test_tickets import _as, fleet  # noqa: F401  (fixture)
from flotta.gateway.fake_mcp import MCP_URL, FakeMcp
from flotta.mcp import connection_context
from flotta.sealed import fresh, unseal
from flotta.store import FleetStore

SECRETS = fresh()
CONTROL = "https://control.example.test"
CATALOGUE = [
    {"name": "tracker", "title": "Tracker", "url": MCP_URL, "auth": "oauth"},
    {
        "name": "notes",
        "url": "https://notes.example.test/mcp",
        "auth": "bearer",
        "tools": ["read_note", "write_note"],
    },
]


@pytest.fixture(autouse=True)
def public_names(monkeypatch):
    """`.test` names resolve to a public address, as a real provider's would."""
    monkeypatch.setattr(mcp_oauth, "resolve_host", lambda host, port: ["93.184.216.34"])


@pytest.fixture
def provider():
    return FakeMcp()


@pytest.fixture
def api(fleet, provider, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FLOTTA_MCP_SERVERS", json.dumps(CATALOGUE))
    monkeypatch.setenv("FLOTTA_SECRETS_KEY", SECRETS)
    monkeypatch.setenv("FLOTTA_CONTROL_URL", CONTROL)
    monkeypatch.setenv("FLOTTA_GATEWAY_URL", "https://gw.example.test/v1")
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", KEY)
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
        app_url=APP_URL,
        trusted_proxy_hops=0,
        mcp_oauth_transport=provider.transport(),
    )
    with TestClient(app) as client:
        yield client


class Machine:
    """Runs the command the way a box would: the real writer, into a directory."""

    def __init__(self, home):
        self.home, self.code, self.commands = home, 0, []

    def exec(self, endpoint, command):
        self.commands.append(command)
        if self.code:
            return ExecResult(self.code, "", "disk full")
        payload = command.rsplit(" ", 1)[1]
        said = box_rules.apply(self.home, json.loads(base64.b64decode(payload)))
        return ExecResult(0, said, "")

    @property
    def config(self):
        return yaml.safe_load((self.home / "config.yaml").read_text())


@pytest.fixture
def machine(fleet, monkeypatch, tmp_path):  # noqa: F811
    with FleetStore(fleet) as store:
        store.update_box_status("b-support", "running", endpoint="fake://m-1")
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": {"mine": {"url": "x"}}}))
    found = Machine(home)
    monkeypatch.setattr(provision, "_resolve_backend", lambda box, backend: found)
    return found


def _connection(fleet, server="notes", org="agency-x"):  # noqa: F811
    with FleetStore(fleet) as store:
        return store.mcp_connection(org, server)


# -- the catalogue and connecting with a key ----------------------------------------


def test_anyone_in_the_fleet_reads_the_catalogue_and_nothing_secret_in_it(api, fleet):  # noqa: F811
    answer = api.get("/api/mcp/servers", headers=_as(fleet, "cu@example.com"))
    assert answer.status_code == 200
    assert [s["name"] for s in answer.json()["servers"]] == ["tracker", "notes"]
    assert "oauth" not in answer.json()["servers"][0]


def test_an_owner_connects_a_key_and_it_is_kept_sealed_and_never_answered(api, fleet):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    answer = api.put("/api/orgs/agency-x/mcp/notes", json={"key": "notes-secret"}, headers=ana)
    assert answer.status_code == 200, answer.text
    assert "notes-secret" not in answer.text
    stored = _connection(fleet)
    assert "notes-secret" not in stored.credential
    opened = unseal(
        stored.credential,
        context=connection_context("agency-x", "notes"),
        env={"FLOTTA_SECRETS_KEY": SECRETS},
    )
    assert json.loads(opened) == {"kind": "bearer", "value": "notes-secret"}

    listed = api.get("/api/orgs/agency-x/mcp", headers=ana).json()["servers"]
    notes = next(s for s in listed if s["name"] == "notes")
    assert notes["connection"]["status"] == "ok"
    assert "notes-secret" not in json.dumps(listed)


@pytest.mark.parametrize("email", ["mo@example.com", "cu@example.com", "vi@example.com"])
def test_only_an_owner_connects_one(api, fleet, email):  # noqa: F811
    answer = api.put("/api/orgs/agency-x/mcp/notes", json={"key": "k"}, headers=_as(fleet, email))
    assert answer.status_code == 403
    assert _connection(fleet) is None


def test_another_orgs_connections_are_not_there(api, fleet):  # noqa: F811
    with FleetStore(fleet) as store:
        user = store.create_user("yo@example.com")
        store.add_membership(user.id, "agency-y", "owner")
    yo = _as(fleet, "yo@example.com", org="agency-y")
    assert api.get("/api/orgs/agency-x/mcp", headers=yo).status_code == 404
    assert api.put("/api/orgs/agency-x/mcp/notes", json={"key": "k"}, headers=yo).status_code == 404
    assert api.delete("/api/orgs/agency-x/mcp/notes", headers=yo).status_code == 404
    assert _connection(fleet) is None


def test_a_client_member_doesnt_see_the_agencys_connections(api, fleet):  # noqa: F811
    assert (
        api.get("/api/orgs/agency-x/mcp", headers=_as(fleet, "mo@example.com")).status_code == 404
    )


def test_without_the_secrets_key_nothing_is_stored(api, fleet, monkeypatch):  # noqa: F811
    monkeypatch.delenv("FLOTTA_SECRETS_KEY")
    answer = api.put(
        "/api/orgs/agency-x/mcp/notes", json={"key": "k"}, headers=_as(fleet, "ana@example.com")
    )
    assert answer.status_code == 503 and "FLOTTA_SECRETS_KEY" in answer.text
    assert _connection(fleet) is None


# -- signing in ------------------------------------------------------------------------


def _start(api, fleet, email="ana@example.com", **body):  # noqa: F811
    answer = api.post(
        "/api/orgs/agency-x/mcp/tracker/connect", json=body, headers=_as(fleet, email)
    )
    assert answer.status_code == 200, answer.text
    return answer.json()["url"]


def _callback(api, provider, url):
    code, state = provider.sign_in(url)
    return api.get(
        "/api/mcp/oauth/callback", params={"code": code, "state": state}, follow_redirects=False
    )


def _carried(back):
    """What the browser brings back to the web app: the state, and the code."""
    query = parse_qs(urlsplit(back.headers["location"]).query)
    return {"state": query["mcp_state"][0], "code": query["mcp_code"][0]}


def test_a_whole_sign_in_ends_as_the_orgs_connection(api, fleet, provider):  # noqa: F811
    url = _start(api, fleet, return_to=f"{APP_URL}/agency/mcp")
    query = parse_qs(urlsplit(url).query)
    assert query["redirect_uri"] == [f"{CONTROL}/api/mcp/oauth/callback"]
    assert "code_verifier" not in url

    back = _callback(api, provider, url)
    assert back.status_code == 303
    assert back.headers["location"].startswith(f"{APP_URL}/agency/mcp?")
    # The callback traded nothing: the code is still the browser's.
    assert provider.token_calls == []
    assert _connection(fleet, "tracker") is None

    claimed = api.post(
        "/api/mcp/oauth/claim", json=_carried(back), headers=_as(fleet, "ana@example.com")
    )
    assert claimed.status_code == 200, claimed.text
    assert provider.token_calls == ["authorization_code"]
    assert "at-1" not in claimed.text and "rt-1" not in claimed.text
    stored = _connection(fleet, "tracker")
    assert stored.kind == "oauth"
    credential = json.loads(
        unseal(
            stored.credential,
            context=connection_context("agency-x", "tracker"),
            env={"FLOTTA_SECRETS_KEY": SECRETS},
        )
    )
    assert credential["access_token"] == "at-1"
    assert credential["client"]["client_id"] == "dyn-1"


def _stranger(fleet):  # noqa: F811
    with FleetStore(fleet) as store:
        user = store.create_user("bo@example.com")
        store.add_membership(user.id, "agency-y", "owner")
    return _as(fleet, "bo@example.com", org="agency-y")


def test_a_sign_in_link_sent_to_a_stranger_gives_the_starter_nothing(api, fleet, provider):  # noqa: F811
    """The attack the claim exists for, in the reviewer's direction.

    Ana starts a sign-in for her org and sends the provider's link to Bo, who
    signs in with his own account. Ana holds the state; the code went to Bo's
    browser. She can't claim without it, Bo's claim is refused because he didn't
    start it, and nothing was traded or kept at any point.
    """
    url = _start(api, fleet)
    back = _callback(api, provider, url)  # Bo's browser
    carried = _carried(back)
    assert provider.token_calls == []
    with FleetStore(fleet) as store:
        assert store.mcp_connection("agency-x", "tracker") is None

    bo = api.post("/api/mcp/oauth/claim", json=carried, headers=_stranger(fleet))
    assert bo.status_code == 403
    assert provider.token_calls == []

    ana = _as(fleet, "ana@example.com")
    without = api.post("/api/mcp/oauth/claim", json={"state": carried["state"]}, headers=ana)
    assert without.status_code == 422
    guessed = api.post(
        "/api/mcp/oauth/claim", json={"state": carried["state"], "code": "code-9"}, headers=ana
    )
    assert guessed.status_code == 502  # a code the provider never issued: refused there
    assert _connection(fleet, "tracker") is None


def test_a_sign_in_claimed_by_another_owner_of_the_same_org_is_refused(api, fleet, provider):  # noqa: F811
    with FleetStore(fleet) as store:
        user = store.create_user("cy@example.com")
        store.add_membership(user.id, "agency-x", "owner")
    back = _callback(api, provider, _start(api, fleet))
    answer = api.post(
        "/api/mcp/oauth/claim", json=_carried(back), headers=_as(fleet, "cy@example.com")
    )
    assert answer.status_code == 403
    assert provider.token_calls == []
    assert _connection(fleet, "tracker") is None


def test_a_sign_in_is_claimed_once(api, fleet, provider):  # noqa: F811
    carried = _carried(_callback(api, provider, _start(api, fleet)))
    ana = _as(fleet, "ana@example.com")
    assert api.post("/api/mcp/oauth/claim", json=carried, headers=ana).status_code == 200
    again = api.post("/api/mcp/oauth/claim", json=carried, headers=ana)
    assert again.status_code == 422
    assert provider.token_calls == ["authorization_code"]


def test_a_forged_state_reaches_no_provider(api, fleet, provider):  # noqa: F811
    answer = api.get("/api/mcp/oauth/callback", params={"code": "c", "state": "mcp1.mf-x.9.sig"})
    assert answer.status_code == 422
    claim = api.post(
        "/api/mcp/oauth/claim",
        json={"state": "mcp1.mf-x.9.sig", "code": "c"},
        headers=_as(fleet, "ana@example.com"),
    )
    assert claim.status_code == 422
    assert provider.token_calls == []


def test_the_browser_goes_back_to_the_web_app_and_nowhere_else(api, fleet):  # noqa: F811
    answer = api.post(
        "/api/orgs/agency-x/mcp/tracker/connect",
        json={"return_to": "https://evil.example/steal"},
        headers=_as(fleet, "ana@example.com"),
    )
    assert answer.status_code == 422


def test_a_refused_sign_in_goes_back_and_connects_nothing(api, fleet, provider):  # noqa: F811
    url = _start(api, fleet)
    _, state = provider.sign_in(url)
    back = api.get(
        "/api/mcp/oauth/callback",
        params={"state": state, "error": "access_denied"},
        follow_redirects=False,
    )
    assert back.status_code == 303 and "mcp_error=access_denied" in back.headers["location"]
    assert provider.token_calls == []
    assert _connection(fleet, "tracker") is None


def test_disconnecting_forgets_it_and_asks_the_provider_to_revoke(api, fleet, provider):  # noqa: F811
    carried = _carried(_callback(api, provider, _start(api, fleet)))
    ana = _as(fleet, "ana@example.com")
    api.post("/api/mcp/oauth/claim", json=carried, headers=ana)
    answer = api.delete("/api/orgs/agency-x/mcp/tracker", headers=ana)
    assert answer.json() == {"server": "tracker", "disconnected": True, "revoked": True}
    assert set(provider.revoked) == {"rt-1", "at-1"}
    assert _connection(fleet, "tracker") is None


def test_a_key_server_has_no_sign_in(api, fleet):  # noqa: F811
    answer = api.post(
        "/api/orgs/agency-x/mcp/notes/connect", json={}, headers=_as(fleet, "ana@example.com")
    )
    assert answer.status_code == 422


# -- a client's own connection (slice 3) -------------------------------------------------


def _client_connection(fleet, client="acme", server="notes", org="agency-x"):  # noqa: F811
    with FleetStore(fleet) as store:
        return store.mcp_connection(org, server, client=client)


def test_an_owner_connects_a_clients_own_key_sealed_to_that_client(api, fleet):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    answer = api.put(
        "/api/orgs/agency-x/clients/acme/mcp/notes", json={"key": "acme-secret"}, headers=ana
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["client"] == "acme" and "acme-secret" not in answer.text
    stored = _client_connection(fleet)
    assert _connection(fleet) is None  # the agency's is another thing
    env = {"FLOTTA_SECRETS_KEY": SECRETS}
    opened = unseal(
        stored.credential, context=connection_context("agency-x", "notes", "acme"), env=env
    )
    assert json.loads(opened)["value"] == "acme-secret"

    listed = api.get("/api/orgs/agency-x/mcp", headers=ana).json()["servers"]
    notes = next(s for s in listed if s["name"] == "notes")
    assert notes["connection"] is None
    assert notes["clients"]["acme"]["status"] == "ok"
    mine = api.get("/api/orgs/agency-x/clients/acme/mcp", headers=ana).json()["servers"]
    notes = next(s for s in mine if s["name"] == "notes")
    assert notes["connection"]["status"] == "ok" and notes["agency"] is None
    assert "acme-secret" not in json.dumps([listed, mine])


@pytest.mark.parametrize("email", ["mo@example.com", "cu@example.com", "vi@example.com"])
def test_only_an_owner_connects_one_for_a_client(api, fleet, email):  # noqa: F811
    """Not the agency's members, and never the client's own people."""
    who = _as(fleet, email)
    url = "/api/orgs/agency-x/clients/acme/mcp/notes"
    assert api.put(url, json={"key": "k"}, headers=who).status_code == 403
    assert api.delete(url, headers=who).status_code == 403
    assert _client_connection(fleet) is None


def test_a_client_of_another_org_or_none_at_all_is_a_404(api, fleet):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    for client in ("nobody", "../acme"):
        url = f"/api/orgs/agency-x/clients/{client}/mcp/notes"
        assert api.put(url, json={"key": "k"}, headers=ana).status_code == 404
    with FleetStore(fleet) as store:
        user = store.create_user("yo@example.com")
        store.add_membership(user.id, "agency-y", "owner")
    yo = _as(fleet, "yo@example.com", org="agency-y")
    url = "/api/orgs/agency-x/clients/acme/mcp/notes"
    assert api.put(url, json={"key": "k"}, headers=yo).status_code == 404
    assert api.get("/api/orgs/agency-x/clients/acme/mcp", headers=yo).status_code == 404
    assert api.delete(url, headers=yo).status_code == 404
    assert _client_connection(fleet) is None


def test_a_sign_in_for_a_client_ends_as_that_clients_connection(api, fleet, provider):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    started = api.post(
        "/api/orgs/agency-x/clients/acme/mcp/tracker/connect",
        json={"return_to": f"{APP_URL}/tools"},
        headers=ana,
    )
    assert started.status_code == 200, started.text
    back = _callback(api, provider, started.json()["url"])
    assert "mcp_client=acme" in back.headers["location"]
    assert provider.token_calls == []

    claimed = api.post("/api/mcp/oauth/claim", json=_carried(back), headers=ana)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["client"] == "acme"
    assert _connection(fleet, "tracker") is None
    stored = _client_connection(fleet, server="tracker")
    credential = json.loads(
        unseal(
            stored.credential,
            context=connection_context("agency-x", "tracker", "acme"),
            env={"FLOTTA_SECRETS_KEY": SECRETS},
        )
    )
    assert credential["access_token"] == "at-1"

    gone = api.delete("/api/orgs/agency-x/clients/acme/mcp/tracker", headers=ana)
    assert gone.json() == {
        "server": "tracker",
        "disconnected": True,
        "revoked": True,
        "client": "acme",
    }
    assert _client_connection(fleet, server="tracker") is None


def test_a_clients_sign_in_sent_to_a_stranger_gives_nobody_anything(api, fleet, provider):  # noqa: F811
    """Slice 1's claim rule holds for a client's sign-in too."""
    started = api.post(
        "/api/orgs/agency-x/clients/acme/mcp/tracker/connect",
        json={},
        headers=_as(fleet, "ana@example.com"),
    )
    carried = _carried(_callback(api, provider, started.json()["url"]))
    assert (
        api.post("/api/mcp/oauth/claim", json=carried, headers=_stranger(fleet)).status_code == 403
    )
    assert provider.token_calls == []
    assert _client_connection(fleet, server="tracker") is None


def test_a_client_removed_mid_sign_in_gets_no_connection(api, fleet, provider):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    started = api.post("/api/orgs/agency-x/clients/acme/mcp/tracker/connect", json={}, headers=ana)
    carried = _carried(_callback(api, provider, started.json()["url"]))
    with FleetStore(fleet) as store:
        store._conn.execute(
            "UPDATE mcp_oauth_flows SET client_id = 'gone' WHERE client_id = 'acme'"
        )
    assert api.post("/api/mcp/oauth/claim", json=carried, headers=ana).status_code == 404
    assert provider.token_calls == []


def test_an_agents_grants_say_whose_account_its_calls_use(api, fleet, machine):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    body = {"servers": [{"server": "notes"}, {"server": "tracker"}]}
    assert api.put("/api/boxes/b-support/mcp", json=body, headers=ana).status_code == 200
    api.put("/api/orgs/agency-x/mcp/notes", json={"key": "agency"}, headers=ana)
    read = api.get("/api/boxes/b-support/mcp", headers=ana).json()
    assert read["accounts"] == {"notes": {"account": "org", "status": "ok"}, "tracker": None}
    api.put("/api/orgs/agency-x/clients/acme/mcp/notes", json={"key": "acme"}, headers=ana)
    read = api.get("/api/boxes/b-support/mcp", headers=ana).json()
    assert read["accounts"]["notes"] == {"account": "client", "status": "ok"}
    # Zeta's agent, same org: the agency's, never Acme's.
    api.put("/api/boxes/b-sales/mcp", json={"servers": [{"server": "notes"}]}, headers=ana)
    zeta = api.get("/api/boxes/b-sales/mcp", headers=ana).json()
    assert zeta["accounts"] == {"notes": {"account": "org", "status": "ok"}}


# -- granting ---------------------------------------------------------------------------


GRANTS = {"servers": [{"server": "notes", "tools": ["read_note"], "ask_first": True}]}


def test_the_agency_grants_a_server_and_the_agent_gets_an_entry(api, fleet, machine):  # noqa: F811
    mo = _as(fleet, "mo@example.com")
    answer = api.put("/api/boxes/b-support/mcp", json=GRANTS, headers=mo)
    assert answer.status_code == 200, answer.text
    assert answer.json()["written"] is True
    assert answer.json()["servers"] == GRANTS["servers"]

    entry = machine.config["mcp_servers"]["flotta-notes"]
    assert entry["url"] == "https://gw.example.test/mcp/notes"
    assert entry["tools"] == {"include": ["read_note"]}
    assert entry["trust"] == "untrusted"
    assert machine.config["mcp_servers"]["mine"] == {"url": "x"}
    token = verify(entry["headers"]["Authorization"].removeprefix("Bearer "), key=KEY)
    assert token.scopes == {"mcp:call"}
    assert token.subject == "box:b-support"
    assert "notes.example.test" not in json.dumps(machine.config)

    read = api.get("/api/boxes/b-support/mcp", headers=mo).json()
    assert read["servers"] == GRANTS["servers"]

    api.put("/api/boxes/b-support/mcp", json={"servers": []}, headers=mo)
    assert set(machine.config["mcp_servers"]) == {"mine"}


def test_a_grant_is_in_force_even_when_the_machine_cant_be_written(api, fleet, machine):  # noqa: F811
    """The gateway enforces the record, so a grant taken away must not wait on a disk."""
    mo = _as(fleet, "mo@example.com")
    api.put("/api/boxes/b-support/mcp", json=GRANTS, headers=mo)
    machine.code = 1
    answer = api.put("/api/boxes/b-support/mcp", json={"servers": []}, headers=mo)
    assert answer.status_code == 200
    assert answer.json()["written"] is False and "disk full" in answer.json()["detail"]
    with FleetStore(fleet) as store:
        assert store.box_mcp_grant("b-support", "notes") is None


@pytest.mark.parametrize(
    "body",
    [
        {"servers": [{"server": "github"}]},
        {"servers": [{"server": "notes", "tools": ["delete_all"]}]},
    ],
)
def test_a_grant_that_cant_be_kept_reaches_no_machine(api, fleet, machine, body):  # noqa: F811
    answer = api.put("/api/boxes/b-support/mcp", json=body, headers=_as(fleet, "ana@example.com"))
    assert answer.status_code == 422
    assert machine.commands == []


@pytest.mark.parametrize("email", ["cu@example.com", "vi@example.com"])
def test_a_clients_people_neither_read_nor_grant(api, fleet, machine, email):  # noqa: F811
    headers = _as(fleet, email)
    assert api.get("/api/boxes/b-support/mcp", headers=headers).status_code == 403
    assert api.put("/api/boxes/b-support/mcp", json=GRANTS, headers=headers).status_code == 403
    assert api.get("/api/boxes/b-support/mcp/calls", headers=headers).status_code == 403
    assert machine.commands == []


def test_without_a_gateway_there_is_nothing_to_grant_through(api, fleet, machine, monkeypatch):  # noqa: F811
    monkeypatch.delenv("FLOTTA_GATEWAY_URL")
    answer = api.put("/api/boxes/b-support/mcp", json=GRANTS, headers=_as(fleet, "ana@example.com"))
    assert answer.status_code == 409 and "FLOTTA_GATEWAY_URL" in answer.text


def test_the_calls_read_back_by_tool_and_outcome(api, fleet):  # noqa: F811
    with FleetStore(fleet) as store:
        store.record_mcp_call("b-support", server="notes", tool="read_note", status="ok")
    answer = api.get("/api/boxes/b-support/mcp/calls", headers=_as(fleet, "mo@example.com"))
    assert [(c["tool"], c["status"]) for c in answer.json()["calls"]] == [("read_note", "ok")]


def test_the_box_command_carries_no_upstream_credential(api, fleet, machine):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    api.put("/api/orgs/agency-x/mcp/notes", json={"key": "notes-secret"}, headers=ana)
    api.put("/api/boxes/b-support/mcp", json=GRANTS, headers=ana)
    sent = machine.commands[-1]
    payload = base64.b64decode(re.split(r"\s+", sent)[-1]).decode()
    assert "notes-secret" not in payload and "notes-secret" not in sent
