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

**One JSON-RPC message per request.** MCP's Streamable HTTP dropped batches in
2025-06-18, and a batch is a second place a `tools/call` could hide, so one is
refused rather than inspected.

**An upstream 401 is never passed on.** Its `WWW-Authenticate` would start
Hermes's own OAuth sign-in, on the box, for a server whose tokens must never be
there. For an OAuth connection the token is refreshed and the call tried once
more; otherwise the agent is told the agency's credential was refused.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from flotta import mcp_oauth
from flotta.mcp import (
    Grant,
    McpError,
    Server,
    auth_headers,
    catalogue,
    connection_context,
    grant_from_row,
)
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

_EVENT_END = b"\n\n"


class McpRefused(Exception):
    """A request that stops here, with the status and what the agent is told."""

    def __init__(self, status: int, message: str, *, code: int = UNAVAILABLE) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code

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
        try:
            self.servers = catalogue(env)
        except McpError:
            # A catalogue that can't be read serves nothing, and says why on every call,
            # rather than stopping the gateway that every agent's models go through.
            _log.exception("the MCP catalogue could not be read")
            self.servers = {}
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._audit = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mcp-audit")
        self.upstream: httpx.AsyncClient | None = None

    # -- lifetime --------------------------------------------------------------

    def open(self) -> None:
        self.upstream = httpx.AsyncClient(
            transport=self._transport, timeout=_TIMEOUT, trust_env=False
        )

    async def close(self) -> None:
        if self.upstream is not None:
            await self.upstream.aclose()
        self._audit.shutdown(wait=True)

    # -- the store, on a thread ------------------------------------------------

    def _grant(self, box: Box, server: Server) -> Grant | None:
        store = self._store_factory()
        try:
            row = store.box_mcp_grant(box.id, server.name)
        finally:
            store.close()
        return grant_from_row(row.server, row.tools, row.ask_first) if row else None

    def record(self, box: Box, server: Server, tool: str, status: str) -> None:
        """A row, on a thread nothing awaits: a hung-up client cancels its request's task."""

        def write() -> None:
            try:
                store = self._store_factory()
                try:
                    store.record_mcp_call(box.id, server=server.name, tool=tool, status=status)
                finally:
                    store.close()
            except Exception:
                _log.exception("an MCP call could not be recorded: %s %s", server.name, tool)

        try:
            self._audit.submit(write)
        except RuntimeError:
            write()

    def _lock(self, org: str, server: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault((org, server), threading.Lock())

    def credential(self, box: Box, server: Server, *, stale: str | None = None) -> dict[str, Any]:
        """The org's credential for `server`, refreshed if it has to be. Raises `McpRefused`.

        `stale` is an access token the upstream just refused: refresh unless
        someone else already has.
        """
        context = connection_context(box.org, server.name)
        store = self._store_factory()
        try:
            found = store.mcp_connection(box.org, server.name)
            if found is None or found.kind != server.auth:
                raise McpRefused(503, f"{server.title} isn't connected for this agency yet")
            if found.status != "ok":
                raise McpRefused(
                    503, f"{server.title}'s connection stopped working: connect it again"
                )
            try:
                credential = json.loads(unseal(found.credential, context=context, env=self._env))
            except SealError as exc:
                _log.error("the %s credential for %s won't open: %s", server.name, box.org, exc)
                raise McpRefused(503, "the gateway can't use this agency's credential") from exc
            if server.auth != "oauth":
                return credential
            if stale is None and not mcp_oauth.needs_refresh(credential):
                return credential
            with self._lock(box.org, server.name):
                again = store.mcp_connection(box.org, server.name)
                if again is not None and again.updated_at != found.updated_at:
                    if again.status != "ok":
                        raise McpRefused(
                            503, f"{server.title}'s connection stopped working: connect it again"
                        )
                    opened = unseal(again.credential, context=context, env=self._env)
                    credential = json.loads(opened)
                    if credential.get("access_token") != stale and not mcp_oauth.needs_refresh(
                        credential
                    ):
                        return credential
                    found = again
                return self._refresh(store, box, server, found.updated_at, credential, context)
        finally:
            store.close()

    def _refresh(
        self,
        store: FleetStore,
        box: Box,
        server: Server,
        was: str,
        credential: dict[str, Any],
        context: str,
    ) -> dict[str, Any]:
        token = credential.get("refresh_token")
        if not token:
            store.break_mcp_connection(box.org, server.name, "expired, with no way to refresh")
            raise McpRefused(503, f"{server.title}'s sign-in has expired: connect it again")
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
            store.break_mcp_connection(box.org, server.name, str(exc))
            store.add_event(
                "box", box.id, "mcp_connection_broken", {"server": server.name, "detail": str(exc)}
            )
            raise McpRefused(
                503, f"{server.title} refused to renew the sign-in: connect it again"
            ) from exc
        except mcp_oauth.OAuthError as exc:
            _log.warning("%s could not be refreshed for %s: %s", server.name, box.org, exc)
            raise McpRefused(
                503, f"{server.title} couldn't be reached to renew the sign-in"
            ) from exc
        fresh = mcp_oauth.refreshed(credential, tokens)
        sealed = seal(json.dumps(fresh), context=context, env=self._env)
        if not store.refresh_mcp_connection(box.org, server.name, credential=sealed, was=was):
            # Someone reconnected or refreshed meanwhile. Theirs stands; this one
            # still works for this call.
            _log.info("a newer %s credential for %s was kept", server.name, box.org)
        return fresh

    # -- one request -----------------------------------------------------------

    async def _prepare(
        self, request: Request, name: str, caller: Callable[[Request], Awaitable[Box]]
    ) -> tuple[Box, Server, Grant]:
        box = await caller(request)
        server = self.servers.get(name)
        if server is None:
            raise McpRefused(404, f"no MCP server {name!r} in this fleet", code=NOT_GRANTED)
        grant = await asyncio.to_thread(self._grant, box, server)
        if grant is None:
            raise McpRefused(403, f"{server.title} isn't granted to this agent", code=NOT_GRANTED)
        return box, server, grant

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
                if message.get("method") == "tools/call":
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
            credential = await asyncio.to_thread(self.credential, box, server)
            response = await self._send(request, server, raw, credential)
            if response.status_code in (401, 403) and server.auth == "oauth":
                await response.aclose()
                credential = await asyncio.to_thread(
                    self.credential, box, server, stale=credential.get("access_token")
                )
                response = await self._send(request, server, raw, credential)
            if response.status_code in (401, 403):
                await response.aclose()
                raise McpRefused(
                    502,
                    f"{server.title} refused the agency's credential; it may need connecting again",
                )
        except McpRefused as refused:
            if tool is not None and box is not None and server is not None:
                self.record(box, server, tool, "unavailable")
            return refused.response(request_id)
        except httpx.HTTPError as exc:
            _log.warning("%s could not be reached: %s", name, exc)
            if tool is not None and box is not None and server is not None:
                self.record(box, server, tool, "unreachable")
            return rpc_error(request_id, UNAVAILABLE, f"{name} could not be reached", status=502)
        return await self._answer(response, box, server, grant, request_id, tool)

    async def _send(
        self, request: Request, server: Server, raw: bytes, credential: Mapping[str, Any]
    ) -> httpx.Response:
        assert self.upstream is not None
        upstream = self.upstream.build_request(
            request.method,
            server.url,
            content=raw if request.method == "POST" else None,
            headers=self._headers(request, server, credential),
        )
        return await self.upstream.send(upstream, stream=True)

    async def _answer(
        self,
        response: httpx.Response,
        box: Box,
        server: Server,
        grant: Grant,
        request_id: Any,
        tool: str | None,
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
                    self.record(box, server, tool, events.status or "no_answer")

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
            self.record(box, server, tool, status or "no_answer")
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
