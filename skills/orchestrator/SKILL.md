---
name: flotta-orchestrator
description: Delegate a task to an isolated, disposable cloud worker.
version: 0.1.0
author: Flotta
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [delegation, cloud, modal, flotta, isolation, durable]
    category: orchestration
    requires_toolsets: [terminal]
    related_skills: [subagent-driven-development]
---

# Flotta Orchestrator Skill

Hand a task to a **disposable headless agent in its own cloud container**,
collect its answer, and destroy it. Unlike an in-process subagent, the worker
runs on Modal, survives past this session, and cannot touch this machine.

This skill does *not* do fan-out: v0.1 runs one worker at a time.

## When to Use

The distinguishing property is **isolation**, not merely duration or
durability. Reach for Flotta when the work should run **somewhere that is not
this machine** — a throwaway container with no access to the user's files, git
state, credentials, or environment. It also outlives the session, but so do
some built-ins, so isolation is the deciding factor.

Use Flotta when the task is **isolation-worthy**, **self-contained**, and long
enough to be worth 15–60s of container setup.

**Prefer a built-in instead when:**

| Situation | Use | Why not Flotta |
|---|---|---|
| Untrusted or destructive work that must not touch this machine | **Flotta** | — |
| Long work that must outlive the session, but is fine running locally | `cronjob` (one-shot) | Also survives disconnect, with no container cost |
| In-session helper that should share your toolsets and files | `delegate_task` | The worker can see none of them |
| A long shell command on *this* machine | `terminal` with `background=True` | Same — it needs local context |
| You could just answer it | answer it | A spawn costs real money |

Do not spawn on a hunch. Spawning costs real money and takes 15–60s of setup
before any work begins; a worker that does what you could have done in one
thought is pure waste. If unsure, say what you are considering and ask.

## Prerequisites

- The Flotta checkout path. Ask the user once, then reuse it; there is no
  `flotta` on `PATH`. Referred to below as `$FLOTTA_REPO`.
- Modal credentials and a provider key already configured in that repo. If they
  are missing the worker fails fast with a clear message — see **Pitfalls**.

## How to Run

Use the `terminal` tool for every command in this skill.

```bash
uv run --project "$FLOTTA_REPO" flotta spawn "<task>" --wait --json
```

**Always pass `--wait`.** Flotta's *local* process, not the container, records
the outcome; without `--wait` nothing collects the answer and the worker's row
sits at `running` forever even after the container has died.

Tell the user you are delegating, and what for, before you block on it.

## Quick Reference

| Goal | Command |
| --- | --- |
| Delegate and collect | `uv run --project "$FLOTTA_REPO" flotta spawn "<task>" --wait --json` |
| Bound the run | add `--timeout-s 300` (default 900) |
| Prove plumbing, skip the model | add `--dry-run` |
| Tear down | `uv run --project "$FLOTTA_REPO" flotta kill <worker_id>` |
| Live workers | `uv run --project "$FLOTTA_REPO" flotta ps` |
| One worker's timeline | `uv run --project "$FLOTTA_REPO" flotta logs <worker_id>` |
| Collect a worker spawned earlier | `uv run --project "$FLOTTA_REPO" flotta watch <worker_id>` |

## Procedure

### 1. Write the task as if for a stranger

This is where delegation actually fails. The worker receives **one string**. It
has no access to this conversation, the user's files, your reasoning, or
anything on disk. It starts from nothing and **cannot ask a question**.

Useless — each returns confident nonsense:

```
"Fix the bug in auth.py"               (no auth.py exists over there)
"Summarize the document I mentioned"   (no document was sent)
"Continue what we discussed"           (no conversation was sent)
```

Workable — everything needed is inlined:

```
"Write a 1500-word explainer on how Raft achieves consensus, covering
 leader election, log replication, and safety. Assume a reader who knows
 distributed systems basics. Return markdown."
```

If the task needs context, paste that context into the task string. If it is
too large to paste, the task is not delegable — do it yourself.

### 2. Spawn and wait

Run the command from **How to Run**. Expect 15–60s before the model even starts.

### 3. Check the verdict, not the vibe

```json
{
  "worker_id": "w-d03524ab934a",
  "status": "done",
  "event": "completed",
  "result": { "final_response": "…", "api_calls": 3, "duration_s": 41.2 }
}
```

| `status` | `event` | Meaning |
|---|---|---|
| `done` | `completed` | Finished. The answer is `result.final_response`. |
| `failed` | `timed_out` | Hit the hard timeout. Partial work is **lost**, not returned. |
| `failed` | `failed` | Errored. Reason in `result.error`. |

The command also exits non-zero on anything that is not `done`.

The worker's answer is a **self-report**. The verifiable handle is the store:
`flotta logs <worker_id>` shows the recorded timeline, and that is what to trust
if the two disagree.

### 4. Always tear down

```bash
uv run --project "$FLOTTA_REPO" flotta kill <worker_id>
```

Run it **every time** — after success, after failure, after a timeout, after
giving up. It is idempotent. Skipping it leaves a row that reads as live forever
and, in the worst case, a container still running and billing.

If teardown reports the worker **may still be running**, say so plainly and tell
the user to check their Modal dashboard. Do not quietly move on.

## Pitfalls

1. **Delegating a task that references local context.** The single most common
   failure. Re-read the task string and ask: would a stranger with no machine
   access understand it? See Procedure step 1.
2. **Omitting `--wait`.** The container finishes, nobody collects the result,
   and the worker is stranded at `running`. Fix an existing one with
   `flotta watch <id>`, or close it with `flotta kill <id>`.
3. **Reporting a failure as a success.** Never present your own guess as the
   worker's answer, never summarize a timeout as though work came back, and
   never claim a teardown that errored. Give the user `result.error` and the
   `worker_id`.
4. **Accepting confident nonsense.** A worker given an under-specified task
   returns fluent, wrong output that *looks* like success. If the answer does
   not actually address the task, report that the delegation failed and why —
   do not pass it along.
5. **`missing provider config` in `result.error`.** The container has no model
   credentials. The user must set `FLOTTA_MODEL`, `FLOTTA_MODEL_BASE_URL` and
   `FLOTTA_API_KEY` in the repo's `.env` **and re-run `just deploy`** — the
   secret is baked in at deploy time, so editing `.env` alone changes nothing.
6. **`no fleet-state store at …`.** Wrong directory, or a fleet that has never
   spawned. Pass `--store`, or set `$FLOTTA_STORE`. Note that `spawn` creates
   the store; read commands deliberately do not.
7. **Modal auth or workspace errors.** Ask the user to run `just modal-whoami`
   in the repo.

## Verification

Before telling the user you are done:

- [ ] `status` was `done` — not merely a command that returned output
- [ ] The answer actually addresses the task that was sent
- [ ] `flotta kill` ran and reported `torn_down`
- [ ] Any failure was reported with `result.error` and the `worker_id`
- [ ] Nothing was claimed that the store does not show

## Remember

```
One string in — the worker knows nothing else.
Always --wait, or the result is never collected.
Always tear down, even on failure.
The store is the truth; the worker's summary is a claim.
```
