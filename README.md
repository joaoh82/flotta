# Flotta

**Hand a task to a disposable agent in the cloud. Get the answer back. Destroy it.**

[![CI](https://github.com/joaoh82/flotta/actions/workflows/ci.yml/badge.svg)](https://github.com/joaoh82/flotta/actions/workflows/ci.yml)

![Flotta spawning a box, collecting its answer, and showing an empty fleet afterwards](assets/demo.gif)

*Real run, not a mockup — `just demo` re-records it. The box answers `4.19.0-gvisor`, which is
Modal's sandbox kernel and not the laptop that asked; by the time `flotta ps` runs, the container
is gone and only the store remembers it.*

*(This recording predates the box/task rename, so its `ps` columns are the old ones. Re-recording
costs a real spawn, so it waits for the next demo pass.)*

Flotta (Italian for *fleet*) is an open-source fleet runtime for self-improving agents. One
always-on orchestrator — [Hermes Agent](https://github.com/NousResearch/Hermes-Agent) first —
spawns headless agents in [Modal](https://modal.com) containers, collects their results, and tears
them down.

```
you ──▶ Hermes ──spawn──▶ ┌─ box ──────────────────┐
                          │  headless Hermes       │
                          │  one task, then gone   │
                          └────────────┬───────────┘
        fleet.db ◀── watcher ──────────┘
           ▲
      CLI · dashboard
```

The container never writes fleet state. A machine that dies mid-task — OOM, preemption, a kill —
writes nothing at all, so a local watcher owns the verdict precisely because it outlives the
machine. That one rule shapes most of the design.

> **Status: the model is mid-pivot; read this before the rest.**
>
> Flotta v0.1 shipped a **task runner**: it put the disposable thing in the cloud and kept the
> persistent thing — your agent's memory — on the laptop. That is backwards for what this is
> meant to be, which is a *host* for persistent cloud agents you talk to from a thin client.
>
> The runtime now names three tiers with three lifetimes: a **box** is a machine that *is* an
> agent (months, durable memory); a **workspace** is where its untrusted code runs (hours, no
> memory); a **shard** is one stateless fan-out call (seconds). The store, the CLI and the
> dashboard already speak that vocabulary.
>
> **What is not built yet is the persistence.** On Modal — today's only backend — a box is still
> disposable: `stop` and `start` record the transition but no infrastructure suspends anything,
> and `HERMES_HOME` is still ephemeral, so a box forgets everything when it dies. Durable memory
> across a stop/start cycle is the next milestone and the whole point. Until it lands, treat the
> box vocabulary as the shape the system is growing into, and [Limitations](#limitations) as the
> honest account of what runs.

> [!WARNING]
> **Flotta is a local tool. Nothing in it is authenticated.**
>
> The dashboard has no login and its kill button destroys real machines; anyone who can reach
> the port can kill any box and read every task and result the fleet has produced. The CLI
> and the fleet store assume the same — a single trusted operator on one machine.
>
> Everything binds localhost by design. Do not put the dashboard on a shared host, a public
> interface, or a tunnel without putting authentication in front of it first. Multi-user is not
> a v0.1 feature and there is no permission model to fall back on.

## What it does today

- **Cloud boxes.** A pinned Modal image boots Hermes *headless* — no messaging gateway, one
  provider, fixed toolset — and hard-exits on a watchdog timeout, so a stuck box destroys itself.
- **Every transition recorded.** A local SQLite store is the single source of truth, split into
  `boxes` / `workspaces` / `tasks`, each with its own validated transition table. A box cannot be
  `done` and a task cannot be `stopped`; the store refuses both.
- **A CLI.** `ps`, `spawn`, `watch`, `logs`, `stop`, `start`, `kill`, `reconcile` — all with
  `--json`.
- **A local dashboard.** A browser view of the fleet with a kill button.
- **A Hermes skill.** Teaches your orchestrator when to delegate, how to write a task that
  survives having no context, and to always tear down.

Not yet: durable box memory, real suspend/resume, workspaces, shard fan-out, any hosted component.
`stop` and `start` are store-side only — see the status note above.

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

> **Upgrading an existing checkout?** The store schema changed — `workers`
> became `boxes` / `workspaces` / `tasks` — and there is **no migration**.
> Delete your old store first:
>
> ```bash
> rm -f fleet.db fleet.db-wal fleet.db-shm e2e_fleet.db e2e_fleet.db-wal e2e_fleet.db-shm
> ```
>
> This matters because the schema is created with `CREATE TABLE IF NOT EXISTS`:
> an old file is not rejected, it quietly gains empty new tables beside the
> stale `workers` one and renders as an empty fleet. Old rows are dropped
> rather than migrated on purpose — they describe one-shot task runs, and there
> is no box for them to become. Inventing a synthetic box per historical worker
> would be a lie in the data.

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
just check               # the whole hermetic suite, offline, free
just deploy              # publishes the run_worker function
just e2e                 # full lifecycle against real Modal, no LLM
```

`just e2e` should end with `E2E OK — 36/36 checks passed against real Modal`. That is a genuine
container spawning, running, being watched to completion, stopped, started, and destroyed.

### 4. Add a model and run something real

Put your provider details in `.env`:

```bash
FLOTTA_MODEL=anthropic/claude-sonnet-4
FLOTTA_MODEL_BASE_URL=https://openrouter.ai/api/v1
FLOTTA_API_KEY=sk-or-...
```

Then push them to the box and go:

```bash
just secret-sync         # credentials live in a Modal secret, not in the deploy
flotta spawn "Explain in 150 words why append-only logs simplify distributed systems." --wait
```

Any OpenAI-compatible endpoint works — swap the base URL for OpenAI, Nous Portal, a local vLLM.

### 5. Watch the fleet

```bash
just dashboard           # http://localhost:3001
```

### 6. Talk to a box

The point of the whole thing, and the shape it now has: **the agent lives in
the cloud and you are a client.** A box runs `hermes serve` as PID 1 with its
memory on a durable volume, so it is still there tomorrow and still knows what
you told it.

```bash
just fly-up          # build and deploy the box image
just fly-auth        # mint its dashboard credentials
```

v0.1 did this the other way round — an orchestrator on your laptop shipping
tasks to disposable containers — which is the inversion the pivot exists to
correct. The local orchestrator skill that taught your laptop's Hermes to
delegate is gone: a cloud box's Hermes *is* the orchestrator and does not need
a skill to delegate to itself.

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
FLOTTA_COST_PER_SECOND=0.0000131   # your rate, from Modal's pricing for the box's CPU/memory
```

It has to be *your* number because Modal's billing API cannot attribute cost to a single task:
line items are keyed by **App** id at daily or hourly resolution, every box shares one app, and
function calls cannot be tagged. Rather than derive a dollar figure from a rate nobody chose —
which would look authoritative and be wrong — Flotta shows a blank until you supply one. It covers
**container time only**; model tokens are a separate bill Flotta never sees.

## Command reference

```bash
flotta ps [--all] [--tasks]    # boxes in the fleet; --tasks lists the work
flotta spawn "<task>" --wait   # create a box, put a task on it, block for the result
flotta watch <id>              # re-attach to a task (or a box's live task)
flotta logs <box>              # that box's timeline, across all three tiers
flotta stop <box>              # mark it idle (refused while work is in flight)
flotta start <box>             # wake it again
flotta kill <box>              # destroy it (idempotent)
flotta reconcile               # rescue tasks stranded past their deadline
```

A **box** is a machine that is an agent, a **task** is one piece of work that
visits it, and a box outlives every task on it. `ps` shows boxes because the
fleet *is* the boxes — and a `stopped` box stays in that list, because an idle
fleet is the system working, not rows to tidy away.

**The fleet store can live on a server.** Set `$FLOTTA_DATABASE_URL` to a
`postgres://` URL and the CLI reads that instead of a local file — the last
thing tying a fleet to one laptop. `just test-postgres` runs the store suite
against a throwaway Postgres to prove the two engines behave identically. The
dashboard cannot read a Postgres fleet yet and says so (501) rather than
showing an empty one; that waits on the control-plane API.

**Otherwise the store lives in your working directory** (`./fleet.db`) unless you set `$FLOTTA_STORE`. `spawn`
says where it created one, and every read command names the file it looked at — because spawning in
one directory and running `ps` in another otherwise looks exactly like an empty fleet.

Every command takes `--json`. `ps` and `logs` are pure store reads and need no Modal credentials;
so are `stop` and `start`, for now. Exit codes carry meaning: `1` a failed task or a missing id,
`2` a refusal — the fleet is busy, the store is missing, or the box is in the wrong state
(**only a running box can be stopped, and only a stopped box can be started**; starting is waking
an existing box, never creating one).

**Always pass `--wait`** — or note the ids. Flotta's *local* process records the outcome, not the
container, so a task nobody waits on finishes in the cloud while its row sits at `running`.
`flotta watch <id>` collects it afterwards, and `flotta reconcile` sweeps up any you forgot.

## Limitations

Stated plainly, because finding these yourself is worse.

- **A box does not remember anything.** `HERMES_HOME` is ephemeral, so memory, learned skills and
  history die with the container. This is the single most important gap: an agent that cannot
  remember cannot self-improve, which is the reason to run Hermes rather than a bare model call.
  Fixing it — a persistent volume at `/data` — is the next milestone.
- **`stop` and `start` do not suspend anything.** They record the transition; Modal cannot stop and
  resume a container, so nothing is actually suspended and nothing is actually saved. They become
  real when a persistent backend lands. Because of that, **`stop` is refused while the box has a
  live task** — a "stopped" box whose container is still running would report zero CPU while the
  invoice disagreed. Use `flotta kill` to cancel and destroy, or wait for the task.
- **One task at a time.** Enforced, not merely advised — a second concurrent spawn is refused with
  exit 2. Boxes themselves are uncapped. Raise the task cap with `--max-concurrent` only if you
  mean it; nothing above one is tested.
- **No workspaces and no fan-out.** Boxes cannot see each other or anything you have, code runs in
  the box rather than in an isolated workspace, and there is no shard tier yet.
- **A box only knows the string you send it.** It cannot see your files, your repo, or your
  conversation, and it cannot ask a question. Under-specified tasks come back as confident
  nonsense. (This is the failure durable box memory is meant to remove rather than mitigate.)
- **`--wait`, or the row strands** until `flotta watch` / `flotta reconcile`.
- **The dashboard has no authentication and can destroy boxes.** Localhost only. Do not expose it.
  Its UI still says "worker" in places — it reads the new schema correctly, but the vocabulary
  catches up when it moves off direct SQLite reads and onto an API.
- **Cost estimation is opt-in and container-time only.** Set `FLOTTA_COST_PER_SECOND` or the column stays blank; token spend is never included. It is measured per *task*, not per box. See [What it costs](#what-it-costs).
- **Rotating a provider key is not instant.** `just secret-sync` needs no redeploy, but a secret
  becomes environment variables when a container *starts*, so a warm container serves the old value
  until it scales down. `modal app stop flotta-provision -y` forces it.
- **Boxes run an older Hermes than your orchestrator** — the image pin has drifted behind the
  released agent, and reconciling it is queued.

## How it fits together

| | |
|---|---|
| `src/flotta/store.py` | the fleet-state store — `boxes` / `workspaces` / `tasks`, SQLite, thin SQL, one validated transition table per tier |
| `src/flotta/provision.py` | spawn / watch / stop / start / teardown / reconcile — **runs locally**, the store's only writer. `run_worker`, the one deployed Modal function, lives here too and touches no store |
| `src/flotta/worker/` | the Modal image and the container entrypoint — **runs in the cloud** |
| `src/flotta/cli.py` | the Typer CLI |
| `dashboard/` | Next.js over the same store, via Node's built-in SQLite |

Storage is deliberately plain SQLite behind a thin SQL interface, so pointing it at
[Turso](https://turso.tech) later is a change to one connection factory rather than a rewrite.

`run_worker` keeps its v0.1 name deliberately. It is the stateless one-shot — the *shard* tier —
and renaming it to match the box vocabulary would be wrong in the other direction.

## Development

```bash
just                     # list every recipe
just check               # lint + tests — run before committing
just check-dashboard     # tsc + eslint
just ci                  # both of the above — everything CI runs
just test-one <keyword>  # a single test
```

The test suite is hermetic and free: every Modal touchpoint is injected, never called for real.

That is also why it can run on every pull request:
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs `just check` and the dashboard's
`tsc`/`eslint` as two separate jobs on each PR and each push to `main`. It needs no secrets and
touches no billed infrastructure — the recipes that spend money (`smoke`, `deploy`, `e2e`,
`e2e-live`, anything `fly-*`) are deliberately not part of it and stay a local, deliberate act.

## License

[AGPL-3.0](LICENSE) for the core. The network-use clause is deliberate — a hosted Flotta must share
its changes. Adapters and templates are permissively licensed so they can be embedded freely.
