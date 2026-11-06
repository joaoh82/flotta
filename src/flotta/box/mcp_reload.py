"""Ask this box's Hermes to reload its MCP servers (FLOTTA-125).

Runs on the box, shipped over `exec` the way `flotta.box.rules` is, so it reaches
an agent on the image it already has, and run with the box's Python (Hermes's
venv). `provision.set_mcp_grants` runs it after writing a grant change into
`config.yaml`, so a server taken away leaves the tool list of every open
conversation, not only the next one. The gateway refuses that server either way;
this is so the agent stops offering a tool it can't call.

**What Hermes needs.** `reload.mcp` (`tui_gateway/methods_tools.py` at v0.22.5)
over its own JSON-RPC socket, with `confirm: true`, which is what a person typing
`/reload-mcp now` sends. No session: it reloads the servers for the whole
process and refreshes every open conversation. The socket wants a ticket, and a
ticket wants a login with the box's own dashboard credentials, which the box
already has in its environment (`provision`'s `HERMES_DASHBOARD_BASIC_AUTH_*`).

**Best effort.** It exits non-zero with a line on stderr when anything fails, and
the caller records that; nothing is retried. A reload costs the next turn its
prompt cache, which is the price of not showing a tool that is gone.

Imports nothing of Flotta's: only the standard library, and `websockets`, which
Hermes's venv has because Hermes does.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import urllib.request
from pathlib import Path

#: How long Hermes may take: a reload reconnects to every server it has.
TIMEOUT_S = 60.0


def _environment() -> dict[str, str]:
    """This process's environment, with PID 1's under it: `exec` may not carry the machine's."""
    found: dict[str, str] = {}
    try:
        raw = Path("/proc/1/environ").read_bytes()
    except OSError:
        raw = b""
    for item in raw.split(b"\0"):
        name, sep, value = item.partition(b"=")
        if sep:
            found[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return {**found, **os.environ}


def _post(opener: urllib.request.OpenerDirector, url: str, body: dict | None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"content-type": "application/json"}
    )
    with opener.open(request, timeout=30) as response:
        text = response.read().decode("utf-8") or "{}"
    return json.loads(text)


def reload(env: dict[str, str]) -> dict:
    from websockets.sync.client import connect

    port = env.get("FLOTTA_SERVE_PORT") or "9119"
    base = f"http://[::1]:{port}"
    user = env.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME") or ""
    password = env.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD") or ""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    _post(
        opener,
        f"{base}/auth/password-login",
        {"provider": "basic", "username": user, "password": password, "next": ""},
    )
    ticket = _post(opener, f"{base}/api/auth/ws-ticket", None).get("ticket")
    if not ticket:
        raise RuntimeError("Hermes minted no socket ticket")
    with connect(f"ws://[::1]:{port}/api/ws?ticket={ticket}", open_timeout=30) as ws:
        ws.send(
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "reload.mcp", "params": {"confirm": True}}
            )
        )
        while True:
            frame = json.loads(ws.recv(timeout=TIMEOUT_S))
            if frame.get("id") != 1:
                continue  # an event for an open conversation
            if "error" in frame:
                raise RuntimeError(f"reload.mcp refused: {frame['error']}")
            return frame.get("result") or {}


def main(argv: list[str]) -> int:
    try:
        result = reload(_environment())
    except Exception as exc:  # noqa: BLE001 - the caller records it, whatever it was
        print(f"MCP reload failed: {type(exc).__name__}: {exc}"[:400], file=sys.stderr)
        return 1
    print(json.dumps(result)[:400])
    return 0 if result.get("status") == "reloaded" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
