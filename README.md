# Flotta

**An open-source runtime for running persistent AI agents for multiple clients, each isolated with its own machine, memory, budget and activity log.**

[![CI](https://github.com/joaoh82/flotta/actions/workflows/ci.yml/badge.svg)](https://github.com/joaoh82/flotta/actions/workflows/ci.yml)

Flotta (Italian for *fleet*) hosts agents. Each agent is a long-lived machine that runs
[Hermes Agent](https://github.com/NousResearch/Hermes-Agent), keeps its memory on its own
volume, and sleeps when nobody is talking to it. Hermes is the engine inside the machine: the
agent loop, the tools and the memory. Flotta creates the machines, routes conversations to
them, keeps clients apart, meters what each one spends and records what each agent did.

The managed version is at [flotta.dev](https://flotta.dev).

## What it is for

The main use case is an agency or a freelancer who looks after software for several clients.

Say you have a maintenance contract with a client. You create an agent for that client and give
it access to the client's repository. The client talks to the agent in plain language: "the
footer still says 2025", "add a field for the VAT number to the sign-up form". The agent files
each request, and Flotta rates it from trivial to a whole project. Small requests start at once
and usually end as a pull request. Larger ones wait until you approve them. You set that line
per agent, and you set a monthly budget per client so a busy month cannot become a surprise bill.

The same runtime hosts other agents you would otherwise run by hand for a client: a support
agent, a report on a schedule, an agent that reacts to a webhook from the client's systems.

```
 web app · CLI · your own client             ┌─ agent: dev--acme--studio ───┐
        │  wss://dev--acme--studio.<domain>  │  hermes serve                │
        ▼                                    │  /data/hermes (its memory)   │
   front door ── checks org and client ────▶ └──────────────┬───────────────┘
        │        wakes the agent if asleep                  │ every model call
        ▼                                                   ▼
   control plane ── fleet API, reconcile loop     model gateway ── budgets, usage
        │
     Postgres
```

An agent never writes fleet state. A machine that dies mid-conversation writes nothing, so the
control plane, which outlives it, records what happened.

## Features

Each item names where it lives in this repository.

- **Client tenants.** An org is an agency and a client is one of its customers. Every agent
  belongs to one client and is addressed as `<agent>--<client>--<org>.<domain>`. Every fleet API
  route is scoped by tenant, the front door refuses another tenant's address before anything is
  woken, and another tenant's agent answers 404, the same as one that does not exist. A test
  reads the route table and fails the build if a route is not scoped.
  (`src/flotta/auth.py`, `src/flotta/door/`, `src/flotta/control/test_tenancy.py`)
- **People and sign-in.** Users, memberships and sign-in by emailed link. Roles are `owner`,
  `member`, `client_user` (talks to the client's agents) and `client_viewer` (reads only).
  A new sign-up is a `pending` org that cannot create an agent until the operator approves it.
  (`src/flotta/control/app.py`, `src/flotta/test_accounts.py`)
- **One machine and one memory per agent.** A Fly Machine with a volume at `/data/hermes`, so
  memory survives sleep, restarts and image updates. Idle agents are suspended and the next
  request wakes them. (`fly/`, `src/flotta/backends/fly_backend.py`, `src/flotta/provision.py`)
- **Per-client budgets.** A limit in euros per day or month, on an org, a client or a single
  agent. The model gateway checks it before every model call and refuses the call once the limit
  is reached; an error in the check is also a refusal. At the limit a budget can notify, pause
  or stop the agents it covers, or switch them to a cheaper model. Alerts go by email and by
  signed webhook. (`src/flotta/budgets.py`, `src/flotta/gateway/`, `src/flotta/alerts.py`,
  `docs/ALERTS.md`)
- **Metering.** The gateway writes one usage row per model call, and the reconcile loop records
  machine time and storage per hour. Usage is summed by day, client, agent or model through the
  API. (`src/flotta/metering.py`, `src/flotta/usage_report.py`, `GET /api/orgs/{org}/usage`)
- **Activity timeline with cost per run.** Every agent has an event timeline (`flotta logs`). A
  run is one unit of work, with what started it, its steps, its output and its cost, summed from
  the usage rows the gateway attributed to it. Runs start from the API, a schedule, a signed
  webhook, an API key or an approved request. (`src/flotta/runs.py`, `src/flotta/runner.py`,
  `src/flotta/schedules.py`, `docs/WEBHOOKS.md`, `docs/API_KEYS.md`)
- **Task triage by size.** An agent files the work it is asked for (`flotta-request` on the
  agent's machine). The control plane asks a judge model, TypeSafe's Jev, for the kind of work,
  its complexity (trivial, small, medium, large, project) and its urgency. Work at or below the
  agent's level starts at once; anything above it, and anything the judge could not rate, waits
  for a person. The default level is "small". (`src/flotta/judge.py`,
  `src/flotta/work_requests.py`, `PUT /api/boxes/{id}/request-policy`)
- **Approvals.** When Hermes flags a command for approval, it waits for an agency person to allow
  or deny it, and those people are emailed if nobody answers within a minute. An agency can
  let a client's own people answer their agent's approvals. (`src/flotta/approval_mail.py`,
  `/api/approvals`)
- **Credential handling.** No upstream credential is stored on an agent's machine. A GitHub
  clone or push asks the control plane for a token for that one repository, each time, and
  revoking access takes effect on the next fetch. With the gateway in use, an agent calls models
  with its own short-lived token and never holds the provider key. Credentials the control plane
  stores, such as an MCP server key, are sealed with AES-256-GCM under a key kept outside the
  database. (`src/flotta/box/git_credential.py`, `src/flotta/repo_tokens.py`,
  `src/flotta/sealed.py`)
- **Connectors and custom MCP servers.** The operator keeps a catalogue of MCP servers, which can
  include any server you run. An agency connects a server with a key or an OAuth sign-in, and
  grants it per agent, choosing which tools the agent sees. A client can connect its own account,
  which its agents use in place of the agency's. Calls go through the gateway, which checks the
  grant on every request and adds the credential. (`src/flotta/mcp.py`,
  `src/flotta/mcp_oauth.py`, `src/flotta/gateway/mcp.py`)
- **Ask first.** Each MCP grant has an "ask first" switch, on by default, so the agent asks a
  person before it calls that server's tools. (`src/flotta/mcp.py`)
- **Rules per agent.** What an agent must always do, never do and be careful with, plus commands
  it may never run and tools it does not get. The first three are instructions to the model; the
  last two are enforced by Hermes outside the model. (`src/flotta/rules.py`)
- **Pull requests.** The control plane tracks the pull requests agents open, their reviews, and
  whether a merge has been deployed to production. (`src/flotta/pulls.py`)
- **A screen, if you want one.** An agent can have a desktop and browser that a person can watch
  and take over, for a sign-in or a payment step. (`src/flotta/handoffs.py`, `fly/screen_tools.py`)
- **A CLI, a fleet API and a fleet view.** `flotta` (every command takes `--json`), the control
  plane's HTTP API, and `dashboard/`, a small Next.js view of the fleet.

## Open source and managed

This repository is the runtime. Self-hosted, it gives you:

- the control plane and fleet API, the front door, the model gateway and the agent image;
- tenancy, accounts, sign-in by link, budgets, metering, runs, triggers, triage, approvals,
  MCP servers and rules, all over HTTP;
- the `flotta` CLI and the `dashboard/` fleet view.

It does not include the screens people click through. The managed platform at
[flotta.dev](https://flotta.dev) adds:

- the web app for agencies and their clients: conversations, the requests and approvals pages,
  agent settings, budgets, usage and a CSV export for invoicing;
- a portal where a client sees its own requests and what was done;
- sign-up, plans and billing;
- running and upgrading the infrastructure for you.

The web app talks to this runtime only through its HTTP API, so anything it does, you can do
with the API. When the hosted product needs something new from the runtime, it is added here
as an API first.

## Quickstart: a fleet on your machine

`just local` runs the control plane and the front door on your machine, with every agent a
local stand-in for Hermes. It needs no Fly account and no model key, and it costs nothing. The
stand-in answers with scripted replies, so this proves the plumbing, not the agent.

You need Python 3.11, [uv](https://docs.astral.sh/uv/) and [just](https://github.com/casey/just).

```bash
git clone https://github.com/joaoh82/flotta
cd flotta
just local                # control plane on :8080, front door on :8081
```

`just local` keeps its state in `~/.flotta/local` (set `FLOTTA_LOCAL_DIR` to move it) and stops
everything on Ctrl-C. In a second terminal, act as the operator:

```bash
export FLOTTA_CONTROL_URL=http://127.0.0.1:8080
export FLOTTA_SIGNING_KEY="$(cat ~/.flotta/local/signing-key)"

uv run flotta org create studio --name "Studio Nord"
uv run flotta client create studio acme --name "Acme"
OWNER=$(uv run flotta token mint ana --org studio --role owner --days 1)
```

As the agency's owner, create an agent for the client, give it some work and set a budget:

```bash
curl -s -X POST $FLOTTA_CONTROL_URL/api/boxes \
  -H "Authorization: Bearer $OWNER" -H 'content-type: application/json' \
  -d '{"name": "dev", "client": "acme", "description": "Maintenance developer"}'
# answers with the agent's id, b-...; it is running a few seconds later

AGENT=b-...               # the id from the answer above
curl -s -X POST $FLOTTA_CONTROL_URL/api/boxes/$AGENT/runs \
  -H "Authorization: Bearer $OWNER" -H 'content-type: application/json' \
  -d '{"prompt": "Change the footer year to 2027"}'
curl -s $FLOTTA_CONTROL_URL/api/boxes/$AGENT/runs -H "Authorization: Bearer $OWNER"
# the run is "done", with the stand-in's reply as its output and its cost (zero here)

curl -s -X PUT $FLOTTA_CONTROL_URL/api/orgs/studio/budgets \
  -H "Authorization: Bearer $OWNER" -H 'content-type: application/json' \
  -d '{"client": "acme", "period": "month", "limit_eur": "50", "action": "pause"}'

curl -s $FLOTTA_CONTROL_URL/api/boxes/$AGENT/request-policy -H "Authorization: Bearer $OWNER"
# {"auto_level": 1, "auto_level_name": "small", ...}

FLOTTA_STORE=~/.flotta/local/fleet.db uv run flotta logs $AGENT
# the agent's timeline: provisioning, running, then the run's created, started, done
```

The same API serves a deployed fleet.

## Deploying your own fleet

[`docs/DEPLOY.md`](docs/DEPLOY.md) is the runbook. In short:

| Part | Where it runs | Notes |
|---|---|---|
| Control plane | anywhere that runs a container (`Dockerfile`; `render.yaml` for Render) | always on: the reconcile loop must not sleep |
| Database | Postgres, set by `$FLOTTA_DATABASE_URL` | SQLite when unset |
| Front door | a Fly app (`fly/door/`, `just door-deploy`) | it reaches agents over Fly's private network |
| Model gateway | a Fly app (`fly/gateway/`, `just gateway-deploy`) | needed for budgets and metering |
| Agents | Fly Machines with volumes (`fly/`, `just fly-up`) | they bill while they exist, even asleep |
| DNS | a wildcard record for the door's domain | `just door-dns` prints the records |

`just deploy-config` prints every secret a first deployment needs and where each one goes. The
recipes that touch Fly cost money and are never run in CI.

Any OpenAI-compatible model endpoint works, set with `FLOTTA_MODEL`, `FLOTTA_MODEL_BASE_URL` and
`FLOTTA_API_KEY`.

## Security

- **Tokens.** Every `/api/*` route needs a signed token with explicit scopes, such as
  `fleet:read`, `fleet:write`, `box:destroy` and `box:chat`, and the org and client it belongs
  to. People sign in by emailed link and get sessions that can be ended. `/health` is open and
  reports only whether the reconcile loop is running.
- **No public bind without auth.** The control plane refuses to listen on a public address when
  `$FLOTTA_SIGNING_KEY` is unset.
- **Tenancy is checked where requests arrive**: in the door before an agent is woken, and on
  every fleet API route.
- **Credentials stay off agents**, as described under Features. An agent's own token reaches
  only its own records.

## Limitations

- **An agent has root on its own machine.** Rules, "careful" lists and approvals protect against
  mistakes and against people in a conversation, not against an agent that sets out to get round
  them. Flotta keeps clients apart; inside one agent's machine, the agent can do what it likes.
- **Triage depends on an outside service.** Rating requests needs a TypeSafe API key
  (`$TYPESAFE_API_KEY`). Without one, every request waits for a person.
- **Budgets apply only to calls through the gateway.** An agent set to call its provider directly
  is neither metered nor capped. A budget can also be overrun by the calls already in flight when
  it is reached: at most one call's cost per call running at that moment.
- **Fly is the only real substrate.** The `Backend` interface allows others; the only other
  implementation is the local stand-in.
- **Agents do not share what they learn.** Each keeps its own memory.
- **Tokens minted from the signing key cannot be revoked one by one.** They last until they
  expire, unless the key is rotated, which ends every token at once. Sign-in sessions can be
  ended individually.
- **The CLI is behind the API in places.** `flotta create` writes to a local store rather than
  the control plane, `flotta chat` always dials `https://<agent>.$FLOTTA_DOMAIN` and cannot reach
  a local door, and the CLI's operator token finds agents by name in the default org only. Use
  the API, as the quickstart does.
- **`app/` is a retired desktop app** and will be removed. `dashboard/` still says "worker" in
  places.

## Development

```bash
just                     # list every recipe
just check               # lint and tests, offline and free
just check-dashboard     # tsc and eslint for dashboard/
just test-one <keyword>  # a single test
just ci                  # what CI runs
```

The test suite is hermetic: every Fly call is injected, so it needs no network, credentials or
`flyctl`. CI also runs the store's tests against a real Postgres.

The project's history, including the v0.1 design this README used to describe, is in
[`docs/history.md`](docs/history.md).

## License

[AGPL-3.0](LICENSE) for the whole repository.

The runtime holds clients' agents, credentials and data, so the people who depend on it should
be able to read and change it. The AGPL's network clause means anyone who runs a modified Flotta
as a service must publish their changes, which keeps improvements to the runtime in the open,
ours included. The hosted web app, billing and website are a separate product that uses this
runtime only through its API.
