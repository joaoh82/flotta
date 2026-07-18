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

import json
import pathlib
import sys

import modal

# Make the local `flotta` package importable when this file is run via
# `modal run src/flotta/worker/modal_app.py` (src/ is not otherwise on
# sys.path). This must be defensive: inside the container Modal copies this
# file to /root/modal_app.py, where those parent directories do not exist —
# there the package arrives via add_local_python_source instead, so skip it.
_HERE = pathlib.Path(__file__).resolve()
_SRC = _HERE.parents[2] if len(_HERE.parents) > 2 else None
if _SRC is not None and (_SRC / "flotta" / "worker").is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Hermes Agent pin — matches the vendored clone validated in SEAM_NOTES
# (commit 594308d4bbe9). Bump here to move to a newer Hermes.
HERMES_REF = "594308d4bbe95548c9fe418bb10c449099426f93"
HERMES_PKG = f"hermes-agent[mcp] @ git+https://github.com/NousResearch/Hermes-Agent@{HERMES_REF}"

# The `[mcp]` extra pulls in the MCP SDK (mcp==1.26.0) + starlette; uvicorn
# serves the streamable-http app. Hermes's own deps are exact-pinned upstream.
worker_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git")
    .pip_install(HERMES_PKG, "uvicorn==0.34.0")
    # Ship the local Flotta package into the container, importable as `flotta`.
    # (sys.path was primed above so this module name resolves at build time.)
    .add_local_python_source("flotta")
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


async def _probe(cfg) -> dict:
    """Connect over MCP: list tools, call health, and verify auth is enforced."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = cfg.mcp_url
    good_headers = {"Authorization": f"Bearer {cfg.auth_token}"}

    async with (
        streamablehttp_client(url, headers=good_headers) as (read, write, _),
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
            streamablehttp_client(url, headers={"Authorization": "Bearer WRONG"}) as (
                read,
                write,
                _,
            ),
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
    return asyncio.run(_probe(cfg))


@app.local_entrypoint()
def smoke() -> None:
    result = smoke_check.remote()
    print(json.dumps(result, indent=2))
    if not result.get("ok"):
        raise SystemExit("SMOKE FAILED — MCP endpoint did not answer as expected")
    print(f"SMOKE OK — MCP endpoint answered (tools={result.get('tools')})")
