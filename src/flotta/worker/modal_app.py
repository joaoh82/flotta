"""Modal image definition + hermetic smoke test for the Flotta worker (M2).

Run the smoke test with:

    modal run src/flotta/worker/modal_app.py

It builds the image (Hermes pinned + MCP SDK), boots the worker's MCP endpoint
inside a Modal container, connects an MCP client, lists tools, calls the
provider-free ``health`` tool, and confirms bearer auth rejects a bad token.
No LLM provider or API key is needed — this is the "MCP endpoint answers a
trivial task" acceptance check (M2.4), and it runs for $0.

The full ``run_task`` LLM round-trip uses the same server and lands in M3's
end-to-end lifecycle script, where a real provider secret is attached.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import sys

import modal

# Make the local `flotta` package importable when this file is run via
# `modal run src/flotta/worker/modal_app.py` (src/ is not otherwise on
# sys.path). This must be defensive: inside the container Modal copies this
# file to /root/modal_app.py, where those parent directories do not exist —
# there the package arrives via add_local_python_source instead, so skip it.
# Must run *before* the `flotta.*` import below. (Same block in provision.py;
# it cannot be factored out because factoring it out needs the import it fixes.)
_HERE = pathlib.Path(__file__).resolve()
_SRC = _HERE.parents[2] if len(_HERE.parents) > 2 else None
if _SRC is not None and (_SRC / "flotta" / "worker").is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from flotta.worker.image import (  # noqa: E402  (needs the sys.path prime above)
    HERMES_REF,
    worker_image,
)

app = modal.App("flotta-worker")

# Port used only inside the smoke container (localhost, ephemeral).
_SMOKE_PORT = 8765
_SMOKE_TOKEN = "flotta-smoke-token"


def _serve_in_thread(cfg):
    """Start the worker's ASGI app on a background uvicorn server; wait for ready."""
    import threading
    import time

    import uvicorn

    from flotta.worker.server import build_asgi_app

    server = uvicorn.Server(
        uvicorn.Config(build_asgi_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")
    )
    threading.Thread(target=server.run, name="flotta-smoke-uvicorn", daemon=True).start()

    for _ in range(200):  # up to ~10s
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("worker MCP server did not start within timeout")


def _result_to_obj(result) -> dict:
    """Normalize an MCP CallToolResult into a plain dict."""
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    text = "".join(getattr(part, "text", "") for part in (getattr(result, "content", None) or []))
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {"text": text}


@contextlib.asynccontextmanager
async def _mcp_streams(url: str, headers: dict):
    """Open MCP streamable-http transport, across both SDK generations.

    The two differ in more than a name. In 1.x the function took `headers=`
    and yielded three values (read, write, get_session_id); in 2.x it takes a
    pre-configured `httpx2.AsyncClient` — headers go on the client — and
    yields two. Both shapes are normalised to `(read, write)` here so the
    probe below reads the same either way.
    """
    try:
        import httpx2
        from mcp.client.streamable_http import streamable_http_client  # mcp >= 2

        async with (
            httpx2.AsyncClient(headers=headers) as http_client,
            streamable_http_client(url, http_client=http_client) as streams,
        ):
            yield streams[0], streams[1]
    except ImportError:  # mcp 1.x
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            yield read, write


async def _probe(cfg) -> dict:
    """Connect over MCP: list tools, call health, and verify auth is enforced."""
    from mcp import ClientSession

    url = cfg.mcp_url
    good_headers = {"Authorization": f"Bearer {cfg.auth_token}"}

    async with (
        _mcp_streams(url, good_headers) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        tools = sorted(tool.name for tool in listed.tools)
        health = _result_to_obj(await session.call_tool("health", {}))

    # A wrong token must be rejected (initialize should fail).
    auth_enforced = False
    try:
        async with (
            _mcp_streams(url, {"Authorization": "Bearer WRONG"}) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
    except Exception:
        auth_enforced = True

    ok = (
        {"health", "run_task"}.issubset(set(tools))
        and health.get("status") == "ok"
        and auth_enforced
    )
    return {"ok": ok, "tools": tools, "health": health, "auth_enforced": auth_enforced}


def _installed_hermes() -> dict:
    """What Hermes the container is *actually* running.

    The pin says what we asked for; this says what arrived. Those can differ —
    a stale image layer, a moved tag, a hand-edited ref — and without checking,
    a smoke test that passes proves the image works, not that it works on the
    version you think. `git describe` reads the checkout the editable install
    points at, so it reflects the real source tree.
    """
    import importlib.metadata as md
    import subprocess

    from flotta.worker.image import HERMES_SRC

    info: dict[str, str] = {}
    try:
        info["version"] = md.version("hermes-agent")
    except Exception as exc:  # never fail the smoke test over reporting
        info["version"] = f"unknown ({type(exc).__name__})"
    try:
        info["ref"] = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            cwd=HERMES_SRC,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
    except Exception as exc:
        info["ref"] = f"unknown ({type(exc).__name__})"
    return info


@app.function(image=worker_image, timeout=300)
def smoke_check() -> dict:
    """In-container: boot the MCP endpoint and confirm it answers."""
    import asyncio

    from flotta.worker.config import WorkerConfig

    cfg = WorkerConfig.from_env(
        {
            "FLOTTA_HOST": "127.0.0.1",
            "FLOTTA_PORT": str(_SMOKE_PORT),
            "FLOTTA_AUTH_TOKEN": _SMOKE_TOKEN,
            "FLOTTA_TIMEOUT_S": "120",
            "FLOTTA_ONESHOT": "0",
        }
    )
    _serve_in_thread(cfg)
    result = asyncio.run(_probe(cfg))
    result["hermes"] = _installed_hermes()
    return result


@app.local_entrypoint()
def smoke() -> None:
    result = smoke_check.remote()
    print(json.dumps(result, indent=2))
    if not result.get("ok"):
        raise SystemExit("SMOKE FAILED — MCP endpoint did not answer as expected")
    hermes = result.get("hermes") or {}
    expected = HERMES_REF
    actual = hermes.get("ref", "?")
    print(f"hermes in container: {hermes.get('version', '?')} @ {actual}  (pinned: {expected})")
    if actual != expected and not expected.startswith(actual.rstrip("-dirty")):
        # A warning, not a failure: a SHA pin or a branch will not equal the
        # `git describe` output, and that is legitimate.
        print(f"NOTE: container ref {actual!r} does not match the pin {expected!r}")
    print(f"SMOKE OK — MCP endpoint answered (tools={result.get('tools')})")
