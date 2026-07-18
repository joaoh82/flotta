#!/usr/bin/env python
"""End-to-end lifecycle check against **real Modal** — no agent involved (M3.5).

This is the M3 acceptance criterion executed as a script: spawn a worker, watch
it finish, tear it down, and assert the fleet-state store says the right thing
at every step. Where `src/flotta/test_provision.py` fakes Modal out, this
exercises the real adapters — `Function.spawn`, `FunctionCall.get`,
`FunctionCall.cancel` — so the seam between the local store and the cloud is
actually proven, not mocked.

    just deploy       # publish the flotta-provision app first
    just e2e          # then run this

By default the worker runs in **dry-run** mode: the container boots the real
image and returns a real result, but skips the LLM call. That keeps the whole
lifecycle provable for ~a cent and with no provider key, the same reasoning as
M2's provider-free `health` probe (D9). Pass `--live` to run a genuine one-line
Hermes task instead, which needs FLOTTA_MODEL / FLOTTA_MODEL_BASE_URL /
FLOTTA_API_KEY in the local environment (they are forwarded as a Modal Secret).

Exit code is 0 only if every assertion held.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from flotta.provision import (  # noqa: E402  (needs the sys.path prime above)
    PROVIDER_KEYS,
    endpoint_for,
    function_call_id,
    spawn_worker,
    teardown,
    watch_worker,
)
from flotta.store import FleetStore  # noqa: E402

LIVE_TASK = "Reply with exactly the word FLOTTA_OK and nothing else."
DRY_TASK = "e2e lifecycle probe (dry run)"

_checks = 0
_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion. Collects failures instead of stopping at the first."""
    global _checks
    _checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        suffix = f" — {detail}" if detail else ""
        print(f"  FAIL {label}{suffix}")
        _failures.append(f"{label}{suffix}")


def event_types(store: FleetStore, worker_id: str) -> list[str]:
    return [e.type for e in store.get_events(worker_id)]


def run(store_path: pathlib.Path, *, live: bool, timeout_s: int, watch_timeout_s: int) -> int:
    task = LIVE_TASK if live else DRY_TASK
    mode = "LIVE (real LLM call)" if live else "dry-run (no LLM, no provider needed)"
    print(f"\nFlotta M3 end-to-end lifecycle\n  store: {store_path}\n  mode:  {mode}\n")

    if live:
        import os

        missing = [k for k in PROVIDER_KEYS if not os.environ.get(k)]
        if missing:
            print(f"ERROR: --live needs these in the environment: {', '.join(missing)}")
            return 2

    with FleetStore(store_path) as store:
        # -- 1. spawn ------------------------------------------------------
        print("[1/4] spawn_worker")
        started = time.monotonic()
        result = spawn_worker(task, store=store, timeout_s=timeout_s, dry_run=not live)
        worker_id = result["worker_id"]
        call_id = function_call_id(result["endpoint"])
        print(f"       worker_id={worker_id}  call_id={call_id}")

        worker = store.get_worker(worker_id)
        check("worker row exists", worker is not None)
        check("status is running", worker.status == "running", f"got {worker.status!r}")
        check("task recorded verbatim", worker.task == task)
        check("endpoint is the modal call handle", worker.endpoint == endpoint_for(call_id))
        check("spawned_at stamped", bool(worker.spawned_at))
        check("not finished yet", worker.finished_at is None)
        check(
            "events are [spawned, running]",
            event_types(store, worker_id) == ["spawned", "running"],
            str(event_types(store, worker_id)),
        )
        check(
            "worker appears in the running list",
            worker_id in {w.id for w in store.list_workers(status="running")},
        )

        # -- 2. watch ------------------------------------------------------
        print(f"\n[2/4] watch_worker (up to {watch_timeout_s}s)")
        outcome = watch_worker(worker_id, store=store, timeout_s=watch_timeout_s)
        elapsed = time.monotonic() - started
        print(f"       status={outcome['status']}  after {elapsed:.1f}s")
        if outcome.get("result"):
            print(f"       result={outcome['result']}")

        worker = store.get_worker(worker_id)
        check("status is done", worker.status == "done", f"got {worker.status!r}")
        check("finished_at stamped", worker.finished_at is not None)
        check(
            "events are [spawned, running, completed]",
            event_types(store, worker_id) == ["spawned", "running", "completed"],
            str(event_types(store, worker_id)),
        )

        completed = store.get_events(worker_id)[-1]
        check("completion payload carries a response", bool(completed.payload.get("final_response")))
        if live:
            response = str(completed.payload.get("final_response") or "")
            check("live worker answered FLOTTA_OK", "FLOTTA_OK" in response, response[:120])
            check("live worker made api calls", (completed.payload.get("api_calls") or 0) > 0)
        else:
            check("dry run flagged as such", completed.payload.get("dry_run") is True)

        # -- 3. teardown ---------------------------------------------------
        print("\n[3/4] teardown")
        torn = teardown(worker_id, store=store, reason="e2e")
        worker = store.get_worker(worker_id)
        check("status is torn_down", worker.status == "torn_down", f"got {worker.status!r}")
        check("row is closed", worker.finished_at is not None)
        check(
            "events end with torn_down",
            event_types(store, worker_id) == ["spawned", "running", "completed", "torn_down"],
            str(event_types(store, worker_id)),
        )
        check("teardown reported previous status", torn.get("already_torn_down") is False)

        # -- 4. teardown again (idempotence) -------------------------------
        print("\n[4/4] teardown again (idempotence)")
        again = teardown(worker_id, store=store, reason="e2e-repeat")
        check("second teardown is a no-op", again.get("already_torn_down") is True)
        check(
            "no duplicate torn_down event",
            event_types(store, worker_id).count("torn_down") == 1,
            str(event_types(store, worker_id)),
        )
        check("status unchanged", store.get_worker(worker_id).status == "torn_down")

    print(f"\n{'-' * 60}")
    if _failures:
        print(f"E2E FAILED — {len(_failures)}/{_checks} checks failed:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print(f"E2E OK — {_checks}/{_checks} checks passed against real Modal.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store",
        type=pathlib.Path,
        default=pathlib.Path("e2e_fleet.db"),
        help="fleet-state store file to use (default: ./e2e_fleet.db, gitignored)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="run a real Hermes task instead of a dry run (needs provider env vars)",
    )
    parser.add_argument(
        "--timeout-s", type=int, default=300, help="per-task timeout inside the worker"
    )
    parser.add_argument(
        "--watch-timeout-s",
        type=int,
        default=600,
        help="how long to wait for the container (first run includes the image build)",
    )
    args = parser.parse_args()
    return run(
        args.store,
        live=args.live,
        timeout_s=args.timeout_s,
        watch_timeout_s=args.watch_timeout_s,
    )


if __name__ == "__main__":
    raise SystemExit(main())
