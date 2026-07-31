# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This is the **implementation repo** for **Flotta** — an open-source fleet runtime for self-improving agents: one always-on orchestrator (Hermes Agent) spawns disposable headless worker agents on Modal, collects their results, and tears them down. **v0.1 scope: a single worker lifecycle + CLI + local dashboard — no shared memory, no parallel fan-out, no cloud.** This repo is `github.com/joaoh82/flotta`, a separate git repo nested inside the planning-workspace parent (see below).

## Parent project

**Parent project:** [flotta](/Users/joaoh82/projects/flotta_parent/CLAUDE.md) — read for shared dev commands, cross-service context, **and the global working guidelines** (never-touch-`main`, task management via marvinapp, reserved ports, engineering defaults). Those guideline sections live only in the parent `CLAUDE.md` — this file does not duplicate them.

### How I fit in

The parent (`/Users/joaoh82/projects/flotta_parent`) is a **planning-only workspace** holding the product and execution docs; it has no build/test tooling. This nested `flotta/` directory is the actual code. The two are **independent git repos** — confirm which one you're in before any git operation (the parent gitignores this directory). The authoritative planning docs live in the parent's `docs/` (see *Source of truth* below); this repo holds the implementation those docs describe.

*(No sibling table was auto-generated: the parent has a single implementation child and no sibling table or dedicated architecture section in its `CLAUDE.md`.)*

## Current state

Milestones done: **M1** (seam validation — **GO**, findings in the parent's `docs/SEAM_NOTES.md`, decision D7), **M2** (worker image + Flotta-owned MCP server, D9), **M3** (provisioning + fleet-state store, D10), and **M4** (the Typer CLI). The full thesis has run for real: `just e2e-live` passes **21/21** — a headless Hermes agent booted in a disposable Modal container, made a genuine model call, returned its result, and was torn down, with every transition recorded in the local store. Next up: **M5** (local dashboard). M0.3 (PyPI/npm placeholders) and part of M0.4 (Node 20+, Hermes locally) are still open — check the plan. Follow the milestones in order — do not skip ahead or exceed v0.1 scope.

Local config lives in `.env` (gitignored; copy from `.env.example`). Note that `just` only auto-loads a file named exactly `.env` — a `.env.local` is *not* read. **Sharp edge:** provider credentials reach the worker via `Secret.from_local_environ`, which snapshots them at `modal deploy` time rather than resolving per call — editing `.env` without re-deploying silently keeps serving the old (or empty) secret. A named Modal Secret is the proper fix, parked for M7.1.

## Source of truth

The living planning docs live in the **parent** repo (`/Users/joaoh82/projects/flotta_parent/docs/`):

- **`development-plan.md`** — milestones M0–M7 with acceptance criteria, open questions (OQ1–OQ6), decision log (D1–D10), changelog. **Read it at session start.** Before finishing a session: tick task statuses (`[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked-with-note), add a changelog line, and record any choice in the decision log.
- **`fleet-runtime-product.md`** — product scope. Do **not** exceed v0.1 scope.
- **`claude-code-kickoff.md`** — bootstrap steps + the eight paste-ready per-milestone session prompts (one per session). Reality diverging from this guide? The plan file wins. Known divergence: its §1 starter text and M2 prompt still say the worker "starts `mcp_serve`" — superseded by D7/SEAM_NOTES.
- **`SEAM_NOTES.md`** — M1 findings with file:line refs into the Hermes clone (headless boot recipe, `mcp_serve` correction, storage layout, terminal backends). Read before M2/M3 work.
- **`vendor/hermes/`** (in this repo, gitignored) — read-only reference clone of Hermes Agent (@ `594308d4bbe9`). **Never modify it.**

## Architecture (v0.1 target layout)

The runtime is built around one durable store that is the single source of truth for fleet state; everything else reads from or writes to it.

- **`src/flotta/store.py`** — ✅ built (M3.1). Fleet-state store on SQLite via stdlib `sqlite3`, designed so the connection factory could later point at **Turso** (thin SQL, no ORM — D8). Two tables: `workers` (id, task, status `provisioning|running|done|failed|torn_down`, endpoint, spawned_at, finished_at, cost_estimate) and `events` (id, worker_id, ts, type, payload_json). Status transitions are **validated** by an explicit transition table (e.g. no `done → running`; `torn_down` is terminal).
- **`src/flotta/provision.py`** — ✅ built (M3). Split by *where code runs* (D10): `run_worker` is the deployed Modal function (does the work, never touches the store); `spawn_worker(task) -> {worker_id, endpoint}`, `watch_worker(id)` and `teardown(id)` (idempotent) run **locally** beside the store file and are its only writers. The store is a local SQLite file, unreachable from a container — hence a watcher, not entrypoint self-reporting. The stored `endpoint` is the Modal call handle `modal://flotta-provision/run_worker/<fc_id>`.
- **`src/flotta/worker/`** — ✅ built (M2). `image.py` holds the one shared `modal.Image` (Hermes pinned via the `HERMES_REF` constant — the single bump point) so `flotta-worker` and `flotta-provision` can share it without importing each other's `modal.App`. `modal_app.py` is the smoke-test app, `config.py` the `WorkerConfig.from_env` contract, `server.py` the Flotta MCP server (`health` + `run_task`) behind **pure-ASGI** bearer auth — a Starlette `BaseHTTPMiddleware` would buffer and break MCP's SSE stream (D9). The container entrypoint reads `FLOTTA_TASK` / `FLOTTA_TIMEOUT_S` from env, sets `HERMES_HOME` to a writable ephemeral path, boots Hermes **headless** via `AIAgent` (no messaging gateway, single pinned provider, fixed toolset, `skip_context_files`/`skip_memory` — per SEAM_NOTES Q1), and exits on completion or a hard timeout (default 900s). **Not `hermes mcp serve`** — that is a stdio messaging bridge, not a task endpoint (D7, SEAM_NOTES Q2). The MCP surface, if used over the one-shot form, is a thin Flotta-owned streamable-http server exposing a `run_task` tool; Hermes's MCP client can already dial it by URL.
- **`src/flotta/cli.py`** — ✅ built (M4). Typer CLI, wired as a console entry point so a bare `flotta` lands on PATH. **Five** commands, not four: `ps`, `spawn "<task>"` (`--wait`, `--dry-run`, `--timeout-s`), `watch <id>`, `logs <id>`, `kill <id>` (idempotent). `watch` was added beyond the plan's list because `spawn` returns immediately by design — without it the CLI can start and kill a worker but never observe one *succeed*. Every command takes `--json`; the tested surface is a pure `str`-in/`str`-out formatting layer, so tables are hand-rolled rather than pulled from a rendering library. **Store resolution:** `--store` → `$FLOTTA_STORE` → `./fleet.db` (the same variable M5's dashboard reads). **Modal workspace resolution:** `$MODAL_PROFILE` → `$FLOTTA_MODAL_PROFILE` → `.env` → Modal's active profile, applied *before* `provision` is imported (`modal` reads its config at import time) — the installed binary has no justfile pinning it, so without this an unrelated `modal profile activate` would silently redirect `spawn`. `ps` and `logs` are pure store reads needing no Modal credentials; a non-`done` outcome exits 1 so scripts can branch on it.
- **`dashboard/`** — ⏳ next (M5). Next.js (TypeScript, App Router, Tailwind). API routes read the store file directly via `FLOTTA_STORE`; polling UI (2–5s). Localhost only, no auth in v0.1.
- **`skills/orchestrator/`** — ⏳ planned (M6). The Hermes skill teaching the orchestrator when/how to delegate to a worker and to always tear down (including on failure).

Data flow: orchestrator → `spawn_worker` (Modal) → worker boots headless Hermes (`AIAgent`), runs the task, reports the result → events land in the store → CLI/dashboard read the store → `teardown` closes the row.

## Conventions

- **Python 3.11**, type hints everywhere, **ruff** for lint + format.
- Tests with **pytest** next to the code (`test_*.py`). Every `store`/`provision` change needs a test; validate status transitions and listing filters explicitly. **Keep the suite hermetic and $0** — every Modal touchpoint is injected, never called for real (177 tests green as of the M4 follow-up).
- **One commit per completed task**, message prefixed with the task ID — e.g. `M3.2: spawn_worker writes lifecycle events`.
- Dashboard: TypeScript, **no UI library beyond Tailwind**, keep it boring.
- Secrets only via Modal secrets / `.env` (gitignored) — never hardcode.
- Use **plan mode first** for anything non-trivial; keep session scopes small (≈ one milestone task cluster).

## Commands

Common commands live in the **`justfile`** (`just` lists them; `just check` = lint + tests). **Convention: whenever a milestone lands a new runnable command (M2 modal smoke test, M3 deploy, M4 CLI, M5 dashboard), add a recipe to the justfile in the same PR.** Python tooling runs through **uv** (`pyproject.toml` + `uv.lock` are wired; dashboard `package.json` arrives with M5):

```bash
just check                  # lint + tests — run before committing
just test-one <keyword>     # a single test (uv run pytest -k <keyword>)
just fmt                    # ruff format + --fix
just modal-whoami           # which Modal workspace the recipes target (gates every modal recipe)
just smoke                  # M2: build the image on Modal, verify the MCP endpoint — hermetic, $0, no LLM
just deploy                 # M3: deploy the provisioning app — required before e2e
just e2e                    # M3: full lifecycle against real Modal, dry-run (no LLM, no provider key)
just e2e-live               # same with a real model call — needs FLOTTA_MODEL / FLOTTA_MODEL_BASE_URL / FLOTTA_API_KEY
```

Every Modal recipe pins the workspace profile (`FLOTTA_MODAL_PROFILE`, default `flotta`) through `just modal-whoami`, which authenticates for real — `modal profile current` only echoes the env var back and never validates. This exists because the globally-active profile was once found pointing at an unrelated workspace.

**Run the CLI directly, never through `just`** — there is deliberately no `just flotta` recipe, because just's variadic arguments get re-split by the shell and break on exactly the quoted-task case the CLI exists for:

```bash
uv run flotta ps
uv run flotta spawn "summarize the logs" --wait
```

The dashboard arrives with M5: `cd dashboard && npm run dev -- -p 3001` — **not** port 3000 (reserved; see parent `CLAUDE.md`).

## Knowledge Base

This project shares its knowledge base with its parent (flotta). Do **not** create a separate `projects/<child>/` folder — entries about this repo go in the parent's folder.

Project knowledge lives in the private repo **`joaoh82/projects-knowledge`**, cloned at `~/projects/projects-knowledge` (clone to the same path in cloud environments). Follow the repo workflow in the parent `CLAUDE.md`: pull before writing, work only in the repo working tree (never via the Obsidian vault path), read only this project's folder, and commit + push at session end if anything changed (this notes repo is exempt from the never-touch-`main` rule).

### Project-specific — `~/projects/projects-knowledge/projects/flotta/`

- **Code (this repo):** `/Users/joaoh82/projects/flotta_parent/flotta`
- **Code (parent meta-repo):** `/Users/joaoh82/projects/flotta_parent`
- **Context (read first):** `~/projects/projects-knowledge/projects/flotta/context.md`
- **Notes (running journal):** `~/projects/projects-knowledge/projects/flotta/notes.md`
- **Project wiki:** `~/projects/projects-knowledge/projects/flotta/wiki/`

**How to use each:**

- `context.md` — stable background (product goals, stakeholders, domain). Read before non-trivial work. Update only when underlying facts change.
- `notes.md` — append-only dated journal (`## YYYY-MM-DD` headings) for decisions, blockers, TODOs, incidents. Notes about *this repo* still go here, in the parent's `notes.md`.
- `wiki/` — reference sub-docs (`Architecture.md`, `Local Dev Setup.md`, `Tech Services.md`). Create files as topics emerge.

**When to save:**

- New stable fact about the product/domain → update the parent's `context.md`.
- A decision, incident, or working note → append a dated entry to the parent's `notes.md`.
- Reusable reference material (setup steps, credential locations, architecture) → new/updated file in the parent's `wiki/`.

### Cross-project knowledge — `~/Documents/josh-obsidian-synced/vault/` (Obsidian machines only)

- **General wiki:** `~/Documents/josh-obsidian-synced/vault/wiki/` — start at `_master-index.md`, then drill into the relevant topic's `_index.md`.
- **Raw dumps:** `~/Documents/josh-obsidian-synced/vault/raw/` — drop unprocessed research here as `YYYY-MM-DD-{slug}.md`.

Read the general wiki when the question isn't specific to this project. This vault has not moved to the knowledge repo — it only exists on machines with the Obsidian vault; if the path doesn't exist, skip it.
