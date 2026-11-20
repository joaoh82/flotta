# flotta-agent

Run your own Python agent on [Flotta](https://github.com/joaoh82/flotta): one
async function, served the way Flotta's door expects. MIT licensed.

Flotta hosts it like any other agent. It sleeps between conversations and
keeps its memory on a volume. Its model calls are metered and budgeted, and
schedules and webhooks can run it. Approvals reach a person in the web app or
by email.

```sh
pip install flotta-agent            # add [openai] for ctx.openai()
```

```python
from flotta_agent import agent

@agent
async def support(message, ctx):
    async with ctx.tool("lookup_order", {"order": "42"}) as step:
        step.output = {"status": "delivered"}
    if await ctx.approval("Refund €59 for order 42", command="refund 42"):
        yield "Refunded."
    else:
        yield "I won't refund it without an approval."

support.serve()
```

## What `ctx` gives you

| | |
|---|---|
| `yield "text"` / `ctx.say(text)` | The reply, shown as it arrives. A coroutine may `return` the reply instead. |
| `async with ctx.tool(name, args) as step` | A tool step in the run's timeline. Set `step.output`; an exception marks it failed. |
| `await ctx.approval(description, command=...)` | Ask a person, and wait. `True` to go ahead, `False` if denied. |
| `ctx.thread_id`, `ctx.history` | The conversation, and its earlier messages. Flotta keeps the thread id stable. |
| `ctx.openai()` | An `openai.AsyncOpenAI` on Flotta's model gateway, metered to this run (`pip install flotta-agent[openai]`). |
| `ctx.headers` | `X-Flotta-Run`, for any other client: send it and the call is metered to this run. |

Model access comes in the environment, under the names OpenAI's SDKs read:
`OPENAI_BASE_URL`, `OPENAI_API_KEY` and `OPENAI_MODEL`. There is no provider key
on the machine, only the gateway.

## The contract it serves

All of it, with what an agent is given and what it is not, is in
[docs/CONTRACT.md](../../docs/CONTRACT.md).

On `[::]:9119`. Every request must carry `Authorization: Bearer $FLOTTA_BOX_SECRET`,
and anything else is refused, because other machines on the private network can
reach the port.

```
GET  /healthz                     200 when ready
GET  /threads                     {"threads": [{"id", "title", "updated_at"}]}, newest first
GET  /threads/{id}                {"id", "messages": [{"role", "text"}]}, 404 for none
POST /run                         {"run_id", "thread_id", "input"} -> NDJSON events:
    {"type": "message.delta", "text"}
    {"type": "tool.start", "id", "name", "args"?}
    {"type": "tool.complete", "id", "error"?, "output"?, "duration_s"?}
    {"type": "approval.request", "id", "description", "command"?}
    {"type": "done", "reply"}  or  {"type": "error", "message"}
POST /run/{run_id}/approval       {"id", "choice": "once" | "deny"}
```

Conversations are kept as JSON under `$FLOTTA_DATA/threads/`. `$FLOTTA_DATA`
defaults to `/data`, which is the agent's volume on Flotta. A run lasts as long
as its request: if Flotta hangs up, the run is cancelled.

## Running it yourself

```sh
FLOTTA_BOX_SECRET=dev FLOTTA_HOST=0.0.0.0 FLOTTA_DATA=./data python agent.py
curl -H 'Authorization: Bearer dev' localhost:9119/healthz
```

`FLOTTA_HOST` defaults to `::`, because Flotta's private network is IPv6. Use
`0.0.0.0` when you run the image under Docker on your own machine.
