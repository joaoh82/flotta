# Deploying Flotta

The first deployment, done by hand. Every step here should eventually be a
`just` recipe; writing it down first is how you find out which parts are
actually hard.

**Three things get deployed, in this order, and the order is forced by a
dependency you cannot skip:**

| | What | Where | Depends on |
|---|---|---|---|
| 1 | **Control plane** — the fleet API and the reconcile loop | Railway | Postgres |
| 2 | **Front door** — public TLS access to a box | Fly | the control plane's URL |
| 3 | **DNS** — `*.flotta.dev` | Cloudflare | the door's IP address |

The instinct is to do DNS first. You cannot: the record points at the door's
address, and the door does not have one until it is deployed. The door in turn
needs a control-plane URL to ask "which machine is this box?".

**Running cost:** roughly $2–4/month for the always-on door, plus Railway's
Postgres, plus whatever the boxes cost. A box suspends itself after 30 minutes
idle and its volume keeps billing while it sleeps — cheap, not free.

---

## Step 0 — Generate every secret at once

```bash
uv run python scripts/deploy_config.py --domain flotta.dev
```

Prints a signing key, three tokens, and exactly what to paste where. Nothing is
written to disk. Keep the output open until step 4.

**The one thing that must match:** the control plane and the door share
`FLOTTA_SIGNING_KEY`. If they differ, every token is rejected with
`bad signature`, which reads like a broken token rather than a configuration
mismatch. This script exists mostly to stop that happening.

---

## Step 1 — Postgres

In a new Railway project, add a **PostgreSQL** service. Nothing else to do; the
control plane creates its own schema on first boot.

---

## Step 2 — The control plane

Same Railway project, a second service, deployed from this repo's root
`Dockerfile`.

Variables:

```
FLOTTA_SIGNING_KEY=<from step 0>
FLOTTA_DATABASE_URL=${{Postgres.DATABASE_URL}}
```

The second is a **Railway variable reference**, not a literal — it resolves to
whatever the Postgres service is currently using, so it survives a database
rotation.

Do not set `PORT`. Railway injects it and the image honours it.

**Expect it to refuse to boot if the signing key is missing.** It binds
`0.0.0.0` and the guard fails closed: an unauthenticated fleet API is a kill
switch for every agent you own. That is the guard working.

**Do not enable Railway's serverless/sleep on this service.** The reconcile
loop is continuous background work, and sleeping it reintroduces the exact bug
it exists to fix — a task that strands with nothing watching. `/health` reports
`503` when the loop stops sweeping, so if you do sleep it, you will find out
from a health check rather than from a stranded row a week later.

Verify:

```bash
curl -s https://<your-app>.up.railway.app/health | python3 -m json.tool
```

`reconcile_loop.sweeps` should be climbing and `failing` should be `false`.

---

## Step 3 — A box image, if you do not have one

Skip if `just fly-doctor` already reports a healthy box.

```bash
just fly-up        # builds the box image and boots a machine — REAL Fly spend
just fly-auth      # mints the box's Hermes credentials into .env
```

`fly-auth` writes `FLOTTA_BOX_PASSWORD` into your local `.env`. Step 4 needs it.

---

## Step 4 — The front door

```bash
just door-deploy
```

Then give it its secrets — the exact command is in step 0's output:

```bash
flyctl secrets set --app flotta-door \
  FLOTTA_SIGNING_KEY='<the SAME key as Railway>' \
  FLOTTA_CONTROL_URL='https://<your-app>.up.railway.app' \
  FLOTTA_CONTROL_TOKEN='<the door token from step 0>' \
  FLOTTA_DOMAIN='flotta.dev' \
  FLOTTA_BOX_PASSWORD='<from step 3>'
```

Verify the door is up before touching DNS:

```bash
curl -s https://flotta-door.fly.dev/_door/health
```

---

## Step 5 — DNS

```bash
just door-dns
```

prints the records with the door's real address filled in. Two of them, in
Cloudflare:

1. **`AAAA  *.flotta.dev  →  <the door's IPv6>`**, and an `A` record if you
   allocated a dedicated IPv4.

   **Grey cloud — DNS only.** Cloudflare *can* proxy wildcards on any plan (the
   widely-repeated "Enterprise only" rule is years out of date), but it closes
   idle WebSockets after a period it does not publish. An agent session is idle
   almost all the time, so proxying before the app sends a heartbeat turns
   "the user thought for two minutes" into a dropped conversation. Move to
   orange later, with **Full (Strict)**.

2. **The ACME challenge `CNAME`**, printed by:

   ```bash
   flyctl certs add '*.flotta.dev' --app flotta-door
   ```

   A wildcard needs DNS-01 validation, which is why this record exists at all.

Wait for `flyctl certs show '*.flotta.dev' --app flotta-door` to report the
certificate issued. This is usually a couple of minutes and occasionally longer.

---

## Step 6 — Prove it end to end

```bash
export FLOTTA_CONTROL_URL=https://<your-app>.up.railway.app
export FLOTTA_CONTROL_TOKEN='<the operator token from step 0>'

flotta create eng-a                       # an agent, on Fly, with durable memory
curl -H "Authorization: Bearer <operator token>" https://eng-a.flotta.dev/
```

**The first request to a sleeping box takes 10–60 seconds.** The machine starts
in under a second and Hermes then imports itself. The door holds the connection
rather than failing; it is not a hang.

Then leave it alone for half an hour and check it suspended itself:

```bash
flotta ps          # eng-a should read `stopped`
```

and hit it again — it should wake, transparently, and remember what it knew.
That round trip is the entire thesis of the project in one command.

---

## What is not automated yet

Everything above should be a recipe. In rough order of how much each would
save:

- **`just deploy-control`** — Railway has a CLI and a template format (§8.6's
  M5.5). The template must default to requiring a signing key, or it ships the
  unauthenticated fleet API the bind guard exists to prevent.
- **`just deploy-all`** — the ordering above, with the waits.
- **DNS** — Cloudflare has an API, so `just door-dns` could apply the records
  rather than print them. Deliberately not done: a script that edits your DNS
  is a script that can take your domain offline, and the first time through you
  should see what is being asked for.
