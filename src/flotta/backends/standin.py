"""A stand-in for `hermes serve`, for an agent on `local://` (M12.6).

The whole product path (sign in, create an agent, talk to it) has to run on
one machine for $0, with no Fly and no model call. So a local box runs this
instead of Hermes: it speaks enough of Hermes's protocol for a conversation,
and its replies are scripted.

**What it speaks is what the clients use, and nothing else.**

- The handshake the door does for a browser (`door.app.hermes_ticket`) and
  `flotta chat` does for itself: `POST /auth/password-login` answers with a
  session cookie, `POST /api/auth/ws-ticket` trades it for a one-use ticket,
  and `/api/ws?ticket=` opens the socket. Gated by the same two variables a
  real box reads (`HERMES_DASHBOARD_BASIC_AUTH_*`), so a door holding the
  wrong password is refused here as it would be on Fly.
- On the socket: `gateway.ready` first, then `session.create`,
  `session.most_recent`, `session.resume` and `prompt.submit`. A turn streams
  `message.delta` events and ends with `message.complete`, and the submit's
  own reply is only an acknowledgement, as Hermes's is.

**Hermes's two session ids are kept, on purpose.** The id `session.create`
and `session.resume` answer with is the *live* one, the one `prompt.submit`
takes; `session.most_recent` names the *stored* one, the one `session.resume`
takes. Using either in the other's place is `session not found`. Collapsing
them here would let a client that confuses them pass every local test and
fail on the first real box, and that confusion has shipped once already.

**Standing instructions are fixed when a session starts**, as Hermes renders
its system prompt once per session (FLOTTA-58). `SOUL.md` is seeded from
`$FLOTTA_INSTRUCTIONS` once, as the box's entrypoint does, and rewritten by
`set_instructions` through `LocalBackend.exec`. Each session keeps the copy it
started with and its replies quote it, so a client that resumes a session older
than an edit can be seen doing so, for $0.

Conversations are kept under `--home`, so stopping and starting the agent
keeps them, as a real box's volume does. Live ids are not kept: like Hermes's,
they belong to the process.

Run as `python -m flotta.backends.standin`; `LocalBackend` does. It binds
loopback only and opens no connection of its own.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import time
import weakref
from pathlib import Path
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

LOGIN_PATH = "/auth/password-login"
TICKET_PATH = "/api/auth/ws-ticket"
SOCKET_PATH = "/api/ws"
SESSION_COOKIE = "hermes_session_at"
USERNAME_ENV = "HERMES_DASHBOARD_BASIC_AUTH_USERNAME"
PASSWORD_ENV = "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"
#: What `GET /` says it is. `LocalBackend` reads it to tell its own agent from
#: whatever else might hold the port.
KIND = "flotta-standin"
#: Hermes's own socket ticket lasts thirty seconds and opens one socket.
TICKET_TTL_S = 30
#: `JSON-RPC`'s code for a method nobody serves.
METHOD_NOT_FOUND = -32601
#: What Hermes answers a `prompt.submit` naming a session it does not hold.
SESSION_NOT_FOUND = 4001


def reply_to(
    text: str, *, name: str, turn: int, soul: str | None = None, model: str | None = None
) -> str:
    """The scripted answer: who said it, what it heard, and how far along.

    The turn number is what shows a conversation was kept: ask again after the
    agent has slept and the count carries on. `soul` is the instructions this
    session started with; their first line shows which ones it is following.
    `model` is the one it was started with, which it never calls: it shows
    that a change of model reached the agent (FLOTTA-85).
    """
    said = f"This is {name}, a local stand-in with no model"
    if model:
        said += f" (set to run {model})"
    said += f". You said: {text.strip()!r}. That is message {turn} in this conversation."
    first = (soul or "").strip().splitlines()[:1]
    if first:
        said += f" My standing instructions begin: {first[0]!r}."
    return said


#: Where a box's `$FLOTTA_INSTRUCTIONS` arrives, as on a real box.
INSTRUCTIONS_ENV = "FLOTTA_INSTRUCTIONS"
#: The model a box is set to run, read once at boot, as a real box's entrypoint does.
MODEL_ENV = "FLOTTA_MODEL"


def soul_path(home: Path) -> Path:
    """`$HERMES_HOME/SOUL.md`: `--home` holds this agent's `hermes` directory."""
    return home / "hermes" / "SOUL.md"


def seed_soul(home: Path, env: dict[str, str]) -> None:
    """Write `SOUL.md` from the instructions an agent was created with, once.

    What `fly/box_entrypoint.sh` does: only when there is none, because after
    that the file on the volume is the agent's, and rewriting it is
    `set_instructions`'s job.
    """
    instructions = env.get(INSTRUCTIONS_ENV) or ""
    path = soul_path(home)
    if instructions and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(instructions + "\n", encoding="utf-8")


#: Messages that script a turn the way a real agent's goes, so a client can be
#: tried against tools and approvals with no model: `!tool` uses one, `!approve`
#: asks first. The frames are the shapes captured off a live gateway (v2026.9.14).
TOOL_TRIGGER = "!tool"
APPROVE_TRIGGER = "!approve"
PROBE_COMMAND = "rm -rf /tmp/flotta-standin-probe"


class Agent:
    """One stand-in's state: stored conversations, plus this process's live ids."""

    def __init__(
        self,
        *,
        name: str,
        home: Path,
        credentials: tuple[str, str] | None,
        model: str | None = None,
    ) -> None:
        self.model = model
        self.name = name
        self.store = home / "hermes" / "sessions.json"
        self.soul = soul_path(home)
        self.credentials = credentials
        self.cookies: set[str] = set()
        self.tickets: dict[str, float] = {}
        #: live id -> stored id. Per process, as Hermes's are.
        self.live: dict[str, str] = {}
        #: An approval asked and not yet answered: its request id -> (live id, stored id).
        self.asked: dict[str, tuple[str, str]] = {}

    # -- stored conversations -------------------------------------------------

    def load(self) -> dict[str, Any]:
        try:
            return json.loads(self.store.read_text())
        except FileNotFoundError:
            return {}

    def save(self, sessions: dict[str, Any]) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        partial = self.store.with_suffix(".partial")
        partial.write_text(json.dumps(sessions, indent=1))
        partial.replace(self.store)

    def read_soul(self) -> str | None:
        try:
            return self.soul.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def attach(self, stored_id: str) -> str:
        live_id = f"live-{secrets.token_hex(6)}"
        self.live[live_id] = stored_id
        return live_id

    # -- the handshake ----------------------------------------------------------

    def admits_login(self, body: dict[str, Any]) -> bool:
        if self.credentials is None:
            return True
        username, password = self.credentials
        # Both compared, always: stopping at a wrong username would time it.
        right_user = secrets.compare_digest(str(body.get("username") or ""), username)
        right_password = secrets.compare_digest(str(body.get("password") or ""), password)
        return right_user and right_password

    def take_ticket(self, ticket: str | None) -> bool:
        if self.credentials is None:
            return True
        expires = self.tickets.pop(ticket or "", None)
        return expires is not None and expires > time.time()


AGENT: web.AppKey[Agent] = web.AppKey("agent", Agent)
#: Every socket open on this agent, so stopping it ends them, as a stopped
#: machine does, rather than holding them for the web server's minute of grace.
SOCKETS: web.AppKey[weakref.WeakSet[web.WebSocketResponse]] = web.AppKey("sockets", weakref.WeakSet)


async def _ready(request: web.Request) -> web.Response:
    agent = request.app[AGENT]
    return web.json_response({"agent": KIND, "box": agent.name, "pid": os.getpid()})


async def _login(request: web.Request) -> web.Response:
    agent = request.app[AGENT]
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not agent.admits_login(body if isinstance(body, dict) else {}):
        return web.json_response({"detail": "Invalid credentials"}, status=401)
    cookie = secrets.token_urlsafe(24)
    agent.cookies.add(cookie)
    response = web.json_response({"ok": True})
    response.set_cookie(SESSION_COOKIE, cookie, httponly=True)
    return response


async def _ticket(request: web.Request) -> web.Response:
    agent = request.app[AGENT]
    if agent.credentials is not None and request.cookies.get(SESSION_COOKIE) not in agent.cookies:
        return web.json_response({"detail": "Unauthorized"}, status=401)
    ticket = secrets.token_urlsafe(24)
    agent.tickets[ticket] = time.time() + TICKET_TTL_S
    return web.json_response({"ticket": ticket, "ttl_seconds": TICKET_TTL_S})


def _event(kind: str, payload: dict[str, Any], session_id: str | None = None) -> str:
    params: dict[str, Any] = {"type": kind, "payload": payload}
    if session_id is not None:
        params["session_id"] = session_id
    return json.dumps({"jsonrpc": "2.0", "method": "event", "params": params})


def _result(rid: Any, result: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid: Any, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


async def _socket(request: web.Request) -> web.StreamResponse:
    agent = request.app[AGENT]
    if not agent.take_ticket(request.query.get("ticket")):
        return web.json_response({"detail": "Unauthorized"}, status=401)
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    request.app[SOCKETS].add(ws)
    await ws.send_str(_event("gateway.ready", {"skin": {}, "change_events": True, "standin": True}))
    async for message in ws:
        if message.type is not WSMsgType.TEXT:
            break
        try:
            frame = json.loads(message.data)
        except ValueError:
            continue
        if not isinstance(frame, dict):
            continue
        for out in _answer(agent, frame):
            await ws.send_str(out)
            # A beat between frames, so a client sees a stream rather than one
            # burst: that is the shape it has to handle on a real box.
            await asyncio.sleep(0)
    return ws


def _streamed(text: str, live_id: str) -> list[str]:
    """`text` as the deltas a gateway streams it in, a word at a time."""
    words = text.split(" ")
    return [
        _event("message.delta", {"text": word + (" " if i < len(words) - 1 else "")}, live_id)
        for i, word in enumerate(words)
    ]


def _tool(live_id: str, context: str) -> list[str]:
    """One terminal command, as a gateway reports it."""
    tool_id = f"call_{secrets.token_hex(6)}"
    args = {"command": context}
    return [
        _event("tool.generating", {"name": "terminal"}, live_id),
        _event(
            "tool.start",
            {"tool_id": tool_id, "name": "terminal", "context": context, "args": args},
            live_id,
        ),
        _event(
            "tool.complete",
            {
                "tool_id": tool_id,
                "name": "terminal",
                "duration_s": 0.02,
                "result": {"error": None, "exit_code": 0, "output": ""},
                "args": args,
            },
            live_id,
        ),
    ]


def _finish(agent: Agent, stored_id: str, live_id: str, answer: str) -> list[str]:
    """End a turn with `answer`, kept in the conversation."""
    sessions = agent.load()
    if stored_id in sessions:
        sessions[stored_id]["messages"].append({"role": "assistant", "text": answer})
        agent.save(sessions)
    return [
        *_streamed(answer, live_id),
        _event("message.complete", {"text": answer, "status": "complete"}, live_id),
    ]


def _answered(agent: Agent, rid: str, result: Any) -> list[str]:
    """A person's answer to an approval this agent asked for: run it, or not."""
    live_id, stored_id = agent.asked.pop(rid)
    choice = result.get("choice") if isinstance(result, dict) else None
    if choice in ("once", "session"):
        return [
            *_tool(live_id, PROBE_COMMAND),
            *_finish(agent, stored_id, live_id, f"Approved ({choice}), so I ran it."),
        ]
    return _finish(agent, stored_id, live_id, f"Not approved ({choice}), so I did not run it.")


def _answer(agent: Agent, frame: dict[str, Any]) -> list[str]:
    """Every frame one request produces, in the order they are sent."""
    rid = frame.get("id")
    method = frame.get("method")
    params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
    if method is None and isinstance(rid, str) and rid in agent.asked:
        return _answered(agent, rid, frame.get("result"))
    sessions = agent.load()

    if method == "session.create":
        stored_id = f"stored-{secrets.token_hex(6)}"
        sessions[stored_id] = {
            "title": str(params.get("title") or ""),
            "started_at": time.time(),
            "messages": [],
            # Fixed now, as a real session's system prompt is.
            "soul": agent.read_soul(),
        }
        agent.save(sessions)
        return [_result(rid, {"session_id": agent.attach(stored_id)})]

    if method == "session.most_recent":
        if not sessions:
            return [_result(rid, {"session_id": None})]
        stored_id, row = max(sessions.items(), key=lambda item: item[1]["started_at"])
        return [
            _result(
                rid,
                {"session_id": stored_id, "title": row["title"], "started_at": row["started_at"]},
            )
        ]

    if method == "session.resume":
        stored_id = str(params.get("session_id") or "")
        if stored_id not in sessions:
            return [_error(rid, SESSION_NOT_FOUND, f"session not found: {stored_id}")]
        return [
            _result(
                rid,
                {
                    "session_id": agent.attach(stored_id),
                    "resumed": stored_id,
                    "messages": sessions[stored_id]["messages"],
                },
            )
        ]

    if method == "prompt.submit":
        live_id = str(params.get("session_id") or "")
        stored_id = agent.live.get(live_id)
        if stored_id is None or stored_id not in sessions:
            return [_error(rid, SESSION_NOT_FOUND, f"session not found: {live_id}")]
        text = str(params.get("text") or "")
        messages = sessions[stored_id]["messages"]
        messages.append({"role": "user", "text": text})
        turn = sum(1 for m in messages if m["role"] == "user")
        agent.save(sessions)
        started = [_result(rid, {"status": "submitted"}), _event("message.start", {}, live_id)]
        if text.strip().startswith(APPROVE_TRIGGER):
            # Asked as a gateway that asks: its own request, answered by id.
            request_id = f"srq-{secrets.token_hex(6)}"
            agent.asked[request_id] = (live_id, stored_id)
            ask = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "approval",
                "params": {
                    "allow_permanent": True,
                    "allow_session": True,
                    "choices": ["once", "session", "always", "deny"],
                    "command": PROBE_COMMAND,
                    "description": "delete in root path",
                    "pattern_key": "delete in root path",
                    "pattern_keys": ["delete in root path"],
                    "request_id": secrets.token_hex(16),
                    "session_id": live_id,
                },
            }
            return [
                *started,
                *_streamed("I need to delete something first.", live_id),
                json.dumps(ask),
            ]
        if text.strip().startswith(TOOL_TRIGGER):
            return [
                *started,
                *_streamed("Let me look.", live_id),
                *_tool(live_id, "ls /workspace"),
                *_finish(agent, stored_id, live_id, "The workspace is empty."),
            ]
        return [
            *started,
            *_finish(
                agent,
                stored_id,
                live_id,
                reply_to(
                    text,
                    name=agent.name,
                    turn=turn,
                    soul=sessions[stored_id].get("soul"),
                    model=agent.model,
                ),
            ),
        ]

    if rid is None:
        return []
    return [_error(rid, METHOD_NOT_FOUND, f"the local stand-in does not serve {method!r}")]


def build_app(
    *, name: str, home: Path, credentials: tuple[str, str] | None, model: str | None = None
) -> web.Application:
    app = web.Application()
    app[AGENT] = Agent(name=name, home=home, credentials=credentials, model=model)
    app.router.add_get("/", _ready)
    app.router.add_post(LOGIN_PATH, _login)
    app.router.add_post(TICKET_PATH, _ticket)
    app.router.add_get(SOCKET_PATH, _socket)
    app[SOCKETS] = weakref.WeakSet()
    app.on_shutdown.append(_hang_up)
    return app


async def _hang_up(app: web.Application) -> None:
    """Close every open socket, going away: the agent is stopping."""
    for ws in set(app[SOCKETS]):
        await ws.close(code=WSCloseCode.GOING_AWAY, message=b"the agent is going to sleep")


def credentials_from(env: dict[str, str]) -> tuple[str, str] | None:
    """The login this agent demands, or None for an open one, as on a box."""
    password = (env.get(PASSWORD_ENV) or "").strip()
    if not password:
        return None
    return (env.get(USERNAME_ENV) or "").strip(), password


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv)
    seed_soul(args.home, dict(os.environ))
    app = build_app(
        name=args.name,
        home=args.home,
        credentials=credentials_from(dict(os.environ)),
        model=(os.environ.get(MODEL_ENV) or "").strip() or None,
    )
    web.run_app(app, host="127.0.0.1", port=args.port, print=None, handle_signals=True)


if __name__ == "__main__":
    main()
