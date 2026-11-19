# A LangGraph support agent on Flotta

A support agent for a small shop: it looks orders up on its own and refunds one
only after a person approves. It is about 80 lines of ordinary LangGraph
([`agent.py`](agent.py)) plus `serve(graph)` from
[flotta-langgraph](../../adapters/flotta-langgraph). MIT licensed.

## Try it on your machine, for $0

`just local` runs a whole Flotta fleet here: control plane, front door, and a
model gateway with a fake model. The fake model calls a tool when your message
names one, so the tools and the approval can be tried without a model key.

```sh
just local                                   # terminal 1: the fleet
just org-custom-images default on            # terminal 2
just create-local support                    # this directory, as an agent
just run support "Can you lookup order 42?"  # a lookup, through the gateway
```

Then `just run support "Please refund order 42"` waits for an approval. Answer
it in the web app (flotta-cloud's `just dev` runs the fleet and the web app
together), or with `POST /api/runs/<run>/approval {"choice": "once"}`. The run's steps show the
tool call and the approval, and the gateway's usage rows carry the run's id.

## Run it on Flotta

The image is published at `ghcr.io/joaoh82/flotta-example-support:latest`.
Once your agency has custom images switched on:

```sh
flotta create support --own-image ghcr.io/joaoh82/flotta-example-support:latest \
  --org <your-org> --client <client>
```

Or choose "Your own image" when you create an agent in the web app.

## Build your own

Copy this directory and change `agent.py`. Your image must:

1. listen on `[::]:9119`, which `serve()` does;
2. refuse any request without `Authorization: Bearer $FLOTTA_BOX_SECRET`, which
   `serve()` also does;
3. be public, on ghcr.io or Docker Hub.

```sh
docker build -f examples/langgraph-support/Dockerfile -t ghcr.io/you/agent:1 .
docker run -e FLOTTA_HOST=0.0.0.0 -e FLOTTA_BOX_SECRET=dev -e OPENAI_API_KEY=unused \
  -p 9119:9119 ghcr.io/you/agent:1
curl -H 'Authorization: Bearer dev' localhost:9119/healthz
```

The Dockerfile is built from the repository root because the adapters are not
on PyPI yet.
