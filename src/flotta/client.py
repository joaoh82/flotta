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

import contextlib
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
USERNAME = "flotta"


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


async def login(session, base_url: str, username: str, password: str) -> None:
    """Exchange credentials for session cookies on `session`."""
    response = await session.post(
        f"{base_url}/auth/password-login",
        json={"provider": PROVIDER, "username": username, "password": password, "next": ""},
    )
    if response.status != 200:
        body = (await response.text())[:200]
        raise ChatError(
            f"login refused ({response.status}): {body}. "
            "Credentials are minted by `just fly-auth`; rotating them there "
            "updates both the box and .env."
        )


async def ws_ticket(session, base_url: str) -> str:
    """Mint a single-use WebSocket ticket for the logged-in session."""
    response = await session.post(f"{base_url}/api/auth/ws-ticket")
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


async def open_agent_socket(session, base_url: str, ticket: str):
    """Connect to the box's agent socket. Caller owns the returned WS."""
    return await session.ws_connect(f"{base_url}/api/ws?ticket={ticket}")
