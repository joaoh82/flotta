---
name: flotta-orchestrator
description: "Delegate a long, self-contained task to a disposable cloud worker via Flotta, then summarize the result and always tear down."
version: 0.1.0
author: Flotta
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Delegation, Agents, Modal, Cloud, Flotta, Orchestration]
    related_skills: []
---

# Delegating to a Flotta worker

You are the orchestrator. Flotta lets you hand a task to a **disposable headless
agent** running in its own cloud container, collect its answer, and destroy it.

One command does the whole round trip:

```bash
uv run --project "$FLOTTA_REPO" flotta spawn "<task>" --wait --json
```

`$FLOTTA_REPO` is the path to the Flotta checkout (ask the user once, then
remember it; there is no `flotta` binary on `PATH`).

Everything below is about deciding *whether* to run that, writing the task
string properly, and being honest about what came back.

## Delegate when — and only when

Spawning costs real money and takes 15–60 seconds of setup before any work
starts. That overhead has to buy something.

**Delegate when the task is:**

- **Long-running** — minutes of grinding you would otherwise do inline while
  the user waits on you for everything else.
- **Isolation-worthy** — you want it to run somewhere its mistakes cannot touch
  the user's machine, files, or git state.
- **Self-contained** — see the next section. This is the hard constraint.

**Do not delegate when:**

- You can just answer. A worker that takes 40 seconds to do what you could do
  in one thought is pure waste, and the user pays for it.
- **The task needs the user's files, repo, environment, or credentials.** The
  worker is a bare container. It cannot see the machine you are running on.
- The task needs a follow-up question. The worker runs unattended and **cannot
  ask for clarification** — an ambiguous task comes back as a confident guess
  or as garbage.
- The user is iterating conversationally and wants quick turns.

When you are unsure, say what you are considering and ask. Do not spawn on a
hunch — a wasted worker is money and a minute of the user's time.

## Write the task as if for a stranger with no context

This is where delegation actually fails. The worker gets **one string**. It has
no access to this conversation, the user's files, your earlier reasoning, or
anything on disk. It starts from nothing.

❌ **Useless — every one of these produces confident nonsense:**

```
"Fix the bug in auth.py"                  (no auth.py exists over there)
"Summarize the document I mentioned"      (no document was sent)
"Continue what we discussed"              (no conversation was sent)
"Check whether our tests pass"            (no repo, no tests)
```

✅ **Workable — self-contained, everything needed is inlined:**

```
"Write a 1500-word explainer on how Raft achieves consensus, covering
 leader election, log replication, and safety. Assume a reader who knows
 distributed systems basics. Return markdown."
```

If the task needs context, **paste the context into the task string**. If the
context is too large to paste, the task is not delegable — do it yourself.

## Run it

```bash
uv run --project "$FLOTTA_REPO" flotta spawn "<task>" --wait --json
```

- `--wait` blocks until the worker reaches a terminal state and prints the
  result. **Always use it.** Without `--wait` nothing ever collects the answer
  and the worker's row sits at `running` forever — Flotta's local process, not
  the container, is what records the outcome.
- `--timeout-s N` bounds the run (default 900). Set it near what you expect;
  a runaway worker is billed until the timeout fires.
- `--dry-run` boots the container and skips the model call. Use it if you want
  to prove the plumbing works without paying for tokens.

Tell the user you are delegating, and what for, before you block on it.

## Read the result

Success looks like this:

```json
{
  "worker_id": "w-d03524ab934a",
  "status": "done",
  "event": "completed",
  "result": {
    "completed": true,
    "final_response": "…the worker's answer…",
    "api_calls": 3,
    "duration_s": 41.2
  }
}
```

Check **`status`**, not the exit of the command alone:

| `status` | `event` | What happened |
|---|---|---|
| `done` | `completed` | The worker finished. Its answer is `result.final_response`. |
| `failed` | `timed_out` | It hit the hard timeout. Partial work is **lost**, not returned. |
| `failed` | `failed` | It errored. The reason is in `result.error`. |

The command also **exits non-zero** on anything that is not `done`, so a
non-zero exit means do not go looking for an answer.

## Then always tear down

```bash
uv run --project "$FLOTTA_REPO" flotta kill <worker_id>
```

Run this **every time** — after success, after failure, after a timeout, and
after you give up. It is idempotent, so running it twice is safe and running it
on an already-finished worker is safe.

It cancels the container and closes the worker's row. Skipping it leaves a row
that reads as live forever in `flotta ps` and in the dashboard, and in the worst
case leaves a container running and billing.

If teardown itself reports that the worker **may still be running**, say so
plainly and tell the user to check their Modal dashboard. Do not quietly move
on — that is money burning.

## When it goes wrong, say so

The worker failed, timed out, or returned something useless. Report that. Give
the user `result.error` and the `worker_id`, tear down, and either offer to do
the task yourself or ask whether to retry with a longer timeout.

**Never** paper over a failure:

- Do not present your own guess as the worker's answer.
- Do not summarize a timeout as though work came back.
- Do not claim you tore a worker down if the teardown errored.

A worker that returns confident nonsense is the failure mode to watch for,
because it looks like success. If the answer does not actually address the task
— usually because the task was not self-contained — tell the user the
delegation did not work and why, rather than passing the nonsense along.

## Fault reference

| Symptom | Cause | What to do |
|---|---|---|
| `no fleet-state store at …` | Running from the wrong directory, or a fleet that has never spawned | Pass `--store`, or set `$FLOTTA_STORE`. Note `spawn` creates the store; read commands do not. |
| `missing provider config` in `result.error` | The worker container has no model credentials | The user must set `FLOTTA_MODEL` / `FLOTTA_MODEL_BASE_URL` / `FLOTTA_API_KEY` in the repo's `.env` **and re-run `just deploy`** — the secret is baked in at deploy time, so editing `.env` alone changes nothing. |
| Modal auth or workspace errors | No Modal credentials, or the wrong workspace | Ask the user to run `just modal-whoami` in the repo. |
| Worker stuck at `running` long after it should be done | It was spawned without `--wait` and nobody collected it | `flotta watch <id>` to collect, or `flotta kill <id>` to close it out. Prevent it by always using `--wait`. |

## Checking on the fleet

These are read-only and need no cloud credentials:

```bash
uv run --project "$FLOTTA_REPO" flotta ps            # live workers
uv run --project "$FLOTTA_REPO" flotta ps --all      # include finished
uv run --project "$FLOTTA_REPO" flotta logs <id>     # one worker's timeline
```

## Scope

v0.1 runs **one worker at a time**, with no shared memory between workers and
no fan-out. Do not attempt to spawn several in parallel and merge the results —
that is a later version, and trying it now will just cost money and confuse the
fleet view.
