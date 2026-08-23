"""Run one Hermes turn **on a box**, with memory switched on (M2).

The difference from `flotta.worker.server._run_task_core` is two flags, and
they are the whole milestone:

    worker (Tier 3, disposable)      box (Tier 1, persistent)
    skip_memory=True                 skip_memory=False
    HERMES_HOME=/tmp/hermes          HERMES_HOME=/data/hermes

The pivot doc (§2.2) blames the ephemeral `HERMES_HOME` for deleting the value
proposition, and it is right — but it names only one of the two switches. A box
with a persistent volume and `skip_memory=True` would still write nothing worth
keeping, and would *look* like a passing M2. Both have to move together, which
is why they move together here.

`skip_context_files` stays **True** even on a box. That flag is about ingesting
`SOUL.md` / `AGENTS.md` from the working directory, which is a property of
where the process runs, not of whether it remembers. Turning it on would make a
box's behaviour depend on whatever files happen to sit next to it — the exact
kind of hidden input the workspace tier exists to keep out.

Run it on the box::

    python -m flotta.box.run --task "Remember: the passphrase is X."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import uuid
from typing import Any

DEFAULT_HERMES_HOME = "/data/hermes"

# Matches the worker's default so a box and a shard time out alike.
DEFAULT_TIMEOUT_S = 900
# Distinct from success/failure, same code the worker's watchdog uses.
TIMEOUT_EXIT_CODE = 75


def build_agent(
    *,
    base_url: str | None,
    api_key: str | None,
    model: str,
    toolsets: list[str] | None = None,
) -> Any:
    """Boot a headless Hermes that is allowed to remember.

    Same SEAM_NOTES Q1 recipe as the worker — gateway-free, single pinned
    provider, unattended `clarify_callback=None` — with memory enabled.
    """
    from run_agent import AIAgent  # lazy: only present inside the box image

    return AIAgent(
        base_url=base_url,
        api_key=api_key,
        model=model,
        enabled_toolsets=toolsets,  # None => Hermes default toolset
        skip_context_files=True,  # a box's behaviour must not depend on its cwd
        skip_memory=False,  # ← the box remembers. This is the milestone.
        clarify_callback=None,  # unattended: clarify errors instead of blocking
        save_trajectories=False,
        quiet_mode=True,
    )


def provider_from_env(env: dict[str, str] | None = None) -> tuple[str | None, str | None, str]:
    """(base_url, api_key, model) from the box's environment."""
    env = os.environ if env is None else env

    def clean(name: str) -> str | None:
        value = (env.get(name) or "").strip()
        return value or None

    base_url = clean("FLOTTA_MODEL_BASE_URL") or clean("OPENAI_BASE_URL")
    api_key = clean("FLOTTA_API_KEY") or clean("OPENAI_API_KEY")
    model = clean("FLOTTA_MODEL") or ""
    return base_url, api_key, model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Hermes turn on a Flotta box.")
    parser.add_argument("--task", required=True, help="The prompt for this turn")
    parser.add_argument(
        "--task-id",
        default=None,
        help="Stable id for this turn (defaults to a fresh one)",
    )
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=int(os.environ.get("FLOTTA_TIMEOUT_S") or DEFAULT_TIMEOUT_S),
        help="Hard deadline for the turn [$FLOTTA_TIMEOUT_S, default 900]",
    )
    args = parser.parse_args(argv)

    hermes_home = (os.environ.get("HERMES_HOME") or "").strip() or DEFAULT_HERMES_HOME
    os.environ["HERMES_HOME"] = hermes_home
    os.makedirs(hermes_home, exist_ok=True)

    base_url, api_key, model = provider_from_env()
    missing = [
        name
        for name, value in (
            ("FLOTTA_MODEL", model),
            ("FLOTTA_MODEL_BASE_URL", base_url),
            ("FLOTTA_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        print(
            json.dumps(
                {
                    "completed": False,
                    "error": "missing provider config: " + ", ".join(missing),
                    "hermes_home": hermes_home,
                }
            )
        )
        return 2

    task_id = args.task_id or f"box-{uuid.uuid4().hex[:12]}"

    # Bounded like the worker's `_run_task_core`, and for a sharper reason here:
    # this runs under `fly ssh console`, so when the SSH client gives up the
    # Python child is orphaned under `sleep infinity` — still holding the
    # provider key, still burning CPU on a box nobody is watching. A daemon
    # thread plus a join deadline means the process exits even if the turn
    # never returns.
    outcome: dict[str, Any] = {}

    def _run() -> None:
        try:
            agent = build_agent(base_url=base_url, api_key=api_key, model=model)
            outcome["result"] = agent.run_conversation(args.task, task_id=task_id)
        except Exception as exc:  # reported, never a traceback into the ssh session
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=_run, name=f"flotta-box-{task_id}", daemon=True)
    thread.start()
    thread.join(args.timeout_s)

    if thread.is_alive():
        print(
            json.dumps(
                {
                    "completed": False,
                    "timed_out": True,
                    "error": f"turn exceeded {args.timeout_s}s",
                    "task_id": task_id,
                    "hermes_home": hermes_home,
                }
            )
        )
        # os._exit, not sys.exit: the worker thread is still running inside
        # Hermes and a clean shutdown would block on it, which is the hang this
        # timeout exists to end.
        sys.stdout.flush()
        os._exit(TIMEOUT_EXIT_CODE)

    if "error" in outcome:
        print(
            json.dumps(
                {
                    "completed": False,
                    "error": outcome["error"],
                    "task_id": task_id,
                    "hermes_home": hermes_home,
                }
            )
        )
        return 1
    result = outcome["result"]

    print(
        json.dumps(
            {
                "completed": bool(result.get("completed")),
                "final_response": result.get("final_response"),
                "api_calls": result.get("api_calls"),
                "task_id": task_id,
                "hermes_home": hermes_home,
            }
        )
    )
    return 0 if result.get("completed") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
