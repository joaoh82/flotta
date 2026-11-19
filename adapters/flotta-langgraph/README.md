# flotta-langgraph

Run a [LangGraph](https://langchain-ai.github.io/langgraph/) agent on
[Flotta](https://github.com/joaoh82/flotta) with one call: `serve(graph)`. MIT
licensed, built on [flotta-agent](../flotta-python).

```python
from flotta_langgraph import chat_model, serve

model = chat_model().bind_tools(TOOLS)   # ChatOpenAI on Flotta's gateway
builder = StateGraph(MessagesState)
...                                      # your nodes and edges
serve(builder)                           # compiled here, checkpointed on /data
```

## How the graph maps onto Flotta

- **The reply** is streamed from any node's chat model as it writes. The run's
  answer is the last AI message.
- **Tool steps:** each tool call an AI message makes is a step in the run's
  timeline, ended by its `ToolMessage`. One with `status="error"` is a failed
  step.
- **Approvals:** `interrupt(...)` asks a person. Pass a string, or a dict with
  `description` (or `message`) and an optional `command`. The graph resumes
  with `True` if they approved and `False` if not:

  ```python
  approved = interrupt({"description": "Refund €59 for order 42", "command": "refund 42"})
  ```

- **Memory:** a Flotta conversation is a LangGraph `thread_id`. Pass the
  `StateGraph` itself, and it is compiled with a SQLite checkpointer at
  `$FLOTTA_DATA/langgraph.sqlite`, on the agent's volume. A compiled graph must
  bring its own checkpointer, or an approval could not resume it.
- **Metering:** `chat_model()` is a `ChatOpenAI` on `OPENAI_BASE_URL` that names
  the current run on every request, so usage lands on the run that spent it.
  With another client, send `run_headers()`.

The input is `{"messages": [{"role": "user", "content": message}]}`. For a
different state shape, pass `to_input=`.

See [the example](../../examples/langgraph-support) for a complete agent.
