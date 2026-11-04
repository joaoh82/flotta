# Budget alerts

An org hears when a budget reaches its soft threshold (`soft_pct`, 80% by default), and when it reaches its limit. Each fires once per budget per period, by email to the org's owners and to an optional alert address, and by a signed webhook if the org has one.

Alerts are sent by the control plane within about a minute of the sweep that sees the threshold. Nothing about them can slow a model call: the gateway doesn't send them.

## Setting them

An owner sets them through the API. Each field is optional, and `null` removes it.

```sh
curl -X PUT "$FLOTTA_CONTROL_URL/api/orgs/$ORG/alerts" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"alert_email": "ops@example.com", "webhook_url": "https://example.com/flotta"}'
```

**The answer carries `webhook_secret`, and only that once.** Setting the webhook again, even to the same URL, makes a new secret. `GET` on the same path shows where alerts go and how the latest deliveries went, and never the secret.

Rotating the control plane's signing key changes every webhook's secret, so set them again afterwards.

A webhook URL must be `https`, with a name whose addresses are all public. It's checked again when each alert is sent. Redirects aren't followed.

## What a webhook receives

A `POST` with a JSON body:

```json
{
  "id": "be-3f1c…",
  "type": "budget.limit",
  "org": "studio-nord",
  "client": "acme",
  "box": null,
  "period_start": "2026-11-04T00:00:00+00:00",
  "occurred_at": "2026-11-04T15:02:11+00:00",
  "summary": "Client acme's budget has reached its limit: EUR 20.01 of EUR 20 a day. …",
  "budget": {"id": "…", "scope": "client", "period": "day", "limit_eur": "20", "spent_eur": "20.01", "soft_pct": 0.8, "action": "pause", "fallback_model": null, "resets_at": "2026-11-05T00:00:00+00:00"}
}
```

`type` is `budget.soft_limit` or `budget.limit`. `box` and `client` are `null` for a budget wider than one.

Headers:

- `Flotta-Signature`: `t=<unix seconds>,v1=<hex>`
- `Flotta-Delivery`: an id that stays the same across retries of this alert to this URL

## Answering, and retries

Answer with a `2xx` within 10 seconds. Anything else counts as a failure, including a redirect. A delivery is tried 5 times in all, after waits of about 1 minute, 5 minutes, 30 minutes and 2 hours. If it still fails, a `budget_events` row of type `alert_failed:<threshold>:<channel>` records it.

**A receiver can occasionally get the same alert twice,** if the control plane restarts between sending it and recording that it went. Use `Flotta-Delivery` to ignore a repeat.

## Checking the signature

1. Split the header into `t` and `v1`.
2. Compute HMAC-SHA256, keyed with the secret, over `t`, then `.`, then the **raw** request body, and hex-encode it.
3. Compare it to `v1` in constant time.
4. Refuse a `t` more than 5 minutes from your clock, which stops a recorded request being replayed.

For example, the secret `whsec-example`, the timestamp `1793800000` and the body `{"id":"be-example","type":"budget.limit"}` give:

<!-- This line is checked by src/flotta/test_alerts.py. -->
```text
t=1793800000,v1=ad8286278cc0cb95906884286ea7400025c4b4e4e2c8802af796a2b728e56937
```

In Python:

```python
import hashlib, hmac, time

def verify(secret: str, header: str, body: bytes) -> bool:
    fields = dict(part.split("=", 1) for part in header.split(","))
    signed = fields["t"].encode() + b"." + body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    fresh = abs(time.time() - int(fields["t"])) <= 300
    return fresh and hmac.compare_digest(expected, fields["v1"])
```

## Trying it locally

`just local` prints emails to its terminal and lets a webhook be local:

```sh
just webhook-receiver <webhook_secret>   # listens on http://127.0.0.1:8184
```
