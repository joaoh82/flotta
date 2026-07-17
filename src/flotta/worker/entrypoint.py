"""Container entrypoint — PID 1 inside the Modal worker.

Flow (see the M2 plan / SEAM_NOTES design implications):

    parse env  ->  set HERMES_HOME  ->  arm hard-timeout watchdog  ->
        one-shot:  run FLOTTA_TASK once, print result, exit
        serve:     serve the MCP endpoint until torn down or timed out

The watchdog is the "stuck workers self-destruct" guarantee (M2.3): a daemon
thread that hard-exits the process once `FLOTTA_TIMEOUT_S` elapses, regardless
of what the agent or the server is doing.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable

from .config import WorkerConfig

# Exit code used when the hard timeout fires (distinct from task success/failure).
TIMEOUT_EXIT_CODE = 75


def _hard_exit() -> None:  # pragma: no cover - real self-destruct
    os._exit(TIMEOUT_EXIT_CODE)


def arm_watchdog(
    timeout_s: float,
    *,
    on_timeout: Callable[[], None] | None = None,
) -> threading.Thread:
    """Start a daemon thread that fires `on_timeout` after `timeout_s` seconds."""
    fire = on_timeout or _hard_exit

    def _run() -> None:
        time.sleep(timeout_s)
        print(
            f"[flotta] hard timeout of {timeout_s}s reached; self-destructing",
            file=sys.stderr,
            flush=True,
        )
        fire()

    watchdog = threading.Thread(target=_run, name="flotta-watchdog", daemon=True)
    watchdog.start()
    return watchdog


def main(env: dict[str, str] | None = None) -> int:
    cfg = WorkerConfig.from_env(env)

    os.environ["HERMES_HOME"] = cfg.hermes_home
    os.makedirs(cfg.hermes_home, exist_ok=True)

    arm_watchdog(cfg.timeout_s)

    if cfg.oneshot:
        if not cfg.task:
            print("[flotta] one-shot mode but FLOTTA_TASK is empty", file=sys.stderr, flush=True)
            return 2
        from .server import _run_task_core

        result = _run_task_core(cfg, cfg.task, cfg.timeout_s)
        print(json.dumps(result))
        return 0 if result.get("completed") else 1

    from .server import serve

    print(f"[flotta] serving MCP at {cfg.mcp_url}", file=sys.stderr, flush=True)
    serve(cfg)  # blocks until killed / watchdog fires
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
