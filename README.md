# Flotta

**Hand a task to a disposable agent in the cloud. Get the answer back. Destroy it.**

Flotta (Italian for *fleet*) is an open-source fleet runtime for self-improving agents. One
always-on orchestrator — [Hermes Agent](https://github.com/NousResearch/Hermes-Agent) first —
spawns headless worker agents in throwaway [Modal](https://modal.com) containers, collects their
results, and tears them down.

```
you ──▶ Hermes ──spawn──▶ ┌─ disposable container ─┐
                          │  headless Hermes       │
                          │  one task, then gone   │
                          └────────────┬───────────┘
        fleet.db ◀── watcher ──────────┘
           ▲
      CLI · dashboard
```

The container never writes fleet state. A worker that dies mid-task — OOM, preemption, a kill —
writes nothing at all, so a local watcher owns the verdict precisely because it outlives the
worker. That one rule shapes most of the design.

> **Status: v0.1 is nearly complete and works end to end.** A chat message produces a real cloud
> delegation round-trip in about 40 seconds. What is left before 1.0 is polish, not architecture —
> see [Limitations](#limitations), which is deliberately specific.

## What it does today

- **Disposable cloud workers.** A pinned Modal image boots Hermes *headless* — no messaging
  gateway, one provider, fixed toolset — and hard-exits on a watchdog timeout, so a stuck worker
  destroys itself.
- **Every transition recorded.** A local SQLite store is the single source of truth, with status
  changes validated by an explicit transition table.
- **A CLI.** `ps`, `spawn`, `watch`, `logs`, `kill`, `reconcile` — all with `--json`.
- **A local dashboard.** A browser view of the fleet with a kill button.
- **A Hermes skill.** Teaches your orchestrator when to delegate, how to write a task that
  survives having no context, and to always tear down.

Not in v0.1: shared memory between workers, parallel fan-out, any hosted component.

## Requirements

| | |
|---|---|
| Python | 3.11+ |
| [uv](https://docs.astral.sh/uv/) | for install and dev |
| [Modal](https://modal.com) account | free tier is plenty to try it |
| Node 20+ | only for the dashboard |
| An OpenAI-compatible API key | only to run real tasks; the dry-run path needs none |

## Quickstart

### 1. Install

```bash
git clone https://github.com/joaoh82/flotta
cd flotta
uv tool install .        # puts a bare `flotta` on your PATH
```

Working on Flotta itself? Use `uv run flotta …` from the repo instead and skip the install.

*(`pip install flotta` will work once the package is published; it is not on PyPI yet.)*

### 2. Point at your Modal workspace

```bash
cp .env.example .env
modal token new --profile flotta --no-activate
just modal-whoami        # prints which workspace every command will use
```

The profile is pinned in `.env` rather than relying on Modal's globally-active one. That exists
because the active profile was once found pointing at an unrelated project — a wrong profile would
have deployed into someone else's workspace silently.

### 3. Prove the plumbing without spending on a model

```bash
just check               # 212 tests, offline, free
just deploy              # publishes the worker function
just e2e                 # full lifecycle against real Modal, no LLM
```

`just e2e` should end with `E2E OK — 21/21 checks passed against real Modal`. That is a genuine
container spawning, running, being watched to completion, and being torn down.

### 4. Add a model and run something real

Put your provider details in `.env`:

```bash
FLOTTA_MODEL=anthropic/claude-sonnet-4
FLOTTA_MODEL_BASE_URL=https://openrouter.ai/api/v1
FLOTTA_API_KEY=sk-or-...
```

Then push them to the worker and go:

```bash
just secret-sync         # credentials live in a Modal secret, not in the deploy
flotta spawn "Explain in 150 words why append-only logs simplify distributed systems." --wait
```

Any OpenAI-compatible endpoint works — swap the base URL for OpenAI, Nous Portal, a local vLLM.

### 5. Watch the fleet

```bash
just dashboard           # http://localhost:3001
```

### 6. Let the agent decide

This is the point of the whole thing. Install the skill into your local Hermes, then just ask:

```bash
just install-skill
hermes chat -q "Delegate this to a Flotta cloud worker and summarize: '<your task>'.
                Flotta repo: /path/to/flotta. Tear it down when done."
```

The orchestrator decides delegation is warranted, spawns, collects, summarizes, and cleans up.

## What it costs

Two separate bills, and conflating them is how people get surprised:

| Step | Modal compute | Model tokens |
|---|---|---|
| `just check` | — | — |
| `just modal-whoami` | — | — |
| `just smoke` | **yes** — a real container; a *cold image build* is the single biggest item here | — |
| `just deploy && just e2e` | yes, seconds of container time | — |
| `just e2e-live` | yes | yes, one small call |
| A real task | yes, for the task's duration | yes, whatever the task needs |

A typical short task is cents. The first run is the expensive one, because Hermes is installed
from source into the image; after that the image is cached.

**Cost estimation is opt-in, and deliberately so.** Set a rate and Flotta fills the "Est. cost"
column; leave it unset and every surface shows `—`:

```bash
FLOTTA_COST_PER_SECOND=0.0000131   # your rate, from Modal's pricing for the worker's CPU/memory
```

It has to be *your* number because Modal's billing API cannot attribute cost to a single worker:
line items are keyed by **App** id at daily or hourly resolution, every worker shares one app, and
function calls cannot be tagged. Rather than derive a dollar figure from a rate nobody chose —
which would look authoritative and be wrong — Flotta shows a blank until you supply one. It covers
**container time only**; model tokens are a separate bill Flotta never sees.

## Command reference

```bash
flotta ps [--all]              # live workers (finished hidden by default)
flotta spawn "<task>" --wait   # launch one and block for the result
flotta watch <id>              # re-attach to a worker spawned earlier
flotta logs <id>               # that worker's event timeline
flotta kill <id>               # tear it down (idempotent)
flotta reconcile               # rescue workers stranded past their deadline
```

Every command takes `--json`. `ps` and `logs` are pure store reads and need no Modal credentials.
Exit codes carry meaning: `1` a failed worker, `2` a refusal (the fleet is busy, or the store is
missing).

**Always pass `--wait`** — or note the worker id. Flotta's *local* process records the outcome, not
the container, so a worker nobody waits on finishes in the cloud while its row sits at `running`.
`flotta watch <id>` collects it afterwards, and `flotta reconcile` sweeps up any you forgot.

## Limitations

Stated plainly, because finding these yourself is worse.

- **One worker at a time.** Enforced, not merely advised — a second concurrent spawn is refused
  with exit 2. Raise it with `--max-concurrent` only if you mean it; nothing above one is tested.
- **No shared memory and no fan-out.** Workers cannot see each other or anything you have.
- **A worker only knows the string you send it.** It cannot see your files, your repo, or your
  conversation, and it cannot ask a question. Under-specified tasks come back as confident nonsense.
- **`--wait`, or the row strands** until `flotta watch` / `flotta reconcile`.
- **The dashboard has no authentication and can kill workers.** Localhost only. Do not expose it.
- **Cost estimation is opt-in and container-time only.** Set `FLOTTA_COST_PER_SECOND` or the column stays blank; token spend is never included. See [What it costs](#what-it-costs).
- **Rotating a provider key is not instant.** `just secret-sync` needs no redeploy, but a secret
  becomes environment variables when a container *starts*, so a warm container serves the old value
  until it scales down. `modal app stop flotta-provision -y` forces it.
- **Workers run an older Hermes than your orchestrator** — the image pin has drifted behind the
  released agent, and reconciling it is queued.

## How it fits together

| | |
|---|---|
| `src/flotta/store.py` | the fleet-state store — SQLite, thin SQL, validated transitions |
| `src/flotta/provision.py` | spawn / watch / teardown / reconcile — **runs locally**, the store's only writer |
| `src/flotta/worker/` | the Modal image and the container entrypoint — **runs in the cloud**, touches no store |
| `src/flotta/cli.py` | the Typer CLI |
| `dashboard/` | Next.js over the same store, via Node's built-in SQLite |
| `skills/orchestrator/` | the Hermes skill that teaches delegation |

Storage is deliberately plain SQLite behind a thin SQL interface, so pointing it at
[Turso](https://turso.tech) later is a change to one connection factory rather than a rewrite.

## Development

```bash
just                     # list every recipe
just check               # lint + tests — run before committing
just check-dashboard     # tsc + eslint
just test-one <keyword>  # a single test
```

The test suite is hermetic and free: every Modal touchpoint is injected, never called for real.

## License

[AGPL-3.0](LICENSE) for the core. The network-use clause is deliberate — a hosted Flotta must share
its changes. Adapters and templates are permissively licensed so they can be embedded freely.
