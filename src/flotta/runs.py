"""Runs: one unit of an agent's work, with what started it and what it cost (M14).

A run is a row of `tasks` (D23). The table keeps its name because migrations
only add, and a Render rollback runs the old build against the new schema; its
events keep `entity_kind='task'` because that CHECK is in the frozen baseline.
Everything above the store says "run".

What a run cost is not stored on it. It is the sum of the `usage` rows the
gateway attributed to it (`FleetStore.run_cost`), so it is never worked out
twice and a call whose row lands after the run ended still counts.

This module holds what a run's row is allowed to contain, so the store and the
code that delivers runs share one answer.
"""

from __future__ import annotations

from typing import Any

#: What can start a run. `api` is a person or a client's system asking through
#: the fleet API; `peer` is another agent; `chat` a conversation (M14.6);
#: `review` new feedback on a pull request the agent opened (FLOTTA-172).
TRIGGER_KINDS: frozenset[str] = frozenset({"api", "chat", "peer", "review", "schedule", "webhook"})

#: Steps kept per run, half from the start and half from the end: how it began
#: and how it ended are what someone reading a run needs, and a loop's middle
#: is the part that repeats.
MAX_STEPS = 200

#: Characters kept of any one text in a step. A tool's output can be megabytes.
MAX_TEXT = 500

#: Characters kept of a run's output, the agent's final reply.
MAX_OUTPUT = 64_000

#: Characters kept of what a person said to open a chat run (M14.6). A run's
#: prompt is a sentence; a pasted document is what the conversation holds.
MAX_CHAT_PROMPT = 8_000

#: Characters of JSON a run's input may be. Refused whole above this rather
#: than cut, because half a document means something else.
MAX_INPUT = 64_000

#: Items kept of a list or keys of a dict inside a step, and how deep a step's
#: values may nest.
MAX_ITEMS = 50
_MAX_DEPTH = 6

#: How long a run may stay `running` before the control loop closes it as
#: `stranded`. Whoever delivers a run gives up well inside this, so a run still
#: going after it has lost its deliverer (a restart mid-delivery) and nothing
#: else will end it. A task's 15 minutes is too short for an agent's work.
RUN_DEADLINE_S = 3600

#: How long the control plane holds a run's conversation open before it gives
#: up and fails the run (`flotta.runner`). Inside `RUN_DEADLINE_S`, so the
#: deadline only ever closes a run that lost its deliverer. Not the relay's
#: 240 s: that sits under the asking agent's own tool deadline, and a run has
#: no asking agent.
RUN_TIMEOUT_S = 50 * 60

_CUT = "…"


def check_trigger_kind(kind: str) -> None:
    if kind not in TRIGGER_KINDS:
        raise ValueError(f"trigger kind must be one of {sorted(TRIGGER_KINDS)}, got {kind!r}")


def check_input_size(encoded: str) -> None:
    if len(encoded) > MAX_INPUT:
        raise ValueError(
            f"a run's input is {len(encoded)} characters of JSON; the limit is {MAX_INPUT}"
        )


def clip_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + _CUT


def cap_steps(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """`steps` as a run keeps them: at most `MAX_STEPS`, each text clipped,
    with a count of the steps left out of the middle."""
    omitted = max(0, len(steps) - MAX_STEPS)
    if omitted:
        head = MAX_STEPS // 2
        steps = steps[:head] + steps[len(steps) - (MAX_STEPS - head) :]
    return {"steps": [_clip(step, 0) for step in steps], "omitted": omitted}


def _clip(value: Any, depth: int) -> Any:
    if isinstance(value, str):
        return clip_text(value, MAX_TEXT)
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= _MAX_DEPTH:
        return _CUT
    if isinstance(value, dict):
        # Two keys that clip to the same text keep the first, and count as left out.
        kept_keys: dict[str, Any] = {}
        for k, v in value.items():
            key = clip_text(str(k), MAX_TEXT)
            if len(kept_keys) < MAX_ITEMS and key not in kept_keys:
                kept_keys[key] = _clip(v, depth + 1)
        if len(kept_keys) < len(value):
            kept_keys[_CUT] = len(value) - len(kept_keys)
        return kept_keys
    if isinstance(value, list | tuple):
        kept = [_clip(v, depth + 1) for v in value[:MAX_ITEMS]]
        return kept + [_CUT] if len(value) > MAX_ITEMS else kept
    return clip_text(str(value), MAX_TEXT)
