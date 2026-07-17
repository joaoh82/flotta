# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This is the **implementation repo** for **Flotta** — an open-source fleet runtime for self-improving agents: one always-on orchestrator (Hermes Agent) spawns disposable headless worker agents on Modal, collects their results, and tears them down. **v0.1 scope: a single worker lifecycle + CLI + local dashboard — no shared memory, no parallel fan-out, no cloud.** This repo is `github.com/joaoh82/flotta`, a separate git repo nested inside the planning-workspace parent (see below).

## Parent project

**Parent project:** [flotta](/Users/joaoh82/projects/flotta_parent/CLAUDE.md) — read for shared dev commands, cross-service context, **and the global working guidelines** (never-touch-`main`, task management via marvinapp, reserved ports, engineering defaults). Those guideline sections live only in the parent `CLAUDE.md` — this file does not duplicate them.

### How I fit in

The parent (`/Users/joaoh82/projects/flotta_parent`) is a **planning-only workspace** holding the product and execution docs; it has no build/test tooling. This nested `flotta/` directory is the actual code. The two are **independent git repos** — confirm which one you're in before any git operation (the parent gitignores this directory). The authoritative planning docs live in the parent's `docs/` (see *Source of truth* below); this repo holds the implementation those docs describe.

*(No sibling table was auto-generated: the parent has a single implementation child and no sibling table or dedicated architecture section in its `CLAUDE.md`.)*

## Current state

The repo currently contains only `LICENSE` (AGPL-3.0). It has **not been bootstrapped yet**. The paths in *Architecture* below describe the **target** layout, not existing files. The immediate next steps, per the kickoff guide, are:

1. **Bootstrap** (parent `docs/claude-code-kickoff.md` §1): create `src/flotta/`, `dashboard/`, `skills/`, `docs/`, `vendor/`; `git init` is already done; copy `development-plan.md` + `fleet-runtime-product.md` into `docs/`; clone Hermes read-only into `vendor/hermes`; add `vendor/` to `.gitignore`.
2. **M1 seam validation** (Session 1 prompt in the kickoff guide) — read-only investigation of vendored Hermes, no implementation code.

Milestone state is **M0** (namespace & environment). Follow the milestones in order — do not skip ahead or exceed v0.1 scope.

## Source of truth

The living planning docs are in the **parent** repo (`/Users/joaoh82/projects/flotta_parent/docs/`) until bootstrap copies them into this repo's `docs/`:

- **`development-plan.md`** — milestones M0–M7 with acceptance criteria, open questions, decision log (D1–D6), changelog. **Read it at session start.** Before finishing a session: tick task statuses (`[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked-with-note), add a changelog line, and record any choice in the decision log.
- **`fleet-runtime-product.md`** — product scope. Do **not** exceed v0.1 scope.
- **`claude-code-kickoff.md`** — bootstrap steps + the eight paste-ready per-milestone session prompts (one per session). Reality diverging from this guide? The plan file wins.
- **`vendor/hermes/`** (after bootstrap) — read-only reference clone of Hermes Agent. **Never modify it.**

## Architecture (v0.1 target layout)

The runtime is built around one durable store that is the single source of truth for fleet state; everything else reads from or writes to it.

- **`src/flotta/store.py`** — fleet-state store on SQLite, designed so the connection string could later point at **Turso** (thin SQL, no ORM). Two tables: `workers` (id, task, status `provisioning|running|done|failed|torn_down`, endpoint, spawned_at, finished_at, cost_estimate) and `events` (id, worker_id, ts, type, payload_json). Status transitions are **validated** (e.g. no `done → running`).
- **`src/flotta/provision.py`** — Modal app: `spawn_worker(task) -> {worker_id, endpoint}` and `teardown(worker_id)` (idempotent). Both write lifecycle events to the store.
- **`src/flotta/worker/`** — Modal image definition (Hermes installed, pinned) + container entrypoint: reads `FLOTTA_TASK` / `FLOTTA_TIMEOUT_S` from env, boots Hermes **headless** (no messaging gateway, single provider, fixed toolset — per `docs/SEAM_NOTES.md`), starts `mcp_serve`, and exits on completion or a hard timeout (default 900s).
- **`src/flotta/cli.py`** — Typer CLI: `flotta ps | logs <id> | kill <id> | spawn "<task>"`, all against the store and provisioning functions. Human-readable tables with a `--json` flag.
- **`dashboard/`** — Next.js (TypeScript, App Router, Tailwind). API routes read the store file directly via `FLOTTA_STORE`; polling UI (2–5s). Localhost only, no auth in v0.1.
- **`skills/orchestrator/`** — the Hermes skill teaching the orchestrator when/how to delegate to a worker and to always tear down (including on failure).

Data flow: orchestrator → `spawn_worker` (Modal) → worker boots headless Hermes + `mcp_serve` → events land in the store → CLI/dashboard read the store → `teardown` closes the row.

## Conventions

- **Python 3.11**, type hints everywhere, **ruff** for lint + format.
- Tests with **pytest** next to the code (`test_*.py`). Every `store`/`provision` change needs a test; validate status transitions and listing filters explicitly.
- **One commit per completed task**, message prefixed with the task ID — e.g. `M3.2: spawn_worker writes lifecycle events`.
- Dashboard: TypeScript, **no UI library beyond Tailwind**, keep it boring.
- Secrets only via Modal secrets / `.env` (gitignored) — never hardcode.
- Use **plan mode first** for anything non-trivial; keep session scopes small (≈ one milestone task cluster).

## Commands

Nothing is wired yet (no `pyproject.toml`/`package.json` until bootstrap). Target commands once the milestones land:

```bash
pytest                                   # run tests (add -k <name> for a single test)
ruff check .                             # lint
ruff format .                            # format
modal deploy src/flotta/provision.py     # deploy provisioning functions (M3+)
modal run src/flotta/worker/...          # smoke-test the worker image (M2+)
flotta ps | logs <id> | kill <id> | spawn "<task>"   # CLI (M4+)
cd dashboard && npm run dev -- -p 3001   # dashboard — NOT on 3000 (reserved; see parent CLAUDE.md)
```

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
