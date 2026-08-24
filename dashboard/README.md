# Flotta dashboard

A local, browser view of the fleet: which machines exist, what they are doing,
what they produced, and a button to destroy one. Next.js (App Router,
TypeScript, Tailwind) reading the same fleet-state store the CLI reads.

> **A "worker" here is a box.** The store split into `boxes` / `workspaces` /
> `tasks`; a row in this UI is a **box** (a machine that is an agent), showing
> its newest task's prompt and its total spend across tasks, and the kill button
> destroys the machine. The `worker` vocabulary survives in the route paths,
> component names and types — deliberately, because it is cosmetic and belongs
> with the rewrite that moves this app off direct SQLite reads and onto a
> control-plane API. All three SQL queries live in `lib/store.ts` and already
> read the new schema.

```bash
npm install          # first time only
npm run dev          # http://localhost:3001
```

Then spawn something to look at:

```bash
uv run flotta spawn "Count from 1 to 20." --wait
```

## Security posture — read this before exposing it

**This dashboard has no authentication of any kind, and it can destroy boxes.**

It is built to be run on your own machine, reachable only from it. `npm run dev`
binds localhost. Do not put it on a shared host, a public interface, or behind a
tunnel without adding auth first — anyone who can reach the port can destroy
any box in the fleet, and can read every task and every result the fleet has
ever produced.

The port is **3001**, not Next's default 3000, which is reserved for another
local service. That is baked into `package.json` rather than left to a flag, so
it cannot be forgotten.

The dev server prints this warning on every start, along with the store it is
about to read — wired as npm's `predev` hook, so it fires for `npm run dev` and
`just dashboard` alike rather than only the path that happens to go through
`just`. Set `NO_COLOR` if the highlighting is unwelcome.

## It cannot read a Postgres fleet (yet)

The fleet store can live on Postgres (`$FLOTTA_DATABASE_URL`, M4). **This
dashboard cannot read that** — it opens a local SQLite file directly. With a
Postgres URL configured, every API route answers **501 `store_on_postgres`**
with an explanation, rather than rendering an empty fleet.

Refusing matters more than it might look: the fallback would be a UI showing
zero boxes while the fleet is alive and healthy, which is the same "wrong file
looks like no boxes" confusion the CLI spends real effort killing. Use
`flotta ps` in the meantime.

The fix is not "teach the dashboard Postgres" — it is §8.3's control-plane API,
which retires direct-SQLite reads entirely.

## Where the data comes from

| | |
|---|---|
| **Store path** | `$FLOTTA_STORE`, else `../fleet.db` (the repo root) |
| **Reads** | Direct, read-only SQLite via Node's built-in `node:sqlite` |
| **Writes** | None. The only mutation is the kill button, which shells out to the CLI |
| **Refresh** | Client-side poll every 3s; no websockets, no SSE |

If the store file does not exist, the dashboard says so and names the path it
looked at. It deliberately does **not** render an empty fleet — "no store here"
and "no boxes yet" are different facts, and confusing them wastes your time.

Point it at a different store like so:

```bash
FLOTTA_STORE=/path/to/fleet.db npm run dev
```

## Why the kill button shells out to the CLI

Tearing a box down means cancelling a Modal function call, which needs
Python, Modal credentials, and the workspace-profile resolution that already
lives in `flotta.cli`. The API route therefore runs `flotta kill <id> --json`
rather than reimplementing any of it.

The alternative — writing `torn_down` straight into the store from TypeScript —
would have been a few lines and would have been a lie: the row would close
while the container kept running and billing. This also preserves decision D10,
that only code which can actually reach Modal writes to the store.

For the same reason the button checks the *cancel outcome*, not just the HTTP
status. `teardown` records a cancel failure instead of raising, so a request can
succeed while the container survives; when that happens the UI says the box
may still be running instead of reporting a clean kill.

## Known limits

- **A task nobody watches stays `running` forever.** Per D10 the store is
  written only by local code, so if you `flotta spawn` without `--wait` and
  never `flotta watch`, the container will finish while the row sits at
  `running`. The dashboard is a pure reader and cannot advance the state
  machine — it is showing you the truth about the store, which is stale. Use
  `flotta spawn --wait`, or `flotta watch <id>` afterwards.
- **`stopped` boxes are shown, not hidden.** A stopped box is idle, not
  finished — it keeps its disk and is expected to come back. Anything that
  filters the fleet must keep showing them.
- **Est. cost is `—` unless you set a rate.** Modal cannot attribute cost to a
  single task, so the column shows `wall-clock seconds × $FLOTTA_COST_PER_SECOND` when
  that is configured and a blank when it is not. It is summed across the box's
  tasks, and covers container time only — model tokens are a separate bill.
- No auth, no multi-user, no history beyond what the store keeps.
- `node:sqlite` is still marked experimental in Node 24, so the dev server
  prints one `ExperimentalWarning` on first query. It is cosmetic.

## Layout

```
app/
  page.tsx                  fleet list
  workers/[id]/page.tsx     box detail (path name is pre-rename)
  api/workers/route.ts      GET  list
  api/workers/[id]/route.ts GET  detail + events · DELETE kill
  components/               client components (polling, table, timeline)
scripts/
  banner.mjs                startup warning (npm `predev` hook)
lib/
  store.ts                  read-only node:sqlite access  (server-only)
  teardown.ts               shells out to the CLI          (server-only)
  format.ts                 duration/age rendering, matched to the CLI
  types.ts                  shapes shared with the browser
```

`lib/store.ts` and `lib/teardown.ts` are marked `server-only`: importing either
from a client component is a build error rather than a confusing runtime
failure.
