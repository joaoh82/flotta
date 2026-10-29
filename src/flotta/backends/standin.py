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
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

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


def reply_to(text: str, *, name: str, turn: int) -> str:
    """The scripted answer: who said it, what it heard, and how far along.

    The turn number is what shows a conversation was kept: ask again after the
    agent has slept and the count carries on.
    """
    return (
        f"This is {name}, a local stand-in with no model. "
        f"You said: {text.strip()!r}. That is message {turn} in this conversation."
    )


class Agent:
    """One stand-in's state: stored conversations, plus this process's live ids."""

    def __init__(self, *, name: str, home: Path, credentials: tuple[str, str] | None) -> None:
        self.name = name
        self.store = home / "hermes" / "sessions.json"
        self.credentials = credentials
        self.cookies: set[str] = set()
        self.tickets: dict[str, float] = {}
        #: live id -> stored id. Per process, as Hermes's are.
        self.live: dict[str, str] = {}

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


def _answer(agent: Agent, frame: dict[str, Any]) -> list[str]:
    """Every frame one request produces, in the order they are sent."""
    rid = frame.get("id")
    method = frame.get("method")
    params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
    sessions = agent.load()

    if method == "session.create":
        stored_id = f"stored-{secrets.token_hex(6)}"
        sessions[stored_id] = {
            "title": str(params.get("title") or ""),
            "started_at": time.time(),
            "messages": [],
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
        answer = reply_to(text, name=agent.name, turn=turn)
        messages.append({"role": "assistant", "text": answer})
        agent.save(sessions)
        words = answer.split(" ")
        deltas = [
            _event("message.delta", {"text": word + (" " if i < len(words) - 1 else "")}, live_id)
            for i, word in enumerate(words)
        ]
        return [
            _result(rid, {"status": "submitted"}),
            _event("message.start", {}, live_id),
            *deltas,
            _event("message.complete", {"text": answer, "status": "complete"}, live_id),
        ]

    if rid is None:
        return []
    return [_error(rid, METHOD_NOT_FOUND, f"the local stand-in does not serve {method!r}")]


def build_app(*, name: str, home: Path, credentials: tuple[str, str] | None) -> web.Application:
    app = web.Application()
    app[AGENT] = Agent(name=name, home=home, credentials=credentials)
    app.router.add_get("/", _ready)
    app.router.add_post(LOGIN_PATH, _login)
    app.router.add_post(TICKET_PATH, _ticket)
    app.router.add_get(SOCKET_PATH, _socket)
    return app


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
    app = build_app(name=args.name, home=args.home, credentials=credentials_from(dict(os.environ)))
    web.run_app(app, host="127.0.0.1", port=args.port, print=None, handle_signals=True)


if __name__ == "__main__":
    main()
