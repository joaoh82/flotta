# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This is the **implementation repo** for **Flotta** — an open-source fleet runtime for self-improving agents: one always-on orchestrator (Hermes Agent) spawns disposable headless worker agents on Modal, collects their results, and tears them down. **v0.1 scope: a single worker lifecycle + CLI + local dashboard — no shared memory, no parallel fan-out, no cloud.** This repo is `github.com/joaoh82/flotta`, a separate git repo nested inside the planning-workspace parent (see below).

## Parent project

**Parent project:** [flotta](/Users/joaoh82/projects/flotta_parent/CLAUDE.md) — read for shared dev commands, cross-service context, **and the global working guidelines** (never-touch-`main`, task management via marvinapp, reserved ports, engineering defaults). Those guideline sections live only in the parent `CLAUDE.md` — this file does not duplicate them.

### How I fit in

The parent (`/Users/joaoh82/projects/flotta_parent`) is a **planning-only workspace** holding the product and execution docs; it has no build/test tooling. This nested `flotta/` directory is the actual code. The two are **independent git repos** — confirm which one you're in before any git operation (the parent gitignores this directory). The authoritative planning docs live in the parent's `docs/` (see *Source of truth* below); this repo holds the implementation those docs describe.

*(No sibling table was auto-generated: the parent has a single implementation child and no sibling table or dedicated architecture section in its `CLAUDE.md`.)*

## Current state

**M1–M6 are done and M7.1 has landed.** The full thesis runs for real: a chat message to the local Hermes spawns a headless worker in a disposable Modal container, gets a genuine model answer back, and tears the worker down — 41s end to end, with every transition recorded in the local store (M6). `just e2e` passes 21/21 and `just e2e-live` 22/22 against real Modal.

**Next: M7.2 (README + quickstart).** Then OQ3 cost estimation (M7.3), the `HERMES_REF` drift (M7.4), a clean-machine test, the demo GIF, and going public. `M0.3` (PyPI/npm placeholders) is still open and blocks M7.7 — the bare `flotta` name should be reserved before the repo is public. Follow the plan's order; do not exceed v0.1 scope.

**Local config** lives in `.env` (gitignored; copy `.env.example`). `just` only auto-loads a file named exactly `.env` — a `.env.local` is *not* read.

**Provider credentials** come from a **named Modal secret**, `flotta-provider`, not from the deploying shell. `just secret-sync` pushes the three `FLOTTA_*` provider vars from `.env` into it; `just secret-ensure` (a `deploy` dependency) creates it empty so the provider-free dry-run path works, and never overwrites. Rotation needs **no redeploy** — but it is not instant: a secret becomes environment variables when a container *starts*, so a warm container serves the old value until it scales down. `modal app stop flotta-provision -y` forces the turnover. (Measured, because the docs invite over-reading it as per-call.)

## Source of truth

The living planning docs live in the **parent** repo (`/Users/joaoh82/projects/flotta_parent/docs/`):

- **`development-plan.md`** — milestones M0–M7 with acceptance criteria, open questions (OQ1–OQ6), decision log (D1–D12), changelog. **Read it at session start.** Before finishing a session: tick task statuses (`[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked-with-note), add a changelog line, and record any choice in the decision log.
- **`fleet-runtime-product.md`** — product scope. Do **not** exceed v0.1 scope.
- **`claude-code-kickoff.md`** — bootstrap steps + the eight paste-ready per-milestone session prompts (one per session). Reality diverging from this guide? The plan file wins. Known divergence: its §1 starter text and M2 prompt still say the worker "starts `mcp_serve`" — superseded by D7/SEAM_NOTES.
- **`SEAM_NOTES.md`** — M1 findings with file:line refs into the Hermes clone (headless boot recipe, `mcp_serve` correction, storage layout, terminal backends). **Caveat:** `vendor/` is pinned at `594308d4bbe9` while the *installed* Hermes is newer (v0.16.0 / `d62979a6`), so it is no longer a reliable guide to the local agent's behaviour — reading it for skill conventions already caused a wrong turn in M6. Reconciling the pin is M7.4.
- **`vendor/hermes/`** (in this repo, gitignored) — read-only reference clone of Hermes Agent (@ `594308d4bbe9`). **Never modify it.**

## Architecture (v0.1 target layout)

The runtime is built around one durable store that is the single source of truth for fleet state; everything else reads from or writes to it.

- **`src/flotta/store.py`** — ✅ built (M3.1). Fleet-state store on SQLite via stdlib `sqlite3`, designed so the connection factory could later point at **Turso** (thin SQL, no ORM — D8). Two tables: `workers` (id, task, status `provisioning|running|done|failed|torn_down`, endpoint, spawned_at, finished_at, cost_estimate) and `events` (id, worker_id, ts, type, payload_json). Status transitions are **validated** by an explicit transition table (e.g. no `done → running`; `torn_down` is terminal). `TERMINAL` is defined **here and only here** — `provision` and `cli` import it, after all three had drifting copies. `create_worker(max_live=N)` enforces the live-worker cap by counting and inserting inside one `BEGIN IMMEDIATE`; a check-then-insert would race and let two simultaneous spawns both through (M7.1c).
- **`src/flotta/provision.py`** — ✅ built (M3). Split by *where code runs* (D10): `run_worker` is the deployed Modal function (does the work, never touches the store); `spawn_worker(task) -> {worker_id, endpoint}`, `watch_worker(id)` and `teardown(id)` (idempotent) run **locally** beside the store file and are its only writers. The store is a local SQLite file, unreachable from a container — hence a watcher, not entrypoint self-reporting. The stored `endpoint` is the Modal call handle `modal://flotta-provision/run_worker/<fc_id>` — **not a URL**: v0.1 workers are one-shot, so nothing is listening, which is why the M6 skill drives the CLI rather than registering an MCP tool (D12). **M7.1 added:** `reconcile()` resolves workers stranded past their own `timeout_s` — it re-attaches and *recovers the real result* where possible (a 9-hour-old call result was still retrievable) and marks the rest `failed`, **never `completed`**; `resolve_max_concurrent()` is the cap policy (explicit → `$FLOTTA_MAX_CONCURRENT` → **1**, `0` = unlimited); and credentials come from the named secret rather than the deploying shell.
- **`src/flotta/worker/`** — ✅ built (M2). `image.py` holds the one shared `modal.Image` (Hermes pinned via the `HERMES_REF` constant — the single bump point) so `flotta-worker` and `flotta-provision` can share it without importing each other's `modal.App`. `modal_app.py` is the smoke-test app, `config.py` the `WorkerConfig.from_env` contract, `server.py` the Flotta MCP server (`health` + `run_task`) behind **pure-ASGI** bearer auth — a Starlette `BaseHTTPMiddleware` would buffer and break MCP's SSE stream (D9). The container entrypoint reads `FLOTTA_TASK` / `FLOTTA_TIMEOUT_S` from env, sets `HERMES_HOME` to a writable ephemeral path, boots Hermes **headless** via `AIAgent` (no messaging gateway, single pinned provider, fixed toolset, `skip_context_files`/`skip_memory` — per SEAM_NOTES Q1), and exits on completion or a hard timeout (default 900s). **Not `hermes mcp serve`** — that is a stdio messaging bridge, not a task endpoint (D7, SEAM_NOTES Q2). The MCP surface, if used over the one-shot form, is a thin Flotta-owned streamable-http server exposing a `run_task` tool; Hermes's MCP client can already dial it by URL.
- **`src/flotta/cli.py`** — ✅ built (M4, extended in M7.1). Typer CLI wired as a console entry point, so `uv tool install .` puts a bare `flotta` on PATH — `uv run` is the contributor path, not the user path. **Six** commands: `ps`, `spawn "<task>"` (`--wait`, `--dry-run`, `--timeout-s`, `--max-concurrent`), `watch <id>`, `logs <id>`, `kill <id>` (idempotent), and `reconcile`. `watch` exists because `spawn` returns immediately by design — without it the CLI could start and kill a worker but never observe one *succeed*. Every command takes `--json`; the tested surface is a pure `str`-in/`str`-out formatting layer, so tables are hand-rolled rather than pulled from a rendering library. **Store resolution:** `--store` → `$FLOTTA_STORE` → `./fleet.db` (the same variable the dashboard reads); reads require the store to exist, and only `spawn` may create it. **Modal workspace resolution:** `$MODAL_PROFILE` → `$FLOTTA_MODAL_PROFILE` → `.env` → Modal's active profile, applied *before* `provision` is imported (`modal` reads its config at import time) — the installed binary has no justfile pinning it, so without this an unrelated `modal profile activate` would silently redirect `spawn`. `ps` and `logs` are pure store reads needing no Modal credentials; **`reconcile` is deliberately not folded into `ps`** so that stays true. Exit codes carry meaning: `1` a failed worker, `2` a refusal (cap reached, or a missing store).


- **`dashboard/`** — ✅ built (M5). Next.js 16 (App Router, TypeScript, Tailwind 4) reading the same store through Node's built-in **`node:sqlite`** — no native module and no database dependency. Connections are `readOnly` and opened **per request**: under WAL a long-lived reader pins an old snapshot and would quietly serve stale rows to a polling UI. A missing store is a **503 naming the path**, never an empty fleet. Localhost only, no auth, port **3001**. Read-only except the kill button, which shells out to the CLI (D11) and checks the *cancel outcome*, not just the HTTP status.


- **`skills/orchestrator/`** — ✅ built (M6). A Hermes skill teaching the orchestrator to delegate, installed with `just install-skill` (symlink, so repo edits take effect without a reinstall). Written to the installed Hermes's own conventions — `description` ≤ 60 chars is a hardline with an enforcement test in its repo. **It competes with Hermes's built-in `delegate_task` in the always-on skill index**, so it leads with the property that actually distinguishes Flotta: **isolation**, not durability. (Durability was the first draft's claim; a live routing test showed a one-shot `cronjob` covers that too, and the model correctly chose `cronjob` over Flotta — D12.)

Data flow: orchestrator → `spawn_worker` (Modal) → worker boots headless Hermes (`AIAgent`), runs the task, reports the result → events land in the store → CLI/dashboard read the store → `teardown` closes the row.

## Conventions

- **Python 3.11**, type hints everywhere, **ruff** for lint + format.
- Tests with **pytest** next to the code (`test_*.py`). Every `store`/`provision` change needs a test; validate status transitions and listing filters explicitly. **Keep the suite hermetic and $0** — every Modal touchpoint is injected, never called for real (**212 tests** green as of M7.1).
- **One commit per completed task**, message prefixed with the task ID — e.g. `M3.2: spawn_worker writes lifecycle events`.
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
uv run flotta reconcile                          # rescue workers stranded past their deadline
```

The dashboard runs with `just dashboard` on **port 3001** — never 3000 (reserved; see parent `CLAUDE.md`). The port is baked into `dashboard/package.json` rather than passed as a flag, because a flag is too easy to forget.

## Knowledge Base

This project shares its knowledge base with its parent. Do **not** create a separate folder for this repo — entries about it go in the parent's.

Knowledge lives in the **`projects-knowledge` Obsidian vault** at `~/Documents/projects-knowledge/Projects/flotta/`. It is a plain synced folder: no git, nothing to clone, pull, commit or push — just read and write the files. *(Older references to a `joaoh82/projects-knowledge` git repo at `~/projects/projects-knowledge` are retired; that path no longer exists.)*

- **Context (read first):** `~/Documents/projects-knowledge/Projects/flotta/context.md` — stable background: product goals, domain, stakeholders. Update only when the underlying facts change.
- **Notes (running journal):** `.../notes.md` — append-only, dated `## YYYY-MM-DD` headings, for decisions, blockers, incidents.
- **Wiki:** `.../wiki/` — reference sub-docs (architecture, local dev setup, credential locations). Create files as topics emerge.

If `~/Documents/projects-knowledge/` does not exist (cloud sandbox, CI), there is no knowledge base in that environment — skip it silently rather than reconstructing it from a git remote.
