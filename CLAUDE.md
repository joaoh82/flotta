# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This is the **implementation repo** for **Flotta** — an open-source fleet runtime for self-improving agents. This repo is `github.com/joaoh82/flotta`, a separate git repo nested inside the planning-workspace parent (see below).

> **The project has pivoted. `../docs/FLOTTA_PIVOT.md` supersedes the v0.1 architecture** described
> in the parent `CLAUDE.md` and in `../docs/development-plan.md`. Where they conflict, the pivot doc
> wins — treat the older documents as historical rather than reconciling them.
>
> **The one-sentence diagnosis:** v0.1 put the disposable thing in the cloud and kept the persistent
> thing (the agent's memory) on the laptop. The goal requires the opposite — your Hermes lives in
> the cloud permanently, with its memory intact, and you talk to it from a thin client.
>
> **Three tiers, three lifetimes** (this vocabulary is load-bearing; use it):
>
> | | Lifetime | Memory | Substrate |
> |---|---|---|---|
> | **Box** — an agent | Months | Durable `/data/hermes` | Machine + volume |
> | **Workspace** — where code runs | Hours | **None** — the box remembers | Machine, no volume |
> | **Shard** — one-shot fan-out | Seconds | None, correctly | **Modal** |
>
> **M0 has landed** (the rename + the three-table split). Everything below the store is still v0.1
> Modal one-shots: `stop`/`start` write transitions with no infrastructure behind them, and
> `HERMES_HOME` is still ephemeral, so **a box does not yet remember anything**. Durable memory
> across a stop/start cycle is the next milestone and the project's whole thesis.

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

So: **`../docs/development-plan.md` is the single source for what is done and
what is next.** Read it at session start. This file covers what does not change
between milestones — architecture, conventions, and the sharp edges below.

### Sharp edges that outlive any milestone

- **`just` only auto-loads a file named exactly `.env`.** A `.env.local` is
  silently ignored — it looks configured and does nothing.
- **Provider credentials live in a named Modal secret** (`flotta-provider`),
  not in the deploying shell. `just secret-sync` rotates it; `just
  secret-ensure` (a `deploy` dependency) creates it empty so the provider-free
  dry-run path still works, and never overwrites. Rotation needs no redeploy —
  but it is not instant: a secret becomes environment variables when a
  container **starts**, so a warm container serves the old value until it
  scales down. `modal app stop flotta-provision -y` forces the turnover.
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
- **`run_worker` is deliberately not renamed.** It is the Tier 3 stateless
  one-shot living in `provision.py`; calling it a box would be wrong in the
  other direction. Its successor name is `run_shard`, and that belongs to the
  fan-out milestone.
- **Hermes is pinned and bumped deliberately** (D13). `just hermes-check`
  reports pinned vs. latest vs. your local install; `just hermes-bump REF`
  moves it and re-verifies live. Tracking `@main` does not work: Modal caches
  image layers by the build-definition string, so a floating ref silently
  serves whatever Hermes was current on the first build.
- **Port 3000 is reserved** for another local service; the dashboard is 3001,
  baked into `dashboard/package.json` rather than passed as a flag.
- **`just smoke` runs a real, billed container.** No model tokens, but Modal
  compute is real and a cold image build is the largest single cost in the repo.

## Source of truth

The living planning docs live in the **parent** repo (`/Users/joaoh82/projects/flotta_parent/docs/`):

- **`development-plan.md`** — milestones M0–M7 with acceptance criteria, open questions, decision log, changelog. **Read it at session start.** Before finishing a session: tick task statuses (`[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked-with-note), add a changelog line, and record any choice in the decision log.
- **`fleet-runtime-product.md`** — product scope. Do **not** exceed v0.1 scope.
- **`claude-code-kickoff.md`** — bootstrap steps + the eight paste-ready per-milestone session prompts (one per session). Reality diverging from this guide? The plan file wins. Known divergence: its §1 starter text and M2 prompt still say the worker "starts `mcp_serve`" — superseded by D7/SEAM_NOTES.
- **`SEAM_NOTES.md`** — M1 findings with file:line refs into the Hermes clone (headless boot recipe, `mcp_serve` correction, storage layout, terminal backends). **Caveat:** `vendor/` is a snapshot at the *pinned* ref and the Hermes installed on your machine is usually newer, so it is not a reliable guide to the local agent's behaviour — reading it for skill conventions already caused a wrong turn in M6. When a question is about the agent you are running, read `~/.hermes/hermes-agent/`; `just hermes-check` shows how far apart they are.
- **`vendor/hermes/`** (in this repo, gitignored) — read-only reference clone of Hermes Agent at the pinned ref. **Never modify it**, and see the caveat above before trusting it over the installed agent.

## Architecture

The runtime is built around one durable store that is the single source of truth for fleet state; everything else reads from or writes to it.

- **`src/flotta/store.py`** — Fleet-state store on SQLite via stdlib `sqlite3`, designed so the connection factory could later point at **Turso** (thin SQL, no ORM — D8). **Four tables, one per tier plus events:**
  - `boxes` (id, **name** — unique, it is the address — status `provisioning|running|stopped|torn_down`, endpoint, created_at, destroyed_at). No `failed`: machines get destroyed, they do not fail, so a dead provision goes straight to `torn_down` with the reason in its event.
  - `workspaces` (id, box_id, status `provisioning|running|torn_down`, endpoint, repo, created_at, destroyed_at) — schema only until the workspace tier is built.
  - `tasks` (id, box_id, **nullable** workspace_id, prompt, status `pending|running|done|failed`, started_at, finished_at, result_json, cost_estimate). `done`/`failed` live here, on the work, not the machine. No `torn_down`: interrupted work did not happen, and `failed` says so.
  - `events` (id, **entity_kind + entity_id**, ts, type, payload_json) — polymorphic, so one query serves all three tiers. SQLite cannot foreign-key a polymorphic column, so `add_event` checks existence itself and raises the same `UnknownEntityError` the driver used to.
  - **Shards get no table**, deliberately: they are Modal call ids owned by a workspace, and the aggregate lands on `tasks.result_json`. Resisting the urge to track them is the point.
  - `cost_estimate` is on **tasks**, not boxes, because `billable_seconds` measures start-to-verdict — meaningless across a machine that spans months. Boxes get one when there is a disk-cost model.
  - `create_task(max_live=N)` enforces the cap by counting and inserting inside one `BEGIN IMMEDIATE`; a check-then-insert would race and let two simultaneous spawns both through. `get_box_timeline(box_id)` returns the box's events plus its tasks' and workspaces', in order.
- **`src/flotta/provision.py`** — Split by *where code runs* (D10): `run_worker` is the deployed Modal function (does the work, never touches the store); `spawn_box(task) -> {box_id, task_id, endpoint}`, `watch_task(id)`, `stop_box(id)` / `start_box(id)` (store-side only until a persistent backend lands) and `teardown_box(id)` (idempotent) run **locally** beside the store file and are its only writers. One spawn now creates **two rows**: the box owns the endpoint and the machine lifecycle, the task owns the verdict. `teardown_box` marks the box's live tasks **failed** — not torn down — and closes its workspaces. The store is a local SQLite file, unreachable from a container — hence a watcher, not entrypoint self-reporting. The stored `endpoint` is the Modal call handle `modal://flotta-provision/run_worker/<fc_id>` — **not a URL**: v0.1 workers are one-shot, so nothing is listening, which is why the M6 skill drives the CLI rather than registering an MCP tool (D12). `reconcile()` resolves **tasks** stranded past their own `timeout_s` — it re-attaches and *recovers the real result* where possible (a 9-hour-old call result was still retrievable) and marks the rest `failed`, **never `completed`**; `resolve_max_concurrent()` is the cap policy (explicit → `$FLOTTA_MAX_CONCURRENT` → **1**, `0` = unlimited); and credentials come from the named secret rather than the deploying shell.
- **`src/flotta/worker/`** — `image.py` holds the one shared `modal.Image` (Hermes pinned via the `HERMES_REF` constant — the single bump point) so `flotta-worker` and `flotta-provision` can share it without importing each other's `modal.App`. `modal_app.py` is the smoke-test app, `config.py` the `WorkerConfig.from_env` contract, `server.py` the Flotta MCP server (`health` + `run_task`) behind **pure-ASGI** bearer auth — a Starlette `BaseHTTPMiddleware` would buffer and break MCP's SSE stream (D9). The container entrypoint reads `FLOTTA_TASK` / `FLOTTA_TIMEOUT_S` from env, sets `HERMES_HOME` to a writable ephemeral path, boots Hermes **headless** via `AIAgent` (no messaging gateway, single pinned provider, fixed toolset, `skip_context_files`/`skip_memory` — per SEAM_NOTES Q1), and exits on completion or a hard timeout (default 900s). **Not `hermes mcp serve`** — that is a stdio messaging bridge, not a task endpoint (D7, SEAM_NOTES Q2). The MCP surface, if used over the one-shot form, is a thin Flotta-owned streamable-http server exposing a `run_task` tool; Hermes's MCP client can already dial it by URL.
- **`src/flotta/cli.py`** — Typer CLI wired as a console entry point, so `uv tool install .` puts a bare `flotta` on PATH — `uv run` is the contributor path, not the user path. **Eight** commands: `ps` (**lists boxes**; `--tasks` lists the work), `spawn "<task>"` (`--wait`, `--dry-run`, `--timeout-s`, `--max-concurrent`, `--name`), `watch <id>` (a task id, or a box id/name to watch its live task), `logs <box>` (the cross-tier timeline), `stop <box>`, `start <box>`, `kill <box>` (idempotent), and `reconcile`. `watch` exists because `spawn` returns immediately by design — without it the CLI could start and kill a box but never observe a task *succeed*. Every command takes `--json`; the tested surface is a pure `str`-in/`str`-out formatting layer, so tables are hand-rolled rather than pulled from a rendering library. **Store resolution:** `--store` → `$FLOTTA_STORE` → `./fleet.db` (the same variable the dashboard reads); reads require the store to exist, and only `spawn` may create it. **Modal workspace resolution:** `$MODAL_PROFILE` → `$FLOTTA_MODAL_PROFILE` → `.env` → Modal's active profile, applied *before* `provision` is imported (`modal` reads its config at import time) — the installed binary has no justfile pinning it, so without this an unrelated `modal profile activate` would silently redirect `spawn`. `ps` and `logs` are pure store reads needing no Modal credentials; **`reconcile` is deliberately not folded into `ps`** so that stays true. Exit codes carry meaning: `1` a failed task, `2` a refusal (cap reached, or a missing store).


- **`dashboard/`** — Next.js 16 (App Router, TypeScript, Tailwind 4) reading the same store through Node's built-in **`node:sqlite`** — no native module and no database dependency. Connections are `readOnly` and opened **per request**: under WAL a long-lived reader pins an old snapshot and would quietly serve stale rows to a polling UI. A missing store is a **503 naming the path**, never an empty fleet. Localhost only, no auth, port **3001**. Read-only except the kill button, which shells out to the CLI (D11) and checks the *cancel outcome*, not just the HTTP status.


- **`skills/orchestrator/`** — A Hermes skill teaching the orchestrator to delegate, installed with `just install-skill` (symlink, so repo edits take effect without a reinstall). Written to the installed Hermes's own conventions — `description` ≤ 60 chars is a hardline with an enforcement test in its repo. **It competes with Hermes's built-in `delegate_task` in the always-on skill index**, so it leads with the property that actually distinguishes Flotta: **isolation**, not durability. (Durability was the first draft's claim; a live routing test showed a one-shot `cronjob` covers that too, and the model correctly chose `cronjob` over Flotta — D12.)

Data flow: orchestrator → `spawn_box` (Modal) → the container boots headless Hermes (`AIAgent`), runs the task, reports the result → events land in the store against the box and the task → CLI/dashboard read the store → `teardown_box` closes the machine and fails anything still running on it.

**Dashboard note:** `dashboard/lib/store.ts` holds the only SQL in the dashboard and absorbs the split — a row is a **box**, with its newest task's prompt and its total task spend joined in. The components still say "worker"; that vocabulary catches up when the dashboard moves off direct SQLite reads and onto a control-plane API.

## Conventions

- **Python 3.11**, type hints everywhere, **ruff** for lint + format.
- Tests with **pytest** next to the code (`test_*.py`). Every `store`/`provision` change needs a test; validate status transitions and listing filters explicitly. **Keep the suite hermetic and $0** — every Modal touchpoint is injected, never called for real. `just check` is the gate; it must be green before committing.
- **One commit per completed task**, message prefixed with the task ID — e.g. `M3.2: spawn_box writes lifecycle events`.
- Dashboard: TypeScript, **no UI library beyond Tailwind**, keep it boring.
- Secrets only via Modal secrets / `.env` (gitignored) — never hardcode.
- Use **plan mode first** for anything non-trivial; keep session scopes small (≈ one milestone task cluster).

## Commands

Common commands live in the **`justfile`** (`just` lists them; `just check` = lint + tests). **Convention: whenever a milestone lands a new runnable command (M2 modal smoke test, M3 deploy, M4 CLI, M5 dashboard), add a recipe to the justfile in the same PR.** Python tooling runs through **uv** (`pyproject.toml` + `uv.lock` are wired; dashboard `package.json` arrives with M5):

```bash
just check                  # lint + tests — run before committing
just check-dashboard        # tsc + eslint (the Python `check` does not cover the dashboard)
just test-one <keyword>     # a single test (uv run pytest -k <keyword>)
just fmt                    # ruff format + --fix
just modal-whoami           # which Modal workspace the recipes target (gates every modal recipe)
just smoke                  # M2: build the image, verify the MCP endpoint — no LLM, but a REAL billed container
just secret-sync            # M7: push .env's provider vars into the named Modal secret (this is how you rotate)
just deploy                 # M3: deploy the provisioning app — required before e2e; ensures the secret exists
just e2e                    # M3: full lifecycle against real Modal, dry-run (no LLM, no provider key)
just e2e-live               # same with a real model call — needs the provider vars synced
just dashboard              # M5: local fleet view on http://localhost:3001
just install-skill          # M6: symlink the orchestrator skill into ~/.hermes/skills
```

Every Modal recipe pins the workspace profile (`FLOTTA_MODAL_PROFILE`, default `flotta`) through `just modal-whoami`, which authenticates for real — `modal profile current` only echoes the env var back and never validates. This exists because the globally-active profile was once found pointing at an unrelated workspace.

**Run the CLI directly, never through `just`** — there is deliberately no `just flotta` recipe, because just's variadic arguments get re-split by the shell and break on exactly the quoted-task case the CLI exists for:

```bash
uv run flotta ps
uv run flotta spawn "summarize the logs" --wait   # --wait, or the row strands
uv run flotta reconcile                          # rescue tasks stranded past their deadline
```

The dashboard runs with `just dashboard` on **port 3001** — never 3000 (reserved; see parent `CLAUDE.md`). The port is baked into `dashboard/package.json` rather than passed as a flag, because a flag is too easy to forget.

## Knowledge Base

This project shares its knowledge base with its parent. Do **not** create a separate folder for this repo — entries about it go in the parent's.

Knowledge lives in the **`projects-knowledge` Obsidian vault** at `~/Documents/projects-knowledge/Projects/flotta/`. It is a plain synced folder: no git, nothing to clone, pull, commit or push — just read and write the files. *(Older references to a `joaoh82/projects-knowledge` git repo at `~/projects/projects-knowledge` are retired; that path no longer exists.)*

- **Context (read first):** `~/Documents/projects-knowledge/Projects/flotta/context.md` — stable background: product goals, domain, stakeholders. Update only when the underlying facts change.
- **Notes (running journal):** `.../notes.md` — append-only, dated `## YYYY-MM-DD` headings, for decisions, blockers, incidents.
- **Wiki:** `.../wiki/` — reference sub-docs (architecture, local dev setup, credential locations). Create files as topics emerge.

If `~/Documents/projects-knowledge/` does not exist (cloud sandbox, CI), there is no knowledge base in that environment — skip it silently rather than reconstructing it from a git remote.
