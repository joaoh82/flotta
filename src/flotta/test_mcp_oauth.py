"""Flotta as an MCP server's OAuth client (FLOTTA-125), against `fake_mcp`'s provider."""

from __future__ import annotations

import base64
import hashlib
import time

import httpx
import pytest

from flotta import mcp_oauth
from flotta.gateway.fake_mcp import AUTH_URL, FakeMcp, catalogue
from flotta.github_app import State, sign_state
from flotta.mcp import parse_catalogue

KEY = "a-signing-key"
SERVER = parse_catalogue(catalogue(auth="oauth", oauth={"scopes": ["read"]}))["tracker"]
REDIRECT = "https://control.example/api/mcp/oauth/callback"


def test_a_state_names_its_flow():
    assert mcp_oauth.read_state(KEY, mcp_oauth.sign_state(KEY, "mf-1")) == "mf-1"


@pytest.mark.parametrize(
    "state",
    [
        lambda: mcp_oauth.sign_state("another-key", "mf-1"),
        lambda: mcp_oauth.sign_state(KEY, "mf-1", now=time.time() - 3600),
        lambda: mcp_oauth.sign_state(KEY, "mf-1").replace("mf-1", "mf-2"),
        lambda: sign_state(KEY, State(org="o", client=None, subject="s")),
        lambda: "",
    ],
    ids=["other-key", "expired", "edited", "a-github-state", "empty"],
)
def test_a_state_we_didnt_sign_or_that_expired_is_refused(state):
    with pytest.raises(mcp_oauth.BadState):
        mcp_oauth.read_state(KEY, state())


def test_pkce_is_s256():
    verifier, challenge = mcp_oauth.pkce()
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).decode().rstrip("=")


@pytest.fixture
def provider():
    return FakeMcp()


@pytest.fixture
def http(provider):
    with httpx.Client(transport=provider.transport()) as client:
        yield client


def test_a_whole_sign_in_discovers_registers_and_trades_the_code(provider, http):
    endpoints = mcp_oauth.discover(http, SERVER)
    assert endpoints.token == f"{AUTH_URL}/token"
    client = mcp_oauth.client_for(http, SERVER, endpoints, REDIRECT)
    assert client == mcp_oauth.Client("dyn-1", "dyn-secret")
    verifier, challenge = mcp_oauth.pkce()
    url = mcp_oauth.authorize_url(
        SERVER, endpoints, client, redirect_uri=REDIRECT, state="s", challenge=challenge
    )
    assert "scope=read" in url
    code, state = provider.sign_in(url)
    assert state == "s"
    tokens = mcp_oauth.exchange(
        http, SERVER, endpoints, client, code=code, verifier=verifier, redirect_uri=REDIRECT
    )
    assert tokens.access_token == "at-1" and tokens.refresh_token == "rt-1"
    assert tokens.expires_at is not None and tokens.expires_at > time.time()


def test_a_code_with_the_wrong_verifier_is_refused(provider, http):
    endpoints = mcp_oauth.discover(http, SERVER)
    client = mcp_oauth.client_for(http, SERVER, endpoints, REDIRECT)
    _, challenge = mcp_oauth.pkce()
    url = mcp_oauth.authorize_url(
        SERVER, endpoints, client, redirect_uri=REDIRECT, state="s", challenge=challenge
    )
    code, _ = provider.sign_in(url)
    with pytest.raises(mcp_oauth.Refused):
        mcp_oauth.exchange(
            http, SERVER, endpoints, client, code=code, verifier="wrong", redirect_uri=REDIRECT
        )


def test_a_refresh_rotates_and_an_old_refresh_token_is_refused(provider, http):
    endpoints = mcp_oauth.discover(http, SERVER)
    client = mcp_oauth.Client("dyn-1", "dyn-secret")
    issued = provider._issue()
    fresh = mcp_oauth.refresh(http, SERVER, endpoints, client, issued["refresh_token"])
    assert fresh.access_token == "at-2" and fresh.refresh_token == "rt-2"
    with pytest.raises(mcp_oauth.Refused):
        mcp_oauth.refresh(http, SERVER, endpoints, client, issued["refresh_token"])


def test_an_operator_registered_client_reads_its_secret_from_the_environment(http):
    server = parse_catalogue(
        catalogue(
            auth="oauth",
            oauth={"client_id": "registered", "client_secret_env": "FLOTTA_MCP_TRACKER_SECRET"},
        )
    )["tracker"]
    endpoints = mcp_oauth.discover(http, server)
    client = mcp_oauth.client_for(http, server, endpoints, REDIRECT)
    assert client == mcp_oauth.Client("registered", None, from_env=True)
    with pytest.raises(mcp_oauth.OAuthError, match="FLOTTA_MCP_TRACKER_SECRET"):
        mcp_oauth.refresh(http, server, endpoints, client, "rt", env={})


def test_a_found_endpoint_that_isnt_https_is_refused():
    def answer(request):
        if request.url.path.endswith("oauth-authorization-server"):
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "http://evil.example/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                },
            )
        return httpx.Response(404)

    with (
        httpx.Client(transport=httpx.MockTransport(answer)) as http,
        pytest.raises(mcp_oauth.OAuthError, match="https"),
    ):
        mcp_oauth.discover(http, SERVER)


def test_a_revocation_is_asked_for(provider, http):
    endpoints = mcp_oauth.discover(http, SERVER)
    client = mcp_oauth.Client("dyn-1", "dyn-secret")
    assert mcp_oauth.revoke(http, SERVER, endpoints, client, "rt-9")
    assert provider.revoked == ["rt-9"]


def test_a_token_about_to_expire_needs_refreshing():
    now = 1_000_000.0
    assert mcp_oauth.needs_refresh({"expires_at": now + 30}, now=now)
    assert not mcp_oauth.needs_refresh({"expires_at": now + 3600}, now=now)
    assert not mcp_oauth.needs_refresh({"expires_at": None}, now=now)
