"""The MCP half of the gateway: `/mcp/<server>` (FLOTTA-125).

An agent's `config.yaml` names this URL for every server granted to it, with
its own `mcp:call` token (`flotta.mcp`). Every request is checked here, and
nothing about the upstream is on the box, so all of this holds against an agent
that has read its own config:

- **The grant, on every request.** Taken away, the next call fails, on a
  running agent, with no restart: FLOTTA-20's guarantee, for MCP.
- **The tools, off the box.** A `tools/list` answer loses every tool not
  granted, wherever it appears (a JSON reply or an event in a stream), and a
  `tools/call` for one is refused here, before anything goes out. The
  `tools.include` written to the agent's config is a courtesy; this is the
  rule.
- **The credential is put on here,** and the box's token taken off. Request
  headers are an allowlist, as for models, so nothing the agent sends reaches
  the upstream as a credential.
- **A row per tool call,** refused ones too: the agent, the server, the tool
  and what became of it. Never the arguments or the result, which are the
  client's data.

**Tools, and only tools.** Methods are an allowlist (`PASSED`): the session's
handshake, `tools/list` and a granted `tools/call`. Anything else is refused
before the credential is read, since the agent holds the token and needn't go
through Hermes to send `resources/read`.

**One JSON-RPC message per request.** MCP's Streamable HTTP dropped batches in
2025-06-18, and a batch is a second place a `tools/call` could hide, so one is
refused rather than inspected.

**An upstream 401 is never passed on.** Its `WWW-Authenticate` would start
Hermes's own OAuth sign-in, on the box, for a server whose tokens must never be
there. For an OAuth connection the token is refreshed and the call tried once
more; otherwise the agent is told the agency's credential was refused. **A 403
is passed back** (without `WWW-Authenticate`) as the upstream's refusal, and
refreshes nothing: a token that works but may not do this is not a dead one.

## Whose credential (slice 3)

**The box's client's own connection, if it has one, else the agency's**: the
D19 order `resolve_key` keeps for model keys. Read off the box's row, never the
request, so no agent reaches another client's account, and there is no step
from one client to another.

**A client connection that has stopped working is not passed over for the
agency's.** Connecting a client's own account says that client's work goes
through it. Falling back would send it, unasked, through the agency's account,
with the agency's access to everything else there, and nobody would notice the
client's connection had broken. So the call fails and says to connect it
again; disconnecting the client's is how the agency's comes back.

Each call's row says which one served it (`account`: `org` or `client`).

## The catalogue, on every request

Read from the store with each request (`flotta.mcp.catalogue_of`: the stored
setting, or this process's environment), with no cache, so a server the
operator removes or changes in the console is refused on its very next call.
It is one small read beside the grant's, on a path that already reads the
store. A connection made for another address than the entry now has, of
another kind, or sealed under a key this process doesn't hold, is never used
(`flotta.mcp.connection_usable`); the agent is told to have it connected again.

## A templated connector is pinned

n8n and Intercom (and any later library entry with a URL template) take a host
from the operator. At connect time the gateway checks that host the way the
library did at add time, then `alerts.check_addresses`: every address it
resolves to has to be public. The request then goes to that address via
`alerts.pinned_request`, with the Host header and httpcore's `sni_hostname`
extension still the hostname. A redirect is not followed. A hand-written server
without the library marker is not on this path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, NoReturn

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from flotta import mcp_oauth
from flotta.mcp import (
    CREDENTIAL_FAILURE_EXPIRED,
    Grant,
    Server,
    auth_headers,
    catalogue_of,
    connection_context,
    connection_usable,
    grant_from_row,
)
from flotta.mcp_library import LibraryError, assert_template_target, templated
from flotta.sealed import SealError, seal, unseal
from flotta.store import Box, FleetStore

_log = logging.getLogger(__name__)

#: The only request headers that reach an MCP server.
_FORWARDED = frozenset(
    {"content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id"}
)

#: Response headers that describe this connection, or would start a sign-in on the box.
_DROPPED = frozenset(
    {
        "connection",
        "content-encoding",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "set-cookie",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "www-authenticate",
    }
)

#: A stream of server events may be quiet for a long time, and that is fine.
_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0)

#: JSON-RPC's range for a server's own errors.
NOT_GRANTED = -32001
UNAVAILABLE = -32002
METHOD_REFUSED = -32601

#: The methods that reach an MCP server: the session's handshake, and tools.
#: An allowlist, because the token is in the agent's own config and the agent can
#: send anything with it; `resources/*`, `prompts/*` and whatever comes next stop
#: here. Answers to the server's own requests (an elicitation, say) carry no
#: method and pass.
PASSED = frozenset({"initialize", "notifications/initialized", "ping", "tools/list", "tools/call"})


_EVENT_END = b"\n\n"


def _as_clause(why: str, server: Server) -> str:
    """`connection_usable`'s reason, after "<title>'s connection for <whose>"."""
    prefix = f"{server.title} "
    if why.startswith(prefix):
        why = why[len(prefix) :]
    return why if why.startswith("stopped") else f"can't be used: {why}"


class McpRefused(Exception):
    """A request that stops here, with the status and what the agent is told."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: int = UNAVAILABLE,
        account: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        #: The connection it stopped at, if it got that far: for the call's row.
        self.account = account

    def response(self, request_id: Any = None) -> JSONResponse:
        return rpc_error(request_id, self.code, self.message, status=self.status)


def rpc_error(request_id: Any, code: int, message: str, *, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _no_repeats(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found = dict(pairs)
    if len(found) != len(pairs):
        raise ValueError("a key appears twice")
    return found


def filtered(message: Any, server: Server, grant: Grant) -> tuple[Any, bool]:
    """`message` with ungranted tools taken out of any tool list, and whether it changed."""
    if not isinstance(message, dict):
        return message, False
    result = message.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return message, False
    kept = [
        t
        for t in tools
        if isinstance(t, dict)
        and isinstance(t.get("name"), str)
        and grant.allows(server, t["name"])
    ]
    if len(kept) == len(tools):
        return message, False
    return {**message, "result": {**result, "tools": kept}}, True


def call_status(message: Any) -> str | None:
    """What became of a tool call, from its response."""
    if not isinstance(message, dict):
        return None
    if "error" in message:
        return "error"
    result = message.get("result")
    if isinstance(result, dict):
        return "tool_error" if result.get("isError") is True else "ok"
    return None


class Events:
    """Reads an MCP server's event stream as it passes, filtering tool lists.

    Whole events, in order. An event that isn't a tool list goes on byte for
    byte; one that is goes on with the same `id` and `event`, and the tools
    that weren't granted taken out of its data.
    """

    def __init__(self, server: Server, grant: Grant, call_id: Any) -> None:
        self._server = server
        self._grant = grant
        self._call_id = call_id
        self._buffer = b""
        self.status: str | None = None

    def feed(self, chunk: bytes) -> bytes:
        self._buffer += chunk.replace(b"\r\n", b"\n")
        out: list[bytes] = []
        while True:
            end = self._buffer.find(_EVENT_END)
            if end < 0:
                break
            event, self._buffer = self._buffer[:end], self._buffer[end + len(_EVENT_END) :]
            out.append(self._event(event) + _EVENT_END)
        return b"".join(out)

    def flush(self) -> bytes:
        rest, self._buffer = self._buffer, b""
        return self._event(rest) if rest.strip() else rest

    def _event(self, event: bytes) -> bytes:
        lines = event.split(b"\n")
        data = [ln[5:].removeprefix(b" ") for ln in lines if ln.startswith(b"data:")]
        if not data:
            return event
        try:
            message = json.loads(b"\n".join(data))
        except ValueError:
            return event
        if (
            self._call_id is not None
            and isinstance(message, dict)
            and message.get("id") == self._call_id
            and self.status is None
        ):
            self.status = call_status(message)
        changed, did = filtered(message, self._server, self._grant)
        if not did:
            return event
        kept = [ln for ln in lines if not ln.startswith(b"data:")]
        return b"\n".join([*kept, b"data: " + json.dumps(changed).encode()])


class _FinishedStream(StreamingResponse):
    """A stream whose row is written even if it was never started."""

    def __init__(self, content: AsyncIterator[bytes], finish: Callable[[], None], **kw: Any):
        super().__init__(content, **kw)
        self._finish = finish

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._finish()


class Proxy:
    """The routes, and the state they share: locks, the audit thread, HTTP clients."""

    def __init__(
        self,
        *,
        store_factory: Callable[[], FleetStore],
        env: Mapping[str, str],
        transport: httpx.AsyncBaseTransport | None,
        oauth_transport: httpx.BaseTransport | None,
    ) -> None:
        self._store_factory = store_factory
        self._env = env
        self._transport = transport
        self._oauth_transport = oauth_transport
        self._locks: dict[tuple[str, str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._audit = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mcp-audit")
        self.upstream: httpx.AsyncClient | None = None

    # -- lifetime --------------------------------------------------------------

    def open(self) -> None:
        # No keepalive: a templated connector is dialled by IP, and the pool
        # keys on that IP. Reusing the connection would carry one hostname's
        # TLS session to another host on the same address.
        self.upstream = httpx.AsyncClient(
            transport=self._transport,
            timeout=_TIMEOUT,
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        )

    async def close(self) -> None:
        if self.upstream is not None:
            await self.upstream.aclose()
        self._audit.shutdown(wait=True)

    # -- the store, on a thread ------------------------------------------------

    def servers(self) -> dict[str, Server]:
        """The catalogue now. One that can't be read serves nothing, and says why."""
        store = self._store_factory()
        try:
            servers, error = catalogue_of(store, self._env)
        finally:
            store.close()
        if error:
            # Not raised: the gateway every agent's models go through keeps going.
            _log.error("the MCP catalogue could not be read: %s", error)
        return servers

    def _lookup(self, box: Box, name: str) -> tuple[Server | None, Grant | None]:
        """The server as the catalogue has it now, and this box's grant of it."""
        store = self._store_factory()
        try:
            servers, error = catalogue_of(store, self._env)
            if error:
                _log.error("the MCP catalogue could not be read: %s", error)
            server = servers.get(name)
            row = store.box_mcp_grant(box.id, name) if server is not None else None
        finally:
            store.close()
        grant = grant_from_row(row.server, row.tools, row.ask_first) if row else None
        return server, grant

    def record(
        self, box: Box, server: Server, tool: str, status: str, account: str | None = None
    ) -> None:
        """A row, on a thread nothing awaits: a hung-up client cancels its request's task."""

        def write() -> None:
            try:
                store = self._store_factory()
                try:
                    store.record_mcp_call(
                        box.id, server=server.name, tool=tool, status=status, account=account
                    )
                finally:
                    store.close()
            except Exception:
                _log.exception("an MCP call could not be recorded: %s %s", server.name, tool)

        try:
            self._audit.submit(write)
        except RuntimeError:
            write()

    def _lock(self, org: str, client: str | None, server: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault((org, client or "", server), threading.Lock())

    def credential(self, box: Box, server: Server, *, stale: str | None = None) -> dict[str, Any]:
        """The credential a call from `box` goes out on. See `resolve`."""
        return self.resolve(box, server, stale=stale)[0]

    def resolve(
        self, box: Box, server: Server, *, stale: str | None = None
    ) -> tuple[dict[str, Any], str]:
        """`box`'s client's credential for `server`, else its org's, refreshed if need be.

        Answers the credential and which one it is (`client` or `org`), or
        raises `McpRefused`. `stale` is an access token the upstream just
        refused: refresh unless someone else already has.
        """
        store = self._store_factory()
        try:
            client: str | None = box.client
            found = store.mcp_connection(box.org, server.name, client=client)
            if found is None:
                client = None
                found = store.mcp_connection(box.org, server.name)
            account = "client" if client else "org"
            whose = "this client" if client else "this agency"
            if found is None:
                raise McpRefused(
                    503, f"{server.title} isn't connected for {whose} yet", account=account
                )
            why = connection_usable(found, server, self._env)
            if why is not None:
                # Never the org's in its place: see the module docstring.
                if found.status != "ok":
                    why = "stopped working: connect it again"
                raise McpRefused(
                    503,
                    f"{server.title}'s connection for {whose} {_as_clause(why, server)}",
                    account=account,
                )
            context = connection_context(box.org, server.name, client)
            try:
                credential = json.loads(unseal(found.credential, context=context, env=self._env))
            except SealError as exc:
                _log.error(
                    "the %s credential for %s/%s won't open: %s",
                    server.name,
                    box.org,
                    client or "-",
                    exc,
                )
                raise McpRefused(
                    503, f"the gateway can't use {whose}'s credential", account=account
                ) from exc
            if server.auth != "oauth":
                return credential, account
            if stale is None and not mcp_oauth.needs_refresh(credential):
                return credential, account
            with self._lock(box.org, client, server.name):
                again = store.mcp_connection(box.org, server.name, client=client)
                if again is not None and again.updated_at != found.updated_at:
                    if again.status != "ok":
                        raise McpRefused(
                            503,
                            f"{server.title}'s connection for {whose} stopped working: "
                            "connect it again",
                            account=account,
                        )
                    try:
                        opened = unseal(again.credential, context=context, env=self._env)
                    except SealError as exc:
                        raise McpRefused(
                            503, f"the gateway can't use {whose}'s credential", account=account
                        ) from exc
                    credential = json.loads(opened)
                    if credential.get("access_token") != stale and not mcp_oauth.needs_refresh(
                        credential
                    ):
                        return credential, account
                    found = again
                fresh = self._refresh(
                    store, box, client, server, found.updated_at, credential, context
                )
                return fresh, account
        finally:
            store.close()

    def _refresh(
        self,
        store: FleetStore,
        box: Box,
        client: str | None,
        server: Server,
        was: str,
        credential: dict[str, Any],
        context: str,
    ) -> dict[str, Any]:
        account = "client" if client else "org"
        token = credential.get("refresh_token")
        if not token:
            # The flag lives in the credential. The org list reads it; the
            # sentence below stays what a person sees.
            marked = json.dumps({**credential, "failure": CREDENTIAL_FAILURE_EXPIRED})
            try:
                sealed = seal(marked, context=context, env=self._env)
            except SealError:
                sealed = None
            store.break_mcp_connection(
                box.org,
                server.name,
                "expired, with no way to refresh",
                client=client,
                credential=sealed,
            )
            raise McpRefused(
                503, f"{server.title}'s sign-in has expired: connect it again", account=account
            )
        try:
            with httpx.Client(transport=self._oauth_transport, trust_env=False) as http:
                tokens = mcp_oauth.refresh(
                    http,
                    server,
                    mcp_oauth.endpoints_of(credential),
                    mcp_oauth.client_of(credential),
                    token,
                    env=self._env,
                )
        except mcp_oauth.Refused as exc:
            store.break_mcp_connection(box.org, server.name, str(exc), client=client)
            store.add_event(
                "box",
                box.id,
                "mcp_connection_broken",
                {"server": server.name, "account": account, "detail": str(exc)},
            )
            raise McpRefused(
                503,
                f"{server.title} refused to renew the sign-in: connect it again",
                account=account,
            ) from exc
        except mcp_oauth.OAuthError as exc:
            _log.warning("%s could not be refreshed for %s: %s", server.name, box.org, exc)
            raise McpRefused(
                503, f"{server.title} couldn't be reached to renew the sign-in", account=account
            ) from exc
        fresh = mcp_oauth.refreshed(credential, tokens)
        sealed = seal(json.dumps(fresh), context=context, env=self._env)
        if not store.refresh_mcp_connection(
            box.org, server.name, credential=sealed, was=was, client=client
        ):
            # Someone reconnected or refreshed meanwhile. Theirs stands; this one
            # still works for this call.
            _log.info("a newer %s credential for %s was kept", server.name, box.org)
        return fresh

    # -- one request -----------------------------------------------------------

    async def _prepare(
        self, request: Request, name: str, caller: Callable[[Request], Awaitable[Box]]
    ) -> tuple[Box, Server, Grant]:
        box = await caller(request)
        server, grant = await asyncio.to_thread(self._lookup, box, name)
        if server is None:
            # Not recorded: a name that isn't in the catalogue is anything at all,
            # and the audit is of servers the fleet has.
            raise McpRefused(404, f"no MCP server {name!r} in this fleet", code=NOT_GRANTED)
        if grant is None:
            await self._refuse_ungranted(request, box, server)
        assert grant is not None
        return box, server, grant

    async def _refuse_ungranted(self, request: Request, box: Box, server: Server) -> NoReturn:
        """Refuse a server this agent isn't granted, recording a tool call it tried.

        A grant taken away is enforced here on the next call, and an agent still
        calling a server it lost is the refusal most worth seeing in the audit,
        so a `tools/call` leaves a `refused` row like a tool outside the grant
        does. The handshake and `tools/list` leave none: they call nothing.
        """
        if request.method == "POST":
            try:
                message = json.loads(await request.body())
            except ValueError:
                message = None
            if isinstance(message, dict) and message.get("method") == "tools/call":
                params = message.get("params")
                called = params.get("name") if isinstance(params, dict) else None
                if isinstance(called, str):
                    self.record(box, server, called, "refused")
        raise McpRefused(403, f"{server.title} isn't granted to this agent", code=NOT_GRANTED)

    def _headers(
        self, request: Request, server: Server, credential: Mapping[str, Any]
    ) -> dict[str, str]:
        headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARDED}
        headers.update(auth_headers(server, credential))
        headers["accept-encoding"] = "identity"
        return headers

    async def forward(
        self,
        request: Request,
        name: str,
        caller: Callable[[Request], Awaitable[Box]],
    ) -> Response:
        request_id: Any = None
        tool: str | None = None
        box: Box | None = None
        server: Server | None = None
        account: str | None = None
        try:
            box, server, grant = await self._prepare(request, name, caller)
            raw = b""
            if request.method == "POST":
                raw = await request.body()
                try:
                    message = json.loads(raw, object_pairs_hook=_no_repeats)
                except ValueError:
                    return rpc_error(None, -32700, "not one JSON-RPC message", status=400)
                if not isinstance(message, dict):
                    return rpc_error(
                        None, -32600, "one message per request; no batches", status=400
                    )
                request_id = message.get("id")
                method = message.get("method")
                if method is None and not ("result" in message or "error" in message):
                    return rpc_error(
                        request_id, -32600, "not a request, a notification or an answer"
                    )
                if method is not None and method not in PASSED:
                    # Before the credential is read: nothing about the upstream is touched.
                    return rpc_error(
                        request_id,
                        METHOD_REFUSED,
                        f"{method} isn't passed to {server.title} through Flotta: only tools are",
                        status=200 if "id" in message else 400,
                    )
                if method == "tools/call":
                    params = message.get("params")
                    called = params.get("name") if isinstance(params, dict) else None
                    if not isinstance(called, str):
                        return rpc_error(request_id, -32602, "tools/call names no tool")
                    tool = called
                    if not grant.allows(server, tool):
                        self.record(box, server, tool, "refused")
                        return rpc_error(
                            request_id,
                            NOT_GRANTED,
                            f"{tool} isn't one of the {server.title} tools granted to this agent",
                        )
            credential, account = await asyncio.to_thread(self.resolve, box, server)
            response = await self._send(request, server, raw, credential)
            # A 401 is a token the upstream no longer takes; a 403 is a refusal of
            # this request, which a new token would not change. Only the first is
            # refreshed, so a forbidden tool can't break the agency's connection.
            if response.status_code == 401 and server.auth == "oauth":
                await response.aclose()
                credential, account = await asyncio.to_thread(
                    self.resolve, box, server, stale=credential.get("access_token")
                )
                response = await self._send(request, server, raw, credential)
            if response.status_code == 401:
                await response.aclose()
                whose = "the client's" if account == "client" else "the agency's"
                raise McpRefused(
                    502,
                    f"{server.title} refused {whose} credential; it may need connecting again",
                    account=account,
                )
        except McpRefused as refused:
            if tool is not None and box is not None and server is not None:
                self.record(box, server, tool, "unavailable", refused.account)
            return refused.response(request_id)
        except httpx.HTTPError as exc:
            _log.warning("%s could not be reached: %s", name, exc)
            if tool is not None and box is not None and server is not None:
                self.record(box, server, tool, "unreachable", account)
            return rpc_error(request_id, UNAVAILABLE, f"{name} could not be reached", status=502)
        return await self._answer(response, box, server, grant, request_id, tool, account)

    async def _send(
        self, request: Request, server: Server, raw: bytes, credential: Mapping[str, Any]
    ) -> httpx.Response:
        assert self.upstream is not None
        url = server.url
        headers = self._headers(request, server, credential)
        extensions: dict[str, Any] | None = None
        item = templated(server)
        if item is not None:
            # Add-time rules again (literal IPs, private suffixes, port 443),
            # then the webhook DNS check. The connection is pinned to that
            # address; httpcore's `sni_hostname` keeps TLS on the hostname.
            try:
                assert_template_target(item, server.url)
                url, host_header, sni = await mcp_oauth.connect_pinned(server.url)
            except LibraryError as exc:
                raise McpRefused(502, str(exc)) from exc
            except mcp_oauth.OAuthError as exc:
                raise McpRefused(502, str(exc)) from exc
            headers = {**headers, "Host": host_header}
            extensions = {"sni_hostname": sni}
        upstream = self.upstream.build_request(
            request.method,
            url,
            content=raw if request.method == "POST" else None,
            headers=headers,
            extensions=extensions,
        )
        response = await self.upstream.send(upstream, stream=True, follow_redirects=False)
        if item is not None and 300 <= response.status_code < 400:
            await response.aclose()
            raise McpRefused(502, f"{server.title} answered with a redirect, which is not followed")
        return response

    async def _answer(
        self,
        response: httpx.Response,
        box: Box,
        server: Server,
        grant: Grant,
        request_id: Any,
        tool: str | None,
        account: str | None,
    ) -> Response:
        headers = {k: v for k, v in response.headers.items() if k.lower() not in _DROPPED}
        kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind == "text/event-stream":
            events = Events(server, grant, request_id if tool is not None else None)

            async def relay() -> AsyncIterator[bytes]:
                try:
                    async for chunk in response.aiter_raw():
                        out = events.feed(chunk)
                        if out:
                            yield out
                    rest = events.flush()
                    if rest:
                        yield rest
                except httpx.HTTPError as exc:
                    _log.warning("%s's stream ended early: %s", server.name, exc)
                finally:
                    await response.aclose()

            finished = False

            def finish() -> None:
                nonlocal finished
                if tool is not None and not finished:
                    finished = True
                    self.record(box, server, tool, events.status or "no_answer", account)

            return _FinishedStream(
                relay(), finish, status_code=response.status_code, headers=headers
            )
        try:
            body = await response.aread()
        finally:
            await response.aclose()
        status = f"upstream_{response.status_code}" if response.status_code >= 400 else None
        if kind == "application/json" and body:
            try:
                message = json.loads(body)
            except ValueError:
                message = None
            if message is not None:
                status = status or call_status(message)
                changed, did = filtered(message, server, grant)
                if did:
                    body = json.dumps(changed).encode()
        if tool is not None:
            self.record(box, server, tool, status or "no_answer", account)
        return Response(body, response.status_code, headers)


def add_mcp_routes(
    app: FastAPI,
    *,
    caller: Callable[[Request], Awaitable[Box]],
    store_factory: Callable[[], FleetStore],
    env: Mapping[str, str],
    transport: httpx.AsyncBaseTransport | None,
    oauth_transport: httpx.BaseTransport | None,
) -> Proxy:
    """Mount `/mcp/<server>`. The caller opens and closes the returned proxy."""
    proxy = Proxy(
        store_factory=store_factory,
        env=env,
        transport=transport,
        oauth_transport=oauth_transport,
    )

    @app.api_route("/mcp/{server}", methods=["POST", "GET", "DELETE"])
    async def mcp_server(server: str, request: Request) -> Response:
        """Streamable HTTP: POST a message, GET the server's own stream, DELETE a session."""
        return await proxy.forward(request, server, caller)

    return proxy
