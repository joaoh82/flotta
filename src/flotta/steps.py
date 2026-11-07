"""The steps of a turn, from the frames an agent streams (M14.2, M14.6).

Its own module because two processes read the same frames: the control plane,
which is the client of a run it delivers (`flotta.runner`), and the front door,
which sees every frame of a person's conversation (`flotta.door.turns`). The
door does not import the store, and this imports nothing of Flotta's.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


class Steps:
    """The steps of one run, from the frames its turn streams.

    A tool is one step from `tool.start` to `tool.complete`. An approval is one
    step however many ways it arrives: Hermes asks it as a request and may also
    announce it as an event, with the same `request_id`.
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.steps: list[dict[str, Any]] = []
        self.approvals = 0
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tools: dict[str, dict[str, Any]] = {}
        self._asked: set[str] = set()
        self._by_request: dict[str, dict[str, Any]] = {}
        self._by_rpc: dict[str, dict[str, Any]] = {}

    def hear(self, frame: dict[str, Any]) -> None:
        if frame.get("method") == "approval" and "id" in frame:
            self._approval(frame.get("params"), str(frame["id"]))
            return
        params = frame.get("params")
        if not isinstance(params, dict):
            return
        kind = params.get("type")
        payload = params.get("payload")
        if not isinstance(payload, dict):
            return
        if kind == "approval.request":
            self._approval(payload)
        elif kind == "tool.start":
            self._tool_started(payload)
        elif kind == "tool.complete":
            self._tool_completed(payload)

    def answered(self, rpc_id: str, choice: str | None, by: str | None) -> None:
        """A person answered the question the box asked as request `rpc_id`."""
        step = self._by_rpc.get(rpc_id)
        if step is not None:
            step.update(answer=choice, answered_by=by)

    def answered_request(self, request_id: str, choice: str | None, by: str | None) -> None:
        """A person answered the approval Hermes calls `request_id` (`approval.respond`)."""
        step = self._by_request.get(request_id)
        if step is not None:
            step.update(answer=choice, answered_by=by)

    def unanswered(self) -> None:
        """The run ended with questions open: each was denied, by nobody."""
        for step in self.steps:
            if step["kind"] == "approval" and step.get("answer") is None:
                step.update(answer="deny", answered_by=None, expired=True)

    def _approval(self, params: Any, rpc_id: str | None = None) -> None:
        params = params if isinstance(params, dict) else {}
        request_id = str(params.get("request_id") or "")
        if request_id and request_id in self._asked:
            step = self._by_request.get(request_id)
            if rpc_id is not None and step is not None:
                self._by_rpc.setdefault(rpc_id, step)
            return
        self._asked.add(request_id)
        self.approvals += 1
        step = {
            "kind": "approval",
            "command": params.get("command"),
            "description": params.get("description"),
            "choices": params.get("choices"),
            "answer": None,
            "at": self._now(),
        }
        self.steps.append(step)
        if request_id:
            self._by_request[request_id] = step
        if rpc_id is not None:
            self._by_rpc[rpc_id] = step

    def _tool_started(self, payload: dict[str, Any]) -> None:
        step: dict[str, Any] = {"kind": "tool", "name": payload.get("name")}
        step["tool_id"] = payload.get("tool_id")
        for field in ("context", "args"):
            if field in payload:
                step[field] = payload[field]
        step["at"] = self._now()
        self.steps.append(step)
        if isinstance(step["tool_id"], str):
            self._tools[step["tool_id"]] = step

    def _tool_completed(self, payload: dict[str, Any]) -> None:
        tool_id = payload.get("tool_id")
        step = self._tools.pop(tool_id, None) if isinstance(tool_id, str) else None
        if step is None:
            # A completion with no start heard: kept, rather than lost.
            step = {"kind": "tool", "name": payload.get("name"), "tool_id": tool_id}
            step["at"] = self._now()
            self.steps.append(step)
        result = payload.get("result")
        result = result if isinstance(result, dict) else {}
        step["duration_s"] = payload.get("duration_s")
        for field in ("exit_code", "error", "output"):
            step[field] = result.get(field)

    def _now(self) -> str:
        return self._clock().isoformat()
