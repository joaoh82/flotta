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
the caller records that; nothing is retried, with one exception. With `--wait`,
which `wake_box` passes when it has just started the machine, a Hermes that isn't
listening yet is waited for (up to `WAIT_S`) rather than reported as a failure:
the config was written while it booted, so whether boot read it is a race, and a
reload once it is up settles it either way. Without the wait, every wake that
carried a pending grant recorded `mcp_reload_failed` for a reload that had
simply come too early (seen live on `eng-r`, 2026-11-07). A reload costs the next turn its
prompt cache, which is the price of not showing a tool that is gone.

**Or only wait** (`--only-wait`, FLOTTA-252): answer whether Hermes is listening,
waiting up to `ONLY_WAIT_S` for it, and reload nothing. A short wait, asked again
and again by the caller, because Fly ends a command on a machine after about 30
seconds whatever it is doing: one long wait was cut off on every real first
boot, which takes longer than that (seen on staging, 2026-11-20). `provision.create_box` asks this
before it writes a new agent's first MCP config. Hermes listening means
`box_entrypoint.sh` has finished: its steps that also edit `config.yaml`
(approvals, skills, the model, the screen) each read the file and save it, and on
a first boot every one of them saves. On an image from before FLOTTA-253 they did
that with no lock, so a config written before then could be put back without its
servers, or read half-written and saved over the boot step's own; since then they
take the writer's lock (`flotta.box.config_file`). Either way Hermes has to be
listening before it can be asked to reload, so creation still waits for it.

Imports nothing of Flotta's: only the standard library, and `websockets`, which
Hermes's venv has because Hermes does.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

#: How long Hermes may take: a reload reconnects to every server it has.
TIMEOUT_S = 60.0

#: How long `--wait` waits for a Hermes that is still booting to start listening.
WAIT_S = 120.0

#: The flag that says the machine was just started, so Hermes may not be up yet.
WAIT_FLAG = "--wait"

#: The flag that asks only whether Hermes is listening, waiting for it: no reload.
ONLY_WAIT_FLAG = "--only-wait"

#: How long one `--only-wait` waits. Well under the 30 seconds or so after which
#: Fly ends a command on a machine: the caller asks again until Hermes is up.
ONLY_WAIT_S = 15.0


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


def _not_listening(exc: BaseException) -> bool:
    """Nothing on Hermes's port yet: a refused connection, not an answer of any kind."""
    if isinstance(exc, urllib.error.HTTPError):
        return False  # Hermes answered, so it is up: a refusal is a real failure
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason if isinstance(exc.reason, BaseException) else exc
    return isinstance(exc, ConnectionRefusedError)


def listening(
    env: dict[str, str],
    *,
    wait_s: float,
    sleep=time.sleep,
    clock=time.monotonic,
    connect=socket.create_connection,
) -> bool:
    """Whether Hermes accepts a connection on its port, waiting up to `wait_s` for it."""
    port = int(env.get("FLOTTA_SERVE_PORT") or "9119")
    deadline = clock() + wait_s
    while True:
        try:
            connect(("::1", port), timeout=5).close()
            return True
        except OSError:
            if clock() >= deadline:
                return False
            sleep(1.0)


def reload(
    env: dict[str, str],
    *,
    wait_s: float = 0.0,
    sleep=time.sleep,
    clock=time.monotonic,
) -> dict:
    from websockets.sync.client import connect

    port = env.get("FLOTTA_SERVE_PORT") or "9119"
    base = f"http://[::1]:{port}"
    user = env.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME") or ""
    password = env.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD") or ""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    login = {"provider": "basic", "username": user, "password": password, "next": ""}
    deadline = clock() + wait_s
    while True:
        try:
            _post(opener, f"{base}/auth/password-login", login)
            break
        except Exception as exc:  # noqa: BLE001 - only "not listening yet" is waited for
            if not _not_listening(exc) or clock() >= deadline:
                raise
            sleep(1.0)
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
    if ONLY_WAIT_FLAG in argv:
        if listening(_environment(), wait_s=ONLY_WAIT_S):
            print("Hermes is listening")
            return 0
        print(f"Hermes was not listening after {ONLY_WAIT_S:.0f}s", file=sys.stderr)
        return 1
    try:
        result = reload(_environment(), wait_s=WAIT_S if WAIT_FLAG in argv else 0.0)
    except Exception as exc:  # noqa: BLE001 - the caller records it, whatever it was
        print(f"MCP reload failed: {type(exc).__name__}: {exc}"[:400], file=sys.stderr)
        return 1
    print(json.dumps(result)[:400])
    return 0 if result.get("status") == "reloaded" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
