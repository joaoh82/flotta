# The box contract: bring your own agent

**Built in M17 (FLOTTA-244 to FLOTTA-248).** An agency can run an agent it built itself, in LangGraph or its own Python, as a Flotta agent. It gets the same hosting as any other: its own machine and volume, sleep between conversations, model calls metered and budgeted through the gateway, schedules and webhooks, approvals for a person, the web app's conversation, runs and usage. Nothing of Hermes is involved.

The agent is a public container image that serves a small HTTP contract, the **box contract**. The [Python](../adapters/flotta-python) and [LangGraph](../adapters/flotta-langgraph) adapters serve it for you. [The example](../examples/langgraph-support) is a complete agent, published as `ghcr.io/joaoh82/flotta-example-support`.

## Turning it on

Custom images are off for every agency until the operator switches them on, because the image is the agency's code, running on the fleet's substrate beside every other agency's agents:

```sh
flotta org custom-images <org> on     # or HQ → Agencies → "Allow its own images"
```

Then an owner or a builder creates one:

```sh
flotta create support --own-image ghcr.io/your-agency/your-agent:1.0 --org <org> --client <client>
```

In the web app, the same is "Your own image" on the New agent sheet. `PUT /api/boxes/{id}/image` (Settings → Runtime) moves an agent onto another image. Its volume stays, and the timeline's `reimaged` event records the image it ran before. Turning the switch off destroys nothing: agents keep running their images, and none can be created or moved until it is on again.

**Images are public only for now**, on ghcr.io or Docker Hub. The store refuses Fly's own registries, private-network hosts, IP addresses and any reference with credentials in it. Private registries wait until an agency asks.

## The contract, v1

The agent listens on `[::]:9119`. Fly's private network is IPv6; run it locally with `FLOTTA_HOST=0.0.0.0`. Every request carries `Authorization: Bearer $FLOTTA_BOX_SECRET`. The agent must refuse anything else, because every machine on the private network can reach that port.

```
GET  /healthz                     200 when ready
GET  /threads                     {"threads": [{"id", "title", "updated_at"}]}, newest first
GET  /threads/{id}                {"id", "messages": [{"role": "user"|"assistant", "text"}]}, 404 for none
POST /run                         {"run_id", "thread_id", "input"} -> 200, NDJSON, one event a line:
    {"type": "message.delta", "text"}
    {"type": "tool.start", "id", "name", "args"?}
    {"type": "tool.complete", "id", "error"?, "exit_code"?, "output"?, "duration_s"?}
    {"type": "approval.request", "id", "description", "command"?}
    {"type": "done", "reply"?}        or        {"type": "error", "message"}
POST /run/{run_id}/approval       {"id", "choice": "once" | "deny"}
```

- **A run lasts as long as its request.** The stream ends with `done` or `error`. If it ends without either, the run failed.
- **When Flotta hangs up, the run is over.** If a person closes the conversation mid-turn, the door keeps the request open until the turn ends.
- **`thread_id` is stable for a conversation.** The agent keeps the conversation's history, under `/data`, which is its volume and survives sleep and restarts. Flotta reopens a conversation by asking `/threads/{id}`.
- **Approvals offer only `once` and `deny`.** `once` is the only yes. Hermes's `session` and `always` mean nothing to an agency's code, and the web app offers only these two.
- **A run's approvals belong to that run.** One still open when the run ends is withdrawn.
- **`run_id` names the Flotta run.** Send it as `X-Flotta-Run` on model calls, and the usage lands on the run that spent it. The adapters do this.

## What the agent is given

| Variable | What it is |
|---|---|
| `FLOTTA_BOX_SECRET` | The Bearer the door sends. It is derived per agent, so it opens this agent and no other. |
| `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL` | Flotta's model gateway, under the names OpenAI's SDKs read. `ChatOpenAI()` works with no configuration. The key is this agent's own gateway token. |
| `FLOTTA_RUNTIME=custom`, `FLOTTA_BOX_ID`, `FLOTTA_BOX_NAME` | Who it is. |

**It is never given a provider key.** A Hermes agent off the gateway gets the fleet's provider key, which sits inside Flotta's own image. A custom image is the agency's to read, so off the gateway it gets no key at all, and creation records the gap. **It gets none of Hermes's secrets either:** no dashboard login, and no GitHub credential helper.

## What it does not get

The control plane refuses each of these with 409 `custom_runtime`, and the web app hides them:

- instructions (`SOUL.md`);
- the model switch;
- tool rules;
- MCP grants and the gateway switch;
- the screen;
- handoffs;
- the Hermes upgrade. `POST /api/hermes/update` skips custom agents rather than re-imaging them onto Hermes.

**Repositories and the GitHub identity are hidden, not refused.** The web app shows neither for a custom agent, and its image has no credential helper to use a grant. The API still records a repository grant for one, which does nothing. Hermes's skills simply don't exist on a custom image.

On the conversation socket, the screen (`display.*`), attachments and everything else Hermes-specific are refused by name. A conversation offers no Attach.

## How it works

The door does the translation, in one place (`src/flotta/door/contract.py`):
- **To the agent,** it speaks the contract.
- **To its own callers,** it presents the Hermes JSON-RPC subset they already use: `gateway.ready`, `session.create` / `most_recent` / `list` / `resume`, `prompt.submit`, approval requests and their answers, and `session.active_list` for liveness.
- **The login and ticket** that clients do before the socket are answered by the door without waking the agent.

So the web app's conversation, the run loop (`runner.through_door`) and the door's own recording of turns, steps and approvals are the same code for both runtimes.

**Readiness:** the door's wait-for-ready asks `/healthz` with the agent's secret instead of Hermes's `/`.

**Known limit:** if the door's own connection to the agent drops mid-run, the run is recorded as failed. Hermes can pick a running turn back up after a reconnect; the contract has no reattach.

## Trying it for $0

`just local` runs the whole fleet on this machine. It includes a model gateway whose fake model calls a tool when a message names one, so tools and approvals work without a model key. On a local fleet, `local:<dir>` runs a directory's `agent.py` as the agent. The directory needs a `pyproject.toml`, and its dependencies are installed into the agent's own directory:

```sh
just local                                   # terminal 1
just org-custom-images default on            # terminal 2
just create-local support                    # examples/langgraph-support
just run support "Can you lookup order 42?"
```

`local:` is refused on any fleet but a local one.

## Where it is built

| | |
|---|---|
| flotta #259 | `boxes.runtime` and `boxes.image` (migration 42), `orgs.custom_images`, creation, re-imaging, the Hermes-only guards |
| flotta #260 | The door speaks the contract behind a Hermes-shaped socket |
| flotta #261 | The adapters, the example and its GHCR workflow, `local:` agents, the fake model's tool calls, and the handshake answered by the door |
| flotta-cloud #113 | "Your own image" in the web app, the image panel, HQ's switch |
