# Deploying Flotta

The first deployment, done by hand. Every step here should eventually be a
`just` recipe; writing it down first is how you find out which parts are
actually hard.

**Three things get deployed, in this order, and the order is forced by a
dependency you cannot skip:**

| | What | Where | Depends on |
|---|---|---|---|
| 1 | **Control plane** — the fleet API and the reconcile loop | Render | Postgres |
| 2 | **Front door** — public TLS access to a box | Fly | the control plane's URL |
| 3 | **DNS** — `*.flotta.dev` | Cloudflare | the door's IP address |

The instinct is to do DNS first. You cannot: the record points at the door's
address, and the door does not have one until it is deployed. The door in turn
needs a control-plane URL to ask "which machine is this box?".

**Running cost:** roughly $2–4/month for the always-on door, plus Render's
web service and Postgres (about $7 + $6/month on their smallest paid plans), plus whatever the boxes cost. A box suspends itself after 30 minutes
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

## Steps 1 and 2 on Render — apply the Blueprint

[`render.yaml`](../render.yaml) declares both: a project `flotta` with a
Postgres `flotta-db` and a web service `flotta-control` built from the root
`Dockerfile`. In the Render dashboard, **New → Blueprint**, point it at this
repo, and paste the values step 0 printed when it asks for each `sync: false`
variable. `FLOTTA_DATABASE_URL` is wired to the database for you.

`FLOTTA_CONTROL_URL` is the service's own address, which does not exist until
the first deploy: give it `https://flotta-control.onrender.com` (or whatever
name Render assigns) and correct it afterwards if that guess was wrong.

**Both plans in the Blueprint are paid, on purpose.** A free Render web service
sleeps when idle, which stops the reconcile loop (see below). A free Render
Postgres **expires after 30 days**, and the fleet's records go with it.

The two steps below say what each piece is and why, for anyone deploying
somewhere other than Render — the image runs on anything that builds a
Dockerfile and injects `PORT`.

**Why not Railway, which this runbook used to name.** The first deployment ran
there. On 2026-10-13 its project was deleted — by whom is not known — and the
fleet's Postgres with it. Every agent's memory survived, because it lives on
its Fly volume rather than in the control plane; see *Recovering a lost control
plane* below.

## Step 1 — Postgres

Any Postgres. Nothing else to do; the control plane creates its own schema on
first boot.

---

## Step 2 — The control plane

A service deployed from this repo's root `Dockerfile`, beside the database.

Variables:

```
FLOTTA_SIGNING_KEY=<from step 0>
FLOTTA_DATABASE_URL=<the database's internal connection string>
FLY_API_TOKEN=<flyctl tokens create org>
FLOTTA_FLY_APP=<the app `just fly-up` deploys the image into>
FLOTTA_FLY_ORG=personal
```

Wire `FLOTTA_DATABASE_URL` **by reference** where the host allows it (the
Blueprint's `fromDatabase` does) rather than pasting a literal, so it survives
a credential rotation.

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

Do not set `PORT`. Render injects it and the image honours it.

**Expect it to refuse to boot if the signing key is missing.** It binds
`0.0.0.0` and the guard fails closed: an unauthenticated fleet API is a kill
switch for every agent you own. That is the guard working.

**Do not put this service on a plan or setting that sleeps it.** The reconcile
loop is continuous background work, and sleeping it reintroduces the exact bug
it exists to fix — a task that strands with nothing watching. `/health` reports
`503` when the loop stops sweeping, so if you do sleep it, you will find out
from a health check rather than from a stranded row a week later.

Verify:

```bash
curl -s https://<your-app>.onrender.com/health | python3 -m json.tool
```

`reconcile_loop.sweeps` should be climbing and `failing` should be `false`.

---

## Step 3 — A box image, if you do not have one

Skip if `just fly-doctor` already reports a healthy box.

```bash
just fly-up        # builds the box image and boots a machine — REAL Fly spend
just fly-auth      # gives the machine a login, and the fleet a password in .env
```

`fly-auth` writes `FLOTTA_BOX_PASSWORD` into your local `.env` (keeping one
already there). Step 4 needs it. It is the fleet's password: every agent's own
is derived from it, so it goes to the door and the control plane and **never to
an agent** — this first machine included. That machine gets a login of its own
that nothing records; you reach it with `fly ssh` until `flotta create` adopts
it (step 6b), and adoption writes the password the door expects.

---

## Step 4 — The front door

**Secrets first, then deploy.** The order matters and the reverse deadlocks —
see below.

Put these in `.env`:

```
FLOTTA_SIGNING_KEY=<the SAME value the control plane has>
FLOTTA_CONTROL_URL=https://<your-app>.onrender.com
FLOTTA_DOMAIN=flotta.dev
# FLOTTA_BOX_PASSWORD is already there, written by `just fly-auth`
```

then:

```bash
just door-secrets    # stages them; mints the door's token from the same key
just door-deploy     # creates the app if needed, then deploys
```

`door-secrets` mints the control-plane token rather than having you paste one,
so it cannot drift from the key that has to verify it. It carries one scope,
`fleet:route`: the door resolves and wakes any org's box and checks the
caller's token against the answer itself, and the control plane lets that
scope reach those two routes and nothing else (M11.6a).

> **Upgrading a door from before M11.6a:** re-run `just door-secrets` and
> redeploy. Its old token (`fleet:read box:chat`) still reaches the default
> org's boxes — every box that existed before tenancy — but no other org's,
> so any tenant agent is a 404 through the door until it has the new one.

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
export FLOTTA_CONTROL_URL=https://<your-app>.onrender.com
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

## Creating an agent is asynchronous

`POST /api/boxes` answers **202** with the box id and `status: provisioning`,
then provisions on a background thread. Poll `GET /api/boxes/{id}` until it
reaches `running`.

It used to hold the request open for the whole thing — an app, a volume, a
machine and a boot — and the host's proxy (Railway's, then) gave up first:

```
HTTP 502 — Application failed to respond
```

while the work carried on. The caller was told it had failed and a machine
appeared anyway. Worse, the cancelled request took the provisioning with it,
leaving a row at `provisioning` with no endpoint and a real machine that no
Flotta verb could address, because every one of them addresses a box by its
endpoint.

The reconcile loop now sweeps boxes as well as tasks: a provision still
unfinished after 20 minutes is closed and its machine destroyed, and a row
claiming `running` about a machine that is not gets corrected. `flotta logs
<box>` shows both.

`/health` reports the commit it is running, under `build`. Use it after a
deploy — a restart caused by a changed variable looks exactly like one caused
by new code, and reading one as the other has already cost this project a
wasted machine.

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
FLOTTA_CONTROL_URL=https://<your-app>.onrender.com
FLOTTA_DOMAIN=flotta.dev
```

**`FLOTTA_MODEL` is also settable in the app** (Settings → Fleet), which is
where to look when you want to know what the default even is; a value stored
there wins over this variable for agents created afterwards. The endpoint and
key stay here — the app shows the endpoint read-only, because it decides where
the key is sent.

**`FLOTTA_WORKDIR` is the same kind of setting** — Fleet → Projects folder,
default `/workspace`. It is where a new agent clones repositories, on the
machine's disk rather than the memory volume. An image update replaces that
disk, so clones are lost and the agent re-clones on the next task. Existing
agents keep the path they were created with.

**`FLOTTA_MODEL` is the fleet's default model, not every agent's.** An agent
can be given its own — at creation (the Create form's *Model* field, or
`flotta create --model`) or afterwards (**Info → Model**, or `flotta model set`).
The endpoint and key stay fleet-wide: a per-agent endpoint with this key would
send the key to a host it was not issued for. A model id is checked against
OpenRouter's catalogue before an agent is pointed at it; other endpoints are
not checked.

**Which models agents may be given is the operator's list** (FLOTTA-85):
`FLOTTA_MODELS`, comma-separated, settable in the fleet settings
(`PUT /api/settings`) or here. `FLOTTA_MODEL` is always on it, and with the
list empty it is the only one. Every agent calls its model with this fleet's
key, so any other model is refused before a machine is touched, for an agency
and for the operator alike; `GET /api/models` is what a picker offers. Agents
already running a model that is not on the list keep it until it is changed.

**The model reaches Hermes through `config.yaml`, not the environment.** Hermes
never read `FLOTTA_MODEL`; the box's boot step (`flotta.box.inference`) writes
`model.default`, `provider` and `base_url` from it at every boot. For an
endpoint that is not OpenRouter it writes `provider: custom` and the key too —
the only arrangement in which Hermes sends a key to a non-OpenRouter host. An
image built before FLOTTA-39 has no such step, so its agents run Hermes's own
default model whatever this says.

**`FLOTTA_BOX_PASSWORD` must match the door's, and no agent holds it.** Each
agent is given its own password, derived from this one and its id
(`flotta.auth.box_password`, FLOTTA-96), and the door derives the same from its
copy, so a different value locks the door out of every agent. When every agent
held this value, Fly's private network let any agent log in to any other — an
agency's to the operator's — without passing the door, which is where tenancy
is checked.

**A fleet with agents made before FLOTTA-96 needs a fresh value, not just
rotated agents.** Each of them held this value as its own password, and root
could have kept a copy; rotating rewrites an agent's environment but not what
it kept, and with the old value as the key, a kept copy derives every new
password from an agent's id. So, in this order:

1. `just fleet-key` — a fresh `FLOTTA_BOX_PASSWORD` in `.env`, sent nowhere.
2. Set the same value on the control plane (Render → Environment).
3. `just door-secrets && just door-deploy`.
4. `uv run flotta token rotate <agent>` for **every** agent.

The same steps change it at any other time. A rotation writes the agent's
derived password along with its token; a sleeping agent stays asleep and picks
it up when it next wakes. Until an agent is rotated the door cannot log in to
it. The door never falls back to this value: an agent is root on its own
machine and could refuse its own password on purpose to be sent this one.

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
FLOTTA_FLY_IMAGE=registry.fly.io/<builder-app>:deployment-01ABC…
```

**Use `just fly-build`, not `just fly-up`, to publish a new image.** Both
release one; `fly-up` also brings up a box and leaves it running, which is what
you want for a first deployment or for checking a new image boots, and is an
orphan billing for nothing when all you wanted was the image. `fly-build`
destroys the machine it created — only that one, never a machine that existed
before it ran.

**`FLOTTA_FLY_IMAGE` is now optional, and leaving it unset is the better
default.** With it unset, the control plane resolves the fleet image from the
newest **complete** release on `$FLOTTA_FLY_APP` — the app `just fly-up`
deploys into — which is the answer that cannot go stale, because building a new
image *is* updating it.

Set it only to pin deliberately. It wins when present, the same way a stored
setting wins over the environment, and a pin that is older than the newest
build is a real trap: it silently gives every new agent an old image and makes
the app's Upgrade button move agents onto the pinned one. It has gone stale
twice here, once naming an app that had since been deleted. `GET /api/hermes`
reports the value, **where it came from** (`env`, `release` or `none`) and what
the build app last released, and the app shows a warning in each agent's Info
panel when a pin and the newest build disagree.

Note the app must be *configured*: `$FLOTTA_FLY_APP` has a default
(`flotta-box`) and Fly app names are globally unique, so the resolver refuses
to read releases from a name it was not given rather than risk reading a
stranger's registry.

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
`delete_repo`. Set it on the control plane:

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

For a box that predates it, or an identity that is expiring, renew it from the
app (the agent's **Info** panel, **Identity → Renew**) or:

```bash
uv run flotta token rotate eng-a     # or: just box-identity eng-a
```

The control plane writes the new token to **that agent's own Fly app** — the
endpoint says where it lives. A sleeping agent stays asleep and picks the
token up when it next wakes; a running one restarts. Only the token changes.

> The recipe used to mint locally and import into the one app `.env` names.
> On a per-agent fleet that is the image-build app, so it put the token on the
> wrong app and renewed nothing. If you ran it before this changed, check
> `flyctl secrets list --app <your images app>` for a stray `FLOTTA_BOX_TOKEN`.

Either way the token's subject is that box, so it cannot mint credentials for
any other box's repositories. A box can hold several grants: a
task that fixes a bug in one repo and updates the client in another is one
task, not two.

### 7b-bis. Let agents message each other (M7)

An agent's token now carries `box:peer` as well as `git:credential`, which is
what lets it ask the control plane to carry a message to a colleague.

**A box that predates M7 has a token without that scope**, and will be refused
with a 403 naming the missing scope. Renew its identity — **Info → Identity →
Renew** in the app, or:

```bash
uv run flotta token rotate eng-a
```

**Every agent may ask every other by default** (FLOTTA-65) — a new agent is
on everyone's list as soon as it is running. Stop one agent asking another
from the app (the agent's **Info** panel, **Colleagues**) or the CLI. Blocks
are **one way**:

```bash
uv run flotta peer list eng-a
uv run flotta peer block eng-a eng-b     # eng-a may no longer ask eng-b
uv run flotta peer allow eng-a eng-b
```

Agents cannot change their own list; `block` and `allow` need `fleet:write`,
which no box holds. There is no store-first path for these commands: the list
means nothing without a control plane, because the control plane is what
carries the message.

> **`box:chat` must never end up on a box.** The front door checks a token's
> scope and not its subject, so a box holding `box:chat` could reach every
> agent on the fleet regardless of any grant. That is the whole reason
> delegation is relayed rather than dialled direct.

### 7c. Prove it

Ask the agent to clone the private repo, make a commit and push a branch. The
commit will be authored by `eng-a <eng-a@$FLOTTA_DOMAIN>` — **provided
`FLOTTA_DOMAIN` (or `FLOTTA_GIT_EMAIL_DOMAIN`) was set on the control plane
when the agent was created**, which is when it is written to the machine.
Otherwise the box falls back to `eng-a@boxes.invalid`.

Set `FLOTTA_GIT_EMAIL_DOMAIN` instead to put agent commits on a different
domain you own. **Changing it for an existing agent is not supported yet**:
renewing an identity deliberately rewrites only the token, so it cannot move
an agent's commit address by accident.

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

## Step 8 — A second agency (M11.7)

The fleet you deployed is the **default org**, and you are its operator. An
agency is another org on the same control plane: its agents live at
`<agent>--<client>--<org>.flotta.dev`, and its tokens reach nothing of yours.

Needs a control plane from M11.7 on, and a door holding the `fleet:route`
token (Step 4, M11.6a) — without it the door 404s every tenant's agent.

With `$FLOTTA_CONTROL_URL` and the signing key in `.env`:

```bash
just org-create agency-x "Agency X"
just client-create agency-x acme "Acme"
just token-role alice owner agency-x          # hand this to the agency's owner
```

That is the operator's whole part. The owner, with the control plane's URL in
`$FLOTTA_CONTROL_URL` and their token in `$FLOTTA_CONTROL_TOKEN`, adds clients
and mints their people's tokens themselves, without the signing key:

```bash
just client-create agency-x globex "Globex"
just token-role sam member agency-x acme      # one client's agents, read and write
just token-role peter client_viewer agency-x acme
```

Roles map to scopes when minted (`flotta.auth.ROLES`). Revocation is still
rotating the one signing key, which revokes every token in every org — keep
lifetimes short (`--days`).

---

## Step 9 — People who sign in (M12.3)

Tokens are for machines and scripts; people sign in with a link emailed to
them, and the web app keeps the session. Give yourself an account first, as
an owner of the default org, which makes you the operator:

```bash
just user-add you@example.com owner              # through $FLOTTA_CONTROL_URL when set
just user-add alice@agency-x.example owner agency-x
```

To send links, set on the control plane:

- `FLOTTA_RESEND_API_KEY`: a Resend API key, with the sending domain verified.
- `FLOTTA_MAIL_FROM`: the sender, e.g. `Flotta <sign-in@flotta.dev>`.
- `FLOTTA_APP_URL`: the web app, e.g. `https://app.flotta.dev`; links point at
  `<it>/sign-in#<secret>`.

Without a Resend key a deployed control plane makes **no** links, and says so
at startup. It does not print them: a link in a log signs its address in for
anyone who reads that log. Locally, a control plane with no signing key prints
them, which is how sign-in is tried for $0.

`POST /api/auth/link` answers `202` for any address and is limited per address
(5) and per caller (30) every 15 minutes. **The web app's server is one caller
for every browser**, and behind Render's proxy so is everyone else, so the
web app names the browser it is asking for:

```bash
just token-mint web auth:relay     # the web app's server holds this, nothing else does
```

and sends it with the browser's address in `X-Flotta-Caller-IP`. Only an
operator token holding `auth:relay` may name a caller; anything else naming
one is refused, and a request that names nobody is counted against the address
it connected from. On Render that is Render's proxy, one bucket for every
direct caller, which is safe (a direct caller can empty only its own bucket,
never the web app's browsers') but crude.

`FLOTTA_TRUSTED_PROXY_HOPS` tells the control plane how many proxies append to
`X-Forwarded-For` in front of it, and the caller becomes the entry the
outermost of them appended. Leave it unset until that count is **checked on
the deployment** rather than assumed: set it too high and every caller names
their own bucket. Render's own description of what it appends is a forum
reply, not documentation.

### Sign-ups and invites (M12.4)

Anyone can sign an agency up (`POST /api/auth/sign-up`, from the web app). It
arrives `pending`: its people sign in, add clients and invite their team, and
it creates no agent until you answer:

```bash
just org-pending                 # what is waiting
just org-approve studio-pat      # it can create agents from now on
just org-decline studio-pat      # it never will; its people can still sign in
```

Both answers are final. An org you make yourself (`just org-create`) is active
from the start. Invites go out through the same Resend key as sign-in links;
without one the control plane refuses to send them (`503`) rather than pretend.

### Conversations in the browser (M12.5)

The web app's server asks `POST /api/boxes/{id}/ticket` with the person's
session and hands the page what comes back; the page opens the door's socket
with it. The door takes a ticket only from an origin you name, so set the web
app's on the door and redeploy it:

```bash
# in .env: FLOTTA_DOOR_ORIGINS=https://app.flotta.dev
just door-secrets
flyctl secrets deploy --app "${FLOTTA_DOOR_APP:-flotta-door}"
```

Unset, the door refuses every ticket (`4403`), and nothing else changes:
`flotta chat` still goes through with its token. A ticket is good for two
minutes, for one agent, and for nothing but that agent's conversation.

The ticket tells the page where to connect: `wss://<box>.$FLOTTA_DOMAIN/api/ws`,
or, with `$FLOTTA_DOOR_URL` set on the control plane, that door's scheme, host
and port instead (`ws://localhost:8081` for `just local`). A self-hosted door
on a port of its own needs it. The web app reads the same variable for the
page's content security policy, so set it on both or neither.

---

## What is not automated yet

Everything above should be a recipe. In rough order of how much each would
save:

- **`just deploy-control`** — `render.yaml` is the template (§8.6's M5.5);
  what is left is applying it without the dashboard. It already requires a
  signing key — a template that did not would ship the unauthenticated fleet
  API the bind guard exists to prevent.
- **`just deploy-all`** — the ordering above, with the waits.
- **DNS** — Cloudflare has an API, so `just door-dns` could apply the records
  rather than print them. Deliberately not done: a script that edits your DNS
  is a script that can take your domain offline, and the first time through you
  should see what is being asked for.

---

## Recovering a lost control plane

The control plane holds the fleet's *records*; the agents hold their own
*memory*, on Fly volumes. Losing the first loses names, grants and history,
not what any agent knows.

1. Deploy a new control plane (steps 1–2) with the **same** `FLOTTA_SIGNING_KEY`,
   so the door and every token already issued keep working.
2. Point the door at it: `flyctl secrets set --app flotta-door
   FLOTTA_CONTROL_URL=https://<new-url>`, and update your `.env`.
3. Re-register each agent by creating it again **under its old name**, with
   `FLOTTA_FLY_APP_PREFIX` matching the old apps. `create` adopts the machine it
   finds in `<prefix>-<name>` rather than making a new one, so the volume — and
   the memory — come back with it. Try the least important agent first.
4. Re-grant repositories (`flotta repo grant`) and peers; they lived in the
   lost database.
5. **Repair each agent's connection.** An adopted machine keeps the environment
   it booted with, so it still calls the *old* control plane under its old box
   id, and every git credential, `flotta-repos` and `flotta-ask` fails with a
   404 from a dead host. Its Machine reading reports this as `connection`;
   `POST /api/boxes/<name>/connection` (the console's "Repair connection")
   points it at this one and renews its token. Set `FLOTTA_CONTROL_URL` on the
   new control plane first: the repair refuses to guess its address. A running
   agent restarts; an asleep one takes it when it wakes.
