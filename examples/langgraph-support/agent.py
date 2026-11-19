"""A support agent for a small shop, built with LangGraph and run on Flotta.

It looks orders up on its own, and refunds one only after a person approves:
`refund` calls LangGraph's `interrupt()`, which Flotta turns into an approval
in the conversation (or in the approvals list, when a schedule or a webhook
started the run).
"""

from __future__ import annotations

from flotta_langgraph import chat_model, serve
from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

# Stands in for the shop's own order system.
ORDERS = {
    "42": {"item": "Linen shirt", "total_eur": 59.0, "status": "delivered"},
    "43": {"item": "Wool scarf", "total_eur": 35.0, "status": "in transit"},
}


@tool
def lookup_order(order: str) -> str:
    """Look an order up by its number: what was bought, its total and where it is."""
    found = ORDERS.get(order.strip().lstrip("#"))
    if found is None:
        return f"There is no order {order}."
    return f"Order {order}: {found['item']}, €{found['total_eur']:.2f}, {found['status']}."


@tool
def refund(order: str) -> str:
    """Refund an order in full. A person approves every refund before it happens."""
    found = ORDERS.get(order.strip().lstrip("#"))
    if found is None:
        return f"There is no order {order} to refund."
    approved = interrupt(
        {
            "description": f"Refund €{found['total_eur']:.2f} for order {order} ({found['item']})",
            "command": f"refund {order}",
        }
    )
    if not approved:
        return f"The refund for order {order} was not approved."
    found["status"] = "refunded"
    return f"Refunded €{found['total_eur']:.2f} for order {order}."


TOOLS = [lookup_order, refund]
INSTRUCTIONS = (
    "You are the support agent for a small online shop. Look orders up before "
    "answering questions about them. Refund only when the customer asks for it."
)
model = chat_model().bind_tools(TOOLS)


async def respond(state: MessagesState) -> dict:
    reply = await model.ainvoke([SystemMessage(INSTRUCTIONS), *state["messages"]])
    return {"messages": [reply]}


def next_step(state: MessagesState) -> str:
    return "tools" if state["messages"][-1].tool_calls else END


graph = StateGraph(MessagesState)
graph.add_node("respond", respond)
graph.add_node("tools", ToolNode(TOOLS))
graph.add_edge(START, "respond")
graph.add_conditional_edges("respond", next_step, ["tools", END])
graph.add_edge("tools", "respond")

if __name__ == "__main__":
    serve(graph)
