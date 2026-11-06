"""Flotta as the OAuth client of an MCP server (FLOTTA-125).

Most hosted MCP servers (Linear, Notion) take only OAuth, and the token they
give must not be on a box. So Flotta signs in, keeps the tokens sealed
(`flotta.sealed`) and refreshes them, and the gateway puts the access token on
each request. The shape is the MCP authorization spec's: discovery, dynamic
client registration where the operator hasn't registered one, the
authorization-code flow with PKCE, and `resource` naming the server (RFC 8707)
so a token is issued for that server and no other.

## Connecting, in four steps

1. **Start** (`POST /api/orgs/<org>/mcp/<server>/connect`, an owner): discover
   the endpoints, register a client if needed, and keep a flow with the PKCE
   verifier, sealed, in the store. The browser is sent to the provider with a
   signed `state` naming the flow.
2. **Callback** (`GET /api/mcp/oauth/callback`, the provider's redirect, on the
   control plane so a self-hoster has it too): the code is traded for tokens,
   which are kept on the flow, still sealed, and not yet anyone's.
3. **Claim** (`POST /api/mcp/oauth/claim`, the same owner): the tokens become
   the org's connection. **The person who finishes must be the one who
   started.** Otherwise anyone could start a flow for their own org and send
   someone else the sign-in link, and that person's Notion would land in the
   stranger's agents' reach: the attack `control.app._started_by_caller` stops
   for GitHub.
4. **Refresh** (the gateway, before a call): an access token that has expired,
   or is about to, is refreshed under a per-connection lock, so two calls don't
   spend one refresh token twice. A provider that refuses the refresh marks the
   connection broken, and the page says to connect again.

**Why the verifier is in the store and not in `state`:** `state` travels in the
browser's address bar beside the code, and a verifier anyone with the code can
read protects nothing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from flotta.mcp import McpError, Server, check_url

STATE_TTL_S = 10 * 60
#: Refresh this long before expiry, so a token doesn't die in flight.
REFRESH_EARLY_S = 60
_TIMEOUT = httpx.Timeout(15.0)
_STATE_PREFIX = "mcp1"


class OAuthError(Exception):
    """The provider could not be reached, or answered something unusable."""


class Refused(OAuthError):
    """The provider said no: a code or refresh token that is no longer good."""


class BadState(Exception):
    """A state we didn't sign, or one that has expired."""


@dataclass(frozen=True, slots=True)
class Endpoints:
    authorization: str
    token: str
    registration: str | None = None
    revocation: str | None = None


@dataclass(frozen=True, slots=True)
class Client:
    client_id: str
    #: A secret the provider issued at registration. One the operator registered
    #: is read from the environment instead, and never stored.
    client_secret: str | None = None
    #: Whether the secret comes from `client_secret_env`.
    from_env: bool = False


@dataclass(frozen=True, slots=True)
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float | None
    scope: str | None = None


def pkce() -> tuple[str, str]:
    """A verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _signature(key: str, body: str) -> str:
    mac = hmac.new(key.encode(), f"{_STATE_PREFIX}:{body}".encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode("ascii").rstrip("=")


def sign_state(key: str, flow_id: str, *, now: float | None = None) -> str:
    """A state for the provider to carry back: the flow, and when it stops being good.

    Signed under its own prefix, so a GitHub state can't be replayed as this.
    """
    expires = int(time.time() if now is None else now) + STATE_TTL_S
    body = f"{flow_id}.{expires}"
    return f"{_STATE_PREFIX}.{body}.{_signature(key, body)}"


def read_state(key: str, raw: str, *, now: float | None = None) -> str:
    """The flow a state names, or `BadState`."""
    prefix, _, rest = (raw or "").partition(".")
    body, _, signature = rest.rpartition(".")
    flow_id, _, expires = body.partition(".")
    if prefix != _STATE_PREFIX or not flow_id or not expires.isdigit():
        raise BadState("that isn't a sign-in this fleet started")
    if not hmac.compare_digest(signature, _signature(key, body)):
        raise BadState("that isn't a sign-in this fleet started")
    if int(expires) < (time.time() if now is None else now):
        raise BadState("that sign-in took too long; start again")
    return flow_id


def _json(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise OAuthError(f"{what} did not answer JSON") from exc
    if not isinstance(body, dict):
        raise OAuthError(f"{what} did not answer an object")
    return body


def _get_metadata(http: httpx.Client, url: str) -> dict[str, Any] | None:
    try:
        response = http.get(url, headers={"Accept": "application/json"}, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise OAuthError(f"{url} could not be reached: {exc}") from exc
    if response.status_code != 200:
        return None
    return _json(response, url)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def discover(http: httpx.Client, server: Server) -> Endpoints:
    """The server's OAuth endpoints: the operator's, else what the server publishes.

    Protected-resource metadata names the authorization server (RFC 9728), and
    its metadata names the endpoints (RFC 8414). Every URL found is checked as
    a catalogue URL is, since it is where a code and a secret will be sent.
    """
    settings = server.oauth
    assert settings is not None
    found: dict[str, Any] = {}
    if not (settings.authorization_endpoint and settings.token_endpoint):
        path = urlsplit(server.url).path.rstrip("/")
        origin = _origin(server.url)
        resource = None
        for candidate in (
            f"{origin}/.well-known/oauth-protected-resource{path}",
            f"{origin}/.well-known/oauth-protected-resource",
        ):
            resource = _get_metadata(http, candidate)
            if resource is not None:
                break
        issuers = (resource or {}).get("authorization_servers") or [origin]
        issuer = str(issuers[0]).rstrip("/")
        check_url(issuer, f"{server.name}: authorization server")
        for candidate in (
            f"{issuer}/.well-known/oauth-authorization-server",
            f"{issuer}/.well-known/openid-configuration",
        ):
            metadata = _get_metadata(http, candidate)
            if metadata is not None:
                found = metadata
                break
        else:
            raise OAuthError(f"{server.name} publishes no OAuth metadata, and none is configured")
    authorization = settings.authorization_endpoint or found.get("authorization_endpoint")
    token = settings.token_endpoint or found.get("token_endpoint")
    if not authorization or not token:
        raise OAuthError(f"{server.name}'s OAuth metadata names no authorization or token endpoint")
    registration = settings.registration_endpoint or found.get("registration_endpoint")
    revocation = settings.revocation_endpoint or found.get("revocation_endpoint")
    try:
        return Endpoints(
            authorization=check_url(str(authorization), "authorization endpoint"),
            token=check_url(str(token), "token endpoint"),
            registration=check_url(str(registration), "registration endpoint")
            if registration
            else None,
            revocation=check_url(str(revocation), "revocation endpoint") if revocation else None,
        )
    except McpError as exc:
        raise OAuthError(str(exc)) from exc


def client_for(
    http: httpx.Client,
    server: Server,
    endpoints: Endpoints,
    redirect_uri: str,
    env: Mapping[str, str] | None = None,
) -> Client:
    """The operator's registered client, or one registered now (RFC 7591)."""
    settings = server.oauth
    assert settings is not None
    if settings.client_id:
        return Client(settings.client_id, from_env=bool(settings.client_secret_env))
    if not endpoints.registration:
        raise OAuthError(
            f"{server.name} offers no client registration, so the operator must register "
            "Flotta with it and set client_id"
        )
    try:
        response = http.post(
            endpoints.registration,
            json={
                "client_name": "Flotta",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
            },
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise OAuthError(f"{server.name}'s registration could not be reached: {exc}") from exc
    if response.status_code not in (200, 201):
        raise OAuthError(f"{server.name} refused to register Flotta ({response.status_code})")
    body = _json(response, "registration")
    client_id = body.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise OAuthError(f"{server.name}'s registration named no client")
    secret = body.get("client_secret")
    return Client(client_id, secret if isinstance(secret, str) and secret else None)


def _secret(server: Server, client: Client, env: Mapping[str, str] | None) -> str | None:
    if not client.from_env:
        return client.client_secret
    assert server.oauth is not None and server.oauth.client_secret_env
    source = os.environ if env is None else env
    value = (source.get(server.oauth.client_secret_env) or "").strip()
    if not value:
        raise OAuthError(f"${server.oauth.client_secret_env} is not set where this runs")
    return value


def authorize_url(
    server: Server,
    endpoints: Endpoints,
    client: Client,
    *,
    redirect_uri: str,
    state: str,
    challenge: str,
) -> str:
    assert server.oauth is not None
    query = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": server.url,
    }
    if server.oauth.scopes:
        query["scope"] = " ".join(server.oauth.scopes)
    separator = "&" if urlsplit(endpoints.authorization).query else "?"
    return f"{endpoints.authorization}{separator}{urlencode(query)}"


def _tokens(response: httpx.Response, server: Server, *, now: float) -> Tokens:
    if response.status_code in (400, 401):
        try:
            error = response.json().get("error")
        except (ValueError, AttributeError):
            error = None
        raise Refused(f"{server.name} refused: {error or response.status_code}")
    if response.status_code != 200:
        raise OAuthError(f"{server.name}'s token endpoint answered {response.status_code}")
    body = _json(response, "token endpoint")
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise OAuthError(f"{server.name} gave no access token")
    expires_in = body.get("expires_in")
    refresh = body.get("refresh_token")
    scope = body.get("scope")
    return Tokens(
        access_token=access,
        refresh_token=refresh if isinstance(refresh, str) and refresh else None,
        expires_at=now + float(expires_in) if isinstance(expires_in, int | float) else None,
        scope=scope if isinstance(scope, str) else None,
    )


def _post_token(
    http: httpx.Client,
    server: Server,
    endpoints: Endpoints,
    client: Client,
    form: dict[str, str],
    env: Mapping[str, str] | None,
) -> httpx.Response:
    form = {**form, "client_id": client.client_id, "resource": server.url}
    secret = _secret(server, client, env)
    if secret:
        form["client_secret"] = secret
    try:
        return http.post(
            endpoints.token,
            data=form,
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise OAuthError(f"{server.name}'s token endpoint could not be reached: {exc}") from exc


def exchange(
    http: httpx.Client,
    server: Server,
    endpoints: Endpoints,
    client: Client,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> Tokens:
    response = _post_token(
        http,
        server,
        endpoints,
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        },
        env,
    )
    return _tokens(response, server, now=time.time() if now is None else now)


def refresh(
    http: httpx.Client,
    server: Server,
    endpoints: Endpoints,
    client: Client,
    refresh_token: str,
    *,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> Tokens:
    """New tokens. A provider that doesn't send a new refresh token keeps the old one."""
    response = _post_token(
        http,
        server,
        endpoints,
        client,
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
        env,
    )
    tokens = _tokens(response, server, now=time.time() if now is None else now)
    if tokens.refresh_token is None:
        tokens = Tokens(tokens.access_token, refresh_token, tokens.expires_at, tokens.scope)
    return tokens


def revoke(
    http: httpx.Client,
    server: Server,
    endpoints: Endpoints,
    client: Client,
    token: str,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Ask the provider to forget a token (RFC 7009). False if it couldn't be asked."""
    if not endpoints.revocation:
        return False
    form = {"token": token, "client_id": client.client_id}
    try:
        secret = _secret(server, client, env)
    except OAuthError:
        secret = None
    if secret:
        form["client_secret"] = secret
    try:
        response = http.post(endpoints.revocation, data=form, timeout=_TIMEOUT)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def needs_refresh(credential: Mapping[str, Any], *, now: float | None = None) -> bool:
    expires_at = credential.get("expires_at")
    if not isinstance(expires_at, int | float):
        return False
    return expires_at - REFRESH_EARLY_S <= (time.time() if now is None else now)


# -- what is kept, sealed --------------------------------------------------------


def oauth_credential(endpoints: Endpoints, client: Client, tokens: Tokens) -> dict[str, Any]:
    """Everything a refresh needs, in one sealed value: no other table to keep in step."""
    return {
        "kind": "oauth",
        "endpoints": asdict(endpoints),
        "client": asdict(client),
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_at": tokens.expires_at,
        "scope": tokens.scope,
    }


def refreshed(credential: Mapping[str, Any], tokens: Tokens) -> dict[str, Any]:
    return {
        **credential,
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_at": tokens.expires_at,
        "scope": tokens.scope or credential.get("scope"),
    }


def endpoints_of(credential: Mapping[str, Any]) -> Endpoints:
    return Endpoints(**credential["endpoints"])


def client_of(credential: Mapping[str, Any]) -> Client:
    return Client(**credential["client"])


def flow_payload(endpoints: Endpoints, client: Client, verifier: str, redirect_uri: str) -> str:
    return json.dumps(
        {
            "endpoints": asdict(endpoints),
            "client": asdict(client),
            "verifier": verifier,
            "redirect_uri": redirect_uri,
        }
    )


def flow_context(flow_id: str) -> str:
    return f"mcp-oauth-flow:{flow_id}"
