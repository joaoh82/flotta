"""An upstream that answers like an OpenAI-compatible provider, for $0.

Injected as the gateway's HTTP transport, so nothing opens a socket: the test
suite uses it, and so does `flotta gateway --fake-upstream` for local work.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from flotta.pricing import NANO, Price

FAKE_UPSTREAM_URL = "http://fake-upstream.invalid/v1"
FAKE_MODEL = "fake/model"
FAKE_PRICE = Price(
    model=FAKE_MODEL,
    input_nano_per_m=2 * NANO,
    output_nano_per_m=8 * NANO,
    cached_nano_per_m=NANO,
    currency="USD",
    source="fake",
)
_WORDS = ("Hello", " from", " the", " fake", " upstream")


def _event(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


class _Stream(httpx.AsyncByteStream):
    def __init__(self, owner: FakeUpstream, chunks: list[bytes], cut_after: int | None) -> None:
        self._owner = owner
        self._chunks = chunks
        self._cut_after = cut_after

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self._chunks):
            if self._cut_after is not None and index >= self._cut_after:
                raise httpx.ReadError("the fake upstream went away")
            if index and self._owner.chunk_delay_s:
                await asyncio.sleep(self._owner.chunk_delay_s)
            yield chunk

    async def aclose(self) -> None:
        self._owner.closed += 1


class FakeUpstream(httpx.AsyncBaseTransport):
    """Records what it was sent and answers as scripted."""

    def __init__(
        self,
        *,
        status: int = 200,
        cut_after: int | None = None,
        reports_usage: bool = True,
        unreachable: bool = False,
        chunk_delay_s: float = 0.0,
    ) -> None:
        self.status = status
        self.cut_after = cut_after
        self.reports_usage = reports_usage
        self.unreachable = unreachable
        #: Time for a client to hang up part-way, as a real model gives it.
        self.chunk_delay_s = chunk_delay_s
        self.requests: list[httpx.Request] = []
        self.closed = 0
        self.last_stream = b""
        self.usage = {
            "prompt_tokens": 1200,
            "completion_tokens": 30,
            "total_tokens": 1230,
            "prompt_tokens_details": {"cached_tokens": 200},
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        if self.unreachable:
            raise httpx.ConnectError("the fake upstream cannot be reached")
        cookie = {"set-cookie": "__cf_bm=abc; Path=/"}
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"object": "list", "data": [{"id": FAKE_MODEL}]}, headers=cookie
            )
        if self.status >= 400:
            return httpx.Response(
                self.status, json={"error": {"message": "the fake upstream says no"}}
            )
        body = json.loads(request.content)
        words, calls = _decide(body)
        if body.get("stream") is True:
            wants_usage = (body.get("stream_options") or {}).get("include_usage") is True
            return self._stream(wants_usage and self.reports_usage, cookie, words, calls)
        message: dict[str, Any] = {"role": "assistant", "content": "".join(words)}
        if calls:
            message = {"role": "assistant", "content": None, "tool_calls": calls}
        reply: dict[str, Any] = {
            "id": "fake-1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if calls else "stop",
                }
            ],
        }
        if self.reports_usage:
            reply["usage"] = self.usage
        return httpx.Response(200, json=reply, headers=cookie)

    def _stream(
        self,
        with_usage: bool,
        headers: dict[str, str],
        words: tuple[str, ...] = _WORDS,
        calls: list[dict[str, Any]] | None = None,
    ) -> httpx.Response:
        if calls:
            deltas = [{**call, "index": i} for i, call in enumerate(calls)]
            chunks = [
                _event(
                    {
                        "choices": [
                            {"index": 0, "delta": {"role": "assistant", "tool_calls": deltas}}
                        ]
                    }
                ),
                _event({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
            ]
        else:
            chunks = [_event({"choices": [{"index": 0, "delta": {"content": w}}]}) for w in words]
        if with_usage:
            chunks.append(_event({"choices": [], "usage": self.usage}))
        chunks.append(b"data: [DONE]\n\n")
        self.last_stream = b"".join(chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", **headers},
            stream=_Stream(self, chunks, self.cut_after),
        )


# -- tool calls, scripted (M17.3) -------------------------------------------------


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return ""


def _decide(body: dict[str, Any]) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    """What the fake model says: a tool call when the person's message names one
    of the tools it was offered, a line about the tool's result after one, else
    the usual greeting. Enough for an agent's tools and approvals to be tried for
    $0: "refund order 42" calls `refund` with `{"order": "42"}`-shaped arguments.
    """
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    last = messages[-1] if messages and isinstance(messages[-1], dict) else {}
    tools = [
        t["function"]
        for t in body.get("tools") or []
        if isinstance(t, dict) and isinstance(t.get("function"), dict)
    ]
    if last.get("role") == "tool":
        said = " ".join(_text_of(last.get("content")).split())[:200]
        return ("Done", ": ", said or "the tool answered."), []
    if last.get("role") != "user" or not tools:
        return _WORDS, []
    text = _text_of(last.get("content")).lower()
    for tool in tools:
        name = str(tool.get("name") or "")
        if name and (name.lower() in text or name.lower().replace("_", " ") in text):
            arguments = _arguments(tool.get("parameters") or {}, text)
            return (), [
                {
                    "id": f"call_{len(messages)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
    return _WORDS, []


def _arguments(schema: dict[str, Any], text: str) -> dict[str, Any]:
    """Every required argument filled from the message: numbers from its first
    number, anything else from that number too, or the message itself."""
    number = re.search(r"\d+(?:\.\d+)?", text)
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    out: dict[str, Any] = {}
    for name in schema.get("required") or list(properties):
        kind = (properties.get(name) or {}).get("type")
        if kind == "integer":
            out[name] = int(float(number.group())) if number else 1
        elif kind == "number":
            out[name] = float(number.group()) if number else 1.0
        elif kind == "boolean":
            out[name] = True
        else:
            out[name] = number.group() if number else text
    return out
