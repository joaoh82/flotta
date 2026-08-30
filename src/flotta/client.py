"""Talking to a box — the local half of the inversion (M3).

v0.1 ran the orchestrator on the laptop and shipped tasks to disposable
containers. The whole pivot is that this is backwards: the agent lives in the
cloud with its memory, and the laptop is a thin client. This module is that
thin client, and it is deliberately small — if it ever grows agent logic, the
inversion has quietly reversed.

## How it reaches the box

The box serves Hermes on **loopback** (`127.0.0.1:9119`), and this opens a
tunnel to it over Fly's private WireGuard network rather than talking to
anything public. Two reasons, in order of importance:

1. **Hermes refuses to serve unauthenticated off loopback.** `--insecure` no
   longer overrides it; the gate was hardened after a campaign that found
   `--host 0.0.0.0 --insecure` dashboards with open config/MCP/agent surfaces.
   Binding public would mean standing up an auth provider — §M5's job, and it
   needs a front door designed first.
2. **Nothing is exposed at all**, so there is no window between "boxes are
   reachable" and "boxes are authenticated". §M5 adds the public route
   deliberately, with scoped tokens, instead of inheriting one by accident.

The tunnel is a subprocess (`flyctl proxy`), for the same reason `FlyBackend`
shells out: flyctl already owns WireGuard setup, and the seam that matters is
this module's interface, not its transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass

from flotta.backend import BackendError
from flotta.backends.fly_backend import parse_endpoint

DEFAULT_REMOTE_PORT = 9119
#: How long to wait for `hermes serve` to answer after the tunnel is up. A cold
#: box has to import Hermes first, which is not fast — and is exactly the cost
#: `suspend` avoids (M1: suspend keeps the VM's memory, cold stop does not).
DEFAULT_READY_TIMEOUT_S = 90


class ChatError(Exception):
    """Could not reach or drive the agent on a box."""


@dataclass(frozen=True, slots=True)
class Turn:
    """One exchange with a box."""

    session_id: str
    response: str
    raw: dict


def free_local_port() -> int:
    """An unused local port for the tunnel.

    Asked of the OS rather than picked from a range: two `flotta chat` sessions
    against different boxes must not collide, and a hardcoded 9119 locally would
    also fight a Hermes the operator is running on their own machine.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _spawn_proxy(app: str, machine_id: str, local_port: int, remote_port: int):
    """Start `flyctl proxy` against one specific machine."""
    return subprocess.Popen(
        [
            "flyctl",
            "proxy",
            f"{local_port}:{remote_port}",
            "--app",
            app,
            # Address the machine directly rather than letting Fly's internal
            # DNS pick "the first machine": with several boxes in one app that
            # would silently connect you to somebody else's agent.
            f"{machine_id}.vm.{app}.internal",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@contextlib.contextmanager
def tunnel(
    endpoint: str,
    *,
    remote_port: int = DEFAULT_REMOTE_PORT,
    local_port: int | None = None,
    ready_timeout_s: float = 20.0,
    dns_retry_s: float = 30.0,
) -> Iterator[str]:
    """Open a WireGuard tunnel to a box's loopback port; yield the local base URL.

    Always closed on exit, including on error: a leaked `flyctl proxy` holds a
    port and keeps a WireGuard session alive long after the command that made
    it is gone, and the next run then fails with a confusing bind error.
    """
    app, machine_id = parse_endpoint(endpoint)
    port = local_port or free_local_port()

    # Fly's internal DNS lags a machine reaching `started` by a beat, so a
    # proxy opened the instant after a wake can still be told the host does not
    # exist. Retrying is the difference between "run it again" and "it works" —
    # the caller has already done everything right by waking the box first.
    proc = _spawn_proxy(app, machine_id, port, remote_port)
    dns_deadline = time.monotonic() + dns_retry_s
    try:
        while True:
            deadline = time.monotonic() + ready_timeout_s
            failure: str | None = None
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    stderr = (proc.stderr.read() if proc.stderr else "") or ""
                    failure = stderr.strip()[:300]
                    break
                with (
                    contextlib.suppress(OSError),
                    socket.create_connection(("127.0.0.1", port), timeout=0.5),
                ):
                    failure = None
                    break
                time.sleep(0.2)
            else:
                raise ChatError(f"tunnel to {endpoint} never came up within {ready_timeout_s}s")

            if failure is None:
                break

            if "not found in DNS" in failure and time.monotonic() < dns_deadline:
                # Almost certainly propagation, not a wrong address — the box
                # was just woken. Try again on a fresh proxy.
                time.sleep(1.0)
                proc = _spawn_proxy(app, machine_id, port, remote_port)
                continue

            if "not found in DNS" in failure:
                raise ChatError(
                    f"{endpoint} is not in Fly's internal DNS after {dns_retry_s:.0f}s. "
                    "Fly only resolves running machines, so this usually means the "
                    "machine is stopped or failed to start — check `just fly-doctor`.\n"
                    f"  flyctl said: {failure}"
                )
            raise ChatError(f"flyctl proxy exited immediately: {failure}")

        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        if proc.poll() is None:  # pragma: no cover - only on a wedged proxy
            proc.kill()


def wait_until_ready(base_url: str, *, timeout_s: float = DEFAULT_READY_TIMEOUT_S) -> None:
    """Block until `hermes serve` answers, or give up with a useful message.

    A tunnel comes up long before Hermes does — the socket connects as soon as
    `flyctl proxy` is listening locally, which says nothing about whether the
    agent on the other end has finished importing itself.
    """
    deadline = time.monotonic() + timeout_s
    last: str = "no response"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=3) as response:
                if response.status < 500:
                    return
                last = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            # Any HTTP status is proof something is serving; only 5xx means
            # "up but broken", and 401/404 still mean Hermes answered.
            if exc.code < 500:
                return
            last = f"HTTP {exc.code}"
        except Exception as exc:  # connection refused while it boots
            last = f"{type(exc).__name__}"
        time.sleep(0.5)
    raise ChatError(
        f"the box is reachable but hermes serve did not answer within {timeout_s:.0f}s "
        f"(last: {last}). Check `flotta logs <box>` and `just fly-ssh`."
    )


def health(endpoint: str, *, timeout_s: float = DEFAULT_READY_TIMEOUT_S) -> dict:
    """Prove a box is serving. The cheapest possible round trip."""
    try:
        with tunnel(endpoint) as base_url:
            wait_until_ready(base_url, timeout_s=timeout_s)
            return {"endpoint": endpoint, "serving": True, "url": base_url}
    except (BackendError, OSError) as exc:
        raise ChatError(f"could not reach {endpoint}: {exc}") from exc


# --- authenticating against a box -----------------------------------------
#
# The flow, discovered by reading the routes and confirmed against a live box:
#
#   POST /auth/password-login   {provider, username, password}  -> session cookies
#   POST /api/auth/ws-ticket                                    -> single-use ticket (30s)
#   WS   /api/ws?ticket=...                                     -> the agent
#
# The ticket exists because a browser cannot set headers on a WebSocket; it is
# single-use with a short TTL, so mint one per connection rather than caching.

PROVIDER = "basic"
#: The caller's Flotta token, used against the door. Distinct from the control
#: plane's own token: this one only needs `box:chat`.
TOKEN_ENV = "FLOTTA_TOKEN"
USERNAME = "flotta"


DOMAIN_ENV = "FLOTTA_DOMAIN"
DEFAULT_DOMAIN = "flotta.dev"


def door_url(box_name: str, *, domain: str | None = None) -> str:
    """`https://<box>.<domain>` — the public address of a box.

    The whole point of the front door: reaching an agent stops requiring
    `flyctl`, a WireGuard tunnel, and membership of the Fly org that happens to
    host it. A stranger with a scoped token and a hostname can talk to an
    agent, which is what makes the app (M8) shippable to anyone.
    """
    from flotta.dotenv import read_dotenv_value

    resolved = (
        (domain or os.environ.get(DOMAIN_ENV) or read_dotenv_value(DOMAIN_ENV) or DEFAULT_DOMAIN)
        .strip()
        .strip(".")
    )
    return f"https://{box_name}.{resolved}"


def flotta_token(env: dict[str, str] | None = None) -> str:
    """The caller's own Flotta token, for the door.

    Minted with `flotta token mint <you> --scope box:chat`. This replaces the
    box's password on a user's machine: the door holds that, and this holds
    only a scoped, expiring grant to talk to an agent.
    """
    from flotta.dotenv import read_dotenv_value

    env = os.environ if env is None else env
    token = (env.get(TOKEN_ENV) or "").strip() or read_dotenv_value(TOKEN_ENV)
    if not token:
        raise ChatError(
            f"no ${TOKEN_ENV}. Mint one with:\n"
            f"  flotta token mint $USER --scope box:chat\n"
            f"and put it in .env, or pass --tunnel to reach the box directly "
            f"over Fly's private network instead."
        )
    return token


def credentials(dotenv: str = ".env") -> tuple[str, str]:
    """(username, password) for the box, from the local dotenv.

    Written there by `just fly-auth`, which also pushes them to the box as Fly
    secrets. Never committed — the box is reachable only over Fly's private
    network, but that is one layer and Hermes's auth gate is the other.
    """
    from pathlib import Path

    try:
        text = Path(dotenv).read_text(encoding="utf-8")
    except OSError as exc:
        raise ChatError(f"no {dotenv} to read box credentials from — run `just fly-auth`") from exc
    for line in text.splitlines():
        if line.startswith("FLOTTA_BOX_PASSWORD="):
            return USERNAME, line.split("=", 1)[1].strip()
    raise ChatError(f"FLOTTA_BOX_PASSWORD is not in {dotenv} — run `just fly-auth`")


def new_session(*, unsafe_cookies: bool = True):
    """An aiohttp session that will actually keep the box's cookies.

    `unsafe=True` is load-bearing, not laziness: aiohttp refuses to store
    cookies for IP-address hosts, and a tunnel is always `127.0.0.1`. Without
    it the login returns 200, no cookie is retained, and the very next request
    is anonymous — which surfaces as a bare 401 from `/api/auth/ws-ticket` and
    looks like a credentials problem rather than a cookie-jar policy.
    """
    import aiohttp

    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=unsafe_cookies))


async def login(
    session,
    base_url: str,
    username: str | None = None,
    password: str | None = None,
    *,
    token: str | None = None,
) -> None:
    """Exchange credentials for session cookies on `session`.

    Through the **door**, pass `token` and no credentials: the door fills in
    the box's real username and password on the way through, so they never
    need to exist on the caller's machine. Hermes's "basic" provider is a login
    form rather than HTTP Basic, which is why the door rewrites this body
    instead of attaching a header.

    Through a **tunnel**, pass the credentials — there is nothing in the path
    to supply them.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else None
    response = await session.post(
        f"{base_url}/auth/password-login",
        json={
            "provider": PROVIDER,
            "username": username or "",
            "password": password or "",
            "next": "",
        },
        headers=headers,
    )
    if response.status != 200:
        body = (await response.text())[:200]
        if token:
            # Through the door the caller has no credentials to get wrong, so
            # a 401 is never about them. It is the door's copy — or a door
            # deployed before it learned to fill them in, which is exactly how
            # this failed the first time it was tried.
            raise ChatError(
                f"the box refused the door's credentials ({response.status}): {body}.\n"
                f"The door fills these in; the caller never sends them. Check that "
                f"$FLOTTA_BOX_PASSWORD on the door matches the box "
                f"(`just door-secrets`), and that the door is running a build "
                f"that rewrites the login."
            )
        raise ChatError(
            f"login refused ({response.status}): {body}. "
            "Credentials are minted by `just fly-auth`; rotating them there "
            "updates both the box and .env."
        )


async def ws_ticket(session, base_url: str, *, token: str | None = None) -> str:
    """Mint a single-use WebSocket ticket for the logged-in session."""
    headers = {"Authorization": f"Bearer {token}"} if token else None
    response = await session.post(f"{base_url}/api/auth/ws-ticket", headers=headers)
    if response.status != 200:
        body = (await response.text())[:200]
        raise ChatError(
            f"could not mint a ws ticket ({response.status}): {body}. "
            "A 401 here after a successful login usually means the cookie jar "
            "dropped the session — see `new_session`."
        )
    payload = await response.json()
    ticket = payload.get("ticket")
    if not ticket:
        raise ChatError(f"ws-ticket returned no ticket: {payload}")
    return str(ticket)


async def open_agent_socket(session, base_url: str, ticket: str, *, token: str | None = None):
    """Connect to the box's agent socket. Caller owns the returned WS.

    Through the door the Flotta token travels as `?access_token=`, not a
    header — a browser cannot set headers on a WebSocket handshake, so the door
    accepts it there and strips it before proxying. This client *could* use a
    header, but sending it the same way the app must keeps one path tested
    rather than two.
    """
    url = f"{base_url}/api/ws?ticket={ticket}"
    if token:
        url += f"&access_token={token}"
    return await session.ws_connect(url)


# --- talking to the agent --------------------------------------------------
#
# The box's agent surface is **JSON-RPC 2.0 over the WebSocket**, decoded by
# reading `tui_gateway/ws.py` and confirmed against a live box:
#
#   <-  {"method": "event", "params": {"type": "gateway.ready", ...}}
#   ->  {"id": 1, "method": "session.create", "params": {}}
#   <-  {"id": 1, "result": {"session_id": "...", "info": {"model": ...}}}
#   ->  {"id": 2, "method": "prompt.submit", "params": {"session_id", "text"}}
#   <-  {"method": "event", "params": {"type": "message.complete",
#                                      "payload": {"text", "status", "usage"}}}
#
# The server speaks first (`gateway.ready`) and every reply is an *event*, not
# an RPC result — the `prompt.submit` response only acknowledges the submit.

READY_EVENT = "gateway.ready"
COMPLETE_EVENT = "message.complete"

#: JSON-RPC ids only have to be unique per connection, and a global monotonic
#: counter guarantees that for free. Hardcoding 1 and 2 worked while every
#: socket carried exactly one turn — a second `send_turn` on the same socket
#: would have collided with the first.
_rpc_ids = itertools.count(1)

#: Generous, because it bounds a model call rather than a network round trip —
#: and on a cold box the agent may still be loading when the turn arrives.
DEFAULT_TURN_TIMEOUT_S = 300.0


async def _rpc(ws, method: str, params: dict) -> int:
    """Send one JSON-RPC request and return the id its reply will carry."""
    rid = next(_rpc_ids)
    await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
    return rid


async def _next_json(ws, *, timeout_s: float, what: str = "the agent") -> dict:
    """One decoded text frame, or a ChatError explaining what arrived instead.

    `asyncio.wait_for` raises `TimeoutError`, which the CLI does not catch —
    so an unwrapped timeout in the handshake dumps a traceback at the operator
    instead of one line telling them the box did not answer.
    """
    import aiohttp

    try:
        message = await asyncio.wait_for(ws.receive(), timeout=timeout_s)
    except TimeoutError as exc:
        raise ChatError(f"{what} did not respond within {timeout_s:.0f}s") from exc
    if message.type is not aiohttp.WSMsgType.TEXT:
        raise ChatError(f"the agent socket closed ({message.type.name})")
    try:
        return json.loads(message.data)
    except json.JSONDecodeError as exc:
        raise ChatError(f"agent sent non-JSON: {message.data[:200]!r}") from exc


async def await_ready(ws, *, timeout_s: float = 30.0) -> dict:
    """Consume the `gateway.ready` the server sends on connect."""
    frame = await _next_json(ws, timeout_s=timeout_s, what="the agent handshake")
    event = (frame.get("params") or {}).get("type")
    if event != READY_EVENT:
        raise ChatError(f"expected {READY_EVENT}, got {event or frame}")
    return (frame.get("params") or {}).get("payload") or {}


async def create_session(ws, *, title: str = "", timeout_s: float = 60.0) -> dict:
    """Open a conversation on the box and return its `result` block."""
    rid = await _rpc(ws, "session.create", {"title": title} if title else {})

    # A wall-clock deadline, not a per-frame one. Waiting `timeout_s` per
    # *ignored* event means a chatty gateway that never answers this id keeps
    # resetting the clock and the handshake never ends.
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise ChatError(f"session.create was not answered within {timeout_s:.0f}s")
        try:
            frame = await _next_json(ws, timeout_s=remaining, what="session.create")
        except ChatError:
            # The per-frame wait and the wall-clock deadline race as `remaining`
            # shrinks. Report the deadline that actually matters rather than
            # "did not respond within 0s".
            if asyncio.get_running_loop().time() >= deadline:
                raise ChatError(
                    f"session.create was not answered within {timeout_s:.0f}s"
                ) from None
            raise
        if frame.get("id") != rid:
            continue  # events interleave with responses; ignore until ours lands
        if "error" in frame:
            raise ChatError(f"session.create failed: {frame['error']}")
        return frame.get("result") or {}


async def send_turn(
    ws, session_id: str, text: str, *, timeout_s: float = DEFAULT_TURN_TIMEOUT_S
) -> Turn:
    """Send one message and wait for the agent's reply.

    Waits for `message.complete` rather than accumulating `message.delta`.
    The complete frame carries the whole text, so streaming is an option for a
    UI rather than a requirement for a correct answer — and reassembling
    deltas would make this client responsible for a rendering concern it has
    no business holding.
    """
    if not session_id:
        raise ChatError("cannot send a turn without a session id")

    rid = await _rpc(ws, "prompt.submit", {"session_id": session_id, "text": text})

    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise ChatError(f"the agent did not reply within {timeout_s:.0f}s")
        try:
            frame = await _next_json(ws, timeout_s=remaining, what="the agent")
        except ChatError:
            if asyncio.get_running_loop().time() >= deadline:
                raise ChatError(f"the agent did not reply within {timeout_s:.0f}s") from None
            raise

        # A rejected submit — unknown session, malformed params — comes back as
        # a JSON-RPC error against our id, never as a `message.complete`.
        # Treating it as noise means waiting out the whole turn deadline for an
        # answer that was refused in milliseconds.
        if frame.get("id") == rid and "error" in frame:
            raise ChatError(f"prompt.submit was rejected: {frame['error']}")

        params = frame.get("params") or {}
        if params.get("type") != COMPLETE_EVENT:
            continue
        payload = params.get("payload") or {}
        status = payload.get("status")
        reply = payload.get("text") or ""
        if status and status != "complete":
            # A provider failure comes back as a normal completion carrying an
            # error string — surfacing it as a reply would print an error
            # message as if the agent had said it.
            raise ChatError(f"the agent could not answer ({status}): {reply[:300]}")
        return Turn(session_id=session_id, response=reply, raw=payload)
