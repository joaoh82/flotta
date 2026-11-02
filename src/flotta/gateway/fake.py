"""An upstream that answers like an OpenAI-compatible provider, for $0.

Injected as the gateway's HTTP transport, so nothing opens a socket: the test
suite uses it, and so does `flotta gateway --fake-upstream` for local work.
"""

from __future__ import annotations

import asyncio
import json
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
        if body.get("stream") is True:
            wants_usage = (body.get("stream_options") or {}).get("include_usage") is True
            return self._stream(wants_usage and self.reports_usage, cookie)
        reply: dict[str, Any] = {
            "id": "fake-1",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(_WORDS)}}],
        }
        if self.reports_usage:
            reply["usage"] = self.usage
        return httpx.Response(200, json=reply, headers=cookie)

    def _stream(self, with_usage: bool, headers: dict[str, str]) -> httpx.Response:
        chunks = [_event({"choices": [{"index": 0, "delta": {"content": w}}]}) for w in _WORDS]
        if with_usage:
            chunks.append(_event({"choices": [], "usage": self.usage}))
        chunks.append(b"data: [DONE]\n\n")
        self.last_stream = b"".join(chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", **headers},
            stream=_Stream(self, chunks, self.cut_after),
        )
