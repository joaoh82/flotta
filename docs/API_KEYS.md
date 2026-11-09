# API keys

An API key lets a client's own systems start one agent's runs: a script, a backend, a nightly job. It is for code that calls Flotta when something happens on its side. For a system that can only post to a URL (a CRM, a form, a Zap), a webhook is simpler: see `WEBHOOKS.md`.

## Making one

The agency makes a key, or anyone holding `fleet:write` for that agent's client:

```sh
flotta key add eng-a --name crm
```

or through the API:

```sh
curl -X POST "$FLOTTA_CONTROL_URL/api/boxes/eng-a/keys" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "crm"}'
```

**The answer's `key` is shown that once.** The store keeps only a hash of it, so it can't be read back. A key that was lost is revoked and replaced. `prefix` is its first few characters, kept so that a list of keys can tell them apart.

`flotta key ls eng-a` (`GET /api/boxes/<agent>/keys`) lists an agent's keys, newest first, with when each was last used (to the minute). Revoked keys stay on the list. `flotta key rm <key id>` (`DELETE /api/keys/<id>`) revokes one: its next request is refused, and the runs it started stay.

## Using one

Send it as a bearer token, `Authorization: Bearer flotta_key_…`. It starts and reads runs on these three routes:

| | |
| --- | --- |
| `POST /api/boxes/<agent>/runs` | `{"prompt": "…", "input": …}` starts a run. `202` with the run, `pending` until the agent is free. |
| `GET /api/runs/<run id>` | The run: its status, steps, output and cost. |
| `GET /api/boxes/<agent>/runs` | The agent's runs, newest first, each with what it cost. `?since=2026-11-01` (a day, or a moment with its zone) lists those made from then on, and `?status=failed,budget_blocked` only those that ended one of those ways, and `?limit=` up to 200. |

`GET /api/me` answers a key too, as it answers any live credential: with the key's own claims, its subject (`key:<id>`), org, client and `run:create`. Its `expires_at` is about a minute away, because a key is turned into a token for each request. The key itself doesn't expire: revoking it is what stops it.

```sh
curl -X POST "$FLOTTA_CONTROL_URL/api/boxes/eng-a/runs" \
  -H "Authorization: Bearer $FLOTTA_KEY" -H "Content-Type: application/json" \
  -d '{"prompt": "Summarise the new ticket.", "input": {"ticket": 4182}}'
```

## What a key can't do

A key is for **one agent**. On another agent, even the same client's, the answer is the 404 of no agent at all. It holds one scope, `run:create`, that no role and no minted token carries, so every other route refuses it with a 403, except `/api/me` above. That covers:

- opening a conversation, here or at the front door (a key is not `box:chat`);
- reading the agent, its timeline, its triggers or its keys;
- changing anything, or making another key.

A key whose agent is destroyed stops working with it.

The prompt a key sends is the run's instruction, the same as one the agency sends through the API. Unlike a webhook body, it isn't fenced as data, because whoever holds the key was trusted with the agent's work. Give keys only to systems the agency would let give the agent instructions.
