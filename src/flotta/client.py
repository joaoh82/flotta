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
import json
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


@contextlib.contextmanager
def tunnel(
    endpoint: str,
    *,
    remote_port: int = DEFAULT_REMOTE_PORT,
    local_port: int | None = None,
    ready_timeout_s: float = 20.0,
) -> Iterator[str]:
    """Open a WireGuard tunnel to a box's loopback port; yield the local base URL.

    Always closed on exit, including on error: a leaked `flyctl proxy` holds a
    port and keeps a WireGuard session alive long after the command that made
    it is gone, and the next run then fails with a confusing bind error.
    """
    app, machine_id = parse_endpoint(endpoint)
    port = local_port or free_local_port()

    proc = subprocess.Popen(
        [
            "flyctl",
            "proxy",
            f"{port}:{remote_port}",
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
    try:
        deadline = time.monotonic() + ready_timeout_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stderr = (proc.stderr.read() if proc.stderr else "") or ""
                raise ChatError(f"flyctl proxy exited immediately: {stderr.strip()[:300]}")
            with (
                contextlib.suppress(OSError),
                socket.create_connection(("127.0.0.1", port), timeout=0.5),
            ):
                break
            time.sleep(0.2)
        else:
            raise ChatError(f"tunnel to {endpoint} never came up within {ready_timeout_s}s")

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


def _post(base_url: str, path: str, payload: dict, *, timeout_s: float) -> dict:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        body = response.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ChatError(f"{path} did not return JSON: {body[:200]!r}") from exc
