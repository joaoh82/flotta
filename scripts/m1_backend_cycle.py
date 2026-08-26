#!/usr/bin/env python
"""M1 acceptance: drive a real box through the Backend protocol.

    just fly-cycle

The hermetic tests pin the *contract* — routing, the pause policy, the
asymmetry. This drives the actual `flyctl` adapter against the actual machine,
because an abstraction that has never touched its substrate is a guess.

It also re-measures suspend vs stop, since the numbers are the reason
`prefer_suspend` is the default and they are worth re-checking whenever the
adapter changes.
"""

from __future__ import annotations

import pathlib
import sys
import time

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from flotta.backend import BoxSpec, UnknownBackendError, backend_for, pause  # noqa: E402
from flotta.backends.fly_backend import endpoint_for  # noqa: E402
from flotta.fly import FlyConfig  # noqa: E402

_checks = 0
_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        suffix = f" — {detail}" if detail else ""
        print(f"  FAIL {label}{suffix}")
        _failures.append(f"{label}{suffix}")


def main() -> int:
    cfg = FlyConfig.from_env()
    backend = backend_for("fly://")
    print(f"\nFlotta M1 — Backend protocol against real infra\n{cfg.describe()}\n")

    # The machine `just fly-up` built. `create` is idempotent over it.
    handle = backend.create(BoxSpec(name=cfg.app, volume_gb=cfg.volume_gb))
    endpoint = handle.endpoint
    print(f"[1/5] resolved box\n       {endpoint}")
    check("endpoint encodes app and machine", endpoint == endpoint_for(cfg.app, handle.id))
    check("routing picks FlyBackend", type(backend_for(endpoint)).__name__ == "FlyBackend")

    print("\n[2/5] start")
    backend.start(endpoint)
    check("state is started", backend.state(endpoint) == "started", backend.state(endpoint))

    print("\n[3/5] exec")
    result = backend.exec(endpoint, "echo protocol-ok; cat /proc/uptime")
    check("exec succeeded", result.ok, f"exit={result.exit_code} {result.stderr[:120]}")
    check("exec returned stdout", "protocol-ok" in result.stdout, result.stdout[:120])
    uptime_before = _uptime(result.stdout)

    print("\n[4/5] suspend -> start (memory should survive)")
    t0 = time.monotonic()
    method = pause(backend, endpoint)
    check("pause chose suspend", method == "suspend", f"chose {method}")
    check("state is suspended", backend.state(endpoint) == "suspended", backend.state(endpoint))
    backend.start(endpoint)
    suspend_resume = time.monotonic() - t0
    after = _uptime(backend.exec(endpoint, "cat /proc/uptime").stdout)
    check(
        "the VM kept its memory across suspend",
        uptime_before is not None and after is not None and after > uptime_before,
        f"uptime {uptime_before} -> {after}",
    )
    print(f"       uptime {_fmt(uptime_before)} -> {_fmt(after)}   cycle {suspend_resume:.2f}s")

    print("\n[5/5] stop -> start (memory should NOT survive)")
    before_cold = _uptime(backend.exec(endpoint, "cat /proc/uptime").stdout)
    t0 = time.monotonic()
    method = pause(backend, endpoint, prefer_suspend=False)
    check("pause chose stop when told to", method == "stop", f"chose {method}")
    check("state is stopped", backend.state(endpoint) == "stopped", backend.state(endpoint))
    backend.start(endpoint)
    stop_resume = time.monotonic() - t0
    after_cold = _uptime(backend.exec(endpoint, "cat /proc/uptime").stdout)
    check(
        "a cold stop really is cold",
        before_cold is not None and after_cold is not None and after_cold < before_cold,
        f"uptime {before_cold} -> {after_cold}",
    )
    print(f"       uptime {_fmt(before_cold)} -> {_fmt(after_cold)}   cycle {stop_resume:.2f}s")

    # This is where the Modal backend used to be checked for refusing suspend
    # and stop — the asymmetry M1's protocol existed to make explicit. Modal
    # left with the shard tier, so the only thing left to assert is that an
    # endpoint naming no substrate says so rather than resolving to something.
    print("\n[routing] an unknown scheme is refused, not guessed at")
    try:
        backend_for("modal://flotta-provision/run_worker/fc-x")
        check("a cut substrate no longer resolves", False, "it resolved")
    except UnknownBackendError:
        check("a cut substrate no longer resolves", True)

    print("\nleaving the box stopped (disk kept, no CPU)")
    pause(backend, endpoint, prefer_suspend=False)

    print(f"\n{'-' * 60}")
    if _failures:
        print(f"M1 FAILED — {len(_failures)}/{_checks} checks failed:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print(f"M1 OK — {_checks}/{_checks} checks passed against real infra.")
    print(f"suspend cycle {suspend_resume:.2f}s · cold cycle {stop_resume:.2f}s")
    return 0


def _fmt(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1f}s"


def _uptime(stdout: str) -> float | None:
    for line in stdout.splitlines():
        line = line.strip()
        if line and line[0].isdigit():
            try:
                return float(line.split()[0])
            except ValueError:
                continue
    return None


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
