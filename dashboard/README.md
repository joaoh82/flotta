# Flotta dashboard

A local, browser view of the fleet: which workers exist, what they are doing,
what they produced, and a button to kill one. Next.js (App Router, TypeScript,
Tailwind) reading the same fleet-state store the CLI reads.

```bash
npm install          # first time only
npm run dev          # http://localhost:3001
```

Then spawn something to look at:

```bash
uv run flotta spawn "Count from 1 to 20." --wait
```

## Security posture — read this before exposing it

**This dashboard has no authentication of any kind, and it can kill workers.**

It is built to be run on your own machine, reachable only from it. `npm run dev`
binds localhost. Do not put it on a shared host, a public interface, or behind a
tunnel without adding auth first — anyone who can reach the port can tear down
any worker in the fleet, and can read every task and every result the fleet has
ever produced.

The port is **3001**, not Next's default 3000, which is reserved for another
local service. That is baked into `package.json` rather than left to a flag, so
it cannot be forgotten.

## Where the data comes from

| | |
|---|---|
| **Store path** | `$FLOTTA_STORE`, else `../fleet.db` (the repo root) |
| **Reads** | Direct, read-only SQLite via Node's built-in `node:sqlite` |
| **Writes** | None. The only mutation is the kill button, which shells out to the CLI |
| **Refresh** | Client-side poll every 3s; no websockets, no SSE |

If the store file does not exist, the dashboard says so and names the path it
looked at. It deliberately does **not** render an empty fleet — "no store here"
and "no workers yet" are different facts, and confusing them wastes your time.

Point it at a different store like so:

```bash
FLOTTA_STORE=/path/to/fleet.db npm run dev
```

## Why the kill button shells out to the CLI

Tearing a worker down means cancelling a Modal function call, which needs
Python, Modal credentials, and the workspace-profile resolution that already
lives in `flotta.cli`. The API route therefore runs `flotta kill <id> --json`
rather than reimplementing any of it.

The alternative — writing `torn_down` straight into the store from TypeScript —
would have been a few lines and would have been a lie: the row would close
while the container kept running and billing. This also preserves decision D10,
that only code which can actually reach Modal writes to the store.

For the same reason the button checks the *cancel outcome*, not just the HTTP
status. `teardown` records a cancel failure instead of raising, so a request can
succeed while the container survives; when that happens the UI says the worker
may still be running instead of reporting a clean kill.

## Known limits (v0.1)

- **A worker nobody watches stays `running` forever.** Per D10 the store is
  written only by local code, so if you `flotta spawn` without `--wait` and
  never `flotta watch`, the container will finish while the row sits at
  `running`. The dashboard is a pure reader and cannot advance the state
  machine — it is showing you the truth about the store, which is stale. Use
  `flotta spawn --wait`, or `flotta watch <id>` afterwards.
- **Est. cost is `—` unless you set a rate.** Modal cannot attribute cost to a
  single worker, so the column shows `duration × $FLOTTA_COST_PER_SECOND` when
  that is configured and a blank when it is not. It covers container time only —
  model tokens are a separate bill.
- No auth, no multi-user, no history beyond what the store keeps.
- `node:sqlite` is still marked experimental in Node 24, so the dev server
  prints one `ExperimentalWarning` on first query. It is cosmetic.

## Layout

```
app/
  page.tsx                  fleet list
  workers/[id]/page.tsx     worker detail
  api/workers/route.ts      GET  list
  api/workers/[id]/route.ts GET  detail + events · DELETE kill
  components/               client components (polling, table, timeline)
lib/
  store.ts                  read-only node:sqlite access  (server-only)
  teardown.ts               shells out to the CLI          (server-only)
  format.ts                 duration/age rendering, matched to the CLI
  types.ts                  shapes shared with the browser
```

`lib/store.ts` and `lib/teardown.ts` are marked `server-only`: importing either
from a client component is a build error rather than a confusing runtime
failure.
