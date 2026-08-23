#!/usr/bin/env python
"""End-to-end lifecycle check against **real Modal** — no agent involved (M3.5).

This is the M3 acceptance criterion executed as a script, updated for the M0
split: spawn a **box** with one **task** on it, watch the task finish, stop and
start the box, tear it down, and assert the fleet-state store says the right
thing at every step. Where `src/flotta/test_provision.py` fakes Modal out, this
exercises the real adapters — `Function.spawn`, `FunctionCall.get`,
`FunctionCall.cancel` — so the seam between the local store and the cloud is
actually proven, not mocked.

    just deploy       # publish the flotta-provision app first
    just e2e          # then run this

By default the task runs in **dry-run** mode: the container boots the real
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
    spawn_box,
    start_box,
    stop_box,
    teardown_box,
    watch_task,
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


def event_types(store: FleetStore, task_id: str) -> list[str]:
    return [e.type for e in store.get_events("task", task_id)]


def box_events(store: FleetStore, box_id: str) -> list[str]:
    return [e.type for e in store.get_events("box", box_id)]


def run(store_path: pathlib.Path, *, live: bool, timeout_s: int, watch_timeout_s: int) -> int:
    task = LIVE_TASK if live else DRY_TASK
    mode = "LIVE (real LLM call)" if live else "dry-run (no LLM, no provider needed)"
    print(f"\nFlotta end-to-end lifecycle\n  store: {store_path}\n  mode:  {mode}\n")

    if live:
        import os

        missing = [k for k in PROVIDER_KEYS if not os.environ.get(k)]
        if missing:
            print(f"ERROR: --live needs these in the environment: {', '.join(missing)}")
            return 2

    with FleetStore(store_path) as store:
        # -- 1. spawn ------------------------------------------------------
        print("[1/5] spawn_box")
        started = time.monotonic()
        result = spawn_box(task, store=store, timeout_s=timeout_s, dry_run=not live)
        box_id, task_id = result["box_id"], result["task_id"]
        call_id = function_call_id(result["endpoint"])
        print(f"       box_id={box_id}  task_id={task_id}  call_id={call_id}")

        box = store.get_box(box_id)
        work = store.get_task(task_id)
        check("box row exists", box is not None)
        check("task row exists", work is not None)
        check("box status is running", box.status == "running", f"got {box.status!r}")
        check("task status is running", work.status == "running", f"got {work.status!r}")
        check("task belongs to the box", work.box_id == box_id)
        check("prompt recorded verbatim", work.prompt == task)
        # The endpoint is on the box: it addresses a machine, not a piece of work.
        check("endpoint is the modal call handle", box.endpoint == endpoint_for(call_id))
        check("created_at stamped", bool(box.created_at))
        check("box not destroyed yet", box.destroyed_at is None)
        check("task not finished yet", work.finished_at is None)
        check(
            "task events are [spawned]",
            event_types(store, task_id) == ["spawned"],
            str(event_types(store, task_id)),
        )
        check(
            "box events are [running]",
            box_events(store, box_id) == ["running"],
            str(box_events(store, box_id)),
        )
        check(
            "box appears in the running list",
            box_id in {b.id for b in store.list_boxes(status="running")},
        )

        # -- 2. watch ------------------------------------------------------
        print(f"\n[2/5] watch_task (up to {watch_timeout_s}s)")
        outcome = watch_task(task_id, store=store, timeout_s=watch_timeout_s)
        elapsed = time.monotonic() - started
        print(f"       status={outcome['status']}  after {elapsed:.1f}s")
        if outcome.get("result"):
            print(f"       result={outcome['result']}")

        work = store.get_task(task_id)
        check("task status is done", work.status == "done", f"got {work.status!r}")
        check("finished_at stamped", work.finished_at is not None)
        check(
            "task events are [spawned, completed]",
            event_types(store, task_id) == ["spawned", "completed"],
            str(event_types(store, task_id)),
        )
        # The box outlives the work — that is the whole pivot, asserted.
        check("box is still running", store.get_box(box_id).status == "running")
        check(
            "box was not destroyed by the task finishing",
            store.get_box(box_id).destroyed_at is None,
        )

        completed = store.get_events("task", task_id)[-1]
        check(
            "completion payload carries a response", bool(completed.payload.get("final_response"))
        )
        check("result persisted on the task row", bool(work.result))
        if live:
            response = str(completed.payload.get("final_response") or "")
            check("live task answered FLOTTA_OK", "FLOTTA_OK" in response, response[:120])
            check("live task made api calls", (completed.payload.get("api_calls") or 0) > 0)
        else:
            check("dry run flagged as such", completed.payload.get("dry_run") is True)

        # -- 3. stop / start -----------------------------------------------
        # Store-side only until M1 lands a backend that can actually suspend a
        # machine; what is proven here is that the state machine round-trips.
        print("\n[3/5] stop_box / start_box")
        stopped = stop_box(box_id, store=store, reason="e2e")
        check("box status is stopped", store.get_box(box_id).status == "stopped")
        check("stopping is not finishing", store.get_box(box_id).destroyed_at is None)
        check("stop was not a no-op", stopped.get("already_stopped") is False)

        start_box(box_id, store=store, reason="e2e")
        check("box status is running again", store.get_box(box_id).status == "running")
        check(
            "box events record the cycle",
            box_events(store, box_id) == ["running", "stopped", "running"],
            str(box_events(store, box_id)),
        )

        # -- 4. teardown ---------------------------------------------------
        print("\n[4/5] teardown_box")
        torn = teardown_box(box_id, store=store, reason="e2e")
        box = store.get_box(box_id)
        check("box status is torn_down", box.status == "torn_down", f"got {box.status!r}")
        check("box row is closed", box.destroyed_at is not None)
        check(
            "box events end with torn_down",
            box_events(store, box_id) == ["running", "stopped", "running", "torn_down"],
            str(box_events(store, box_id)),
        )
        # The finished task keeps its verdict — teardown must not overwrite it.
        check("completed task keeps its verdict", store.get_task(task_id).status == "done")
        check("no live task was left to fail", torn.get("failed_tasks") == [])
        check("teardown reported previous status", torn.get("already_torn_down") is False)
        # The regression guard for the bug M5 surfaced: teardown records a
        # cancel failure instead of raising, so a rejected cancel closed the
        # row while the container kept running and billing. Asserting only the
        # store state cannot see that — the cancel outcome has to be checked.
        check(
            "modal cancel did not error",
            torn.get("cancel_error") is None,
            f"cancel_error={torn.get('cancel_error')!r}",
        )

        # -- 5. teardown again (idempotence) -------------------------------
        print("\n[5/5] teardown_box again (idempotence)")
        again = teardown_box(box_id, store=store, reason="e2e-repeat")
        check("second teardown is a no-op", again.get("already_torn_down") is True)
        check(
            "no duplicate torn_down event",
            box_events(store, box_id).count("torn_down") == 1,
            str(box_events(store, box_id)),
        )
        check("status unchanged", store.get_box(box_id).status == "torn_down")

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
