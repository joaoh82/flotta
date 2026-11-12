"""Flotta as an MCP server's OAuth client (FLOTTA-125), against `fake_mcp`'s provider."""

from __future__ import annotations

import base64
import hashlib
import json
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


@pytest.fixture(autouse=True)
def public_names(monkeypatch):
    """`.test` names resolve to a public address, as a real provider's would."""
    monkeypatch.setattr(mcp_oauth, "resolve_host", lambda host, port: ["93.184.216.34"])


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


def _metadata(issuer=None, **endpoints):
    """A catalogue server whose metadata names `issuer`, and an issuer naming `endpoints`."""

    def answer(request):
        path = request.url.path
        if path.startswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(200, json={"authorization_servers": [issuer]})
        if path.endswith("oauth-authorization-server"):
            return httpx.Response(200, json=endpoints)
        return httpx.Response(404)

    return httpx.MockTransport(answer)


@pytest.mark.parametrize(
    "issuer",
    ["http://127.0.0.1:4318", "https://127.0.0.1", "https://169.254.169.254", "https://x.local"],
)
def test_an_authorization_server_the_metadata_names_must_be_public(issuer):
    with (
        httpx.Client(transport=_metadata(issuer)) as http,
        pytest.raises(mcp_oauth.OAuthError, match="public|https"),
    ):
        mcp_oauth.discover(http, SERVER)


@pytest.mark.parametrize("address", ["169.254.169.254", "10.0.0.5", "127.0.0.1", "::1"])
def test_a_name_that_resolves_somewhere_private_is_never_sent_a_code(monkeypatch, address):
    monkeypatch.setattr(mcp_oauth, "resolve_host", lambda host, port: [address])
    sent = []

    def answer(request):
        sent.append(request)
        return httpx.Response(200, json={"access_token": "a"})

    endpoints = mcp_oauth.Endpoints(
        authorization="https://auth.example.test/authorize",
        token="https://auth.example.test/token",
    )
    with (
        httpx.Client(transport=httpx.MockTransport(answer)) as http,
        pytest.raises(mcp_oauth.OAuthError, match="isn't public"),
    ):
        mcp_oauth.exchange(
            http,
            SERVER,
            endpoints,
            mcp_oauth.Client("c", "secret"),
            code="code",
            verifier="v",
            redirect_uri=REDIRECT,
        )
    assert sent == []


def test_one_private_address_among_public_ones_is_enough_to_refuse(monkeypatch):
    monkeypatch.setattr(mcp_oauth, "resolve_host", lambda h, p: ["93.184.216.34", "10.0.0.5"])
    with pytest.raises(mcp_oauth.OAuthError, match="isn't public"):
        mcp_oauth._send(
            httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
            SERVER,
            "GET",
            "https://auth.example.test/x",
            "metadata",
        )


def test_the_connection_goes_to_the_address_that_was_checked():
    seen = []

    def answer(request):
        seen.append(request)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(answer)) as http:
        mcp_oauth._send(http, SERVER, "GET", "https://auth.example.test/x", "metadata")
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "auth.example.test"
    assert seen[0].extensions["sni_hostname"] == "auth.example.test"


def test_a_local_server_the_operator_wrote_may_be_loopback():
    """The catalogue's own origin keeps the catalogue's rule, for a laptop."""
    local = parse_catalogue(
        '[{"name": "dev", "url": "http://127.0.0.1:9000/mcp", "auth": "oauth"}]'
    )["dev"]
    transport = _metadata(
        "http://127.0.0.1:9000",
        authorization_endpoint="http://127.0.0.1:9000/authorize",
        token_endpoint="http://127.0.0.1:9000/token",
    )
    with httpx.Client(transport=transport) as http:
        endpoints = mcp_oauth.discover(http, local)
    assert endpoints.token == "http://127.0.0.1:9000/token"


def test_an_endpoint_the_metadata_names_elsewhere_is_held_to_the_rule():
    transport = _metadata(
        AUTH_URL,
        authorization_endpoint="https://auth.example.test/authorize",
        token_endpoint="http://127.0.0.1:4318/token",
    )
    with (
        httpx.Client(transport=transport) as http,
        pytest.raises(mcp_oauth.OAuthError, match="https"),
    ):
        mcp_oauth.discover(http, SERVER)


def test_registration_uses_client_secret_post_unless_the_server_only_offers_none():
    assert mcp_oauth.registration_auth_method("") == "client_secret_post"
    assert mcp_oauth.registration_auth_method("none client_secret_post") == "client_secret_post"
    assert mcp_oauth.registration_auth_method("none", "S256") == "none"
    with pytest.raises(mcp_oauth.OAuthError, match="S256"):
        mcp_oauth.registration_auth_method("none")
    with pytest.raises(mcp_oauth.OAuthError, match="S256"):
        mcp_oauth.registration_auth_method("none", "plain")
    with pytest.raises(mcp_oauth.OAuthError, match="neither"):
        mcp_oauth.registration_auth_method("client_secret_basic")


def test_a_path_bearing_issuer_is_discovered_where_rfc_8414_puts_it():
    seen = []

    def answer(request):
        seen.append(request.url.path)
        if request.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(
                200,
                json={"authorization_servers": ["https://auth.example.test/oauth2/v1"]},
            )
        if request.url.path == "/.well-known/oauth-authorization-server/oauth2/v1":
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://auth.example.test/oauth2/v1/authorize",
                    "token_endpoint": "https://auth.example.test/oauth2/v1/token",
                    "registration_endpoint": "https://auth.example.test/oauth2/v1/register",
                    "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(answer)) as http:
        endpoints = mcp_oauth.discover(http, SERVER)
    assert endpoints.registration == "https://auth.example.test/oauth2/v1/register"
    assert endpoints.token_auth_methods == "none client_secret_basic"
    assert "/oauth2/v1/.well-known/oauth-authorization-server" not in seen


def test_a_suffix_metadata_url_still_answers_when_insertion_does_not():
    """Atlassian's issuer answers `{issuer}/.well-known/...`, and insertion can 404."""

    def answer(request):
        path = request.url.path
        if path.startswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(
                200, json={"authorization_servers": ["https://auth.example.test/oauth/v1"]}
            )
        if path == "/oauth/v1/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://auth.example.test/oauth/v1/authorize",
                    "token_endpoint": "https://auth.example.test/oauth/v1/token",
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(answer)) as http:
        endpoints = mcp_oauth.discover(http, SERVER)
    assert endpoints.token == "https://auth.example.test/oauth/v1/token"


def test_a_server_that_only_offers_none_registers_a_public_client():
    posted = {}

    def answer(request):
        if request.url.path.startswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(200, json={"authorization_servers": [AUTH_URL]})
        if request.url.path.endswith("oauth-authorization-server"):
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": f"{AUTH_URL}/authorize",
                    "token_endpoint": f"{AUTH_URL}/token",
                    "registration_endpoint": f"{AUTH_URL}/register",
                    "token_endpoint_auth_methods_supported": ["none"],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if request.url.path == "/register":
            posted["body"] = json.loads(request.content)
            return httpx.Response(201, json={"client_id": "pub-1"})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(answer)) as http:
        endpoints = mcp_oauth.discover(http, SERVER)
        client = mcp_oauth.client_for(http, SERVER, endpoints, REDIRECT)
    assert posted["body"]["token_endpoint_auth_method"] == "none"
    assert client == mcp_oauth.Client("pub-1", None)


def _metadata_issuer(issuer: str | None):
    def answer(request):
        if request.url.path.startswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(
                200, json={"authorization_servers": ["https://auth.example.test"]}
            )
        if request.url.path.endswith("oauth-authorization-server"):
            body = {
                "authorization_endpoint": "https://auth.example.test/authorize",
                "token_endpoint": "https://auth.example.test/token",
            }
            if issuer is not None:
                body["issuer"] = issuer
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    return httpx.MockTransport(answer)


def test_a_metadata_issuer_matches_including_one_trailing_slash():
    with httpx.Client(transport=_metadata_issuer("https://auth.example.test")) as http:
        exact = mcp_oauth.discover(http, SERVER)
    assert exact.token == "https://auth.example.test/token"
    with httpx.Client(transport=_metadata_issuer("https://auth.example.test/")) as http:
        slashed = mcp_oauth.discover(http, SERVER)
    assert slashed.token == "https://auth.example.test/token"


def test_a_metadata_issuer_that_is_someone_elses_is_not_used():
    with (
        httpx.Client(transport=_metadata_issuer("https://evil.example/")) as http,
        pytest.raises(mcp_oauth.OAuthError, match="no OAuth metadata"),
    ):
        mcp_oauth.discover(http, SERVER)


def test_sealed_endpoints_round_trip_the_old_shape_and_the_new_one():
    """A seal written now is the old shape, and a seal that already has the new key still opens."""
    live = mcp_oauth.Endpoints(
        authorization="https://auth.example.test/authorize",
        token="https://auth.example.test/token",
        token_auth_methods="none",
        code_challenge_methods="S256",
    )
    sealed = mcp_oauth.oauth_credential(
        live, mcp_oauth.Client("c"), mcp_oauth.Tokens("at", "rt", None)
    )
    assert set(sealed["endpoints"]) == {
        "authorization",
        "token",
        "registration",
        "revocation",
    }
    assert mcp_oauth.endpoints_of(sealed).token == live.token
    assert mcp_oauth.endpoints_of(sealed).token_auth_methods == ""
    newer = {
        "endpoints": {
            **sealed["endpoints"],
            "token_auth_methods": "none",
            "code_challenge_methods": "S256",
            "not_a_field": True,
        }
    }
    opened = mcp_oauth.endpoints_of(newer)
    assert opened.token_auth_methods == "none"
    assert opened.code_challenge_methods == "S256"
    older = {"endpoints": {"authorization": live.authorization, "token": live.token}}
    assert mcp_oauth.endpoints_of(older).registration is None
