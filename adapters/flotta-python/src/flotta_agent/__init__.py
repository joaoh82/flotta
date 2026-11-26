"""Run your own Python agent on Flotta.

from flotta_agent import agent

@agent
async def support(message, ctx):
    async with ctx.tool("lookup_order", {"order": "42"}) as step:
        step.output = {"status": "shipped"}
    if await ctx.approval("Refund €20 on order 42"):
        yield "Refunded."
    else:
        yield "I won't refund it."

support.serve()
"""

from flotta_agent.server import Agent, Context, ToolCall, agent

__all__ = ["Agent", "Context", "ToolCall", "agent"]
__version__ = "0.1.0"
