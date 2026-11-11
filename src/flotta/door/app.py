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

The door asks the **control plane** to resolve and wake — two questions, and
the door checks *whose* box it is between them (M11): a token for another org,
another client or another box gets a 404 before anything is woken. D10 says fleet state is
written only by code that can reach the substrate; the door is a data-path
component that should be restartable, replaceable and — eventually — horizontally
scaled, and giving it a database handle would make it a second writer.

It therefore holds a Flotta token of its own, with `fleet:route` (M11.6a): the
one scope that resolves and wakes any org's box, because every other route is
scoped to its caller's tenant and the door finds a box before it knows whose.

## Auth composes rather than stacks

A caller presents a **Flotta token**; the door validates it and then supplies
the **box's own Hermes credentials** on the way out. Two consequences, both
deliberate:

- The box's password never leaves the server side. A client holds a scoped,
  expiring Flotta token and nothing else.
- Hermes's own auth gate is *satisfied*, not bypassed. M3 hit that gate, tried
  to route around it with a socat forwarder, and the workaround was the wrong
  answer — it would have removed a layer rather than passed one.

## WebSocket auth: a header, or the query string for the desktop app

A WS upgrade takes `Authorization` like any request, which is what `flotta chat`
and a colleague's question send. The desktop app couldn't set headers, so the
door also accepts `?access_token=` for WS upgrades, never for plain HTTP. That
has a real cost: query strings land in logs and proxy access records in a way
headers do not, so the door's own log hides every query value
(`flotta.server_logs`, FLOTTA-118). The path can go with the app (FLOTTA-80).

## A browser comes with a ticket (M12.5)

The web app's conversation is the one connection a browser opens itself, and
it does not hold a token: the web app's server asks the control plane for a
**chat ticket** (`flotta.auth.mint_ticket`) — one box, `box:chat`, two minutes
— and the page opens

    new WebSocket("wss://<box>.flotta.dev/api/ws",
                  ["flotta.chat.v1", "flotta.ticket.<ticket>"])

A subprotocol rather than the query string, so the ticket is not in a URL. The
door takes a ticket there and nowhere else, and only:

- from an `Origin` in `$FLOTTA_DOOR_ORIGINS` (none by default, so no browser
  gets in until the operator names the web app);
- for the agent socket (`/api/ws`), not anything else the box serves;
- for the box it names (`admits`), checked before the box is woken.

It answers with `flotta.chat.v1` alone: the protocol carrying the ticket is a
credential and is never echoed. A browser can do none of Hermes's handshake
(a login form, then Hermes's own ticket), so for a ticket the door does both
on the server side (`hermes_ticket`), with the credentials it already holds.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from flotta.address import parse as parse_address
from flotta.auth import (
    CHAT_PROTOCOL,
    DEFAULT_CLIENT,
    DEFAULT_ORG,
    SCOPE_BOX_CHAT,
    TICKET_PROTOCOL_PREFIX,
    AuthError,
    Token,
    admits,
    admits_tenant,
    box_password,
    is_client_person,
    verify,
    verify_ticket,
)
from flotta.door.conversation import ConversationGate
from flotta.door.turns import TurnRecorder, records

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
#: The browser origins a chat ticket is accepted from, comma-separated: the web
#: app's, e.g. `https://app.flotta.dev`. Unset, no browser gets in.
ALLOWED_ORIGINS_ENV = "FLOTTA_DOOR_ORIGINS"
#: `1` on a door that runs on the same machine as its `local://` agents
#: (`just local`). Unset everywhere else, so a deployed door routes none.
LOCAL_BOXES_ENV = "FLOTTA_DOOR_LOCAL"

#: The one path a ticket opens: Hermes's agent socket.
AGENT_SOCKET_PATH = "api/ws"
#: The largest frame taken from a box. A resume is one frame holding the whole
#: conversation, and `websockets` would otherwise close the socket at 1 MiB,
#: which leaves an agent with a long history unreachable. Bounded because a box
#: runs whatever its agent decides to.
BOX_FRAME_LIMIT = 64 * 1024 * 1024
#: Hermes's own handshake, which the door does for a browser.
HERMES_TICKET_PATH = "/api/auth/ws-ticket"

DEFAULT_DOMAIN = "flotta.dev"
DEFAULT_BOX_PORT = 9119
#: How long to wait for `hermes serve` after the machine reports started.
#: Matches `flotta.client`. Measured on a shared-cpu-1x box on 2026-11-06:
#: 162 s from a wake and 152 s after a restart (FLOTTA-156), so the 90 s this
#: once was refused agents that were booting normally. The web app's own wait
#: (`READY_TIMEOUT_MS` in flotta-cloud) has to stay above this one.
DEFAULT_READY_TIMEOUT_S = 240.0
#: How often to tell the control plane a live conversation is still live.
#: Comfortably under the default 30-minute idle threshold: this only has to
#: beat the sweep, and a chattier heartbeat would write more events for no gain.
HEARTBEAT_S = 120.0
#: Least time between two touches from one conversation. The control plane writes
#: `addressed` at most once a minute, so a sooner touch moves nothing, and each one
#: asks the substrate about the machine.
TOUCH_GAP_S = 60.0
#: How often the door pings the box's socket, and how long it waits for the pong
#: before calling the box gone (FLOTTA-195). These were websockets' defaults, 20 s
#: and 20 s, and a box compiling on a shared CPU starves Hermes's event loop for
#: longer than that: on 2026-11-10 a 43 s stall dropped a run whose agent went on
#: working for another 27 minutes. A stalled agent is not a dead one. The ping is
#: kept for a box that has truly gone without closing its socket, and five
#: minutes is longer than any stall but a runaway one.
BOX_PING_INTERVAL_S = 20.0
BOX_PING_TIMEOUT_S = 300.0
#: Longest the door keeps an agent's socket after the person's has gone, while
#: a turn of theirs waits on an answer someone else may give (FLOTTA-204): the
#: approval's hour (`flotta.box.approvals`), and a quarter more to finish. A
#: stopping door doesn't wait for it: `cli.door` bounds that (`DOOR_DRAIN_S`).
HELD_AFTER_LEAVING_S = 3600.0 + 900.0
#: Conversations going on, kept so a task outliving its handler isn't collected.
_CONVERSATIONS: set[asyncio.Task[None]] = set()
#: What a page sends to keep its socket through Cloudflare. Nobody is behind it.
KEEPALIVE_METHOD = "gateway.ping"
#: Longest frame read as a keepalive. A ping is short, and an upload needn't be parsed.
_KEEPALIVE_MAX_CHARS = 256
#: How Hermes says what its sessions are doing. Its own liveness signal.
BUSY_METHOD = "session.active_list"
#: How the door's own ids start, so an answer can be taken out of what the client is sent.
BUSY_ID_PREFIX = "flotta-door-busy-"
#: A status with nothing at work. Any other, known or not, holds the box awake.
QUIET_STATUS = "idle"
#: How long Hermes has to answer. Well under a beat, and no answer holds it awake.
BUSY_TIMEOUT_S = 30.0


def is_keepalive(frame: str) -> bool:
    """Whether a frame from the client only keeps the socket open."""
    if len(frame) > _KEEPALIVE_MAX_CHARS or KEEPALIVE_METHOD not in frame:
        return False
    try:
        parsed = json.loads(frame)
    except ValueError:
        return False
    return isinstance(parsed, dict) and parsed.get("method") == KEEPALIVE_METHOD


def read_working(frame: dict[str, Any]) -> bool | None:
    """Whether an answer to `BUSY_METHOD` says anything is at work. `None` if it doesn't say."""
    result = frame.get("result")
    sessions = result.get("sessions") if isinstance(result, dict) else None
    if not isinstance(sessions, list):
        return None
    statuses = [row.get("status") if isinstance(row, dict) else None for row in sessions]
    if not all(isinstance(status, str) for status in statuses):
        return None
    return any(status != QUIET_STATUS for status in statuses)


class Liveness:
    """Whether one open conversation still needs its box awake (FLOTTA-117).

    An open socket isn't someone talking: a forgotten tab pings for ever, and that
    used to keep a machine billing for ever. A beat now counts if the client said
    something since the last one, or if Hermes says something is at work. Frames
    from the box can't tell: a long tool sends none, and an idle Hermes sends some.

    **Anything short of Hermes saying it is idle holds the box awake.** That costs
    what the door has always cost, and sleeping under a turn loses the work.

    **Someone saying something brings the beat forward** (FLOTTA-120). The prompt is
    on the box already, and the sweep only reads `addressed`, so waiting for a beat
    could put the box to sleep under a turn that has just started. Opening a
    conversation needs none of this: the wake that opened it has just been counted.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._spoke = asyncio.Event()
        self._send: Callable[[str], Awaitable[None]] | None = None
        self._asked = 0
        self._answer: asyncio.Future[bool | None] | None = None
        self._warned = False
        #: Whether the last beat held the box only because Hermes gave no plain answer.
        self.unanswered = False

    def connected(self, send: Callable[[str], Awaitable[None]]) -> None:
        self._send = send

    def heard(self, frame: str | bytes) -> None:
        """A frame from the client, on its way to the box."""
        if isinstance(frame, bytes) or not is_keepalive(frame):
            self._spoke.set()
            # This beat counts now, whatever Hermes has yet to say.
            self._settle(True)

    def take_answer(self, frame: str) -> bool:
        """Whether a frame from the box answers the door, and so stops here."""
        if BUSY_ID_PREFIX not in frame:
            return False
        try:
            parsed = json.loads(frame)
        except ValueError:
            return False
        rid = parsed.get("id") if isinstance(parsed, dict) else None
        if not isinstance(rid, str) or not rid.startswith(BUSY_ID_PREFIX):
            return False
        # An answer to an older question stops here too, and decides nothing.
        if rid == self._busy_id():
            self._settle(read_working(parsed))
        return True

    def _busy_id(self) -> str:
        return f"{BUSY_ID_PREFIX}{self._asked}"

    def _settle(self, working: bool | None) -> None:
        if self._answer is not None and not self._answer.done():
            self._answer.set_result(working)

    def _took_speech(self) -> bool:
        spoke = self._spoke.is_set()
        self._spoke.clear()
        return spoke

    async def _working(self) -> bool | None:
        if self._send is None:
            return None
        self._asked += 1
        self._answer = asyncio.get_running_loop().create_future()
        question = {"jsonrpc": "2.0", "id": self._busy_id(), "method": BUSY_METHOD, "params": {}}
        try:
            await self._send(json.dumps(question))
            return await asyncio.wait_for(self._answer, BUSY_TIMEOUT_S)
        except Exception:
            return None
        finally:
            self._answer = None

    async def beat(self) -> None:
        """Until the next beat is due, or someone says something."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._spoke.wait(), HEARTBEAT_S)

    async def needs_box(self) -> bool:
        """Whether this beat should hold the box awake."""
        self.unanswered = False
        if self._took_speech():
            return True
        working = await self._working()
        if self._took_speech():
            return True
        self.unanswered = working is None
        if working is None and not self._warned:
            self._warned = True
            _log.warning("%s did not say whether it is working; holding it awake", self._name)
        return working is not False


async def _is_down(plane: Any, box: str) -> bool:
    """Whether the fleet says a box isn't running. Never wakes it, and not knowing isn't down."""
    with contextlib.suppress(Exception):
        return (await plane.resolve(box)).status not in (None, "running")
    return False


class BoxUnavailable(Exception):
    """The box could not be reached, and the reason is worth reporting."""

    def __init__(self, message: str, *, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Address:
    """Whose a box is, learned **without waking it** (M11).

    The tenant has to be known before the wake, not after: waking first would
    let a token from another org start a machine it was never going to be
    allowed to talk to — a denial-of-wallet, the same reason auth is checked
    before the wake at all.
    """

    box_id: str
    name: str
    org: str = DEFAULT_ORG
    client: str = DEFAULT_CLIENT
    #: What the fleet says it is doing. `None` from a control plane that doesn't say.
    status: str | None = None


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
    if is_client_person(token):
        raise BoxUnavailable("a client's people talk to an agent through the web app", status=403)
    return token


def resolve_allowed_origins(env: dict[str, str] | None = None) -> frozenset[str]:
    """`$FLOTTA_DOOR_ORIGINS`, as origins to compare exactly."""
    env = os.environ if env is None else env
    raw = env.get(ALLOWED_ORIGINS_ENV) or ""
    return frozenset(o for o in (_origin(part) for part in raw.split(",")) if o)


def _origin(value: str | None) -> str:
    """An origin as a browser sends one: lowercase, no trailing slash."""
    return (value or "").strip().rstrip("/").lower()


def offers_ticket(protocols: list[str]) -> bool:
    """Whether a handshake came with a ticket, rightly or not."""
    return any(p.startswith(TICKET_PROTOCOL_PREFIX) for p in protocols)


def admit_ticket(
    *,
    protocols: list[str],
    origin: str | None,
    path: str,
    allowed_origins: frozenset[str],
    key: str | None,
) -> Token:
    """The ticket a browser's handshake carries, if the door takes it here.

    The origin first, so a page the operator never named learns nothing about
    the ticket it holds; then the path, then the ticket itself. The box it
    names is `_target`'s to check, against the hostname's.
    """
    if _origin(origin) not in allowed_origins:
        raise BoxUnavailable(
            f"origin {origin or '(none)'!r} may not open a conversation", status=403
        )
    if path != AGENT_SOCKET_PATH:
        raise BoxUnavailable("a ticket opens the agent's conversation and nothing else", status=404)
    tickets = [
        p[len(TICKET_PROTOCOL_PREFIX) :] for p in protocols if p.startswith(TICKET_PROTOCOL_PREFIX)
    ]
    if CHAT_PROTOCOL not in protocols or len(tickets) != 1:
        raise BoxUnavailable(f"offer {CHAT_PROTOCOL} and exactly one ticket", status=400)
    try:
        return verify_ticket(tickets[0], key=key)
    except AuthError as exc:
        raise BoxUnavailable(str(exc), status=401) from exc


# -- talking to the control plane ------------------------------------------


@dataclass(frozen=True, slots=True)
class ControlPlane:
    """The door's client for the fleet API.

    Small on purpose. The door needs exactly two questions answered — *whose
    box is this* and *please wake it* — and giving it a broader client
    would invite it to grow opinions about fleet state, which is the control
    plane's job.
    """

    base_url: str
    token: str | None = None
    timeout_s: float = 30.0
    #: How requests travel: the network when `None`. A test hands it an
    #: `httpx.ASGITransport` around a real control plane, so the door and the
    #: fleet API run in one process with nothing but the machines faked.
    transport: Any = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def touch(self, box: str) -> None:
        """Tell the control plane this box is still in use. Best effort.

        `box` is the id the conversation was opened against, not the name: a
        heartbeat must never wake a different box that has since taken it.

        Waking is idempotent and the control plane rate-limits the event it
        writes, so calling this on a timer is cheap. It exists because idle
        sleep runs on the event log, and a *long conversation writes nothing*:
        the WebSocket is open, the agent is thinking, and without a heartbeat
        the sweep would read the box as quiet and suspend it mid-thought.
        `Liveness` decides when a conversation is still in use.

        Failures are swallowed. Losing a heartbeat costs an unnecessary wake
        later; raising here would drop a live conversation over bookkeeping.
        """
        with contextlib.suppress(Exception):
            await self.wake(box, box_id=box)

    async def wake(self, name: str, *, box_id: str | None = None) -> Target:
        """Resolve `name` and ensure it is running. Returns where to proxy.

        `box_id`, when the caller has one, is what gets woken; `name` is then
        only for messages. The door always has one by now, because the subject
        check ran against an id — and waking by name would start whatever holds
        the name *now*, which after a release and reuse is a box nobody checked.

        Wake rather than get-then-maybe-wake: `wake_box` is idempotent and
        cheap on a box that is already up, and asking two questions would
        leave a race between them — a box can be stopped by Fly during a host
        drain between the read and the connect.
        """
        response = await self._call(
            "POST", f"/api/boxes/{box_id or name}/wake", name=name, verb="wake"
        )
        body = response.json()
        endpoint = (body.get("box") or {}).get("endpoint")
        box_id = (body.get("box") or {}).get("id") or box_id or name
        if not endpoint:
            raise BoxUnavailable(f"box {name!r} has no endpoint to proxy to", status=502)
        # Where it is, whatever it is: whether this door may go there is the
        # door's own decision (`create_door`), made before anything is proxied.
        host, port = upstream(endpoint, local=True)
        return Target(box_id=box_id, name=name, endpoint=endpoint, host=host, port=port)

    async def resolve(self, name: str) -> Address:
        """Which box `name` is, and whose. Never wakes it.

        A separate question from `wake` on purpose, and the cost is one more
        round trip per request: the answer decides whether the wake may happen
        at all, so it cannot arrive with the wake.
        """
        response = await self._call("GET", f"/api/boxes/{name}/address", name=name, verb="resolve")
        box = response.json().get("box") or {}
        if not box.get("id"):
            raise BoxUnavailable(f"the control plane did not say which box {name!r} is", status=502)
        return Address(
            box_id=box["id"],
            name=box.get("name") or name,
            # Absent means the default, exactly as an absent claim does on a
            # token: a control plane that predates tenancy has one org.
            org=box.get("org") or DEFAULT_ORG,
            client=box.get("client") or DEFAULT_CLIENT,
            status=box.get("status"),
        )

    async def client_conversations(self, box_id: str) -> list[dict[str, Any]]:
        """This box's client conversations, newest first (FLOTTA-126)."""
        response = await self._call(
            "GET", f"/api/boxes/{box_id}/client-conversations", name=box_id, verb="read"
        )
        return list(response.json().get("conversations") or [])

    async def working(self, box_id: str) -> dict[str, Any] | None:
        """The run this box is doing now, if not a conversation's (FLOTTA-197)."""
        response = await self._call("GET", f"/api/boxes/{box_id}/working", name=box_id, verb="read")
        found = response.json().get("working")
        return found if isinstance(found, dict) else None

    async def record_client_conversation(self, box_id: str, session_id: str) -> None:
        """A session a client just made is the client's from now on."""
        await self._call(
            "POST",
            f"/api/boxes/{box_id}/client-conversations",
            name=box_id,
            verb="record",
            json={"session_id": session_id},
        )

    async def open_chat_run(
        self,
        box_id: str,
        *,
        prompt: str,
        session_id: str,
        by: str,
        client: bool,
        started_at: str,
        stored_session_id: str | None = None,
    ) -> str | None:
        """A turn began on `box_id`: its `chat` run's id, or None while the agent
        is doing another run (M14.6, `flotta.door.turns`)."""
        import httpx

        url = f"{self.base_url.rstrip('/')}/api/boxes/{box_id}/chat-runs"
        body = {
            "prompt": prompt,
            "session_id": session_id,
            "by": by,
            "client": client,
            "started_at": started_at,
        }
        if stored_session_id:
            body["stored_session_id"] = stored_session_id
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport) as http:
            response = await http.post(url, headers=self._headers(), json=body)
        if response.status_code == 409:
            return None
        response.raise_for_status()
        return str(response.json()["id"])

    async def finish_chat_run(
        self,
        box_id: str,
        run_id: str,
        *,
        steps: list[dict[str, Any]],
        status: str,
        output: str | None,
        error_class: str | None,
        approvals: int,
        ended_at: str,
    ) -> None:
        """The turn ended: its steps and reply, in one batch."""
        import httpx

        url = f"{self.base_url.rstrip('/')}/api/boxes/{box_id}/runs/{run_id}/steps"
        body = {
            "steps": steps,
            "status": status,
            "output": output,
            "error_class": error_class,
            "approvals": approvals,
            "ended_at": ended_at,
        }
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport) as http:
            response = await http.post(url, headers=self._headers(), json=body)
        response.raise_for_status()

    async def ask_chat_approval(
        self, box_id: str, run_id: str, *, request_id: str, params: dict[str, Any]
    ) -> str:
        """The agent asked a person in this turn: the approval's id (FLOTTA-166)."""
        body = {
            "request_id": request_id,
            "command": params.get("command"),
            "description": params.get("description"),
            "choices": params.get("choices"),
        }
        response = await self._door_post(f"/api/boxes/{box_id}/runs/{run_id}/approvals", body)
        return str(response.json()["id"])

    async def held_chat_approvals(self, box_id: str, run_id: str) -> list[dict[str, Any]]:
        """The turn's approvals, saying the door still holds its socket."""
        import httpx

        url = f"{self.base_url.rstrip('/')}/api/boxes/{box_id}/runs/{run_id}/approvals"
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport) as http:
            response = await http.get(url, headers=self._headers())
        response.raise_for_status()
        return list(response.json().get("approvals") or [])

    async def settle_chat_approval(
        self, box_id: str, run_id: str, *, request_id: str, choice: str | None, by: str | None
    ) -> None:
        """Settled on the socket: answered there, or withdrawn by the box."""
        body = {"request_id": request_id, "choice": choice, "by": by}
        await self._door_post(f"/api/boxes/{box_id}/runs/{run_id}/approvals/settled", body)

    async def chat_approval_delivered(self, box_id: str, run_id: str, approval_id: str) -> None:
        """An answer from outside reached the box."""
        await self._door_post(
            f"/api/boxes/{box_id}/runs/{run_id}/approvals/{approval_id}/delivered", None
        )

    async def _door_post(self, path: str, body: Any) -> Any:
        import httpx

        url = f"{self.base_url.rstrip('/')}{path}"
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport) as http:
            response = await http.post(url, headers=self._headers(), json=body)
        response.raise_for_status()
        return response

    async def _call(self, method: str, path: str, *, name: str, verb: str, json: Any = None) -> Any:
        """One request to the fleet API, with the door's errors mapped once."""
        import httpx

        url = f"{self.base_url.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_s, transport=self.transport
            ) as client:
                response = await client.request(method, url, headers=self._headers(), json=json)
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
                "fleet:route, signed with the same key (`just door-secrets`).",
                response.status_code,
            )
            raise BoxUnavailable("the door is not authorised to reach the fleet", status=500)
        if response.status_code == 402:
            # A budget paused it (FLOTTA-91). The control plane's words, and the
            # caller has already shown it may talk to this agent.
            raise BoxUnavailable(_detail(response), status=402)
        if response.status_code >= 400:
            raise BoxUnavailable(f"could not {verb} {name!r}: {response.text[:200]}", status=502)
        return response


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json()["detail"])
    except (ValueError, KeyError, TypeError):
        return response.text[:200]


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


def resolve_local_boxes(env: dict[str, str] | None = None) -> bool:
    """Whether this door routes `local://` boxes: only when told, with `1`."""
    env = os.environ if env is None else env
    return (env.get(LOCAL_BOXES_ENV) or "").strip() == "1"


def upstream(endpoint: str, *, local: bool = False) -> tuple[str, int]:
    """Where a box's Hermes listens: `(host, port)`, by the endpoint's scheme.

    A `fly://` box is reached over Fly's private network on Hermes's port. A
    `local://` box (M12.6) is a stand-in on this machine, and its endpoint
    names the port; `parse_endpoint` there refuses any host but loopback, so
    a local row can never send the door somewhere else.

    **And only a local door routes one** (`local`, from `$FLOTTA_DOOR_LOCAL`).
    On Fly, loopback is the door's own machine, so a deployed door that took
    `local://` would proxy authenticated traffic to whatever listens there
    after one misplaced `FLOTTA_BACKEND=local`. Anything else is not an
    address the door knows how to reach.
    """
    from flotta.backend import BackendError, scheme_of

    scheme = scheme_of(endpoint)
    try:
        if scheme == "fly":
            return internal_host(endpoint), DEFAULT_BOX_PORT
        if scheme == "local" and not local:
            raise BoxUnavailable(
                f"this door routes no local:// box; a door on the same machine as its "
                f"agents is started with ${LOCAL_BOXES_ENV}=1",
                status=502,
            )
        if scheme == "local":
            from flotta.backends.local_backend import parse_endpoint

            host, port, _ = parse_endpoint(endpoint)
            return host, port
    except BackendError as exc:
        raise BoxUnavailable(
            f"the box's endpoint is not one the door can reach: {exc}", status=502
        ) from exc
    raise BoxUnavailable(f"the door cannot reach a {scheme!r} box", status=502)


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
    # Every connection to a box ignores `$http_proxy` (`trust_env=False`, and
    # `proxy=None` for the socket). A box is on a private network or on this
    # machine; a proxy can reach neither, and for a `local://` box it would
    # carry a loopback request off the machine.
    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
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


async def hermes_ticket(base_url: str, credentials: tuple[str, str]) -> str:
    """Hermes's own one-use socket ticket, got the way `flotta chat` gets one.

    Its login form answers with session cookies, and those mint the ticket. A
    browser cannot do this through the door without the door's credentials, so
    the door does it, here, and the browser only ever opens the socket. The
    cookies are passed by hand rather than kept in a jar: a jar's policy is
    what dropped them once before (`flotta.client.new_session`).
    """
    username, password = credentials
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0, trust_env=False) as client:
            login = await client.post(
                LOGIN_PATH,
                json={"provider": "basic", "username": username, "password": password, "next": ""},
            )
            if login.status_code != 200:
                raise BoxUnavailable(
                    f"the box refused the door's credentials ({login.status_code})", status=502
                )
            cookie = "; ".join(f"{k}={v}" for k, v in login.cookies.items())
            minted = await client.post(HERMES_TICKET_PATH, headers={"cookie": cookie})
        ticket = minted.json().get("ticket") if minted.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        # Anything else escaping here fails the upgrade before `accept`, and the
        # browser gets an opaque 1006 instead of a reason it can show.
        raise BoxUnavailable(f"the box's socket handshake failed: {exc}", status=502) from exc
    if not ticket:
        raise BoxUnavailable(
            f"the box would not issue a socket ticket ({minted.status_code})", status=502
        )
    return str(ticket)


# -- the app ----------------------------------------------------------------


def resolve_box_credentials(env: dict[str, str] | None = None) -> tuple[str, str] | None:
    """The fleet's Hermes credentials, from which each box's own is derived.

    Never sent to a box as they are: `_own` derives the password of the box
    being proxied (FLOTTA-96), and that is the only one the box accepts. When
    every box held the fleet's, Fly's private network let any agent log in to
    any other — an agency's to the operator's — around this door.
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
    allowed_origins: frozenset[str] | None = None,
    local_boxes: bool | None = None,
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

    def _own(target: Target) -> tuple[str, str] | None:
        """The credentials of the box being proxied, and of no other.

        Derived and nothing else — never a retry with the fleet's own on a
        refusal. A box's agent has root on its machine and can refuse on
        purpose, and a door that then tried the fleet's password would hand it
        the one secret that opens everyone.
        """
        if creds is None:
            return None
        username, fleet_password = creds
        return username, box_password(fleet_password, target.box_id)

    origins = allowed_origins if allowed_origins is not None else resolve_allowed_origins()
    if local_boxes is None:
        local_boxes = resolve_local_boxes()

    app = FastAPI(title="Flotta front door")

    async def _target(token: Token, request_host: str | None) -> Target:
        """Resolve, check whose it is, wake, wait. In that order, and only that.

        The subject check sits between resolve and wake: after resolve because
        the box's tenant is not known before it, and before wake because a
        token that may not talk to a box has no business starting it.
        """
        name = host_to_box_name(request_host, domain=domain)
        # The hostname says whose box it is (`flotta.address`), so a token
        # for another org or client is refused here, before the control plane
        # hears of it — no resolve to time, nothing for it to log. The check
        # after resolve still runs: that one compares against the row.
        try:
            _, host_client, host_org = parse_address(name)
        except ValueError:
            raise BoxUnavailable(f"no box named {name!r}", status=404) from None
        refusal = admits_tenant(token, org=host_org, client=host_client)
        if refusal is not None:
            _log.info("refused %r for %r before resolving: %s", name, token.subject, refusal)
            raise BoxUnavailable(f"no box named {name!r}", status=404)
        address = await plane.resolve(name)
        refusal = admits(
            token,
            box_id=address.box_id,
            box_name=address.name,
            org=address.org,
            client=address.client,
        )
        if refusal is not None:
            # Logged for the operator, never said to the caller: "exists, but
            # not yours" would make every hostname a probe for another org's
            # agents. A box you may not address is a box that is not there.
            _log.info("refused %r for %r: %s", name, token.subject, refusal)
            raise BoxUnavailable(f"no box named {name!r}", status=404)
        # By id: the name can be released and reused between resolve and here,
        # and waking by name would start the replacement on this token's
        # account — the cost the check above exists to refuse.
        target = await plane.wake(name, box_id=address.box_id)
        if target.box_id != address.box_id:
            # Should be unreachable now; kept because proxying to a box other
            # than the one checked is the failure, however it comes about.
            raise BoxUnavailable(f"box {name!r} changed while it was being addressed", status=409)
        # Before the first byte goes to it: a deployed door routes no
        # `local://` box (`upstream`), so this refuses one here.
        upstream(target.endpoint, local=local_boxes)
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
            token = authorize(header=request.headers.get("authorization"), key=key)
            target = await _target(token, request.headers.get("host"))
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

        body = await request.body()
        own = _own(target)
        if own:
            # Two mechanisms, because Hermes only honours one of them and it is
            # not the obvious one. Its "basic" provider is a *login form* that
            # exchanges a JSON body for session cookies, so the header below is
            # inert — kept because a future provider (or another agent runtime
            # behind the same door) may want it, and it costs nothing.
            #
            # The rewrite is what actually authenticates: the client posts the
            # shape Hermes expects with no password it could know, and the door
            # fills in the real one here.
            forwarded["authorization"] = _basic(*own)
            if request.method == "POST" and f"/{path}" == LOGIN_PATH:
                body = rewrite_login(body, *own)
                forwarded.pop("content-length", None)

        url = f"http://{authority(target.host, target.port)}/{path}"
        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                upstream = await client.request(
                    request.method,
                    url,
                    params=strip_door_params(request.query_params),
                    headers=forwarded,
                    content=body,
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
        browser cannot set headers on a WS handshake. See `authorize`. A
        browser's ticket arrives as a subprotocol instead (`admit_ticket`).
        """
        protocols = list(websocket.scope.get("subprotocols") or [])
        ticketed = offers_ticket(protocols)
        # Only ever the public protocol, and only to a client that offered it:
        # a browser that offered subprotocols fails a handshake answering none.
        answer = CHAT_PROTOCOL if CHAT_PROTOCOL in protocols else None
        try:
            if ticketed:
                token = admit_ticket(
                    protocols=protocols,
                    origin=websocket.headers.get("origin"),
                    path=path,
                    allowed_origins=origins,
                    key=key,
                )
            else:
                token = authorize(
                    header=websocket.headers.get("authorization"),
                    query_token=websocket.query_params.get("access_token"),
                    key=key,
                    allow_query=True,
                )
            target = await _target(token, websocket.headers.get("host"))
            upstream_ticket = None
            own = _own(target)
            if ticketed and own:
                upstream_ticket = await hermes_ticket(
                    f"http://{authority(target.host, target.port)}", own
                )
        except BoxUnavailable as exc:
            # The handshake has to be accepted before a close code carries a
            # reason a client can read; rejecting outright gives the browser an
            # opaque failure. Accept, say why, close.
            await websocket.accept(subprotocol=answer)
            await websocket.close(code=4000 + min(exc.status, 999), reason=str(exc)[:120])
            return

        await websocket.accept(subprotocol=answer)

        liveness = Liveness(target.name)
        # Tokens that reach this far aren't a client's: see `authorize`.
        a_clients = ticketed and not token.full_access
        # Each turn of a person's conversation becomes a `chat` run (M14.6).
        turns = (
            TurnRecorder(box_id=target.box_id, subject=token.subject, client=a_clients, plane=plane)
            if records(token.subject)
            else None
        )
        gate = ConversationGate(
            box_id=target.box_id,
            subject=token.subject,
            plane=plane,
            conversation_only=a_clients,
            answers_approvals=token.answers_approvals,
            turns=turns,
        )

        async def keep_awake() -> None:
            """Hold the box awake for as long as someone is talking to it.

            The conversation itself is invisible to the fleet — the socket is
            open and nothing is written — so without this the idle sweep would
            suspend a box someone is mid-sentence with. The interval is well
            under the idle threshold so a single missed beat is survivable.
            An open socket with nobody on it holds nothing: see `Liveness`.

            One task, off the relay, so a slow control plane never holds up what
            someone said and a burst of frames can't become a burst of requests.

            Returning ends the conversation. A suspended machine may leave this
            socket open and silent, and a touch is a wake, so holding it awake
            on no answer would start the box it had just let sleep (FLOTTA-121).
            Someone saying something still wakes it: they are there.
            """
            while True:
                await liveness.beat()
                if await liveness.needs_box():
                    if liveness.unanswered and await _is_down(plane, target.box_id):
                        _log.info("%s is asleep; closing its quiet conversation", target.name)
                        return
                    await plane.touch(target.box_id)
                    await asyncio.sleep(TOUCH_GAP_S)

        heartbeat = asyncio.create_task(keep_awake())

        # Rebuilt from the filtered pairs rather than passed through: the raw
        # query carries `access_token`, and the box must not receive it.
        from urllib.parse import urlencode

        pairs = strip_door_params(websocket.query_params)
        if upstream_ticket is not None:
            # Hermes's ticket, not anything the browser put in the URL.
            pairs = [(k, v) for k, v in pairs if k != "ticket"] + [("ticket", upstream_ticket)]
        query = urlencode(pairs)
        upstream_url = f"ws://{authority(target.host, target.port)}/{path}" + (
            f"?{query}" if query else ""
        )
        headers = {"Authorization": _basic(*own)} if own else {}

        async def converse() -> None:
            """The relay, and whatever outlives the person's socket (FLOTTA-204)."""
            try:
                async with websockets.connect(
                    upstream_url,
                    additional_headers=headers,
                    proxy=None,
                    max_size=BOX_FRAME_LIMIT,
                    ping_interval=BOX_PING_INTERVAL_S,
                    ping_timeout=BOX_PING_TIMEOUT_S,
                ) as upstream:
                    liveness.connected(upstream.send)

                    # Set once the person's socket has gone while the agent's is
                    # kept for an answer (FLOTTA-204): nothing more goes their way.
                    left = asyncio.Event()

                    async def to_person(frame: str) -> None:
                        if left.is_set():
                            return
                        try:
                            await websocket.send_text(frame)
                        except Exception:
                            # Gone before its disconnect was read: the agent is
                            # still heard, and a hold is still decided on the read.
                            _log.info("%s's person went mid-frame", target.name)

                    async def deliver(srq: str, session: str, choice: str) -> None:
                        """An answer given from outside the conversation (FLOTTA-166):
                        to the box as the reply to its own request, then the card down."""
                        reply = {"jsonrpc": "2.0", "id": srq, "result": {"choice": choice}}
                        await upstream.send(json.dumps(reply))
                        withdrawn = gate.answered_elsewhere(srq, session)
                        if withdrawn is not None:
                            with contextlib.suppress(Exception):
                                await to_person(withdrawn)

                    if turns is not None:
                        turns.attach(deliver)

                    async def downstream_to_box() -> None:
                        while True:
                            message = await websocket.receive()
                            if message["type"] == "websocket.disconnect":
                                if turns is not None and turns.awaiting_answer():
                                    # Their turn waits on someone else's answer, which
                                    # only this socket can carry to the agent: keep it.
                                    left.set()
                                    return
                                await upstream.close()
                                return
                            frame = message.get("text")
                            if frame is None:
                                frame = message.get("bytes")
                            if frame is None:
                                continue
                            step = await gate.from_client(frame)
                            if step.to_box is not None:
                                liveness.heard(step.to_box)
                                await upstream.send(step.to_box)
                            if step.to_client is not None:
                                await websocket.send_text(step.to_client)

                    async def box_to_downstream() -> None:
                        async for message in upstream:
                            if isinstance(message, str):
                                if liveness.take_answer(message):
                                    continue
                                # Heard even with nobody to tell: it is how the turn's
                                # end, and an answer's arrival, are recorded.
                                answer = await gate.from_box(message)
                                if answer is not None:
                                    await to_person(answer)
                                for notice in gate.notices():
                                    await to_person(notice)
                            elif not left.is_set():
                                with contextlib.suppress(Exception):
                                    await websocket.send_bytes(message)

                    # Whichever side hangs up first ends the pair. Without this the
                    # surviving task would wait forever on a socket nobody is on.
                    person = asyncio.create_task(downstream_to_box())
                    agent = asyncio.create_task(box_to_downstream())
                    done, pending = await asyncio.wait(
                        [person, agent, heartbeat], return_when=asyncio.FIRST_COMPLETED
                    )
                    if left.is_set() and turns is not None:
                        # The person left mid-turn, with an approval of theirs still
                        # waiting (FLOTTA-204). Keep the agent's socket, the answer
                        # poll and the keep-awake beat until the turn ends, the agent
                        # hangs up, or `HELD_AFTER_LEAVING_S`: an answer from Home
                        # then reaches the agent, and the turn finishes and is
                        # recorded, for the person to read when they come back.
                        _log.info(
                            "%s's person left mid-turn awaiting an answer; holding", target.name
                        )
                        over = asyncio.create_task(turns.asked_turns_over())
                        done, pending = await asyncio.wait(
                            [agent, heartbeat, over],
                            timeout=HELD_AFTER_LEAVING_S,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        pending = {*pending, over} - done
                        _log.info(
                            "%s's held turn: %s",
                            target.name,
                            "ended"
                            if over in done
                            else "the agent hung up"
                            if agent in done
                            else "stopped holding",
                        )
                        with contextlib.suppress(Exception):
                            await upstream.close()
                    for task in pending:
                        task.cancel()
                    # Read what ended it. A side that closed, with or without a
                    # close frame (a killed agent sends none), is how a
                    # conversation ends; left unread, asyncio logged each one as a
                    # crash ("Task exception was never retrieved").
                    for task in done:
                        error = task.exception()
                        if isinstance(error, websockets.exceptions.ConnectionClosed):
                            _log.info("ws to %s ended: %s", target.name, error)
                        elif error is not None:
                            raise error
            except Exception as exc:  # pragma: no cover - network faults
                _log.warning("ws proxy to %s failed: %s", target.name, exc)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(Exception):
                    await websocket.close()
                if turns is not None:
                    # After the person's socket is closed: it costs them nothing.
                    await turns.close()

        # Its own task, awaited through a shield: a handler cancelled when the
        # person's socket goes (a test client, a server shutting the request
        # down) must not take a held turn, and the agent's socket, with it.
        conversation = asyncio.create_task(converse())
        _CONVERSATIONS.add(conversation)
        conversation.add_done_callback(_CONVERSATIONS.discard)
        await asyncio.shield(conversation)

    app.state.plane = plane
    app.state.signing_key = key
    app.state.allowed_origins = origins
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


#: Hermes's login endpoint. The door fills the credentials in here rather than
#: sending an `Authorization` header, because Hermes's "basic" provider is a
#: **login form**, not HTTP Basic: it exchanges a JSON body for session
#: cookies. An `Authorization: Basic` header — which is what this door sent at
#: first — is simply ignored by it.
LOGIN_PATH = "/auth/password-login"


def rewrite_login(body: bytes, username: str, password: str) -> bytes:
    """Put the box's real credentials into a login request. Best effort.

    The client sends the shape Hermes expects but with no password it could
    know; the door substitutes the real one on the way through. So the
    credential lives only on the server side, and the **session cookies Hermes
    returns belong to the client** — no session state here, nothing to expire
    or replicate, and a door restart does not log anyone out.

    Malformed input is passed through untouched rather than rejected: this is a
    proxy, and Hermes is entitled to answer its own 400 for a body it does not
    like. Guessing at what a caller meant is how a proxy starts having opinions.
    """
    if not body:
        # An empty body is not a login the door should complete. `or b"{}"`
        # here would have turned "the client sent nothing" into a *successful*
        # authentication, which is a surprising thing for a proxy to do on
        # someone's behalf.
        return body
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return body
    if not isinstance(payload, dict):
        return body
    payload["username"] = username
    payload["password"] = password
    payload.setdefault("provider", "basic")
    return json.dumps(payload).encode()


def _basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
