# Flotta

**Hand a task to a disposable agent in the cloud. Get the answer back. Destroy it.**

[![CI](https://github.com/joaoh82/flotta/actions/workflows/ci.yml/badge.svg)](https://github.com/joaoh82/flotta/actions/workflows/ci.yml)

![Flotta spawning a box, collecting its answer, and showing an empty fleet afterwards](assets/demo.gif)

*Real run, not a mockup — `just demo` re-records it. The box answers `4.19.0-gvisor`, which is
Modal's sandbox kernel and not the laptop that asked; by the time `flotta ps` runs, the container
is gone and only the store remembers it.*

*(This recording predates the box/task rename, so its `ps` columns are the old ones. Re-recording
costs a real spawn, so it waits for the next demo pass.)*

Flotta (Italian for *fleet*) is an open-source fleet runtime for self-improving agents. A **box**
is a long-lived machine that *is* an agent: it runs [Hermes Agent](https://github.com/NousResearch/Hermes-Agent)
as PID 1, keeps its memory on a volume at `/data/hermes`, and sleeps between conversations. You
create one, then talk to it as a client.

Hermes is the engine inside the box — the agent loop, the tooling, the memory — the way
[exe.dev](https://exe.dev) loads a box with Claude Code and a toolchain. Flotta hosts it.

```
you ──▶ Hermes ──spawn──▶ ┌─ box ──────────────────┐
                          │  headless Hermes       │
                          │  one task, then gone   │
                          └────────────┬───────────┘
        fleet.db ◀── watcher ──────────┘
           ▲
     control plane
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
> The runtime names two tiers with two lifetimes: a **box** is a machine that *is* an agent
> (months, durable memory on a volume); a **workspace** is where its untrusted code runs (hours,
> no memory — M6, not built). A third, stateless Modal shards, was cut in 2026-08.
>
> **The persistence is real now.** A box boots `hermes serve` on a Fly Machine with `/data/hermes`
> on a volume, and memory survives a stop/start. What is *not* built is the front door: nothing is
> authenticated, so boxes are reached over a private tunnel and the control plane refuses a public
> bind. See [Limitations](#limitations) for the honest account.

> [!WARNING]
> **The control plane is authenticated. A box is not yet publicly routed.**
>
> Set `$FLOTTA_SIGNING_KEY` and every fleet-API request needs a scoped token
> (`flotta token key`, then `flotta token mint`). Leave it unset and the control plane runs
> unauthenticated — which it will only do on a loopback bind; a public bind with no key is
> refused at startup.
>
> One thing is still open: **there is no multi-user model** — a token carries scopes, not an
> identity, so "who did this" is not a question the fleet can answer. Treat a signing key as an
> admin credential for the whole fleet.

## What it does today

- **Persistent cloud boxes.** A pinned image boots `hermes serve` as PID 1 on a Fly Machine with
  a volume at `/data/hermes`, so an agent's memory survives a stop/start.
- **Every transition recorded.** A local SQLite store is the single source of truth, split into
  `boxes` / `workspaces` / `tasks`, each with its own validated transition table. A box cannot be
  `done` and a task cannot be `stopped`; the store refuses both.
- **A CLI.** `create`, `chat`, `ps`, `logs`, `stop`, `start`, `kill`, `serve` — all with `--json`.
- **A control plane.** `flotta serve` — the fleet API plus a reconcile loop on a timer.
- **A local dashboard.** A browser view of the fleet, reading that API.

Not yet: the front door and authentication (M5), the Flotta app (M8), shared memory across the
fleet (M9). Nothing here is authenticated — localhost only.

## Requirements

| | |
|---|---|
| Python | 3.11+ |
| [uv](https://docs.astral.sh/uv/) | for install and dev |
| [Fly.io](https://fly.io) account | boxes and volumes bill while they exist |
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

### 2. Point at your Fly org

```bash
cp .env.example .env
flyctl auth login
just fly-whoami          # prints the org, app and volume every recipe will act on
```

Pinned in `.env` rather than relying on flyctl's current org. That exists because an ambient,
globally-active profile was once found pointing at an unrelated project — the wrong one would
provision into someone else's account silently.

### 3. Prove the plumbing without spending anything

```bash
just check               # the whole hermetic suite, offline, free
```

The suite is hermetic: every Fly touchpoint is injected, so this needs no network, no credentials
and no `flyctl`.

### 4. Create an agent and talk to it

Put your provider details in `.env`:

```bash
FLOTTA_MODEL=anthropic/claude-sonnet-4
FLOTTA_MODEL_BASE_URL=https://openrouter.ai/api/v1
FLOTTA_API_KEY=sk-or-...
```

Then build the box image, create an agent, and open a session:

```bash
just fly-up              # build the image and boot a machine — REAL Fly spend
just fly-secrets         # provider credentials as Fly secrets, not baked into the image
flotta create eng-a      # a persistent agent with durable memory
flotta chat eng-a        # talk to it
```

Any OpenAI-compatible endpoint works — swap the base URL for OpenAI, Nous Portal, a local vLLM.

`eng-a` keeps its memory on a volume at `/data/hermes`, so `flotta stop eng-a` and
`flotta start eng-a` leave it knowing what it knew.

### 5. Watch the fleet

Two processes: the control plane serves the fleet, the dashboard renders it.

```bash
just serve               # http://127.0.0.1:8080 — the fleet API
just dashboard           # http://localhost:3001 — in another terminal
```

The dashboard reads the API, so it shows a Postgres fleet and a SQLite one
identically, and its kill button no longer shells out to the CLI. Started on its
own it answers 503 and tells you to run `just serve`.

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

| Step | Fly | Model tokens |
|---|---|---|
| `just check` / `just ci` | — | — |
| `just fly-whoami` | — | — |
| `just fly-up` | **yes** — builds the image and boots a machine; the cold build is the biggest single item | — |
| `flotta create <name>` | yes — a machine and a volume | — |
| A stopped box | **yes, still** — the volume and rootfs bill while they exist | — |
| `flotta chat` | yes, while the box is awake | yes, whatever the conversation needs |

**A stopped box is cheap, not free.** That is the difference from v0.1's disposable containers and
it is the trade the whole design makes: you pay storage to keep an agent that remembers.

**Cost estimation is opt-in, and deliberately so.** Set a rate and Flotta fills the "Est. cost"
column; leave it unset and every surface shows `—`:

```bash
FLOTTA_COST_PER_SECOND=0.0000131   # your rate, from your provider's pricing for the box's CPU/memory
```

It has to be *your* number: a substrate's billing API attributes cost to an app or an org over a
day, not to one task, and inventing a per-task figure from a rate nobody chose would look
authoritative and be wrong. Flotta shows a blank until you supply one. It covers **machine time
only** — model tokens are a separate bill Flotta never sees, and so is volume storage.

## Command reference

```bash
flotta create <name>           # create a box — a persistent agent
flotta chat <box>              # talk to the agent on a box
flotta ps [--all] [--tasks]    # boxes in the fleet; --tasks lists the work
flotta logs <box>              # that box's timeline, across both tiers
flotta watch <id>              # re-attach to a task  (dormant until M6)
flotta stop <box>              # mark it idle (refused while work is in flight)
flotta start <box>             # wake it again
flotta kill <box>              # destroy it (idempotent)
flotta reconcile               # rescue tasks stranded past their deadline
flotta chat <box>              # talk to the agent on a box
flotta serve [--port 8080]     # the control plane: fleet API + reconcile on a timer
```

A **box** is a machine that is an agent, a **task** is one piece of work that
visits it, and a box outlives every task on it. `ps` shows boxes because the
fleet *is* the boxes — and a `stopped` box stays in that list, because an idle
fleet is the system working, not rows to tidy away.

**The fleet store can live on a server.** Set `$FLOTTA_DATABASE_URL` to a
`postgres://` URL and the CLI reads that instead of a local file — the last
thing tying a fleet to one laptop. `just test-postgres` runs the store suite
against a throwaway Postgres to prove the two engines behave identically. The
dashboard reads whichever engine the control plane is on, because it no longer
reads the store at all — see [The control plane](#the-control-plane).

**Otherwise the store lives in your working directory** (`./fleet.db`) unless you set `$FLOTTA_STORE`.
`create` says where it made one, and every read command names the file it looked at — because
creating in one directory and running `ps` in another otherwise looks exactly like an empty fleet.

Every command takes `--json`. `ps` and `logs` are pure store reads and need no credentials;
so are `stop` and `start`, for now. Exit codes carry meaning: `1` a failed task or a missing id,
`2` a refusal — the fleet is busy, the store is missing, or the box is in the wrong state
(**only a running box can be stopped, and only a stopped box can be started**; starting is waking
an existing box, never creating one).

**Pass `--wait`, or run the control plane.** The verdict is recorded by whatever local process
outlives the container — never the container itself — so a task nobody is watching finishes in the
cloud while its row sits at `running`. `flotta watch <id>` collects it afterwards and
`flotta reconcile` sweeps up any you forgot, but both are things you have to remember. `flotta serve`
runs that sweep on a timer so you do not have to.

## The control plane

`flotta serve` is a small always-on process that owns two things: the fleet API
the dashboard reads, and the reconcile loop.

The loop is the point. Until now a task's verdict belonged to whichever local
process happened to be holding `--wait`, which is why "always pass `--wait`" was
the central caveat — and why one task was found stranded at `running` for 138
hours. `flotta reconcile` already knew how to rescue those; what it lacked was
somewhere to run that is not a terminal someone has to remember to open.

```bash
just serve                       # 127.0.0.1:8080
curl -s localhost:8080/health    # is the loop actually sweeping?
```

`/health` answers that question rather than "is the process up", because those
are not the same question and only one of them is useful. A loop that has been
slept by a platform's serverless setting looks *identical* to a loop with
nothing to do — both report a healthy process and zero reconciled tasks. So the
loop records when each sweep **finished** and whether it raised, and `/health`
returns 503 when sweeps have stopped landing or keep failing. That distinction
is not hypothetical: the first live run of this loop failed every single sweep
while `/health` cheerfully said `ok`.

**It authenticates, and refuses to be exposed without doing so.** Three states,
each defensible on its own:

| `$FLOTTA_SIGNING_KEY` | bind | result |
|---|---|---|
| unset | loopback | runs unauthenticated — local development |
| unset | public | **refused at startup**, with the fix in the message |
| set | any | every request needs a scoped token |

`FLOTTA_CONTROL_ALLOW_INSECURE_BIND` is **gone**. It existed because there was
no way to authenticate a public bind, so the only options were "refuse" and
"refuse unless you promise you own the network". There is a way now, and
keeping an override that skips it would ship the hole this closes.

Tokens are signed JSON — permissions travel *in* the token, so verifying one is
a hash and a clock read rather than a database lookup. Three scopes, flat, no
hierarchy:

```
fleet:read     list and inspect boxes, read their events
fleet:write    create boxes
box:destroy    tear a box down
```

`box:destroy` is separate from `fleet:write` deliberately — it is the verb that
deletes an agent's entire memory, and a dashboard that only *shows* a fleet
should not carry it. A hierarchy would have granted it automatically.

**Revocation is a key rotation**, which is coarse and stated plainly: an issued
token cannot be individually revoked before it expires, so mint short ones.
Rotating `$FLOTTA_SIGNING_KEY` kills every token at once — the operation you
actually want when something leaks.

```bash
flotta token key                                       # generate a signing key
flotta token mint dashboard --scope fleet:read         # a read-only token
flotta token inspect <token>                           # expired, or missing a scope?
```

`/health` stays open: it reports whether the reconcile loop is sweeping and
nothing about the fleet's contents — no box names, no endpoints, no task text.
A liveness probe cannot hold a credential.

A `Dockerfile` at the repo root builds it, so the fleet can outlive the laptop
entirely. Do not put it behind a serverless/scale-to-zero setting: the reconcile
loop is continuous background work, and sleeping it reintroduces the exact bug
it exists to fix, one layer down.

## The front door

`flotta door` is what makes a box reachable without `flyctl` and a WireGuard
tunnel: `<box>.flotta.dev` terminates TLS and proxies to that box's
`hermes serve`.

**It is not a reverse proxy, and could not be.** A request normally arrives
while the box is *asleep* — that is the cost argument, not an edge case — so the
door has to resolve the hostname to a box, **wake it**, wait for Hermes to
finish importing itself, and only then proxy. Caddy and nginx cannot do the
first three: they need fleet state, substrate credentials, and the knowledge
that Fly's internal DNS only resolves *running* machines.

```
client ──TLS──▶ flotta door      1. validate the Flotta token
                    │            2. ask the control plane to wake the box
                    │            3. attach the box's own credentials
                    └──6PN──▶ box:9119
```

**Auth composes rather than stacks.** A caller presents a scoped Flotta token
with `box:chat`; the door validates it and attaches the *box's* Hermes
credentials outbound. The box's password never leaves the server side, and
Hermes's own gate is satisfied rather than bypassed.

It runs as its own Fly app, deliberately separate from the control plane: the
door must be on Fly to reach boxes over the private network, and the control
plane must not be pinned there because it is meant to self-host anywhere.

```bash
just door                # locally, against a running control plane
just door-deploy         # a real, billed, always-on Fly machine
just door-dns            # the exact Cloudflare records to add
```

**First contact with a sleeping agent takes 10–60s.** That is Hermes booting,
not a hang — the door holds the connection rather than failing, and says so.

### Boxes put themselves to sleep

A box with no live task and nothing happening for 30 minutes is **suspended**,
and the next request through the door wakes it. `$FLOTTA_IDLE_AFTER_S` changes
the threshold; `0` switches it off for anyone who would rather pay than wait.

This is what makes the cost argument true rather than aspirational — before it,
a box you created billed CPU until you remembered to stop it.

Suspend rather than stop, where the substrate offers it: a suspend restores the
machine's memory, which is worth little when PID 1 is `sleep infinity` and worth
a great deal when it is a Hermes that takes seconds to import itself.

**A box in a conversation is never suspended.** The sweep skips any box with a
live task, and the door reports a heartbeat while a WebSocket is open — a long
conversation writes nothing to the fleet on its own, so without that the agent
you are mid-sentence with would look idle.

## Limitations

Stated plainly, because finding these yourself is worse.

- **Boxes do not share what they learn.** Each keeps its own `/data/hermes`, so a skill one agent
  acquires is invisible to the others. The original goal was one shared, versioned brain; that is
  M9 and it is not built.
- **`stop` is refused while the box has a live task** — a "stopped" box still running would report zero CPU while the
  invoice disagreed. Use `flotta kill` to cancel and destroy, or wait for the task.
- **One task at a time.** Enforced, not merely advised — a second concurrent spawn is refused with
  exit 2. Boxes themselves are uncapped. Raise the task cap with `--max-concurrent` only if you
  mean it; nothing above one is tested.
- **No workspaces and no fan-out.** Boxes cannot see each other or anything you have, code runs in
  the box rather than in an isolated workspace, and there is no shard tier yet.
- **A box only knows the string you send it.** It cannot see your files, your repo, or your
  conversation, and it cannot ask a question. Under-specified tasks come back as confident
  nonsense. (This is the failure durable box memory is meant to remove rather than mitigate.)
- **`--wait`, or the row strands** until something sweeps for it — `flotta watch`,
  `flotta reconcile`, or a running `flotta serve`.
- **A token has scopes, not an identity.** There is no user model, so the fleet cannot answer
  "who destroyed that box". A signing key is an admin credential for everything.
- **A token cannot be individually revoked** before it expires. Rotating the signing key revokes
  all of them; mint short-lived ones.
  The dashboard's UI still says "worker" in places — renaming it is cosmetic churn, queued behind
  the parts that are not.
- **Cost estimation is opt-in and container-time only.** Set `FLOTTA_COST_PER_SECOND` or the column stays blank; token spend is never included. It is measured per *task*, not per box. See [What it costs](#what-it-costs).
- **Rotating a provider key needs a restart.** Fly secrets become environment variables when a
  machine *starts*, so a running box serves the old value until `flotta stop` + `flotta start`.
- **Boxes run an older Hermes than your orchestrator** — the image pin has drifted behind the
  released agent, and reconciling it is queued.

## How it fits together

| | |
|---|---|
| `src/flotta/store.py` | the fleet-state store — `boxes` / `workspaces` / `tasks`, SQLite, thin SQL, one validated transition table per tier |
| `src/flotta/provision.py` | create / stop / start / wake / teardown / reconcile — **runs where it can reach the substrate**, and is the store's only writer |
| `fly/` | the box image: `hermes serve` as PID 1, `/data/hermes` on a volume |
| `src/flotta/cli.py` | the Typer CLI |
| `src/flotta/db.py` | the engine seam — one store, SQLite or Postgres |
| `src/flotta/control/` | the control plane: the fleet API, and the reconcile loop on a timer |
| `dashboard/` | Next.js over the control-plane API — it does not open the store |

Storage is plain SQL behind a thin interface, so the engine is a startup decision rather than a
rewrite: SQLite by default, Postgres when `$FLOTTA_DATABASE_URL` is set.

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

The test suite is hermetic and free: every Fly touchpoint is injected, never called for real.

That is also why it can run on every pull request:
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs `just check` and the dashboard's
`tsc`/`eslint` as two separate jobs on each PR and each push to `main`. It needs no secrets and
touches no billed infrastructure — the recipes that spend money (`smoke`, `deploy`, `e2e`,
`e2e-live`, anything `fly-*`) are deliberately not part of it and stay a local, deliberate act.

## License

[AGPL-3.0](LICENSE) for the core. The network-use clause is deliberate — a hosted Flotta must share
its changes. Adapters and templates are permissively licensed so they can be embedded freely.
