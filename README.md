# Flotta

**Your agent already learns. Now it can scale without forgetting.**

Flotta (Italian for *fleet*) is an open-source fleet runtime for self-improving agents: one always-on orchestrator agent ([Hermes Agent](https://github.com/NousResearch/Hermes-Agent) first) spawns disposable headless worker agents on [Modal](https://modal.com), collects their results, and tears them down. Later versions fold what the workers learned back into one shared, versioned brain.

> **Status: early development — not usable yet.** v0.1 ("one worker") is being built milestone by milestone. This README will grow a real quickstart when it ships. Until then, expect the layout below to be partially filled in.

## What v0.1 will be

- A baked Modal image that boots a **headless, task-scoped Hermes worker** (no messaging gateway, single pinned provider, fixed toolset)
- Two deployed Modal functions — `spawn_worker(task)` / `teardown(worker_id)` — recording every lifecycle event in a local **fleet-state store** (SQLite)
- A **CLI** (`flotta ps | logs | kill | spawn`) and a minimal **local dashboard** (Next.js, polling) over that same store
- A **Hermes skill** teaching the orchestrator when and how to delegate — and to always tear down

Explicitly *not* in v0.1: shared memory, parallel fan-out, any cloud component.

## Layout

```
src/flotta/store.py    fleet-state store (SQLite, thin SQL, validated status transitions)  ✅
src/flotta/provision.py  Modal app: spawn_worker / teardown                                 planned
src/flotta/worker/       Modal image + headless worker entrypoint                           planned
src/flotta/cli.py        Typer CLI                                                          planned
dashboard/               Next.js local dashboard                                            planned
skills/orchestrator/     the Hermes delegation skill                                        planned
vendor/hermes/           read-only Hermes reference clone (gitignored)
```

## Development

Python 3.11+, [uv](https://docs.astral.sh/uv/), and optionally [just](https://just.systems):

```bash
just            # list available commands
just test       # uv run pytest
just check      # lint + tests
```

Planning docs (product doc, development plan, seam notes) live in the parent workspace repo; see `CLAUDE.md` for the working conventions.

## License

[AGPL-3.0](LICENSE) for the core runtime. Adapters and client-facing integration surfaces (the Hermes skill, Modal templates) will be MIT/Apache-2.0 — a `LICENSING.md` explaining the split lands with v0.1.
