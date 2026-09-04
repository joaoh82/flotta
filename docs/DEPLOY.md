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
FLY_API_TOKEN=<flyctl tokens create org>
FLOTTA_FLY_APP=<the app your boxes live in>
FLOTTA_FLY_ORG=personal
```

`FLOTTA_DATABASE_URL` is a **Railway variable reference**, not a literal — it
resolves to whatever the Postgres service is currently using, so it survives a
database rotation.

**The last three are why this is not a read-only service.** D10 says fleet
state is written only by code that can reach the substrate, and the control
plane *is* that code: `POST /api/boxes` creates a machine, `DELETE` destroys
one, `POST /api/boxes/{id}/wake` starts one, and the idle sweep suspends one.
Without a Fly token it serves reads and silently fails every write.

The wake matters most: **the front door depends on it**, and a request for a
sleeping box is the normal case rather than an edge one. The idle sweep matters
quietly — its failures are logged rather than raised, so without a token the
fleet simply never sleeps while the loop keeps reporting healthy.

Mint the token with:

```bash
flyctl tokens create org --name flotta-control-plane
```

An **org-scoped** token, because the control plane creates apps and machines
rather than deploying to one that exists.

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

**Secrets first, then deploy.** The order matters and the reverse deadlocks —
see below.

Put these in `.env`:

```
FLOTTA_SIGNING_KEY=<the SAME value the control plane has>
FLOTTA_CONTROL_URL=https://<your-app>.up.railway.app
FLOTTA_DOMAIN=flotta.dev
# FLOTTA_BOX_PASSWORD is already there, written by `just fly-auth`
```

then:

```bash
just door-secrets    # stages them; mints the door's token from the same key
just door-deploy     # creates the app if needed, then deploys
```

`door-secrets` mints the control-plane token rather than having you paste one,
so it cannot drift from the key that has to verify it.

> **Why secrets before deploy.** `flyctl secrets set` waits for the app to
> become healthy — and the door **cannot** become healthy until it has a
> signing key, because it refuses to run unauthenticated. Run it the other way
> round and the command hangs on a deadlock it created, while the machine
> crash-loops until Fly gives up ("exhausted its maximum restart attempts").
>
> Neither is broken when that happens: the guard is working and the machine is
> waiting. `just door-secrets` uses `--stage`, which writes the secrets and
> returns immediately.

**Fly app names are globally unique across all of Fly**, not per-org, so
`flotta-door` may well be taken. If the create fails on a name conflict, pick
another and set `FLOTTA_DOOR_APP` in `.env`.

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

## Step 6a — What the control plane needs to create working agents

Creating an agent provisions a machine that must boot on its own, and a box
refuses to serve without credentials. The control plane is what puts them
there, so it needs them:

```
FLOTTA_BOX_PASSWORD=<the same value the door has>
FLOTTA_MODEL=<e.g. z-ai/glm-5.2>
FLOTTA_MODEL_BASE_URL=<e.g. https://openrouter.ai/api/v1>
FLOTTA_API_KEY=<your provider key>
FLOTTA_CONTROL_URL=https://<your-app>.up.railway.app
FLOTTA_DOMAIN=flotta.dev
```

**`FLOTTA_BOX_PASSWORD` must match the door's.** The door logs into every box
on the caller's behalf using its copy; a different value locks it out of the
agent that was just created. That is also why creation does not generate one
per box.

**The last two look redundant on the control plane and are not.** It stamps
them onto every box it creates, so the box knows where to fetch git credentials
and what domain to sign commits under — and it cannot infer its own public URL.
Before this was documented, an agent created through the API booted with no
credential helper and committed as `boxes.invalid`, because creation from the
CLI had been reading them from the operator's `.env` all along.

Without these, creating an agent still returns `201` and the machine then
restarts until Fly gives up:

```
box_entrypoint.sh: HERMES_DASHBOARD_BASIC_AUTH_USERNAME: set it with: just fly-auth
Main child exited normally with code: 1
```

The box's own timeline says so — `flotta logs <box>` shows a
`fleet_secrets_missing` event naming what was absent.

---

## Step 6b — More than one agent

**A fleet with no `FLOTTA_FLY_APP_PREFIX` holds exactly one box.** `create`
adopts the existing machine on `$FLOTTA_FLY_APP` rather than adding a second,
so the second agent is refused before anything reaches Fly:

```
box b-4cdc… (eng-a) already occupies fly://little-stream-574/48ed…
```

That was not a deliberate limit — it is M1's single-box shape surviving into a
product about fleets. Two variables fix it, and they go together:

```
FLOTTA_FLY_APP_PREFIX=yourname-flotta
FLOTTA_FLY_IMAGE=registry.fly.io/little-stream-574:deployment-01M1HY4QN88V205AZR44G3C7N3
```

**The prefix** gives each agent its own Fly app, named `<prefix>-<box>`. A
prefix rather than a bare `flotta-<box>` because Fly app names are globally
unique across all of Fly, not per organisation.

**The image** is needed the moment agents have their own apps: `create` refuses
without a released image, a brand-new app has no releases, and the only thing
that makes one is `fly deploy` — which would also make the machine `create` is
trying to make. `just fly-up` builds the image; this points every new agent at
it. Read the current one with:

```bash
flyctl releases --app $FLOTTA_FLY_APP --json | jq -r '[.[]|select(.Status=="complete")][0].ImageRef'
```

Agents created before this keep working untouched: a box's app lives in its
stored endpoint (`fly://app/machine`), so only *creation* consults these.

---

## Step 7 — Give an agent a GitHub identity (optional)

Without this a box can read public repositories and nothing else: `git clone`
of a private repo fails, `git push` fails, and `gh` reports that it is not
logged in. With it, the box can clone, commit and open pull requests — **and
still holds no GitHub credential of its own.**

### How it works, in one paragraph

The box's git is configured with a credential helper that calls the control
plane, presenting a Flotta token scoped to `git:credential` and to that box.
The control plane checks the repository against that box's grants and returns
a GitHub credential for that request only. Nothing is stored on the box, so
revoking a grant takes effect on the next fetch — no redeploy, no restart.

### 7a. Give the control plane a source token

Mint a **fine-grained** GitHub PAT over the repositories you want agents to
reach. Contents read/write and pull requests; **not** administration and not
`delete_repo`. Set it on Railway:

```
FLOTTA_GITHUB_TOKEN=github_pat_...
```

Without it the credential endpoint answers 503 and says so, which is a
legitimate state rather than a broken box.

> **What this does and does not enforce.** Flotta refuses to hand a box a
> credential for a repository it was not granted. It does *not* constrain what
> the returned token can reach — the source is one fleet token, so a box that
> extracted it could use it beyond its grants. Per-box isolation is policy
> enforced by the control plane, not by GitHub. Closing that means GitHub App
> installation tokens scoped to `repository_ids`; the box side would not
> change. Scope the PAT to the repositories you are willing to lose.

### 7b. Load the identity onto the box, and grant it repositories

**A box created after this shipped already has an identity** — `flotta create`
mints and injects one, so there is nothing to run afterwards. Grant it a
repository and it can work:

```bash
uv run flotta repo grant eng-a joaoh82/flotta
uv run flotta repo list eng-a
```

For a box that predates it, or an identity that is expiring, `just box-identity
eng-a` re-issues one and restarts the machine.

Either way the token's subject is that box, so it cannot mint credentials for
any other box's repositories. A box can hold several grants: a
task that fixes a bug in one repo and updates the client in another is one
task, not two.

### 7c. Prove it

Ask the agent to clone the private repo, make a commit and push a branch. The
commit will be authored by `eng-a <eng-a@$FLOTTA_DOMAIN>` — **provided
`FLOTTA_DOMAIN` was set in `.env` when you ran `just box-identity`**, which is
the only channel that carries it to the machine. Otherwise the box falls back
to `eng-a@boxes.invalid`.

Set `FLOTTA_GIT_EMAIL_DOMAIN` instead to put agent commits on a different
domain you own. Either way, changing it means re-running `box-identity` — the
box reads the value at boot, from its secrets.

> **Not a `users.noreply.github.com` address, and this is deliberate.** The
> legacy noreply format is `<login>@users.noreply.github.com` and GitHub still
> links it to that account. A box named `eng-a` had its first commit attributed
> to github.com/Eng-A — a real person. The address a box commits under has to
> be one nobody can hold, which is why the fallback is the reserved `.invalid`
> TLD rather than a name that merely looks unclaimed.

If it cannot, the reason is on git's stderr, prefixed `flotta:` — the box's
helper passes the control plane's own words through rather than paraphrasing
them.

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
