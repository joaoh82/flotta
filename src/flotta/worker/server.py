"""The thin, Flotta-owned MCP server the worker exposes.

Per SEAM_NOTES Q2 / decision D7, the worker's MCP surface is **not**
`hermes mcp serve` (a stdio messaging bridge). It is this small streamable-http
server wrapping a headless `AIAgent`. It exposes two tools:

- ``health()``   — provider-free liveness; the hermetic smoke-test probe.
- ``run_task()`` — boots headless Hermes per SEAM_NOTES Q1 and runs one task
  under a hard timeout, returning a structured result.

Import discipline: `mcp`, `starlette`, `uvicorn`, and `run_agent.AIAgent` are
present only inside the Modal image, so they are imported lazily. The pure
cores below (`authorize`, `health_payload`, `_run_task_core`) use the standard
library only and carry the unit tests.
"""

from __future__ import annotations

import secrets
import threading
import uuid
from collections.abc import Callable
from typing import Any

from .config import WorkerConfig

# Type alias: given a config, produce an object with `.run_conversation(task, task_id=...)`.
AgentFactory = Callable[[WorkerConfig], Any]


def authorize(expected_token: str | None, auth_header: str | None) -> bool:
    """Constant-time bearer-token check.

    When no token is configured the server is open (dev / localhost smoke).
    Otherwise the request must carry ``Authorization: Bearer <token>``.
    """
    if not expected_token:
        return True
    if not auth_header:
        return False
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    return secrets.compare_digest(token, expected_token)


def health_payload(cfg: WorkerConfig) -> dict[str, Any]:
    """Provider-free liveness payload — proves the MCP endpoint answers."""
    return {
        "status": "ok",
        "service": "flotta-worker",
        "oneshot": cfg.oneshot,
        "has_provider": cfg.provider_missing() is None,
        "timeout_s": cfg.timeout_s,
    }


def _default_agent_factory(cfg: WorkerConfig) -> Any:
    """Boot a headless, task-scoped ``AIAgent`` (SEAM_NOTES Q1 recipe)."""
    from run_agent import AIAgent  # lazy: only present in the Modal image

    return AIAgent(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        model=cfg.model,
        enabled_toolsets=cfg.toolsets,  # None => Hermes default toolset
        skip_context_files=True,  # don't ingest host SOUL.md / AGENTS.md
        skip_memory=True,  # no persistent memory in a disposable worker
        clarify_callback=None,  # unattended: clarify errors instead of blocking
        save_trajectories=False,
        quiet_mode=True,
    )


def _run_task_core(
    cfg: WorkerConfig,
    task: str,
    timeout_s: int,
    *,
    agent_factory: AgentFactory = _default_agent_factory,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Run one task under a hard timeout, returning a structured result.

    Never raises: a missing provider, an agent exception, and a timeout all
    resolve to ``completed: False`` with an ``error``/``timed_out`` marker so
    the MCP caller always gets a well-formed reply.
    """
    task_id = task_id or f"flotta-{uuid.uuid4().hex[:12]}"

    reason = cfg.provider_missing()
    if reason:
        return {"completed": False, "timed_out": False, "task_id": task_id, "error": reason}

    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            agent = agent_factory(cfg)
            box["result"] = agent.run_conversation(task, task_id=task_id)
        except Exception as exc:  # surfaced to the caller, not swallowed
            box["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=_run, name=f"flotta-task-{task_id}", daemon=True)
    worker.start()
    worker.join(timeout_s)

    if worker.is_alive():
        return {
            "completed": False,
            "timed_out": True,
            "task_id": task_id,
            "error": f"task exceeded hard timeout of {timeout_s}s",
        }
    if "error" in box:
        return {
            "completed": False,
            "timed_out": False,
            "task_id": task_id,
            "error": box["error"],
        }

    result = box.get("result") or {}
    return {
        "completed": bool(result.get("completed")),
        "timed_out": False,
        "task_id": task_id,
        "final_response": result.get("final_response"),
        "api_calls": result.get("api_calls"),
    }


def _new_mcp_server(cfg: WorkerConfig) -> Any:
    """Construct the MCP server object, across both major SDK versions.

    The SDK renamed its high-level server between 1.x and 2.x — `FastMCP`
    became `MCPServer` — and moved `host` from the constructor onto
    `streamable_http_app()`. Both are supported here because the Hermes pin
    decides which SDK lands in the image: v1 for older Hermes, v2 for
    `v2026.7.30`+. Without this, moving the pin forward breaks the worker with
    a bare `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`, which
    is how this was found.

    `.tool()` and `.streamable_http_app()` keep their names in both, so only
    construction differs.
    """
    try:
        from mcp.server import MCPServer  # mcp >= 2

        return MCPServer("flotta-worker")
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP("flotta-worker", host=cfg.host, port=cfg.port)


def build_server(cfg: WorkerConfig) -> Any:
    """Construct the MCP server with the two Flotta tools registered."""
    mcp = _new_mcp_server(cfg)

    @mcp.tool()
    def health() -> dict[str, Any]:
        """Liveness check — returns worker status without touching the model."""
        return health_payload(cfg)

    @mcp.tool()
    def run_task(task: str, timeout_s: int | None = None) -> dict[str, Any]:
        """Run a single task on the headless Hermes agent and return the result."""
        return _run_task_core(cfg, task, timeout_s or cfg.timeout_s)

    return mcp


def _bearer_auth_asgi(app: Any, token: str) -> Any:
    """Pure-ASGI bearer-auth wrapper.

    Written as raw ASGI (not Starlette's BaseHTTPMiddleware) on purpose:
    BaseHTTPMiddleware buffers responses and breaks the long-lived SSE stream
    that MCP streamable-http uses. This wrapper only inspects the request
    headers and short-circuits unauthorized HTTP requests with a 401; every
    other scope (``lifespan``, authorized traffic) passes straight through, so
    FastMCP's session manager and streaming are untouched.
    """

    async def asgi(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            raw = dict(scope.get("headers") or {}).get(b"authorization")
            header = raw.decode("latin-1") if raw else None
            if not authorize(token, header):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await app(scope, receive, send)

    return asgi


def build_asgi_app(cfg: WorkerConfig) -> Any:
    """Return the streamable-http ASGI app, gated by bearer auth when configured."""
    server = build_server(cfg)
    try:
        # mcp >= 2 takes the bind host here (it drives DNS-rebinding checks);
        # uvicorn still does the actual binding in `serve`.
        app = server.streamable_http_app(host=cfg.host)
    except TypeError:
        app = server.streamable_http_app()  # mcp 1.x
    if cfg.auth_token:
        app = _bearer_auth_asgi(app, cfg.auth_token)
    return app


def serve(cfg: WorkerConfig) -> None:
    """Serve the worker's MCP endpoint (blocks until the process is killed)."""
    import uvicorn  # lazy: image-only

    uvicorn.run(build_asgi_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")
