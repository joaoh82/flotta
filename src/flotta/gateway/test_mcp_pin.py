"""Templated library connectors are checked and pinned at connect time (FLOTTA-225).

n8n and Intercom take a host from the operator. The gateway resolves it, refuses
any address that is not public, and dials the address it just vetted. The Host
header and httpcore's `sni_hostname` stay the hostname. A hand-written server
with no library marker is not on this path.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_MCP_CALL, box_subject, mint
from flotta.gateway.app import create_gateway
from flotta.mcp import connection_context
from flotta.mcp_library import catalogue_entry, entry
from flotta.sealed import fresh, seal
from flotta.store import FleetStore

SIGNING_KEY = "a-signing-key-for-these-tests"
SECRETS_KEY = fresh()
SECRET = "sk_live_supersecretvalue"
PUBLIC = "93.184.216.34"


def _env(catalogue: str) -> dict[str, str]:
    return {
        "FLOTTA_SIGNING_KEY": SIGNING_KEY,
        "FLOTTA_SECRETS_KEY": SECRETS_KEY,
        "FLOTTA_MCP_SERVERS": catalogue,
    }


@pytest.fixture
def path(tmp_path):
    db = str(tmp_path / "fleet.db")
    with FleetStore(db) as fleet:
        fleet.create_org("studio-nord", "Studio Nord")
        fleet.create_client("studio-nord", "acme", "Acme Dental")
        fleet.create_box("one", box_id="b-one", org="studio-nord", client="acme")
    return db


def _connect(path, name, url):
    context = connection_context("studio-nord", name)
    sealed = seal(
        json.dumps({"kind": "bearer", "value": "agency-key"}), context=context, env=_env("[]")
    )
    with FleetStore(path) as fleet:
        fleet.set_mcp_connection(
            "studio-nord",
            name,
            kind="bearer",
            credential=sealed,
            by="test",
            server_url=url,
        )
        fleet.set_box_mcp_grants("b-one", [(name, json.dumps(["search_workflows"]), True)])


def _auth():
    token = mint(
        subject=box_subject("b-one"),
        scopes={SCOPE_MCP_CALL},
        key=SIGNING_KEY,
        org="studio-nord",
        client="acme",
    )
    return {"Authorization": f"Bearer {token}"}


def _call():
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "search_workflows", "arguments": {}},
    }


class _Upstream:
    def __init__(self, status=200, location=""):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.location = location

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {"location": self.location} if self.location else {}
        if self.status != 200:
            return httpx.Response(self.status, headers=headers)
        body = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "ok"}]}}
        return httpx.Response(200, json=body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def _post(path, catalogue, upstream, resolve, monkeypatch):
    import flotta.mcp_oauth as mcp_oauth

    monkeypatch.setattr(mcp_oauth, "resolve_host", resolve)
    app = create_gateway(
        store_factory=lambda: FleetStore(path),
        env=_env(catalogue),
        mcp_transport=upstream.transport(),
    )
    with TestClient(app) as client:
        return client.post("/mcp/n8n", json=_call(), headers=_auth())


def _marked(url: str, name: str = "n8n", tool: str = "search_workflows") -> str:
    return json.dumps(
        [
            {
                "name": name,
                "title": name,
                "url": url,
                "auth": "bearer",
                "ask_first": True,
                "library": True,
                "tools": [tool],
            }
        ]
    )


def test_a_templated_connector_is_pinned_to_the_vetted_address(path, monkeypatch):
    item = entry("n8n")
    assert item is not None
    url = catalogue_entry(item, {"host": "automation.example.com"})["url"]
    _connect(path, "n8n", url)
    lookups: list[str] = []

    def resolve(host, port):
        lookups.append(host)
        if len(lookups) == 1:
            return [PUBLIC]
        return ["10.9.9.9"]

    upstream = _Upstream()
    catalogue = json.dumps([catalogue_entry(item, {"host": "automation.example.com"})])
    answer = _post(path, catalogue, upstream, resolve, monkeypatch)
    assert answer.status_code == 200, answer.text
    assert lookups == ["automation.example.com"]
    sent = upstream.requests[0]
    assert sent.url.host == PUBLIC
    assert sent.headers["host"] == "automation.example.com"
    assert sent.extensions["sni_hostname"] == "automation.example.com"


def test_intercom_is_pinned_the_same_way(path, monkeypatch):
    item = entry("intercom")
    assert item is not None
    built = catalogue_entry(item, {"host": "mcp.intercom.com"})
    url = built["url"]
    context = connection_context("studio-nord", "intercom")
    sealed = seal(
        json.dumps({"kind": "bearer", "value": "agency-key"}), context=context, env=_env("[]")
    )
    with FleetStore(path) as fleet:
        fleet.set_mcp_connection(
            "studio-nord",
            "intercom",
            kind="bearer",
            credential=sealed,
            by="test",
            server_url=url,
        )
        fleet.set_box_mcp_grants("b-one", [("intercom", json.dumps(["search"]), True)])

    def resolve(host, port):
        return ["2001:4860:4860::8888"]

    import flotta.mcp_oauth as mcp_oauth

    monkeypatch.setattr(mcp_oauth, "resolve_host", resolve)
    upstream = _Upstream()
    app = create_gateway(
        store_factory=lambda: FleetStore(path),
        env=_env(json.dumps([built])),
        mcp_transport=upstream.transport(),
    )
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "search", "arguments": {}},
    }
    with TestClient(app) as client:
        answer = client.post("/mcp/intercom", json=body, headers=_auth())
    assert answer.status_code == 200, answer.text
    sent = upstream.requests[0]
    assert sent.url.host == "2001:4860:4860::8888"
    assert sent.headers["host"] == "mcp.intercom.com"
    assert sent.extensions["sni_hostname"] == "mcp.intercom.com"


def test_a_hand_written_server_is_not_pinned(path, monkeypatch):
    url = "https://automation.example.com/mcp-server/http"
    catalogue = json.dumps(
        [
            {
                "name": "n8n",
                "title": "n8n",
                "url": url,
                "auth": "bearer",
                "tools": ["search_workflows"],
            }
        ]
    )
    _connect(path, "n8n", url)

    def resolve(host, port):
        raise AssertionError("a hand-written server is not resolved here")

    upstream = _Upstream()
    answer = _post(path, catalogue, upstream, resolve, monkeypatch)
    assert answer.status_code == 200, answer.text
    assert upstream.requests[0].url.host == "automation.example.com"
    assert "sni_hostname" not in upstream.requests[0].extensions


@pytest.mark.parametrize(
    "url",
    [
        "https://10.0.0.5/mcp-server/http",
        "https://192.168.1.9/mcp-server/http",
        "https://[::1]/mcp-server/http",
        "https://[::ffff:10.0.0.1]/mcp-server/http",
        "https://8.8.8.8/mcp-server/http",
        "https://automation.example.com:8443/mcp-server/http",
        f"https://automation.example.com/mcp-server/http?token={SECRET}",
    ],
)
def test_a_literal_or_non_443_target_is_refused_before_it_is_resolved(path, url, monkeypatch):
    _connect(path, "n8n", url)

    def resolve(host, port):
        raise AssertionError("refused before DNS")

    upstream = _Upstream()
    answer = _post(path, _marked(url), upstream, resolve, monkeypatch)
    assert answer.status_code == 502
    assert upstream.requests == []
    assert SECRET not in answer.text
    assert "10.0.0.5" not in answer.text


@pytest.mark.parametrize(
    "address",
    [
        "10.1.2.3",
        "127.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "::",
        "fd00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "::ffff:8.8.8.8",
    ],
)
def test_a_name_that_resolves_to_a_non_public_address_is_refused(path, address, monkeypatch):
    url = "https://automation.example.com/mcp-server/http"
    _connect(path, "n8n", url)
    upstream = _Upstream()
    answer = _post(
        path, _marked(url), upstream, lambda host, port: [address], monkeypatch=monkeypatch
    )
    assert answer.status_code == 502
    assert "isn't public" in answer.text
    assert upstream.requests == []
    assert address not in answer.text


def test_a_nip_io_name_that_resolves_to_loopback_is_refused(path, monkeypatch):
    url = "https://127.0.0.1.nip.io/mcp-server/http"
    _connect(path, "n8n", url)
    upstream = _Upstream()
    answer = _post(
        path, _marked(url), upstream, lambda host, port: ["127.0.0.1"], monkeypatch=monkeypatch
    )
    assert answer.status_code == 502
    assert "isn't public" in answer.text
    assert upstream.requests == []


def test_a_name_that_resolves_to_a_public_and_a_private_address_is_refused(path, monkeypatch):
    url = "https://automation.example.com/mcp-server/http"
    _connect(path, "n8n", url)
    upstream = _Upstream()
    answer = _post(
        path,
        _marked(url),
        upstream,
        lambda host, port: [PUBLIC, "10.0.0.5"],
        monkeypatch,
    )
    assert answer.status_code == 502
    assert upstream.requests == []


def test_a_redirect_is_not_followed(path, monkeypatch):
    url = "https://automation.example.com/mcp-server/http"
    _connect(path, "n8n", url)
    upstream = _Upstream(status=302, location=f"http://127.0.0.1/steal?token={SECRET}")
    answer = _post(
        path, _marked(url), upstream, lambda host, port: [PUBLIC], monkeypatch=monkeypatch
    )
    assert answer.status_code == 502
    assert len(upstream.requests) == 1
    assert "redirect" in answer.text
    assert SECRET not in answer.text
    assert "127.0.0.1" not in answer.text
