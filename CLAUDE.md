# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This is the **implementation repo** for **Flotta** — an open-source fleet runtime for self-improving agents. This repo is `github.com/joaoh82/flotta`, a separate git repo nested inside the planning-workspace parent (see below).

> **The project has pivoted. `../docs/FLOTTA_PIVOT.md` supersedes the v0.1 architecture** described
> in `../docs/development-plan.md`. Where they conflict, the pivot doc wins — treat the older
> documents as historical rather than reconciling them. Read **both** of its §0 revision notes.
>
> **The one-sentence diagnosis:** v0.1 put the disposable thing in the cloud and kept the persistent
> thing (the agent's memory) on the laptop. The goal requires the opposite — your Hermes lives in
> the cloud permanently, with its memory intact, and you talk to it from a thin client.
>
> **Two tiers, two lifetimes** (this vocabulary is load-bearing; use it):
>
> | | Lifetime | Memory | Substrate |
> |---|---|---|---|
> | **Box** — an agent | Months | Durable `/data/hermes` | Machine + volume |
> | **Workspace** — where code runs | Hours | **None** — the box remembers | Machine, no volume |
>
> There was a third — stateless Modal **shards** — **cut 2026-08-26**. Modal has left the codebase
> entirely; `src/flotta/worker/` and `flotta spawn` are gone with it. Fan-out belongs to M7, between
> persistent agents. Anything below still describing three tiers is stale, not subtle.
>
> **The pivot is delivered.** A box runs `hermes serve` as PID 1 with `HERMES_HOME` on a Fly volume,
> the control plane owns fleet state, and `flotta chat <box>` reaches an agent through
> `<box>.flotta.dev` from an empty directory — no `flyctl`, no tunnel, no fleet database. Boxes hold
> a real toolchain and clone private repositories while holding **no GitHub credential**.
>
> **This file does not track milestone status** — see *Where things stand*. The parent
> `CLAUDE.md` carries the current state and what is next.

## Parent project

**Parent project:** [flotta](/Users/joaoh82/projects/flotta_parent/CLAUDE.md) — read for shared dev commands, cross-service context, **and the global working guidelines** (never-touch-`main`, task management via marvinapp, reserved ports, engineering defaults). Those guideline sections live only in the parent `CLAUDE.md` — this file does not duplicate them.

### How I fit in

The parent (`/Users/joaoh82/projects/flotta_parent`) is a **planning-only workspace** holding the product and execution docs; it has no build/test tooling. This nested `flotta/` directory is the actual code. The two are **independent git repos** — confirm which one you're in before any git operation (the parent gitignores this directory). The authoritative planning docs live in the parent's `docs/` (see *Source of truth* below); this repo holds the implementation those docs describe.

*(No sibling table was auto-generated: the parent has a single implementation child and no sibling table or dedicated architecture section in its `CLAUDE.md`.)*

## Where things stand

**This file does not track milestone status, deliberately.** It described the
repo as of M4 while three milestones had landed, and had to be rewritten twice
in one session — because duplicating a status that lives elsewhere goes stale
by construction, and a confidently wrong file is worse than no file. Reading
the stale version already caused one wrong turn (see the `vendor/` caveat
below).

So: **`../docs/FLOTTA_PIVOT.md` is the authority on what is being built, and the
parent `CLAUDE.md` on what is done and what is next.** Read both at session
start. `../docs/development-plan.md` is v0.1's plan — historical, and its M0–M7
are *not* the pivot's M0–M7, which is the single easiest thing to confuse here.
It remains the home of the decision log.

This file covers what does not change between milestones — architecture,
conventions, and the sharp edges below.

### Sharp edges that outlive any milestone

- **`just` only auto-loads a file named exactly `.env`.** A `.env.local` is
  silently ignored — it looks configured and does nothing.
- **Secrets reach a box only through `flyctl secrets`, and only at boot.** Four
  channels could carry something to a machine and three of them do not: the
  `[env]` block in `fly/fly.toml` names three variables, `BoxSpec.env` is empty
  (`provision.create_box` builds `BoxSpec(name=name)`), and the image bakes in
  nothing. So a variable set in your `.env` does **not** reach a box unless
  something puts it there — `$FLOTTA_DOMAIN` was documented as reaching one for
  a whole PR and never did. `just fly-secrets` carries the provider vars, `just
  fly-auth` the dashboard credentials. Setting a secret restarts the machine,
  which is how a new value takes effect: the entrypoint reads them at boot.
- **A box's own identity is injected at creation**, not afterwards.
  `create_box` mints `box:<id>` and hands it to the backend in `BoxSpec` —
  `env` for what may be read off the machine (`FLOTTA_BOX_ID`,
  `FLOTTA_BOX_NAME`, the control URL, the commit domain) and `secrets` for what
  may not (`FLOTTA_BOX_TOKEN` alone). On Fly the secrets must be written
  **before** `machine run`, because a machine takes the app's secrets when it
  is created — which is why they travel in the spec rather than through a
  `set_secrets()` verb the caller would have to sequence. `just box-identity`
  is now rotation only.
- **`--wait` or the row strands.** The *local* process records a task's
  outcome, not the container. Without `--wait` (and without a later `flotta
  watch`), the container finishes and the row sits at `running` forever.
  `flotta reconcile` sweeps those up.
- **A box is not a task, and the store enforces it.** Three status vocabularies
  with three transition tables: a box cannot be `done`, a task cannot be
  `stopped` or `torn_down`. One shared set would wave through exactly the
  transitions the table exists to reject. The *code* is shared
  (`_check_transition(kind, ...)`); the *words* are not.
- **`stopped` is not terminal, and that distinction is the product.** A stopped
  box costs disk and no CPU, still exists, and is expected to come back. v0.1's
  single `LIVE` set could not express it, so the store now separates "can still
  transition" from "burning CPU" (`BOX_ACTIVE`). Anything that filters the fleet
  must keep showing stopped boxes — hiding an idle fleet hides the point of it.
- **The concurrency cap is on tasks, not boxes.** v0.1 capped worker creation at
  1; carrying that to boxes would cap the fleet at one machine when the whole
  arithmetic assumes tens. A live task is what burns CPU, so that is what is
  rationed. `create_workspace(max_live=...)` is the same guard, wired and tested
  ahead of the tier that will use it.
- **The fleet store runs on SQLite or Postgres**, chosen by the value it is
  given: a `postgres://` URL selects Postgres, anything else is a SQLite path.
  `$FLOTTA_DATABASE_URL` is the switch (the same variable §8.3's Railway
  template wires). SQLite stays the default so `just check` needs no server.
- **Every transaction in the store is a read-check-write and must be guarded.**
  `transaction(guard="tasks")` is `BEGIN IMMEDIATE` on SQLite and
  `LOCK TABLE … IN SHARE ROW EXCLUSIVE MODE` on Postgres. An unguarded
  transaction silently drops to an optimistic `BEGIN` and the concurrency caps
  become racy — and **the hermetic suite cannot see it**: three transactions
  lost their guard during the migration and all 453 tests still passed.
  `test_every_read_check_write_is_guarded` reads the source and insists;
  `test_concurrent_creates_cannot_both_win` (Postgres only) is the one that
  actually races two connections.
- **Postgres has no `lastrowid`.** Inserts that need the generated id go
  through `execute_returning_id` (`RETURNING id`), not `cursor.lastrowid`.
- **§M4/D3 says "the connection factory is already isolated". It was not** —
  the store called `sqlite3.connect` directly with inline PRAGMAs. M4 built
  that seam rather than swapping something behind one.
- **No store migration; delete the old file.** The schema is created with
  `CREATE TABLE IF NOT EXISTS`, so a pre-M0 `fleet.db` is not rejected — it
  quietly gains empty new tables beside the stale `workers` one and renders as
  an empty fleet. The same property has a useful half: a new **table** appears
  on an existing store by itself (this is how `box_repos` shipped with no
  migration), while a new **column** does not — verified, not assumed.
  `rm -f fleet.db fleet.db-wal fleet.db-shm`. Old rows are dropped rather than migrated
  on purpose: they describe one-shot task runs and there is no box for them to
  become.
- **A headless `run_conversation` does NOT populate `state.db`.** SEAM_NOTES Q3
  describes a rich schema (sessions, messages, FTS); measured on a live box,
  after a completed turn `state.db` holds exactly one table
  (`async_delegations`, zero rows). Those tables are written by the
  gateway/CLI path, not by `AIAgent.run_conversation`. So conversation
  *history* does not survive that path — **memories and skills do**, and that is
  the surface the pivot's claim actually rests on. **M3 changed this for boxes:**
  `hermes serve` brings up all 22 tables, so a box's conversation history now
  survives a restart. The note is kept because the distinction still bites —
  the headless path writes one table, the serving path writes the schema.
- **The memory tool must be named in the prompt.** "Remember X" is treated as
  conversational and writes nothing; the box then honestly reports an empty
  memory store on recall, which looks exactly like a durability failure and is
  not one. "Use your memory tool to save this fact permanently: X" writes
  `$HERMES_HOME/memories/MEMORY.md`.
- **Memory accumulates across runs, so test questions must be run-scoped.** The
  M2 proof once passed by answering with a passphrase from a *previous* cycle.
  That is the product working correctly and the test being wrong — the fix is a
  question only this run can answer, never wiping the store between runs.
- **A box's memory outlives its machine.** Verified: the machine was destroyed
  outright, `create_box` provisioned a new one, and `/data/hermes` came back
  byte-identical (same SHA-256 on `memories/MEMORY.md`). The volume is the box;
  the machine is replaceable. That is also the "fork a box" primitive §M2
  gestures at, already working.
- **`fly image show` returns `null` when an app has no machines.** It derives
  the image *from a machine*, which is exactly the moment `create` needs one.
  Read `flyctl releases` (`ImageRef`, newest complete release) instead — an
  image belongs to the app's release history, not to any machine.
- **`create` never builds an image.** Building is a fleet operation — build
  once, create many boxes — so `BoxSpec.image` names an existing one and
  `just fly-up` owns producing it. The same split will hold for a Firecracker
  rootfs.
- **aiohttp will not store cookies for an IP host** unless the jar is built
  with `unsafe=True`. A tunnel is always `127.0.0.1`, so without it the login
  returns 200, the cookie is dropped, and the next call is anonymous — which
  surfaces as a bare 401 from `/api/auth/ws-ticket` and reads as bad
  credentials rather than a cookie-jar policy.
- **The box's agent protocol is JSON-RPC 2.0 over `/api/ws`.** The server
  speaks first (`gateway.ready`), then `session.create` -> `{session_id, info}`
  and `prompt.submit` `{session_id, text}`. **The reply is an *event*
  (`message.complete`), not the RPC result** — the `prompt.submit` response only
  acknowledges the submit. `message.complete` carries the whole text, so
  reassembling `message.delta` is a rendering choice, not a requirement.
- **`hermes serve` does not read the `FLOTTA_*` provider vars.** The Tier 3
  one-shot path passes base_url/api_key straight to `AIAgent`; the gateway
  resolves a provider through Hermes's own config instead, so a box with only
  `FLOTTA_*` set answers every turn with "No inference provider configured".
  `just fly-secrets` sets both vocabularies — `OPENROUTER_API_KEY` for
  OpenRouter, `OPENAI_*` otherwise. Found by sending a real turn; no test
  could have caught it.
- **A provider failure arrives as a normal `message.complete`** whose text is
  an error string, with `status != "complete"`. Returning it as the reply would
  print "No inference provider configured" as though the agent had said it.
- **The box's auth flow** is `POST /auth/password-login` ({provider, username,
  password}) for session cookies, then `POST /api/auth/ws-ticket` for a
  single-use 30s ticket, then `WS /api/ws?ticket=...`. The ticket exists
  because a browser cannot set headers on a WebSocket — mint one per
  connection rather than caching it.
- **Fly's internal DNS only resolves *running* machines.** Addressing a
  stopped box fails with "host was not found in DNS", which reads as a bad
  address rather than a sleeping agent. Anything that addresses a box must wake
  it first — `provision.wake_box`, which `flotta chat` calls.
- **`wake_box` is not `start_box`.** `start_box` is the operator's verb and
  refuses anything that is not `stopped`, because asking to start a
  mid-provision box is a mistake worth reporting. `wake_box` is the
  *addressing* path: it does not care what state the box was in, and it
  reconciles a row that disagrees with the substrate (Fly can stop a machine on
  its own during a host drain). §M7: "delegation wakes a stopped box; it does
  not create one".
- **Credentials and the app name live in the MAIN checkout's `.env`.** It is
  gitignored, so a worktree copy dies with the branch — the Fly app name was
  lost that way once, and `FLOTTA_BOX_PASSWORD` a second time. `just fly-auth`
  re-mints.
- **Fly's private network is IPv6-only — bind `::`, never `0.0.0.0`.** An
  IPv4-only bind is invisible to `flyctl proxy`, which dials the machine's
  `fdaa:` address: the connection resets and nothing in the logs explains it.
  Diagnosed by reading `/proc/net/tcp6` on the box, where the only IPv6
  listener was port 22. `::` binds IPv6 and, with Linux dual-stack, accepts
  IPv4 too. The same trap catches health checks: probe **both** address
  families or a perfectly healthy box reports as down.
- **Do not build shell incantations for `fly ssh` — ship a command.** Checking
  a box's state through `flyctl ssh console -C "..."` wrapping `/bin/sh -c
  '...'` wrapping `python3 -c "..."` is three layers of quoting, and one
  attempt ended up spelling a path as `chr(47)+chr(100)+...` to escape them.
  `python3 -m flotta.box.doctor` (`just fly-doctor`) has no quoting problem and
  reports the things worth asking: HERMES_HOME on the volume, the session
  schema, memories, skills, whether Hermes is listening.
- **`flyctl machines start` needs an id.** A bare `--app X` fails with "a
  machine ID must be specified when not running interactively". Swallowing that
  with `|| true` is worse than not trying: the failure resurfaces as `fly ssh`'s
  "app has no started VMs. It may be unhealthy or not have been deployed yet",
  which sends you to check deploys and health for a box that is merely asleep.
- **`started` is not `ready`.** A machine reaching `started` says nothing about
  Hermes, which imports the agent first (~6s). Any check run straight after a
  start needs a bounded wait — `flotta.box.doctor --wait-s`.
- **`fly ssh` needs a running machine.** A stopped box gives a connection
  error, not "the box is stopped" — `just fly-doctor` starts it first.
- **`flyctl ssh console -C` does not run a shell.** It execs the string as
  argv, so `echo a; cat b` runs `echo` with the literal arguments `a;`, `cat`,
  `b` — no error, just quietly wrong output. `FlyBackend.exec` wraps commands
  in `/bin/sh -c` for this reason. Heredocs are still mangled in transit
  (base64 them), and a backgrounded process does not survive the session.
- **Suspend is not faster than stop — it keeps memory.** Measured, 3 runs
  each: cold stop reaches `started` in ~0.31s, suspend in ~0.43s. What suspend
  buys is the VM's RAM (uptime 44.4s -> 53.5s, versus 72.1s -> 6.7s cold).
  §8.4 argues for suspend on latency grounds and that part is wrong; the
  state-preservation reason is right and is why `prefer_suspend` is the
  default. It is worth nothing while PID 1 is `sleep infinity`, and everything
  once a box runs Hermes as a service.
- **The Fly app name belongs in the main checkout's `.env`, not a worktree's.**
  It is gitignored, so a worktree copy dies with the branch — and the fleet's
  address goes with it. `just fly-whoami` shows what is resolved.
- **`stop` is refused while a box has live tasks.** Until a backend can really
  suspend, stopping changes a row and nothing else — the container keeps
  running and keeps billing while `count_active_boxes()` reports zero. A
  `stop` that does not stop spend is a money footgun with a reassuring name.
  When M2 lands real suspend this becomes a decision (snapshot mid-task)
  rather than a refusal.
- **Only `running` can stop; only `stopped` can start.** `provisioning ->
  running` is legal in the store (it is how `spawn_box` records a launch), so
  an unguarded `start_box` would wake a box mid-spawn into `running` with no
  endpoint — the wake/create collapse the pivot doc warns about — and then make
  `spawn_box`'s own transition illegal, stranding a billed container. Both
  functions refuse with `ProvisionError` (CLI exit 2).
- **Check transition legality *before* writing the event.** `add_event` then
  `update_*_status` looks harmless and is not: an illegal transition leaves a
  committed event describing something that never happened, then raises. Events
  are the audit trail; a lie in them outlives the traceback.
- **Tasks have two clocks.** `created_at` (insert, NOT NULL) is when the work
  was asked for; `started_at` (nullable, stamped on `pending -> running`) is
  when something began doing it. Anything measuring runtime or deadlines —
  `billable_seconds`, `overdue_tasks`, the CLI's duration column — must use
  `started_at`, and must treat NULL as "never ran". Using the insert clock
  bills a task for waiting on a sleeping box and reconciles it to `failed` for
  having waited, which is the opposite of what a sleeping fleet is for.
- **Hermes is pinned and bumped deliberately** (D13). `just hermes-check`
  reports pinned vs. latest vs. your local install; `just hermes-bump REF`
  moves it and re-verifies live. Tracking `@main` does not work: a builder caches
  layers by the build definition, so a floating ref silently serves whatever
  Hermes was current on the first build. `HERMES_REF` lives in
  `src/flotta/box/image.py` and is the single bump point — it moved there when
  the shard tier was cut, and three justfile recipes kept importing the old
  path while 419 tests passed, which is why `test_recipes.py` exists.
- **Port 3000 is reserved** for another local service; the dashboard is 3001,
  baked into `dashboard/package.json` rather than passed as a flag.
- **The recipes that cost money are the Fly ones**, and they are never run in
  CI: `fly-up` / `fly-cycle` / `fly-proof` / `fly-down`, `door-deploy`,
  `door-secrets`, `fly-auth`, `fly-secrets`, `box-identity`. A cold image build
  is the largest single cost in the repo.
- **Run `just check` in the main checkout, not only in a worktree.** It loads
  `.env`, and code under test may re-read that file at run time — the CLI does,
  in its root callback. A suite that is green in a fresh worktree and red for
  anyone who has deployed has now happened twice: fifteen control-plane tests
  returning 401, and later the `token box` tests.
- **The suite pins decisions; running things finds bugs.** Nearly every real
  defect in M5b and after was found by executing something, not by a test: the
  door bracketing hostnames as IPv6 (every proxied request would have failed),
  a control-plane image with no `flyctl`, `just box-identity` reading a local
  store a deployed fleet does not use, and a box committing under an address
  that belonged to a real stranger's GitHub account. Write the guard *and*
  verify it by reintroducing the bug.

## Source of truth

The living planning docs live in the **parent** repo (`../docs/`):

- **`FLOTTA_PIVOT.md`** — **the authority.** The architecture, the two tiers, milestones M0–M10, the infra milestones in §8.6. **Read both §0 revision notes first** (2026-08-26: shard tier cut, app as M8, shared memory as M9; 2026-09-01: M5b shipped, boxes do engineering work, FLOTTA-21/22 named).
- **`../CLAUDE.md`** (the parent) — current state, what is next, and the global working guidelines this file does not duplicate.
- **`development-plan.md`** — v0.1's execution plan. Historical, **and its milestone numbers are not the pivot's**. Still the home of the decision log (D1–D14); still worth a changelog line.
- **`fleet-runtime-product.md`** — the product doc. Its scope section describes v0.1 and is superseded.
- **`SEAM_NOTES.md`** — seam findings with file:line refs into the Hermes clone. The storage-layout and headless-boot notes still hold; the `mcp_serve` material is superseded twice over (by D7/D9, then by the move to `hermes serve`). **Caveat:** `vendor/` is a snapshot at the *pinned* ref and your installed Hermes is usually newer, so it is not a reliable guide to the local agent's behaviour — that already caused one wrong turn. For the agent you are running, read `~/.hermes/hermes-agent/`; `just hermes-check` shows the drift.

In **this** repo:

- **`docs/DEPLOY.md`** — the deployment runbook: Postgres, control plane, box image, front door, DNS, and §7 giving an agent a GitHub identity.
- **`vendor/hermes/`** (gitignored) — read-only reference clone at the pinned ref. **Never modify it**, and see the caveat above.

## Architecture

One durable store is the single source of truth for fleet state; everything else reads from or writes to it. The laptop is a client — `src/flotta/client.py` has a test asserting it never imports an agent or a model client, because if it does, the laptop has started thinking again and the pivot has reversed.

- **`src/flotta/store.py`** — fleet state, thin SQL and no ORM (D8). **Four tables plus events:**
  - `boxes` (id, **name** — unique, it is the address — status `provisioning|running|stopped|torn_down`, endpoint, created_at, destroyed_at). No `failed`: machines get destroyed, they do not fail, so a dead provision goes straight to `torn_down` with the reason in its event.
  - `workspaces` (id, box_id, status, endpoint, repo, …) — schema only until the workspace tier (M6) is built.
  - `tasks` (id, box_id, **nullable** workspace_id, prompt, status `pending|running|done|failed`, started_at, finished_at, result_json, cost_estimate). `done`/`failed` live here, on the work, not the machine. No `torn_down`: interrupted work did not happen, and `failed` says so.
  - `box_repos` (box_id, repo) — which repositories a box may use, normalised to lowercase `owner/name` by `normalise_repo` so a slug, an https URL and an ssh URL are one grant rather than three.
  - `events` (id, **entity_kind + entity_id**, ts, type, payload_json) — polymorphic, so one query serves every tier. SQLite cannot foreign-key a polymorphic column, so `add_event` checks existence itself.
  - **Three status vocabularies, one shared validator** (`_check_transition(kind, …)`). A single status set would permit `stopped → done` on a task.
  - `cost_estimate` is on **tasks**, not boxes: `billable_seconds` measures start-to-verdict, which is meaningless across a machine that spans months.
- **`src/flotta/db.py`** — the engine seam: SQLite or Postgres behind one `Connection` protocol, chosen by the value given (`postgres://…` selects Postgres, anything else is a file path). The load-bearing difference is the transaction — see the sharp edge on guards above.
- **`src/flotta/backend.py`** + **`src/flotta/backends/`** — `create/start/suspend/stop/destroy/exec/state/endpoint`, routed on the endpoint scheme (`fly://app/machine`). `FlyBackend` is the only implementation; Hetzner + Firecracker is the intended second. **`create` never builds an image** — building is a fleet operation (build once, create many), so `BoxSpec.image` names an existing one and `just fly-up` owns producing it.
- **`src/flotta/provision.py`** — split by *where code runs* (D10). `create_box`, `spawn_box`, `watch_task`, `stop_box` / `start_box` (operator verbs, state-guarded), `wake_box` (the *addressing* path — Fly's DNS resolves only running machines), `teardown_box`, `reconcile`, and the idle sweep (`idle_boxes`, `sleep_idle_boxes`). Fleet state is written only by code that can reach the substrate, never by the box itself: a machine that dies mid-task writes nothing, so something that outlives it owns the verdict.
- **`src/flotta/control/`** — the control plane (M4.5), which is what runs on Railway. `app.py` serves the fleet API (`/api/boxes`, its events, repo grants, `git-credential`, `wake`); `loop.py` runs `reconcile` on a timer. `/health` reports whether the loop is **sweeping**, not whether the process is up. Refuses a non-loopback bind while there is no auth.
- **`src/flotta/auth.py`** — scoped, expiring signed-JSON tokens (M5a). Five scopes: `fleet:read`, `fleet:write`, `box:destroy`, `box:chat`, `git:credential`. Revocation is a **key rotation**, so mint short ones. Subjects beginning `box:` are confined to their own box — that check is what makes it safe to put a token on a machine whose agent has root.
- **`src/flotta/door/`** — the front door (M5b). It **cannot** be a reverse proxy: a request normally arrives while the box is asleep, so it must resolve → wake → wait-for-ready → proxy, including the WS upgrade. A separate Fly app, because reaching a box needs 6PN and pinning the control plane to Fly would break the Railway self-host recipe.
- **`src/flotta/box/`** — code that runs *on* a box: `git_credential.py` (the credential helper git invokes), `doctor.py` (`just fly-doctor`), `image.py` (the `HERMES_REF` pin), `run.py`.
- **`src/flotta/cli.py`** — Typer CLI wired as a console entry point, so `uv tool install .` puts a bare `flotta` on PATH. `ps`, `create`, `chat`, `logs`, `watch`, `stop`, `start`, `kill`, `serve`, `door`, `reconcile`, plus the `repo` and `token` groups. Every command takes `--json`; tables are hand-rolled so the tested surface is a pure `str`-in/`str`-out layer. **Store resolution:** `--store` → `$FLOTTA_STORE` → `./fleet.db`. **`repo` and `token box` go through the control-plane API when `$FLOTTA_CONTROL_URL` is set** — a store-first command cannot see a deployed fleet, and the alternative is putting the production database URL on a laptop. Exit codes carry meaning: `1` a failure, `2` a refusal.
- **`src/flotta/client.py`** — the thin client half of the inversion. Speaks Hermes's JSON-RPC over `/api/ws`; see the sharp edges on the auth flow and on `message.complete`.
- **`fly/`** — the box image (`Dockerfile`, `box_entrypoint.sh`, `gh_shim.sh`, `fly.toml`) and `fly/door/`. The box binds `::`; the **door** binds `0.0.0.0`, because Fly's proxy reaches it over IPv4.
- **`dashboard/`** — Next.js over the **control-plane API**, never the store directly. Its route and component names still say "worker"; cosmetic, queued.
- **`skills/orchestrator/` — deleted (M3).** A cloud box's Hermes *is* the orchestrator; a skill teaching it to delegate to itself is dead weight.

**Data flow:** `flotta chat <box>` → the door at `<box>.flotta.dev` → asks the control plane to resolve and wake → proxies to `hermes serve` on the box → the agent thinks, with its memory on `/data` and its work in `/workspace` → events land in the store against the box → the reconcile loop sweeps stranded tasks and sleeps idle boxes.

**GitHub identity:** a box holds no GitHub credential. `git`'s `credential.helper=flotta` runs `git-credential-flotta`, which posts the repository to the control plane with the box's `git:credential` token and gets a credential back — per repository, per invocation, never written down. Grants live in `box_repos`, so revoking one takes effect on the next fetch with no restart. **Flotta enforces the grant; GitHub does not** — the source is one fleet token, so this is policy, not enforcement, until installation tokens scoped to `repository_ids` replace it (FLOTTA-22).

## Conventions

- **Python 3.11**, type hints everywhere, **ruff** for lint + format.
- Tests with **pytest** next to the code (`test_*.py`). Every `store`/`provision` change needs a test; validate status transitions and listing filters explicitly. **Keep the suite hermetic and $0** — every Fly touchpoint is injected, never called for real, so `just check` needs no network, no credentials and no `flyctl`. It is the gate and must be green before committing; `.github/workflows/ci.yml` runs it on every PR and every push to `main`, so a green suite is verified on the commit rather than asserted in the PR description. Keep the workflow free of secrets and of any recipe that touches real Fly.
- **One commit per completed task**, message prefixed with the task ID — e.g. `M3.2: spawn_box writes lifecycle events`.
- Dashboard: TypeScript, **no UI library beyond Tailwind**, keep it boring. Node version is pinned in `dashboard/.nvmrc` (24) so nvm/fnm and CI agree. **Regenerate `package-lock.json` on Linux**, not on the Mac: npm writes a lockfile for the platform it ran on, and a macOS-generated one omits `@emnapi/core`/`@emnapi/runtime` (optional peers reached through `@tailwindcss/oxide-wasm32-wasi`), which makes `npm ci` fail on CI with "Missing ... from lock file". `docker run --rm -v "$PWD:/w" -w /w node:24-bookworm npm install --package-lock-only` produces one that installs on both.
- Secrets only via Fly secrets, Railway variables and `.env` (gitignored) — never hardcode. **Check `.dockerignore` before adding a build context**: `.env` once shipped to Fly's builder because none existed.
- Use **plan mode first** for anything non-trivial; keep session scopes small (≈ one milestone task cluster).

## Commands

Common commands live in the **`justfile`** (`just` alone lists them). Python tooling runs through **uv** (`pyproject.toml` + `uv.lock`). **Convention: whenever a milestone lands a runnable command, add a recipe in the same PR.**

```bash
just check                  # lint + tests — run before committing
just check-dashboard        # tsc + eslint (the Python `check` does not cover the dashboard)
just ci                     # check + check-dashboard — exactly what GitHub Actions runs
just test-one <keyword>     # a single test (uv run pytest -k <keyword>)
just test-postgres          # the store suite against a real Postgres (throwaway container)
just fmt                    # ruff format + --fix
just serve                  # the control plane — fleet API + reconcile loop, 127.0.0.1:8080
just door                   # the front door, 127.0.0.1:8081
just dashboard              # the fleet view on http://localhost:3001 (needs `just serve`)
just deploy-config          # every secret a first deployment needs — see docs/DEPLOY.md
just hermes-check           # Hermes version drift: pinned vs. latest vs. your local install
just fly-doctor             # what is true about a live box (starts it first)
```

**Run `just check` in the main checkout, not only in a worktree** — it loads `.env`, and code under test may re-read that file at run time.

**Recipes that touch real infrastructure cost money** and are never run in CI: `fly-up`, `fly-cycle`, `fly-proof`, `fly-down`, `fly-auth`, `fly-secrets`, `box-identity`, `door-deploy`, `door-secrets`.

The dashboard reads the control-plane API, not the store — `just dashboard` alone answers 503 on every page. Run both. It is on **port 3001**, never 3000 (reserved; see parent `CLAUDE.md`), baked into `dashboard/package.json` rather than passed as a flag, because a flag is too easy to forget.

**Run the CLI directly, never through `just`** — there is deliberately no `just flotta` recipe, because just's variadic arguments get re-split by the shell and break on exactly the quoted-message case the CLI exists for:

```bash
uv run flotta ps
uv run flotta create eng-b                      # a persistent agent with its own memory
uv run flotta chat eng-a "what did you learn?"  # through the door; needs $FLOTTA_TOKEN
uv run flotta repo grant eng-a owner/name       # which repositories that agent may use
uv run flotta reconcile                         # rescue tasks stranded past their deadline
```

## Knowledge Base

This project shares its knowledge base with its parent. Do **not** create a separate folder for this repo — entries about it go in the parent's.

Knowledge lives in the **`projects-knowledge` Obsidian vault** at `~/Documents/projects-knowledge/Projects/flotta/`. It is a plain synced folder: no git, nothing to clone, pull, commit or push — just read and write the files. *(Older references to a `joaoh82/projects-knowledge` git repo at `~/projects/projects-knowledge` are retired; that path no longer exists.)*

- **Context (read first):** `~/Documents/projects-knowledge/Projects/flotta/context.md` — stable background: product goals, domain, stakeholders. Update only when the underlying facts change.
- **Notes (running journal):** `.../notes.md` — append-only, dated `## YYYY-MM-DD` headings, for decisions, blockers, incidents.
- **Wiki:** `.../wiki/` — reference sub-docs (architecture, local dev setup, credential locations). Create files as topics emerge.

If `~/Documents/projects-knowledge/` does not exist (cloud sandbox, CI), there is no knowledge base in that environment — skip it silently rather than reconstructing it from a git remote.
