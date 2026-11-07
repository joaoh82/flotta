# Webhooks

A webhook starts an agent's run when a client's system calls it: a CRM when a lead arrives, a form when it's submitted, a Zap. The system posts JSON, and the agent is told the webhook's prompt with that JSON after it.

## Making one

The agency, or a client's own systems with a `fleet:write` token, add a webhook to an agent:

```sh
flotta trigger add eng-a --webhook --prompt "Qualify the lead {{body.title}} and reply to {{body.email}}."
```

or through the API:

```sh
curl -X POST "$FLOTTA_CONTROL_URL/api/boxes/eng-a/triggers" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"kind": "webhook", "prompt": "Qualify the lead {{body.title}}."}'
```

**The answer carries `url` and `secret`, and the secret only that once.** It isn't stored: it's derived from the control plane's signing key and a value on the trigger, so it can't be read back. `flotta trigger secret <id>`, or `POST /api/triggers/<id>/secret`, makes a new one, and the old one stops working at once. Rotating the signing key changes every webhook's secret.

The URL is `<control plane>/hooks/<trigger id>`. It doesn't name the org or the client. A URL ends up in a CRM's settings, in tickets and in logs, and the trigger's id already says whose agent it is.

## Calling one

`POST` the URL with a JSON body, at most 64 KB, and the secret in one of three ways:

| How | Header | Who can send it |
| --- | --- | --- |
| A signature | `Flotta-Signature: t=<unix time>,v1=<hex HMAC-SHA256 of "<t>.<body>">` | Your own code. The same format as Flotta's alerts (`ALERTS.md`). Refused more than 300 s from the control plane's clock, so a captured call can't be replayed. |
| A bearer secret | `Authorization: Bearer <secret>` | HubSpot workflows ("Send a webhook", authentication "API key", key name `Authorization`, location request header, value `Bearer <secret>`). Zapier's "Webhooks by Zapier" (a header). |
| Basic auth | `Authorization: Basic …`, the secret as the password, any username | Pipedrive, whose webhooks can send nothing else. Zapier too. |

Only a signature proves the body wasn't changed on the way, so use it where you can. The secret is never accepted in the query string: URLs are written to logs.

To try one:

```sh
FLOTTA_HOOK_SECRET=<secret> flotta hook send <trigger id> '{"title": "New lead"}' --wait 30
```

## What it answers

| | |
| --- | --- |
| `202 {"run_id", "status"}` | The run is made and waits for its agent. Read it at `GET /api/runs/<run_id>`. |
| `200 {"run_id", "status", "output"}` | With `?wait=N` (at most 30), when the run ended within N seconds. Otherwise still `202`. |
| `404` | No such webhook. Also a missing, wrong or expired secret, and a webhook that's off: the answer is the same, so it doesn't say which ids exist. |
| `413` / `415` | The body is over 64 KB, or isn't JSON. |
| `429` | More than 30 calls in a minute to one webhook. |

An agent with three runs already waiting gets the next one recorded as `cancelled` with `queue_full`, and a `webhook_dropped` event, instead of a pile it will never get through. A paused agent's run is recorded and blocked without waking it, as for any run.

## What the agent is told

The webhook's prompt, with each `{{body.path}}` replaced by that value from the body: `{{body.contact.name}}`, `{{body.items.0.sku}}`. A path that isn't there is empty. A value is put in as JSON, so a string arrives in quotes, and it's cut at 500 characters.

Then the body itself, between two markers, labelled as data from outside Flotta.

**The body is untrusted.** Whoever can call a webhook can put words in front of an agent. So the body is never part of the instructions: it comes after the prompt, fenced, with the agent told not to follow anything in it. The markers carry a token picked for each run, so a body can't close the fence and carry on as if it were the prompt. A value quoted into the prompt can't start a line of its own. Write the prompt as the instruction, and treat what the body says as what the agent works on.

The run keeps the body in its input, whole if it fits in 64,000 characters of JSON, otherwise as text, cut, and marked `clipped`.
