"""Measure where an agent's turns spend their time (FLOTTA-61, part 3).

    just latency eng-g 3

**Costs money**: a wake and a few dozen model calls. Never run in CI.

For each round: a fresh conversation, then the fixed prompt set below, one
after another like a person would send them. Each turn is timed from the
client (what a person sees), then Hermes's own log is read off the box for the
same window (what each model call cost, and which upstream served it). The
report is Markdown, ready to paste into the ticket.

The prompt set is fixed so a *before* and an *after* are comparable. Changing
it resets the baseline; say so if you do.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request

from flotta import client as chat
from flotta.latency import TurnTiming, calls_between, report

#: A spread of what people actually ask: no tools, one tool, the skill path,
#: and a short multi-step one. Plain enough that the model rarely refuses and
#: nothing trips the approval gate, which would measure a person, not a model.
PROMPTS = (
    "hey",
    "In one sentence, what does git rebase do?",
    "Use your terminal to run `uname -sr` and tell me the kernel version.",
    "Which GitHub repositories do you have access to?",
    "Use your terminal to count the entries in /etc, then tell me the number in one sentence.",
)

#: Events that put something on the screen. Not `thinking.delta` (Hermes's
#: spinner) and not session bookkeeping — see `agent.rs`'s `read_step`.
VISIBLE = {"reasoning.delta", "message.delta", "tool.generating", "tool.start", "approval.request"}


async def timed_turn(ws, session_id: str, text: str, *, timeout_s: float = 300.0) -> TurnTiming:
    """One turn, with the moments a person would notice."""
    rid = await chat._rpc(ws, "prompt.submit", {"session_id": session_id, "text": text})
    start = time.monotonic()
    first_visible = first_text = None
    tools = 0
    while True:
        remaining = timeout_s - (time.monotonic() - start)
        if remaining <= 0:
            raise chat.ChatError(f"no reply within {timeout_s:.0f}s")
        frame = await chat._next_json(ws, timeout_s=remaining)
        if frame.get("id") == rid and "error" in frame:
            raise chat.ChatError(f"prompt.submit was rejected: {frame['error']}")
        params = frame.get("params") or {}
        kind = params.get("type")
        payload = params.get("payload") or {}
        now = time.monotonic() - start
        if kind in VISIBLE:
            # An empty delta arrives between phases and shows nothing.
            if kind.endswith(".delta") and not payload.get("text"):
                continue
            first_visible = first_visible if first_visible is not None else now
            if kind == "message.delta":
                first_text = first_text if first_text is not None else now
            if kind == "tool.start":
                tools += 1
        if kind == chat.COMPLETE_EVENT:
            if payload.get("status") not in (None, "complete"):
                raise chat.ChatError(f"the agent could not answer: {payload.get('text', '')[:200]}")
            return TurnTiming(text, first_visible, first_text, now, tools)


async def run_rounds(box: str, rounds: int) -> tuple[list[TurnTiming], float, float]:
    base_url = chat.door_url(box)
    token = chat.flotta_token()
    turns: list[TurnTiming] = []
    window_start = time.time()
    async with chat.new_session() as session:
        await chat.login(session, base_url, token=token)
        for round_no in range(1, rounds + 1):
            ticket = await chat.ws_ticket(session, base_url, token=token)
            ws = await chat.open_agent_socket(session, base_url, ticket, token=token)
            try:
                await chat.await_ready(ws, timeout_s=180)
                created = await chat.create_session(ws, timeout_s=180)
                session_id = created.get("session_id") or ""
                for prompt in PROMPTS:
                    turn = await timed_turn(ws, session_id, prompt)
                    turns.append(turn)
                    print(
                        f"round {round_no}: {turn.total_s:5.1f}s tools={turn.tools}  {prompt[:50]}",
                        file=sys.stderr,
                    )
            finally:
                await ws.close()
    return turns, window_start, time.time()


def box_endpoint(box: str) -> tuple[str, str]:
    """`fly://app/machine` for a box, from the control plane."""
    control = os.environ["FLOTTA_CONTROL_URL"].rstrip("/")
    request = urllib.request.Request(
        f"{control}/api/boxes/{box}",
        headers={"Authorization": f"Bearer {os.environ['FLOTTA_READ_TOKEN']}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        endpoint = json.loads(response.read())["endpoint"]
    app, machine = endpoint.removeprefix("fly://").split("/", 1)
    return app, machine


def read_log(box: str) -> list[str]:
    """The model-call lines from the box's agent.log. Read-only."""
    app, machine = box_endpoint(box)
    command = "/bin/sh -c " + shlex.quote(
        "grep -h 'agent.conversation_loop: API call #' /data/hermes/logs/agent.log | tail -n 400"
    )
    result = subprocess.run(
        ["flyctl", "machine", "exec", machine, "-a", app, "--json", command],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout).get("stdout", "").splitlines()


def main() -> int:
    box = sys.argv[1] if len(sys.argv) > 1 else "eng-g"
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    turns, start, end = asyncio.run(run_rounds(box, rounds))
    # A few seconds either side: the log line is written as the call returns,
    # and the box's clock is not this laptop's.
    calls = calls_between(read_log(box), start - 5, end + 5)
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(start))
    print(report(turns, calls, title=f"{box}, {rounds} round(s) × {len(PROMPTS)} prompts, {stamp}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
