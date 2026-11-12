"""MCP servers an agent may use, through the gateway (FLOTTA-125).

Design: `docs/MCP_SERVERS.md` in the parent repo. The short version: an agent
reaches an MCP server only through `/mcp/<server>` on the gateway, which checks
the grant on every request, hides and refuses tools that weren't granted, and
puts the agency's credential on the request. **No upstream credential is ever on
a box**, for the reason FLOTTA-20 put none there for GitHub: the agent has root,
so whatever is on its machine is its to read, and to keep after the grant goes.

## Who decides what

- **The operator defines the catalogue,** the fleet setting
  `FLOTTA_MCP_SERVERS`, a JSON list, edited in the operator's console beside the
  model list and kept the way that list is (`flotta.settings`): **a stored value
  wins, the environment is the fallback**, the whole list either way. Writing
  it is the operator's alone (`PUT /api/settings`), because an entry decides
  where an agency's credential is sent. Two guards make that safe to edit over
  HTTP: a connection remembers the URL it was made for and is never sent to
  another one (`connection_usable`), and an operator's OAuth client secret is
  not in the setting at all but sealed in a table of its own
  (`mcp_server_secrets`), set and never read back. Read on every request by the
  control plane and the gateway (`catalogue_of`), so a change is in force on
  the next call.
- **An agency picks from it**: its owners connect a server (a key, or an OAuth
  sign-in), and its owners and members grant it to agents, choosing tools and
  whether calls ask first. Never a client's people.

## A catalogue entry

```json
{"name": "linear", "title": "Linear", "url": "https://mcp.linear.app/mcp",
 "auth": "oauth", "ask_first": true, "tools": ["list_issues", "create_issue"],
 "oauth": {"scopes": ["read", "write"]}}
```

This example is hand-written. It carries no `"library": true`, so an omitted
grant still means every tool it lists. Only an entry the connector library
wrote has that marker (`flotta.mcp_library`).

`auth` is `bearer` (sent as `Authorization: Bearer`), `api_key` (sent in the
`header` the entry names) or `oauth` (see `flotta.mcp_oauth`; endpoints are
discovered unless given, and a client registers itself unless `client_id` is
given, with its secret either set in the console, sealed and never returned, or
read from the variable `client_secret_env` names).
`tools`, if given, is every tool an agency may grant; without it, any tool the
server lists. Remote servers only: a server that runs on the box would need its
secret on the box.

## What the agent's `config.yaml` gets

One `mcp_servers` entry per grant, `flotta-<server>`, pointing at the gateway
and carrying the box's own `mcp:call` token, written by the FLOTTA-124 writer
(`provision.set_mcp_grants`). **The token is written into the file, not as
`${VAR}`**: it is the agent's own and confined to it, so the agent reading it
learns nothing it couldn't already use, and a literal value needs neither a
restart to add a secret nor Hermes resolving the variable. `tools.include` is
written so the agent isn't shown tools it can't call; the gateway enforces it.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

CATALOGUE_ENV = "FLOTTA_MCP_SERVERS"
ENTRY_PREFIX = "flotta-"
AUTH_KINDS = ("bearer", "api_key", "oauth")

#: A server's name is part of a URL path and a Hermes toolset (`mcp-flotta-<name>`).
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
#: Exact names only: the gateway compares them, so a glob would grant more than it shows.
_TOOL = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
_HEADER = re.compile(r"^[A-Za-z0-9-]{1,64}$")
#: Headers a key must not be sent in: they mean something else to every hop.
_NOT_A_KEY_HEADER = frozenset(
    {"host", "content-type", "content-length", "accept", "mcp-session-id", "cookie"}
)
MAX_TOOLS = 200


class McpError(ValueError):
    """A catalogue, a grant or a credential that can't be used as it is."""


@dataclass(frozen=True, slots=True)
class OAuthSettings:
    """What the operator says about a server's OAuth. Anything left out is discovered."""

    client_id: str | None = None
    client_secret_env: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    registration_endpoint: str | None = None
    revocation_endpoint: str | None = None
    scopes: tuple[str, ...] = ()
    #: The operator's client secret set in the console, still sealed
    #: (`server_secret_context`). Never in an answer, never in the setting.
    client_secret_sealed: str | None = None

    def spec(self) -> dict[str, Any]:
        """What the operator wrote, without the secret."""
        out: dict[str, Any] = {}
        for field in (
            "client_id",
            "client_secret_env",
            "authorization_endpoint",
            "token_endpoint",
            "registration_endpoint",
            "revocation_endpoint",
        ):
            value = getattr(self, field)
            if value:
                out[field] = value
        if self.scopes:
            out["scopes"] = list(self.scopes)
        return out


@dataclass(frozen=True, slots=True)
class Server:
    name: str
    title: str
    url: str
    auth: str
    ask_first: bool = True
    #: Every tool an agency may grant, or `None` for whatever the server lists.
    tools: tuple[str, ...] | None = None
    #: The header an `api_key` goes in.
    header: str | None = None
    oauth: OAuthSettings | None = None
    #: Set by the connector library. A hand-written entry leaves it false, and an
    #: omitted grant then still means every tool (`flotta.mcp_library`).
    library: bool = False

    @property
    def entry(self) -> str:
        return f"{ENTRY_PREFIX}{self.name}"

    def spec(self) -> dict[str, Any]:
        """The entry as `parse_server` reads it: what the setting holds."""
        out: dict[str, Any] = {
            "name": self.name,
            "title": self.title,
            "url": self.url,
            "auth": self.auth,
            "ask_first": self.ask_first,
        }
        if self.tools is not None:
            out["tools"] = list(self.tools)
        if self.header is not None:
            out["header"] = self.header
        if self.oauth is not None:
            out["oauth"] = self.oauth.spec()
        if self.library:
            out["library"] = True
        return out

    def for_operator(self) -> dict[str, Any]:
        """What the console edits: the whole entry, and whether a client secret is set."""
        return {**self.spec(), "has_client_secret": self.has_client_secret}

    @property
    def has_client_secret(self) -> bool:
        return bool(self.oauth and self.oauth.client_secret_sealed)

    def public(self) -> dict[str, Any]:
        """What anyone in the fleet may read: no client secret's name, no endpoints."""
        return {
            "name": self.name,
            "title": self.title,
            "url": self.url,
            "auth": self.auth,
            "ask_first": self.ask_first,
            "tools": list(self.tools) if self.tools is not None else None,
        }


@dataclass(frozen=True, slots=True)
class Grant:
    server: str
    #: The tools granted, or `None` for every tool the catalogue allows.
    tools: tuple[str, ...] | None
    ask_first: bool

    def allows(self, server: Server, tool: str) -> bool:
        if self.tools is not None:
            return tool in self.tools
        return server.tools is None or tool in server.tools

    def to_json(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "tools": list(self.tools) if self.tools is not None else None,
            "ask_first": self.ask_first,
        }


def _loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(url: str, what: str) -> str:
    """HTTPS, or HTTP to this machine for local work. No credentials in it."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.username or parts.password:
        raise McpError(f"{what} must not carry a user or password")
    if parts.scheme == "https" and host:
        return url
    if parts.scheme == "http" and _loopback(host):
        return url
    raise McpError(f"{what} must be an https:// URL")


def _str(raw: Any, what: str, *, required: bool = True) -> str | None:
    if raw is None and not required:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise McpError(f"{what} is a string")
    return raw.strip()


def _tool_names(raw: Any, what: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise McpError(f"{what} is a list of tool names")
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not _TOOL.match(item):
            raise McpError(f"{what} holds tool names: letters, digits, _ . -")
        if item not in names:
            names.append(item)
    if len(names) > MAX_TOOLS:
        raise McpError(f"{what} holds at most {MAX_TOOLS}")
    return tuple(names)


def _oauth(raw: Any, name: str) -> OAuthSettings:
    if raw is None:
        return OAuthSettings()
    if not isinstance(raw, dict):
        raise McpError(f"{name}: oauth is an object")
    known = set(OAuthSettings.__slots__)
    unknown = set(raw) - known
    if unknown:
        raise McpError(f"{name}: unknown oauth fields: {', '.join(sorted(unknown))}")
    endpoints = {}
    for field in (
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "revocation_endpoint",
    ):
        value = _str(raw.get(field), f"{name}: oauth.{field}", required=False)
        endpoints[field] = check_url(value, f"{name}: oauth.{field}") if value else None
    scopes = raw.get("scopes") or []
    if not isinstance(scopes, list) or not all(isinstance(s, str) and s for s in scopes):
        raise McpError(f"{name}: oauth.scopes is a list of strings")
    secret_env = _str(raw.get("client_secret_env"), f"{name}: client_secret_env", required=False)
    if secret_env and not secret_env.startswith("FLOTTA_MCP_"):
        # Read from this process's environment: confined to names that can only
        # have been set for this, so an entry can't name `$FLOTTA_SIGNING_KEY`.
        raise McpError(f"{name}: client_secret_env must start with FLOTTA_MCP_")
    return OAuthSettings(
        client_id=_str(raw.get("client_id"), f"{name}: client_id", required=False),
        client_secret_env=secret_env,
        scopes=tuple(scopes),
        **endpoints,
    )


def parse_server(raw: Any) -> Server:
    if not isinstance(raw, dict):
        raise McpError("each MCP server is an object")
    name = _str(raw.get("name"), "name")
    assert name is not None
    if not _NAME.match(name):
        raise McpError(f"{name!r}: a name is lowercase letters, digits and -, at most 40")
    known = {"name", "title", "url", "auth", "ask_first", "tools", "header", "oauth", "library"}
    unknown = set(raw) - known
    if unknown:
        raise McpError(f"{name}: unknown fields: {', '.join(sorted(unknown))}")
    url = check_url(_str(raw.get("url"), f"{name}: url") or "", f"{name}: url")
    auth = raw.get("auth")
    if auth not in AUTH_KINDS:
        raise McpError(f"{name}: auth is one of {', '.join(AUTH_KINDS)}")
    header = _str(raw.get("header"), f"{name}: header", required=False)
    if auth == "api_key":
        if header is None or not _HEADER.match(header):
            raise McpError(f"{name}: an api_key server names the header it goes in")
        if header.lower() in _NOT_A_KEY_HEADER:
            raise McpError(f"{name}: a key can't go in {header}")
    elif header is not None:
        raise McpError(f"{name}: only an api_key server names a header")
    if auth != "oauth" and raw.get("oauth") is not None:
        raise McpError(f"{name}: only an oauth server has oauth settings")
    ask_first = raw.get("ask_first", True)
    if not isinstance(ask_first, bool):
        raise McpError(f"{name}: ask_first is true or false")
    tools = raw.get("tools")
    library = raw.get("library", False)
    if not isinstance(library, bool):
        raise McpError(f"{name}: library is true or false")
    return Server(
        name=name,
        title=_str(raw.get("title"), f"{name}: title", required=False) or name,
        url=url,
        auth=auth,
        ask_first=ask_first,
        tools=_tool_names(tools, f"{name}: tools") if tools is not None else None,
        header=header,
        oauth=_oauth(raw.get("oauth"), name) if auth == "oauth" else None,
        library=library,
    )


def parse_catalogue(raw: str | None) -> dict[str, Server]:
    if not raw or not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise McpError(f"${CATALOGUE_ENV} is not JSON: {exc}") from exc
    if not isinstance(body, list):
        raise McpError(f"${CATALOGUE_ENV} is a JSON list of servers")
    servers: dict[str, Server] = {}
    for item in body:
        server = parse_server(item)
        if server.name in servers:
            raise McpError(f"{server.name} is in ${CATALOGUE_ENV} twice")
        servers[server.name] = server
    return servers


def catalogue(env: Mapping[str, str] | None = None) -> dict[str, Server]:
    """The servers `env` names, by name. Raises `McpError` for one that can't be used.

    Pass `flotta.settings.layered(store)` for the fleet's: the stored list, or
    the environment's. `catalogue_of` does, and adds the stored secrets.
    """
    source = os.environ if env is None else env
    return parse_catalogue(source.get(CATALOGUE_ENV))


def normalized(raw: str) -> str:
    """The setting as it is stored: parsed, then written back, so a bad one is refused."""
    servers = parse_catalogue(raw)
    return json.dumps([s.spec() for s in servers.values()], separators=(",", ":"))


def catalogue_of(
    store: Any, env: Mapping[str, str] | None = None
) -> tuple[dict[str, Server], str | None]:
    """The fleet's servers, by name, with their client secrets, and an error if unreadable.

    Served empty with the reason when the list can't be read, rather than raising:
    a server that isn't offered is refused at the gateway, which is safe, and one
    typo must not stop every agent's models going through the same process.
    """
    from flotta.settings import layered

    try:
        servers = catalogue(layered(store, env))
    except McpError as exc:
        return {}, str(exc)
    secrets = store.mcp_server_secrets()
    for name, server in list(servers.items()):
        held = secrets.get(name)
        oauth = server.oauth
        if held and oauth is not None and oauth.client_id and not oauth.client_secret_env:
            servers[name] = replace(server, oauth=replace(oauth, client_secret_sealed=held))
    return servers, None


def server_secret_context(name: str) -> str:
    """What an operator's OAuth client secret is sealed to: its own server, nowhere else."""
    return f"mcp-server-secret:{name}"


def parse_client_secret(body: Any, server: Server) -> str:
    """The client secret the operator sets for `server`, or `McpError`."""
    if server.oauth is None:
        raise McpError(f"{server.name} doesn't sign in with OAuth, so it has no client secret")
    if not server.oauth.client_id:
        raise McpError(
            f"{server.name} registers itself with the provider; a client secret goes with "
            "the client_id the operator registered"
        )
    if server.oauth.client_secret_env:
        raise McpError(
            f"{server.name}'s client secret is read from ${server.oauth.client_secret_env}"
        )
    if not isinstance(body, dict) or set(body) - {"client_secret"}:
        raise McpError('a client secret is {"client_secret": "..."}')
    value = body.get("client_secret")
    if not isinstance(value, str) or not value.strip():
        raise McpError("the client secret is empty")
    value = value.strip()
    if len(value) > 4096 or any(ch in value for ch in "\r\n\0"):
        raise McpError("that isn't a client secret")
    return value


#: Written into a sealed OAuth credential when its access token is spent and
#: there is no refresh token. Readers use this, never the human `detail`.
CREDENTIAL_FAILURE_EXPIRED = "expired"


def connection_status(
    connection: Any, server: Server | None, *, env: Mapping[str, str] | None = None
) -> str:
    """`not_connected`, `connected`, `error` or `expired` for an org's MCP list.

    No row is `not_connected`. One the gateway would use (`connection_usable`
    is silent) is `connected`. A broken OAuth credential that says
    `failure` is `expired`, or that has no refresh token and an `expires_at`
    already past, is `expired`. Every other refusal is `error`. The human
    `detail` is not read.
    """
    if connection is None:
        return "not_connected"
    if connection_usable(connection, server, env) is None:
        return "connected"
    if connection.status == "broken" and _expired_unrefreshable(connection, env):
        return "expired"
    return "error"


def _expired_unrefreshable(connection: Any, env: Mapping[str, str] | None) -> bool:
    """Whether this broken OAuth credential can no longer be renewed.

    The flag the gateway writes, or the credential's own `expires_at` and
    `refresh_token`. A bearer key, a seal that will not open, and a token that
    still has a refresh token are not this.
    """
    if connection.kind != "oauth":
        return False
    from flotta.sealed import SealError, unseal

    client = connection.client_id or None
    try:
        raw = unseal(
            connection.credential,
            context=connection_context(connection.org_id, connection.server, client),
            env=env,
        )
        body = json.loads(raw)
    except (SealError, ValueError):
        return False
    if not isinstance(body, dict):
        return False
    if body.get("failure") == CREDENTIAL_FAILURE_EXPIRED:
        return True
    refresh = body.get("refresh_token")
    if isinstance(refresh, str) and refresh:
        return False
    expires_at = body.get("expires_at")
    return isinstance(expires_at, int | float) and float(expires_at) <= time.time()


def library_fields(server: Server) -> dict[str, Any]:
    """The explicit library marker, and its manifest id.

    Only `library: true`, which `catalogue_entry` writes. The same name and
    host, and a spec that merely matches, are a hand-written server: `library`
    false and no `library_id`.
    """
    if not server.library:
        return {"library": False}
    return {"library": True, "library_id": server.name}


def connection_usable(
    connection: Any, server: Server | None, env: Mapping[str, str] | None = None
) -> str | None:
    """Why a stored connection won't be used for `server` now, or `None` if it will.

    The gateway's rule, and what every read of a connection reports, so a page
    never shows a connection in use that the gateway refuses:

    - **no longer offered,** or **of another kind** than the server now signs in
      with;
    - **made for another URL, or for none on record.** An entry's URL can change
      in the console, and an agency's credential was given for the server it was
      connected to, never for wherever the entry points next. Connections from
      before the URL was recorded were bound when the store was upgraded
      (`FleetStore._bind_mcp_connections`); one that couldn't be is never used,
      so removing a server and adding it back elsewhere can't rebind it;
    - **sealed under a key this process doesn't hold** (`flotta.sealed.opens_here`):
      the signing key changed, or a secrets key was set since;
    - **broken** already, once a provider refused a refresh.
    """
    from flotta.sealed import opens_here

    if server is None:
        return "no longer offered by this Flotta"
    if connection.kind != server.auth:
        return f"{server.title} now signs in another way: connect it again"
    if not connection.server_url:
        return (
            f"Flotta has no record of the address {server.title} was connected for: "
            "connect it again"
        )
    if connection.server_url != server.url:
        return f"{server.title} has moved to another address since: connect it again"
    if not opens_here(connection.credential, env=env):
        return "sealed under a key this Flotta no longer holds: connect it again"
    if connection.status != "ok":
        return connection.detail or f"{server.title}'s connection stopped working: connect it again"
    return None


def parse_grants(body: Any, servers: Mapping[str, Server]) -> tuple[Grant, ...]:
    """An agent's grants from an API body, `{"servers": [...]}`, or `McpError`.

    A full replace, like the rules: a server left out is no longer granted.
    """
    if not isinstance(body, dict) or set(body) - {"servers"}:
        raise McpError('grants are {"servers": [...]}')
    items = body.get("servers") or []
    if not isinstance(items, list):
        raise McpError("servers is a list")
    grants: dict[str, Grant] = {}
    for item in items:
        if not isinstance(item, dict) or set(item) - {"server", "tools", "ask_first"}:
            raise McpError('each grant is {"server", "tools", "ask_first"}')
        name = item.get("server")
        server = servers.get(name) if isinstance(name, str) else None
        if server is None:
            raise McpError(f"{name!r} is not one of this fleet's MCP servers")
        if name in grants:
            raise McpError(f"{name} is granted twice")
        raw_tools = item.get("tools")
        tools = None if raw_tools is None else _tool_names(raw_tools, f"{name}: tools")
        if tools is not None and server.tools is not None:
            outside = [t for t in tools if t not in server.tools]
            if outside:
                raise McpError(f"{name} doesn't offer {', '.join(outside)}")
        # A library connector's omitted tool list is its read tools, not every
        # tool the catalogue names. A hand-written entry is left as it was.
        from flotta.mcp_library import default_grant_tools

        tools = default_grant_tools(server, tools)
        ask_first = item.get("ask_first", server.ask_first)
        if not isinstance(ask_first, bool):
            raise McpError(f"{name}: ask_first is true or false")
        grants[server.name] = Grant(server.name, tools, ask_first)
    return tuple(grants.values())


def grant_from_row(server: str, tools: str | None, ask_first: bool) -> Grant:
    names = json.loads(tools) if tools else None
    return Grant(server, tuple(names) if names is not None else None, bool(ask_first))


def mcp_base(env: Mapping[str, str]) -> str | None:
    """Where the gateway is, without the `/v1` models live under."""
    url = (env.get("FLOTTA_GATEWAY_URL") or "").strip().rstrip("/")
    if not url:
        return None
    return url[: -len("/v1")] if url.endswith("/v1") else url


def config_entry(grant: Grant, server: Server, *, base: str, token: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "url": f"{base}/mcp/{server.name}",
        "headers": {"Authorization": f"Bearer {token}"},
    }
    # An empty grant is a filter of nothing. Omitting `tools` is "no filter",
    # which would show Hermes every tool the server lists.
    tools = grant.tools if grant.tools is not None else server.tools
    if tools is not None:
        entry["tools"] = {"include": list(tools)}
    if grant.ask_first:
        entry["trust"] = "untrusted"
    return entry


def config_changes(
    grants: Iterable[Grant],
    servers: Mapping[str, Server],
    *,
    base: str,
    token: str,
    previous: Iterable[str] = (),
) -> list[list[Any]]:
    """`[section, key, value]` for the FLOTTA-124 writer: each `flotta-<server>` set or removed.

    Removed for every server in the catalogue that isn't granted, and for every
    one granted before, so a grant taken away leaves no entry behind. An entry
    the agent wrote under another name is not Flotta's and is never touched.
    """
    granted = {g.server: g for g in grants}
    names = list(dict.fromkeys([*servers, *previous, *granted]))
    changes: list[list[Any]] = []
    for name in names:
        grant, server = granted.get(name), servers.get(name)
        value = config_entry(grant, server, base=base, token=token) if grant and server else None
        changes.append(["mcp_servers", f"{ENTRY_PREFIX}{name}", value])
    return changes


# -- credentials ---------------------------------------------------------------


def connection_context(org: str, server: str, client: str | None = None) -> str:
    """What a connection's sealed credential is bound to (`flotta.sealed`).

    A client's is bound to that client as well, so a sealed value copied onto
    another client's row, or onto the org's, doesn't open there. The org's
    context is the one slice 1 sealed with, so its rows still open.
    """
    if client:
        return f"mcp-client-connection:{org}:{client}:{server}"
    return f"mcp-connection:{org}:{server}"


def parse_key(body: Any, server: Server) -> str:
    """The key an owner pastes for a `bearer` or `api_key` server."""
    if server.auth == "oauth":
        raise McpError(f"{server.name} is connected by signing in, not with a key")
    if not isinstance(body, dict) or set(body) - {"key"}:
        raise McpError('a key is {"key": "..."}')
    key = body.get("key")
    if not isinstance(key, str) or not key.strip():
        raise McpError("the key is empty")
    key = key.strip()
    if len(key) > 4096 or any(ch in key for ch in "\r\n\0"):
        raise McpError("that isn't a key that fits in a header")
    return key


def auth_headers(server: Server, credential: Mapping[str, Any]) -> dict[str, str]:
    """The header the upstream reads a credential from."""
    if server.auth == "api_key":
        assert server.header is not None
        return {server.header: str(credential["value"])}
    if server.auth == "oauth":
        return {"Authorization": f"Bearer {credential['access_token']}"}
    return {"Authorization": f"Bearer {credential['value']}"}
