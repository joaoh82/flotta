"""The HQ connector library (FLOTTA-225).

A pre-built connector is a normal catalogue entry. Adding one writes
`FLOTTA_MCP_SERVERS` through the same validation as the settings page, so
custom entries (Marvin, a hand-written server) stay exactly what they are.

`auth` here is `oauth` or `api_key`, which is what the console shows. An
`api_key` connector with no named header is stored as catalogue `bearer`
(`Authorization: Bearer`), the header an access token already uses. OAuth
entries name no `client_id`, so the existing dynamic registration signs Flotta
in. Flotta registers no OAuth app of its own.

Read tools are the default grant. Write tools are in the catalogue so an
agency can opt into them, and off until it does. A tool this file does not
classify is write: it is not in the default grant. Where the vendor publishes
no stable names (Linear's docs, Make's per-account scenario tools), the entry
is `dynamic_tools`: nothing is on by default, and naming a tool opts into it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flotta.alerts import WebhookRefused, check_webhook_url
from flotta.mcp import McpError, Server, parse_server

_PATH = Path(__file__).with_name("mcp_library.json")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"^(?:{_LABEL}\.)+[a-z]{{2,63}}$")
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain")
_TEMPLATE = re.compile(r"^https://\{host\}/[A-Za-z0-9._~/-]+$")
_TOOL = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
_FIELDS = frozenset(
    {
        "id",
        "name",
        "icon",
        "category",
        "summary",
        "transport",
        "auth",
        "url",
        "url_template",
        "allowed_hosts",
        "params",
        "key_hint",
        "key_header",
        "read_tools",
        "write_tools",
        "oauth_scopes",
        "docs_url",
        "dynamic_tools",
    }
)


class LibraryError(ValueError):
    """A library entry or a template URL that cannot be used. Never echoes a secret."""


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    label: str


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    id: str
    name: str
    icon: str
    category: str
    summary: str
    transport: str
    auth: str
    url: str | None
    url_template: str | None
    allowed_hosts: tuple[str, ...]
    params: tuple[Param, ...]
    key_hint: str | None
    key_header: str | None
    read_tools: tuple[str, ...]
    write_tools: tuple[str, ...]
    oauth_scopes: tuple[str, ...]
    docs_url: str
    dynamic_tools: bool

    def public(self) -> dict[str, Any]:
        """What the console lists. No secret, and no field the catalogue would seal."""
        return {
            "id": self.id,
            "name": self.name,
            "icon": self.icon,
            "category": self.category,
            "summary": self.summary,
            "transport": self.transport,
            "auth": self.auth,
            "url": self.url,
            "url_template": self.url_template,
            "allowed_hosts": list(self.allowed_hosts),
            "params": [{"name": p.name, "label": p.label} for p in self.params],
            "key_hint": self.key_hint,
            "read_tools": list(self.read_tools),
            "write_tools": list(self.write_tools),
            "oauth_scopes": list(self.oauth_scopes),
            "docs_url": self.docs_url,
            "dynamic_tools": self.dynamic_tools,
        }


def classify_tool(
    name: str, *, read_only_hint: bool | None = None, known_read: frozenset[str] = frozenset()
) -> str:
    """`read` or `write`. An annotation wins. An unknown tool with no annotation is write."""
    if read_only_hint is True:
        return "read"
    if read_only_hint is False:
        return "write"
    if name in known_read:
        return "read"
    return "write"


def parse_library(raw: str) -> tuple[LibraryEntry, ...]:
    """The manifest, or `LibraryError`. Each entry must already be a valid catalogue server."""
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise LibraryError("the connector library is not JSON") from exc
    if not isinstance(body, list) or not body:
        raise LibraryError("the connector library is a non-empty list")
    found: dict[str, LibraryEntry] = {}
    for item in body:
        entry = _entry(item)
        if entry.id in found:
            raise LibraryError(f"{entry.id} is in the library twice")
        found[entry.id] = entry
        _proves_it_parses(entry)
    return tuple(found.values())


def entries() -> tuple[LibraryEntry, ...]:
    return _ENTRIES


def entry(entry_id: str) -> LibraryEntry | None:
    return _BY_ID.get(entry_id)


def public_entries() -> list[dict[str, Any]]:
    return [item.public() for item in _ENTRIES]


def catalogue_entry(item: LibraryEntry, params: dict[str, str]) -> dict[str, Any]:
    """One catalogue object, as `parse_server` reads it."""
    url = resolve_url(item, params)
    tools = [*item.read_tools, *item.write_tools]
    built: dict[str, Any] = {
        "name": item.id,
        "title": item.name,
        "url": url,
        "auth": "oauth" if item.auth == "oauth" else "bearer",
        "ask_first": True,
    }
    if tools:
        built["tools"] = tools
    elif not item.dynamic_tools:
        raise LibraryError(f"{item.id} has no tools and is not marked dynamic")
    if item.auth == "oauth":
        oauth: dict[str, Any] = {}
        if item.oauth_scopes:
            oauth["scopes"] = list(item.oauth_scopes)
        built["oauth"] = oauth
    elif item.key_header:
        built["auth"] = "api_key"
        built["header"] = item.key_header
    return built


def resolve_url(item: LibraryEntry, params: dict[str, str]) -> str:
    """The entry's URL, or `LibraryError` that does not repeat what was submitted.

    A template is `https://{host}/<fixed path>`. The host is a public DNS name
    matching the entry's pattern: no address, no localhost, no `.local` or
    `.internal`, and no userinfo or query a secret could hide in.
    """
    if not isinstance(params, dict):
        raise LibraryError("params is an object of strings")
    unknown = set(params) - {p.name for p in item.params}
    if unknown:
        raise LibraryError("this connector does not take that parameter")
    missing = [p.name for p in item.params if p.name not in params or params[p.name] == ""]
    if missing:
        raise LibraryError("this connector needs a host")
    if item.url_template is None:
        if params:
            raise LibraryError("this connector does not take a host")
        return _checked(item, item.url or "")
    if set(params) != {"host"}:
        raise LibraryError("this connector needs a host")
    host = _public_host(params["host"])
    return _checked(item, item.url_template.replace("{host}", host, 1))


def for_server(server: Server) -> LibraryEntry | None:
    """The library entry a catalogue server was added from, matched by name and host.

    A hand-written server that merely reuses a name, or points somewhere else,
    is not one of these.
    """
    item = _BY_ID.get(server.name)
    if item is None:
        return None
    host = (urlsplit(server.url).hostname or "").lower().rstrip(".")
    if not _host_allowed(host, item.allowed_hosts):
        return None
    return item


def default_grant_tools(
    server: Server, requested: tuple[str, ...] | None
) -> tuple[str, ...] | None:
    """The tools a grant stores. An explicit list is kept. An omitted one is the read set.

    A server this library does not recognise keeps `None`, which means every
    tool its catalogue entry allows. A library server whose catalogue entry
    lists no tools (a dynamic one) defaults to its read tools, which may be
    none: nothing is on until a tool is named.
    """
    if requested is not None:
        return requested
    item = for_server(server)
    if item is None:
        return None
    if item.dynamic_tools:
        return tuple(item.read_tools)
    if server.tools is None:
        # The same name and host, but the operator left the tool list open.
        # That is a hand-written entry, and it keeps meaning "every tool".
        return None
    return tuple(tool for tool in item.read_tools if tool in server.tools)


def _proves_it_parses(item: LibraryEntry) -> None:
    sample = {}
    if item.params:
        sample = {"host": _sample_host(item)}
    try:
        parse_server(catalogue_entry(item, sample))
    except McpError as exc:
        raise LibraryError(f"{item.id} is not a valid catalogue entry") from exc


def _sample_host(item: LibraryEntry) -> str:
    for pattern in item.allowed_hosts:
        if pattern != "*" and "*" not in pattern:
            return pattern
    return "library.example.com"


def _entry(raw: Any) -> LibraryEntry:
    if not isinstance(raw, dict):
        raise LibraryError("each connector is an object")
    unknown = set(raw) - _FIELDS
    if unknown:
        raise LibraryError("a connector has an unknown field")
    missing = _FIELDS - set(raw)
    if missing:
        raise LibraryError("a connector is missing a field")
    identifier = raw["id"]
    if not isinstance(identifier, str) or not _NAME.match(identifier):
        raise LibraryError("a connector id is a lowercase catalogue name")
    _text(raw["name"], "name")
    _text(raw["icon"], "icon")
    _text(raw["category"], "category")
    _text(raw["summary"], "summary")
    if raw["transport"] != "streamable-http":
        raise LibraryError(f"{identifier} is not streamable HTTP")
    if raw["auth"] not in ("oauth", "api_key"):
        raise LibraryError(f"{identifier} auth is oauth or api_key")
    url, template = raw["url"], raw["url_template"]
    if (url is None) == (template is None):
        raise LibraryError(f"{identifier} has one of a url and a url template")
    if url is not None and not isinstance(url, str):
        raise LibraryError(f"{identifier} url is a string")
    if template is not None and (not isinstance(template, str) or not _TEMPLATE.match(template)):
        raise LibraryError(f"{identifier}'s template is https://{{host}}/ and a fixed path")
    hosts = _strings(raw["allowed_hosts"], f"{identifier} allowed_hosts")
    if not hosts:
        raise LibraryError(f"{identifier} names the hosts it allows")
    params = _params(raw["params"], template is not None)
    key_hint = raw["key_hint"]
    if raw["auth"] == "api_key":
        if not isinstance(key_hint, str) or not key_hint.strip():
            raise LibraryError(f"{identifier} needs a key format hint")
    elif key_hint is not None:
        raise LibraryError(f"{identifier} takes no key")
    key_header = raw["key_header"]
    if key_header is not None and (
        raw["auth"] != "api_key" or not isinstance(key_header, str) or not key_header
    ):
        raise LibraryError(f"{identifier} names a key header only for an api_key connector")
    read_tools = _tools(raw["read_tools"])
    write_tools = _tools(raw["write_tools"])
    overlap = set(read_tools) & set(write_tools)
    if overlap:
        raise LibraryError(f"{identifier} lists a tool as both read and write")
    dynamic = raw["dynamic_tools"]
    if not isinstance(dynamic, bool):
        raise LibraryError(f"{identifier} dynamic_tools is true or false")
    if not read_tools and not write_tools and not dynamic:
        raise LibraryError(f"{identifier} has no tools and is not marked dynamic")
    scopes = _strings(raw["oauth_scopes"], f"{identifier} oauth_scopes")
    if raw["auth"] != "oauth" and scopes:
        raise LibraryError(f"{identifier} has no OAuth scopes")
    docs = raw["docs_url"]
    if not isinstance(docs, str) or urlsplit(docs).scheme != "https":
        raise LibraryError(f"{identifier} docs_url is https")
    return LibraryEntry(
        id=identifier,
        name=raw["name"].strip(),
        icon=raw["icon"].strip(),
        category=raw["category"].strip(),
        summary=raw["summary"].strip(),
        transport=raw["transport"],
        auth=raw["auth"],
        url=url,
        url_template=template,
        allowed_hosts=tuple(hosts),
        params=params,
        key_hint=key_hint.strip() if isinstance(key_hint, str) else None,
        key_header=key_header,
        read_tools=tuple(read_tools),
        write_tools=tuple(write_tools),
        oauth_scopes=tuple(scopes),
        docs_url=docs,
        dynamic_tools=dynamic,
    )


def _text(value: Any, what: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\u2014" in value:
        raise LibraryError(f"a connector {what} is plain text")


def _strings(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise LibraryError(f"{what} is a list of strings")
    return value


def _tools(value: Any) -> list[str]:
    names = _strings(value, "tools")
    if any(_TOOL.match(name) is None for name in names):
        raise LibraryError("a tool name is letters, digits and ._-")
    if len(names) != len(set(names)):
        raise LibraryError("a tool is listed twice")
    return names


def _params(value: Any, templated: bool) -> tuple[Param, ...]:
    if not isinstance(value, list):
        raise LibraryError("params is a list")
    if templated:
        if len(value) != 1 or not isinstance(value[0], dict):
            raise LibraryError("a template takes one host parameter")
        if set(value[0]) != {"name", "label"} or value[0]["name"] != "host":
            raise LibraryError("a template's parameter is host")
        _text(value[0]["label"], "parameter label")
        return (Param("host", value[0]["label"].strip()),)
    if value:
        raise LibraryError("a fixed connector takes no parameters")
    return ()


def _public_host(value: str) -> str:
    if not isinstance(value, str):
        raise LibraryError("the host is not a public DNS name")
    host = value.strip().lower()
    if host.endswith("."):
        host = host[:-1]
    if not _HOST.match(host) or host.endswith(_BLOCKED_SUFFIXES):
        raise LibraryError("the host is not a public DNS name")
    return host


def _host_allowed(host: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        if pattern == "*":
            return True
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if host.endswith(suffix) and host != suffix.lstrip("."):
                return True
        elif host == pattern:
            return True
    return False


def _checked(item: LibraryEntry, url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or not host
        or not _host_allowed(host, item.allowed_hosts)
    ):
        raise LibraryError("the address is not allowed for this connector")
    if not _HOST.match(host) or host.endswith(_BLOCKED_SUFFIXES):
        raise LibraryError("the address is not allowed for this connector")
    try:
        check_webhook_url(url)
    except WebhookRefused:
        raise LibraryError("the address is not allowed for this connector") from None
    return url


_ENTRIES = parse_library(_PATH.read_text())
_BY_ID = {item.id: item for item in _ENTRIES}
