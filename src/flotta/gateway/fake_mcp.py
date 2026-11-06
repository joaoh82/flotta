"""An MCP server and an OAuth provider that answer for $0 (FLOTTA-125).

One `httpx.MockTransport` for both, injected as the gateway's and the control
plane's transports, so the suite opens no socket. The server speaks enough of
MCP's Streamable HTTP to be proxied: `initialize`, `tools/list`, `tools/call`,
a JSON or an event-stream answer, and a 401 with `WWW-Authenticate` for a
credential it doesn't know. The provider does discovery, registration, the
code exchange with PKCE checked, refresh with rotation, and revocation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

MCP_URL = "https://mcp.example.test/mcp"
AUTH_URL = "https://auth.example.test"
TOOLS = ("read_issue", "create_issue", "delete_everything")


def _tool(name: str) -> dict[str, Any]:
    return {"name": name, "description": f"{name}, for tests", "inputSchema": {"type": "object"}}


class FakeMcp:
    def __init__(self, *, keys: set[str] | None = None, stream: bool = False) -> None:
        #: Credentials the server takes, as the whole `Authorization` value.
        self.keys = set(keys or {"Bearer agency-key"})
        self.stream = stream
        self.requests: list[httpx.Request] = []
        self.calls: list[str] = []
        # The provider.
        self.challenges: dict[str, str] = {}
        self.codes: dict[str, str] = {}
        self.refresh_tokens: set[str] = set()
        self.revoked: list[str] = []
        self.refreshes = 0
        self.registered = 0
        self.expires_in = 3600
        #: Seconds a refresh takes, so concurrent ones overlap.
        self.refresh_delay = 0.0
        self._issued = 0
        self._lock = threading.Lock()

    # -- the provider ----------------------------------------------------------

    def sign_in(self, authorize_url: str) -> tuple[str, str]:
        """What a person's browser would do: approve, and come back with a code and state."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = query["code_challenge"]
        assert query["code_challenge_method"] == "S256"
        assert query["resource"] == MCP_URL
        return code, query["state"]

    def _issue(self) -> dict[str, Any]:
        with self._lock:
            self._issued += 1
            n = self._issued
        access, refresh = f"at-{n}", f"rt-{n}"
        self.keys.add(f"Bearer {access}")
        self.refresh_tokens.add(refresh)
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
        }

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("resource") != MCP_URL:
            return httpx.Response(400, json={"error": "invalid_target"})
        if form["grant_type"] == "authorization_code":
            challenge = self.codes.pop(form.get("code", ""), None)
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            if challenge != base64.urlsafe_b64encode(digest).decode().rstrip("="):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._issue())
        if form["grant_type"] == "refresh_token":
            time.sleep(self.refresh_delay)
            with self._lock:
                token = form.get("refresh_token", "")
                if token not in self.refresh_tokens:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.refresh_tokens.discard(token)
                self.refreshes += 1
            return httpx.Response(200, json=self._issue())
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _provider(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "issuer": AUTH_URL,
                    "authorization_endpoint": f"{AUTH_URL}/authorize",
                    "token_endpoint": f"{AUTH_URL}/token",
                    "registration_endpoint": f"{AUTH_URL}/register",
                    "revocation_endpoint": f"{AUTH_URL}/revoke",
                },
            )
        if path == "/register":
            self.registered += 1
            return httpx.Response(201, json={"client_id": "dyn-1", "client_secret": "dyn-secret"})
        if path == "/token":
            return self._token(request)
        if path == "/revoke":
            self.revoked.append(parse_qs(request.content.decode())["token"][0])
            return httpx.Response(200)
        return httpx.Response(404)

    # -- the MCP server --------------------------------------------------------

    def _answer(self, message: dict[str, Any]) -> dict[str, Any]:
        method = message.get("method")
        if method == "initialize":
            result: dict[str, Any] = {"protocolVersion": "2025-06-18", "capabilities": {}}
        elif method == "tools/list":
            result = {"tools": [_tool(t) for t in TOOLS]}
        elif method == "tools/call":
            name = message["params"]["name"]
            self.calls.append(name)
            result = {"content": [{"type": "text", "text": f"did {name}"}], "isError": False}
        else:
            return {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32601}}
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}

    def _server(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json={"authorization_servers": [AUTH_URL]})
        self.requests.append(request)
        if request.headers.get("authorization") not in self.keys:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Bearer resource_metadata="https://elsewhere"'},
                json={"error": "unauthorized"},
            )
        if request.method == "GET":
            listed = {"jsonrpc": "2.0", "id": 9, "result": {"tools": [_tool(t) for t in TOOLS]}}
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(
                    f"id: 1\nevent: message\ndata: {json.dumps(listed)}\n\n".encode()
                ),
            )
        if request.method == "DELETE":
            return httpx.Response(200)
        message = json.loads(request.content)
        if "id" not in message:
            return httpx.Response(202)
        answer = self._answer(message)
        headers = {"mcp-session-id": "session-1"}
        if self.stream:
            note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
            body = f"data: {json.dumps(note)}\n\nid: 7\ndata: {json.dumps(answer)}\n\n"
            return httpx.Response(
                200,
                headers={**headers, "content-type": "text/event-stream"},
                # A stream, as from a socket: `content=` arrives already read.
                stream=httpx.ByteStream(body.encode()),
            )
        return httpx.Response(200, headers=headers, json=answer)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == urlsplit(AUTH_URL).hostname:
            return self._provider(request)
        return self._server(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def catalogue(*, auth: str = "bearer", **extra: Any) -> str:
    """`$FLOTTA_MCP_SERVERS` naming this server."""
    entry: dict[str, Any] = {"name": "tracker", "title": "Tracker", "url": MCP_URL, "auth": auth}
    if auth == "api_key":
        entry["header"] = "X-Api-Key"
    entry.update(extra)
    return json.dumps([entry])
