"""The front door: resolve, wake, wait, proxy (M5b).

## Why this is not a reverse proxy

Caddy, nginx and Traefik were all considered and none of them can be the whole
answer, for one reason: **a request normally arrives while the box is asleep.**
That is not an edge case to handle, it is the cost argument (§8.4) — a box is
meant to be asleep most of the time, and an idle fleet costs about what a fleet
of disks costs. So before anything can be proxied, the door has to:

1. **resolve** the hostname to a box in the fleet,
2. **wake** it — and Fly's internal DNS only resolves *running* machines, which
   is why `wake_box` exists at all and why M3's `flotta chat` failed the first
   time it met a genuinely idle box,
3. **wait** for `hermes serve` to answer — "started" is not "ready"; the
   machine comes up in under a second and Hermes then imports for several,
4. and only then **proxy**, including the WebSocket upgrade the agent protocol
   needs.

Steps 1–3 need fleet state, substrate credentials and Flotta's own logic. This
is Flotta code with a proxy inside it, not a proxy with a configuration file.

## It does not touch the store

The door asks the **control plane** to resolve and wake. D10 says fleet state is
written only by code that can reach the substrate; the door is a data-path
component that should be restartable, replaceable and — eventually — horizontally
scaled, and giving it a database handle would make it a second writer.

It therefore holds a Flotta token of its own, with `fleet:read` and `box:chat`.

## Auth composes rather than stacks

A caller presents a **Flotta token**; the door validates it and then attaches
the **box's own Hermes credentials** on the way out. Two consequences, both
deliberate:

- The box's password never leaves the server side. A client holds a scoped,
  expiring Flotta token and nothing else.
- Hermes's own auth gate is *satisfied*, not bypassed. M3 hit that gate, tried
  to route around it with a socat forwarder, and the workaround was the wrong
  answer — it would have removed a layer rather than passed one.

## WebSocket auth goes in the query string, and only there

A browser cannot set headers on a WebSocket handshake. So the door accepts
`?access_token=` for WS upgrades — the standard workaround, and one with a real
cost: query strings land in logs and proxy access records in a way headers do
not. It is accepted **only** for the WS path, never for plain HTTP, where an
`Authorization` header is always available and there is no excuse.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from flotta.auth import SCOPE_BOX_CHAT, AuthError, Token, verify

# Imported at module level, not lazily inside `create_door`, and the reason is
# a bug rather than a preference: `from __future__ import annotations` turns
# every annotation into a string, and FastAPI resolves those against the
# **module** namespace. With `Request` imported inside the function it resolved
# to nothing, FastAPI fell back to treating the parameter as a query field, and
# every proxied request answered 422.
#
# `flotta.control` gets away with lazy imports because the CLI imports it for
# commands that never build an app. Nothing imports `flotta.door` except the
# door, which cannot run without these anyway — so the honesty is free.

_log = logging.getLogger("flotta.door")

CONTROL_URL_ENV = "FLOTTA_CONTROL_URL"
CONTROL_TOKEN_ENV = "FLOTTA_CONTROL_TOKEN"
BOX_PASSWORD_ENV = "FLOTTA_BOX_PASSWORD"
BOX_USERNAME_ENV = "FLOTTA_BOX_USERNAME"
DOMAIN_ENV = "FLOTTA_DOMAIN"

DEFAULT_DOMAIN = "flotta.dev"
DEFAULT_BOX_PORT = 9119
#: How long to wait for `hermes serve` after the machine reports started.
#: Matches `flotta.client`, which learned the number the hard way.
DEFAULT_READY_TIMEOUT_S = 90.0
#: How often to tell the control plane a live conversation is still live.
#: Comfortably under the default 30-minute idle threshold: this only has to
#: beat the sweep, and a chattier heartbeat would write more events for no gain.
HEARTBEAT_S = 120.0


class BoxUnavailable(Exception):
    """The box could not be reached, and the reason is worth reporting."""

    def __init__(self, message: str, *, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Target:
    """Where a request is going, once the box is known to be up."""

    box_id: str
    name: str
    endpoint: str
    #: `<machine>.vm.<app>.internal` — addressed directly rather than by app
    #: name. Fly's DNS resolves an app name to *some* machine, and with several
    #: boxes in one app that would silently connect you to someone else's agent.
    host: str
    port: int = DEFAULT_BOX_PORT


def host_to_box_name(host: str | None, *, domain: str | None = None) -> str:
    """`eng-a.flotta.dev` → `eng-a`.

    Rejects anything that is not a single label under the configured domain.
    Deliberately strict: the name goes on to select a machine, so accepting
    `../` or a nested label would be handing an attacker the box selector.
    """
    domain = (domain or os.environ.get(DOMAIN_ENV) or DEFAULT_DOMAIN).strip().lower()
    if not host:
        raise BoxUnavailable("no Host header", status=400)

    # Strip the port; a Host header carries one whenever the listener is not on
    # the scheme's default.
    bare = host.split(",")[0].strip().lower()
    if bare.startswith("["):  # IPv6 literal — never a box hostname
        raise BoxUnavailable(f"{host!r} is not a box hostname", status=400)
    bare = bare.rsplit(":", 1)[0] if ":" in bare else bare

    suffix = f".{domain}"
    if not bare.endswith(suffix):
        raise BoxUnavailable(f"{bare!r} is not under {domain}", status=404)

    label = bare[: -len(suffix)]
    if not label or "." in label:
        raise BoxUnavailable(f"{bare!r} does not name a single box", status=404)
    # Box names come from `flotta create`, which allows what the store allows.
    # Restrict here anyway: this value is about to choose a machine.
    if not all(c.isalnum() or c in "-_" for c in label):
        raise BoxUnavailable(f"{label!r} is not a valid box name", status=404)
    return label


def authorize(
    *,
    header: str | None,
    query_token: str | None = None,
    key: str | None = None,
    allow_query: bool = False,
) -> Token:
    """Validate the caller's Flotta token and require `box:chat`.

    `allow_query` is passed only by the WebSocket path. A browser cannot set
    headers on a WS handshake, so the token has to travel in the URL there —
    and a query string is logged in places a header is not, which is why plain
    HTTP never gets the same allowance.
    """
    raw: str | None = None
    if header and header.lower().startswith("bearer "):
        raw = header.split(" ", 1)[1].strip()
    elif allow_query and query_token:
        raw = query_token.strip()

    if not raw:
        raise BoxUnavailable("missing bearer token", status=401)
    try:
        token = verify(raw, key=key)
    except AuthError as exc:
        raise BoxUnavailable(str(exc), status=401) from exc
    if not token.allows(SCOPE_BOX_CHAT):
        raise BoxUnavailable(
            f"token for {token.subject!r} lacks scope: {SCOPE_BOX_CHAT}", status=403
        )
    return token


# -- talking to the control plane ------------------------------------------


@dataclass(frozen=True, slots=True)
class ControlPlane:
    """The door's client for the fleet API.

    Small on purpose. The door needs exactly two questions answered — *which
    machine is this box* and *please wake it* — and giving it a broader client
    would invite it to grow opinions about fleet state, which is the control
    plane's job.
    """

    base_url: str
    token: str | None = None
    timeout_s: float = 30.0

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def touch(self, name: str) -> None:
        """Tell the control plane this box is still in use. Best effort.

        Waking is idempotent and the control plane rate-limits the event it
        writes, so calling this on a timer is cheap. It exists because idle
        sleep runs on the event log, and a *long conversation writes nothing*:
        the WebSocket is open, the agent is thinking, and without a heartbeat
        the sweep would read the box as quiet and suspend it mid-thought.

        Failures are swallowed. Losing a heartbeat costs an unnecessary wake
        later; raising here would drop a live conversation over bookkeeping.
        """
        with contextlib.suppress(Exception):
            await self.wake(name)

    async def wake(self, name: str) -> Target:
        """Resolve `name` and ensure it is running. Returns where to proxy.

        Wake rather than get-then-maybe-wake: `wake_box` is idempotent and
        cheap on a box that is already up, and asking two questions would
        leave a race between them — a box can be stopped by Fly during a host
        drain between the read and the connect.
        """
        import httpx

        url = f"{self.base_url.rstrip('/')}/api/boxes/{name}/wake"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(url, headers=self._headers())
        except Exception as exc:
            # The control plane being unreachable is the door's own outage, not
            # the box's. Say which one is down.
            raise BoxUnavailable(
                f"cannot reach the control plane at {self.base_url}: {exc}", status=503
            ) from exc

        if response.status_code == 404:
            raise BoxUnavailable(f"no box named {name!r}", status=404)
        if response.status_code in (401, 403):
            # The *door's* credential is wrong, not the caller's. Reporting 401
            # would tell the user to re-authenticate for an operator's mistake.
            _log.error(
                "the door's own control-plane token was rejected (%s). It needs "
                "fleet:read and box:chat, signed with the same key.",
                response.status_code,
            )
            raise BoxUnavailable("the door is not authorised to reach the fleet", status=500)
        if response.status_code >= 400:
            raise BoxUnavailable(f"could not wake {name!r}: {response.text[:200]}", status=502)

        body = response.json()
        endpoint = (body.get("box") or {}).get("endpoint")
        box_id = (body.get("box") or {}).get("id") or name
        if not endpoint:
            raise BoxUnavailable(f"box {name!r} has no endpoint to proxy to", status=502)
        return Target(box_id=box_id, name=name, endpoint=endpoint, host=internal_host(endpoint))


def authority(host: str, port: int) -> str:
    """`host:port`, bracketing **only** an IPv6 literal.

    Square brackets in a URL authority mean "this is an IPv6 address". Fly
    hands out a *hostname* (`<machine>.vm.<app>.internal`), and bracketing that
    produces `http://[m1.vm.app.internal]:9119`, which httpx rejects outright
    as an invalid IPv6 address — so every proxied request would have failed.

    Written as its own function because the same string is needed in four
    places (the HTTP URL, the WS URL, the readiness probe and the rewritten
    `Host` header) and getting it right in three of them is the same as getting
    it wrong.
    """
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def internal_host(endpoint: str) -> str:
    """`fly://app/machine` → `machine.vm.app.internal`.

    Addressed by machine rather than by app deliberately. Fly's internal DNS
    resolves an app name to *some* machine in it, so with several boxes in one
    app that would connect you to whichever one answered — someone else's
    agent, with someone else's memory.
    """
    from flotta.backends.fly_backend import parse_endpoint

    app, machine_id = parse_endpoint(endpoint)
    return f"{machine_id}.vm.{app}.internal"


# -- readiness --------------------------------------------------------------


async def wait_until_ready(
    base_url: str,
    *,
    timeout_s: float = DEFAULT_READY_TIMEOUT_S,
    sleep: Any = None,
) -> None:
    """Block until `hermes serve` answers, or give up with a useful message.

    **"started" is not "ready".** The machine reports started in well under a
    second and Hermes then imports itself for several more, so proxying on the
    strength of the substrate's word produces a connection refused that looks
    like the box is broken rather than waking.

    Any HTTP status below 500 counts as ready: a 401 or a 404 both prove
    something is serving, and the door has not authenticated yet at this point.
    """
    naps = sleep or asyncio.sleep
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    last = "no response"
    async with httpx.AsyncClient(timeout=5.0) as client:
        while loop.time() < deadline:
            try:
                response = await client.get(base_url + "/")
                if response.status_code < 500:
                    return
                last = f"HTTP {response.status_code}"
            except Exception as exc:
                last = type(exc).__name__
            await naps(0.5)
    raise BoxUnavailable(
        f"the box is up but hermes serve did not answer within {timeout_s:.0f}s (last: {last})",
        status=504,
    )


# -- the app ----------------------------------------------------------------


def resolve_box_credentials(env: dict[str, str] | None = None) -> tuple[str, str] | None:
    """The box's own Hermes credentials, attached on the way out.

    One pair for the whole app today, because `just fly-auth` mints one pair
    per Fly app and every box in that app shares it. That is a real limitation
    and it is bounded: the credential never leaves the server side, so its blast
    radius is "someone who already compromised the door", not "any user".
    Per-box credentials become necessary at M10, where boxes belong to different
    people.
    """
    env = os.environ if env is None else env
    password = (env.get(BOX_PASSWORD_ENV) or "").strip()
    if not password:
        return None
    return (env.get(BOX_USERNAME_ENV) or "flotta").strip(), password


def create_door(
    *,
    control: ControlPlane | None = None,
    signing_key: str | None = None,
    domain: str | None = None,
    box_credentials: tuple[str, str] | None = None,
    ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S,
) -> Any:
    """Build the front-door ASGI app.

    Every dependency is injectable because none of them can be exercised in a
    test otherwise: the control plane is an HTTP service, the box is a machine
    on a private network, and the signing key is a secret. The alternative is a
    module that can only be tested by deploying it.
    """
    from flotta.auth import resolve_signing_key

    key = signing_key if signing_key is not None else resolve_signing_key()
    plane = control or ControlPlane(
        base_url=os.environ.get(CONTROL_URL_ENV, "http://127.0.0.1:8080"),
        token=os.environ.get(CONTROL_TOKEN_ENV) or None,
    )
    creds = box_credentials if box_credentials is not None else resolve_box_credentials()

    app = FastAPI(title="Flotta front door")

    async def _target(request_host: str | None) -> Target:
        name = host_to_box_name(request_host, domain=domain)
        target = await plane.wake(name)
        await wait_until_ready(
            f"http://{authority(target.host, target.port)}", timeout_s=ready_timeout_s
        )
        return target

    @app.get("/_door/health")
    def health() -> Any:
        """The door's own liveness. Open, and says nothing about the fleet."""
        return {"status": "ok", "domain": domain or os.environ.get(DOMAIN_ENV) or DEFAULT_DOMAIN}

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def proxy(path: str, request: Request) -> Response:
        try:
            authorize(header=request.headers.get("authorization"), key=key)
            target = await _target(request.headers.get("host"))
        except BoxUnavailable as exc:
            return JSONResponse({"detail": str(exc)}, status_code=exc.status)

        # Hop-by-hop headers describe *this* connection and must not be
        # forwarded; `host` must be rewritten or the box sees the public name.
        # `authorization` is dropped deliberately — the caller's Flotta token
        # has done its job, and forwarding it would hand a box a credential for
        # the fleet it lives in.
        forwarded = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP and k.lower() not in ("host", "authorization")
        }
        forwarded["host"] = authority(target.host, target.port)
        if creds:
            forwarded["authorization"] = _basic(*creds)

        url = f"http://{authority(target.host, target.port)}/{path}"
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                upstream = await client.request(
                    request.method,
                    url,
                    params=strip_door_params(request.query_params),
                    headers=forwarded,
                    content=await request.body(),
                )
        except Exception as exc:
            return JSONResponse(
                {"detail": f"box {target.name!r} did not answer: {exc}"}, status_code=502
            )

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP},
        )

    @app.websocket("/{path:path}")
    async def proxy_ws(websocket: WebSocket, path: str) -> None:
        """Proxy the agent socket. The reason the door exists.

        `flotta chat` and the Flotta app both speak JSON-RPC over a WebSocket;
        HTTP proxying alone would give you a login page and nothing to say to
        it.

        The token may arrive in the query string here, and only here — a
        browser cannot set headers on a WS handshake. See `authorize`.
        """
        try:
            authorize(
                header=websocket.headers.get("authorization"),
                query_token=websocket.query_params.get("access_token"),
                key=key,
                allow_query=True,
            )
            target = await _target(websocket.headers.get("host"))
        except BoxUnavailable as exc:
            # The handshake has to be accepted before a close code carries a
            # reason a client can read; rejecting outright gives the browser an
            # opaque failure. Accept, say why, close.
            await websocket.accept()
            await websocket.close(code=4000 + min(exc.status, 999), reason=str(exc)[:120])
            return

        await websocket.accept()

        async def keep_awake() -> None:
            """Hold the box awake for as long as someone is talking to it.

            The conversation itself is invisible to the fleet — the socket is
            open and nothing is written — so without this the idle sweep would
            suspend a box someone is mid-sentence with. The interval is well
            under the idle threshold so a single missed beat is survivable.
            """
            while True:
                await asyncio.sleep(HEARTBEAT_S)
                await plane.touch(target.name)

        heartbeat = asyncio.create_task(keep_awake())

        # Rebuilt from the filtered pairs rather than passed through: the raw
        # query carries `access_token`, and the box must not receive it.
        from urllib.parse import urlencode

        query = urlencode(strip_door_params(websocket.query_params))
        upstream_url = f"ws://{authority(target.host, target.port)}/{path}" + (
            f"?{query}" if query else ""
        )
        headers = {"Authorization": _basic(*creds)} if creds else {}

        try:
            async with websockets.connect(upstream_url, additional_headers=headers) as upstream:

                async def downstream_to_box() -> None:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            await upstream.close()
                            return
                        if (text := message.get("text")) is not None:
                            await upstream.send(text)
                        elif (data := message.get("bytes")) is not None:
                            await upstream.send(data)

                async def box_to_downstream() -> None:
                    async for message in upstream:
                        if isinstance(message, str):
                            await websocket.send_text(message)
                        else:
                            await websocket.send_bytes(message)

                # Whichever side hangs up first ends the pair. Without this the
                # surviving task would wait forever on a socket nobody is on.
                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(downstream_to_box()),
                        asyncio.create_task(box_to_downstream()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
        except Exception as exc:  # pragma: no cover - network faults
            _log.warning("ws proxy to %s failed: %s", target.name, exc)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(Exception):
                await websocket.close()

    app.state.plane = plane
    app.state.signing_key = key
    return app


#: Headers that describe a single hop and are meaningless — or harmful —
#: forwarded. `connection` and `upgrade` in particular would confuse the
#: upstream about a connection it is not part of.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "content-encoding",
    }
)


#: Query parameters the door consumes and must not pass upstream.
#: `access_token` is the WS handshake's only way to carry a Flotta token — a
#: browser cannot set headers there — which makes it a *credential in the URL*,
#: and forwarding it hands the box a token for the fleet it lives in.
_DOOR_QUERY_PARAMS = frozenset({"access_token"})


def strip_door_params(query: Any) -> list[tuple[str, str]]:
    """Drop the door's own query parameters before proxying.

    The HTTP path strips `Authorization` so a box never sees a caller's Flotta
    token. This is the same rule for the other channel the token can arrive on,
    and it was missed: the WS path forwarded the query wholesale, which meant
    the one route the door exists for leaked exactly what the HTTP route was
    careful to withhold.

    Returns pairs rather than a dict so a repeated parameter is not silently
    collapsed — the box's own protocol may care.
    """
    return [(k, v) for k, v in query.multi_items() if k.lower() not in _DOOR_QUERY_PARAMS]


def _basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
