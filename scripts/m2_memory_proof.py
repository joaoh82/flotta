#!/usr/bin/env python
"""M2 acceptance: does a box remember across a stop/start cycle?

This is the project's thesis reduced to one runnable assertion. v0.1 could not
have run it — there was no row that outlived a task and no disk that outlived a
container.

    just fly-up          # provision the box (once)
    just fly-proof       # this script

## What it proves

Two claims, in increasing order of interest:

1. **File-level, deterministic.** Every file under `HERMES_HOME` is SHA-256'd,
   the machine is stopped, started, and re-hashed. Nothing may disappear and
   nothing may change.
2. **Agent-level.** A box is told to commit a fact to memory, the machine is
   *killed*, restarted, and a **fresh process** is asked for the fact back. It
   answers correctly, out of a disk that outlived the container.

The second is the whole pitch, and it is a real gate here rather than a
courtesy check — the seed turn instructs the memory tool explicitly, so the
model is not being asked to guess that it should persist something.

## What `state.db` does and does not hold — a correction

SEAM_NOTES Q3 describes a rich `state.db` (sessions, messages, FTS). **A
headless `AIAgent.run_conversation` does not populate it.** Measured on a live
box: after a completed turn, `state.db` contains exactly one table,
`async_delegations`, with zero rows — the session schema is created by the
gateway/CLI path, not this one.

So conversation *history* is not what survives here, and this script does not
pretend otherwise. What survives is the surface that actually matters for the
pivot's claim — `memories/`, `skills/`, `SOUL.md`, and the rest of the store —
because "self-improving" is about what the agent learned, not about a chat
transcript. Session persistence arrives with M3, when the messaging gateway is
turned back on; that is the code path that writes those tables.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import subprocess
import sys
import time
import uuid

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from flotta.fly import DURABLE_PATHS, FlyConfig  # noqa: E402

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


def fly(cfg: FlyConfig, *args: str, capture: bool = True, timeout: int = 300) -> str:
    """Run a flyctl command against the configured app, never the ambient one."""
    cmd = ["flyctl", *args, "--app", cfg.app]
    result = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"{' '.join(cmd)} failed ({result.returncode}): {stderr}")
    return (result.stdout or "").strip()


def on_box(cfg: FlyConfig, script: str, *, timeout: int = 900) -> str:
    """Run a shell snippet inside the box over `fly ssh console`.

    `-C` takes one command, so anything with pipes or multiple statements has
    to go through `bash -lc`. Quoting is done with `shlex.quote` rather than by
    hand — the snippets below embed generated nonces, and a stray quote would
    fail in a way that looks like a durability failure.
    """
    return fly(
        cfg,
        "ssh",
        "console",
        "-C",
        f"bash -lc {shlex.quote(script)}",
        timeout=timeout,
    )


def machine_id(cfg: FlyConfig) -> str:
    raw = fly(cfg, "machines", "list", "--json")
    machines = json.loads(raw)
    if not machines:
        raise RuntimeError(f"no machines in app {cfg.app!r} — run `just fly-up` first")
    if len(machines) > 1:
        ids = ", ".join(m["id"] for m in machines)
        raise RuntimeError(f"expected exactly one box, found {len(machines)}: {ids}")
    return machines[0]["id"]


def machine_state(cfg: FlyConfig, mid: str) -> str:
    raw = fly(cfg, "machines", "list", "--json")
    for m in json.loads(raw):
        if m["id"] == mid:
            return m["state"]
    return "gone"


def wait_for_state(cfg: FlyConfig, mid: str, want: str, timeout_s: int = 120) -> str:
    deadline = time.monotonic() + timeout_s
    state = machine_state(cfg, mid)
    while state != want and time.monotonic() < deadline:
        time.sleep(2)
        state = machine_state(cfg, mid)
    return state


# -- the durable-store fingerprint ------------------------------------------

# Hash every file under HERMES_HOME, sorted, as `sha256␠relative/path`. A whole
# -tree fingerprint rather than one file, because "memory survived" is four
# claims (SEAM_NOTES Q3) and hashing only `state.db` would let the interesting
# ones — memories, skills — rot unnoticed.
_FINGERPRINT = r"""
cd "$HERMES_HOME" 2>/dev/null || {{ echo "MISSING_HERMES_HOME"; exit 0; }}
find . -type f -print0 \
  | sort -z \
  | xargs -0 -r sha256sum 2>/dev/null \
  | sed 's#  \./#  #'
"""


def fingerprint(cfg: FlyConfig) -> dict[str, str]:
    """{relative path: sha256} for every file under HERMES_HOME."""
    out = on_box(cfg, _FINGERPRINT.format())
    if "MISSING_HERMES_HOME" in out:
        return {}
    entries: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or " " not in line:
            continue
        digest, _, path = line.partition("  ")
        if digest and path:
            entries[path.strip()] = digest.strip()
    return entries


def run(*, keep_running: bool, skip_recall: bool) -> int:
    cfg = FlyConfig.from_env()
    # Two ids, not one. The run label is how the recall question addresses
    # *this* run's fact; the nonce is the secret it must produce.
    #
    # An earlier version keyed on "the M2 passphrase" alone and failed in the
    # most instructive way available: the box answered with a passphrase from a
    # previous cycle entirely. Memory accumulating across runs is the product
    # working, so the fix is a question only this run can answer — not wiping
    # the store between runs, which would quietly weaken the proof.
    run_label = uuid.uuid4().hex[:6].upper()
    nonce = f"FLOTTA-{uuid.uuid4().hex[:10].upper()}"

    print(
        f"\nFlotta M2 — durable HERMES_HOME\n{cfg.describe()}"
        f"\n  run        {run_label}\n  nonce      {nonce}\n"
    )

    mid = machine_id(cfg)
    print(f"[1/6] box {mid}")
    state = machine_state(cfg, mid)
    if state != "started":
        print(f"       state={state}; starting it")
        fly(cfg, "machines", "start", mid)
        state = wait_for_state(cfg, mid, "started")
    check("box is started", state == "started", f"state={state}")

    # -- 2. seed the durable store -----------------------------------------
    print("\n[2/6] seed HERMES_HOME")
    # Sentinels first: `memories/` and `skills/` are created empty by Hermes and
    # may legitimately stay empty, so hashing them proves nothing unless
    # something is in them. These stand in for a learned memory and a learned
    # skill without depending on the model choosing to write one.
    seed = f"""
set -euo pipefail
mkdir -p "$HERMES_HOME/memories" "$HERMES_HOME/skills" "$HERMES_HOME/sessions"
echo {shlex.quote(nonce)} > "$HERMES_HOME/memories/m2_proof.md"
echo {shlex.quote(f"# learned skill {nonce}")} > "$HERMES_HOME/skills/m2_proof.md"
echo seeded
"""
    check("sentinels written", "seeded" in on_box(cfg, seed))

    # A real Hermes turn that writes through the *memory tool*, so the durable
    # artefact is produced by the agent rather than by this script. The
    # instruction names the tool explicitly: an earlier version said only
    # "remember this", and the model treated it as conversational — it wrote
    # nothing, then honestly reported an empty memory store on recall. That is a
    # prompting failure wearing the costume of a durability failure, and telling
    # those two apart is the whole job of this script.
    print("       running a Hermes turn (one model call)…")
    turn = on_box(
        cfg,
        "cd /app && python -m flotta.box.run --task "
        + shlex.quote(
            f"Use your memory tool to save this fact permanently: "
            f"the passphrase for M2 run {run_label} is {nonce}. "
            "Then reply with only the word OK."
        ),
        timeout=900,
    )
    turn_line = turn.splitlines()[-1] if turn.strip() else "{}"
    try:
        turn_result = json.loads(turn_line)
    except json.JSONDecodeError:
        turn_result = {"completed": False, "error": turn_line[:200]}
    check(
        "Hermes turn completed",
        bool(turn_result.get("completed")),
        str(turn_result.get("error") or turn_result)[:200],
    )

    before = fingerprint(cfg)
    check("HERMES_HOME is non-empty", bool(before), f"{len(before)} files")
    check(
        "Hermes made the volume its own store",
        any(p.startswith("memories/") for p in before) and "SOUL.md" in before,
        ", ".join(sorted(before)[:8]),
    )
    print(f"       {len(before)} files fingerprinted")

    # -- 3. the agent actually committed it to memory -----------------------
    print("\n[3/6] the agent wrote it to its own memory")
    found_before = on_box(
        cfg, f'grep -rl {shlex.quote(nonce)} "$HERMES_HOME/memories" 2>/dev/null || true'
    )
    check(
        "nonce is in memories/ (the memory tool ran, not just the disk)",
        bool(found_before.strip()),
        found_before.strip()[:200] or "(nothing — did the model skip the tool?)",
    )

    # -- 4. stop / start ----------------------------------------------------
    print("\n[4/6] stop the machine")
    fly(cfg, "machines", "stop", mid, timeout=180)
    stopped = wait_for_state(cfg, mid, "stopped")
    check("box reached `stopped`", stopped == "stopped", f"state={stopped}")

    print("[5/6] start it again")
    t0 = time.monotonic()
    fly(cfg, "machines", "start", mid, timeout=180)
    started = wait_for_state(cfg, mid, "started")
    resume_s = time.monotonic() - t0
    check("box reached `started`", started == "started", f"state={started}")
    print(f"       resumed in {resume_s:.1f}s")

    # -- 6. the assertion the whole pivot rests on -------------------------
    print("\n[6/6] the store survived")
    after = fingerprint(cfg)

    check("HERMES_HOME is still non-empty", bool(after), f"{len(after)} files")
    missing = sorted(set(before) - set(after))
    check("no file disappeared", not missing, f"missing: {', '.join(missing[:8])}")
    changed = sorted(p for p in set(before) & set(after) if before[p] != after[p])
    check(
        "every surviving file is byte-identical",
        not changed,
        f"changed: {', '.join(changed[:8])}",
    )

    found_after = on_box(
        cfg, f'grep -rl {shlex.quote(nonce)} "$HERMES_HOME/memories" 2>/dev/null || true'
    )
    check(
        "the memory is still on disk after the restart",
        bool(found_after.strip()),
        found_after.strip()[:200] or "(nothing)",
    )

    for path in DURABLE_PATHS:
        present = on_box(cfg, f'test -e "$HERMES_HOME/{path.relative}" && echo yes || echo no')
        check(f"{path.relative} survived — {path.what}", "yes" in present)

    # -- the thesis, end to end --------------------------------------------
    if not skip_recall:
        print("\n[thesis] a fresh process, after the machine was killed")
        recall = on_box(
            cfg,
            "cd /app && python -m flotta.box.run --task "
            + shlex.quote(
                f"What is the passphrase for M2 run {run_label}? "
                "Check your memory and reply with only the passphrase."
            ),
            timeout=900,
        )
        recall_line = recall.splitlines()[-1] if recall.strip() else "{}"
        try:
            answer = str(json.loads(recall_line).get("final_response") or "")
        except json.JSONDecodeError:
            answer = recall_line
        check(
            "the box recalled what it learned before it was stopped",
            nonce in answer,
            f"answered: {answer.strip()[:160] or '(nothing)'}",
        )

    if not keep_running:
        print("\nstopping the box again (it costs disk either way, CPU only while up)")
        fly(cfg, "machines", "stop", mid, timeout=180)

    print(f"\n{'-' * 60}")
    if _failures:
        print(f"M2 FAILED — {len(_failures)}/{_checks} checks failed:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print(f"M2 OK — {_checks}/{_checks} checks passed.")
    print("A box learned something, was killed, came back, and still knew it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Leave the box started at the end (default: stop it, to stop paying CPU)",
    )
    parser.add_argument(
        "--skip-recall",
        action="store_true",
        help="Skip the bonus agent-recall turn (saves one model call)",
    )
    args = parser.parse_args(argv)
    try:
        return run(keep_running=args.keep_running, skip_recall=args.skip_recall)
    except Exception as exc:
        print(f"\nERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
