"""The model gateway: a streaming proxy that writes a usage row per call (M13.2).

**Why a proxy and not a log scraper** (spec §3.2): a log proves the number after
the money is spent, and a budget that acts has to be in the request path. M13.3
adds that check here.

## What Hermes calls

Read from its source rather than assumed. On `provider: custom` it speaks
`POST /chat/completions`, and it reads `GET /models` for a context length. It
only uses `/messages` when the URL ends in `/anthropic`, and its embeddings live
in a proxy of its own. So those two routes are all there is; anything else is a
404, which reaches nothing and cannot be an unmetered way to spend.

## Budgets come first

After the token and the model, and before anything goes out, `flotta.budgets`
decides. **Anything that stops it answering is a 402**: a store that cannot be
read, or a check that raises. Never a forward.

## Which models a call may name (FLOTTA-106)

Whoever holds a box's token can ask for anything, and an agent has root on the
machine that holds it. So a call names a model on `provision.models_for_box`, read
for every call, or it is refused before anything goes out. Hermes's auxiliary calls
(titles, compression, approvals) name the conversation's own model unless someone
configures another, and `usage` agrees.

**A 404 whose message says "model not found"**, because of what Hermes does with
it, read from its `error_classifier` at v0.22.5: it doesn't retry, compress or
rotate a key, and it shows this message. A 403 reads as a bad API key, a 402 as
billing. **A conversation still on a refused model** that reaches its compression
threshold loses its middle turns to a placeholder, since a 404 there isn't one of
the failures Hermes aborts on. The turn itself is refused either way.

**A body has to mean one thing here and upstream.** The check reads one parser's
idea of the body and the provider reads another's, so a repeated key is refused,
and so is `Model`, which some parsers read as `model`. `models` and `route` are
refused and not stripped: OpenRouter's fallback list could answer from a model that
isn't the one on the row, and an agent would believe its list had been honoured.
`provider` stays. It picks who hosts the same model, and the reply says what that
cost.

**A body can't buy more than the model it names, either.** `:online` on a model is
refused for not being on the list, and `plugins: [{"id": "web"}]` is OpenRouter's
other spelling of it, billed per search. So `plugins`, `web_search_options` and
`x_search_filter` are refused, along with `preset`, which can bring any of these
with it, and `openrouter:` server tools. Other `tools` stay: Hermes's function calls
are in that array. Hermes sends none of the refused ones to a custom endpoint.

## Who a call belongs to

**The box's own token, and nothing else in the request.** The subject names the
box, and `record_usage` reads the org and client off the box's row, so no box
can meter against another. A person's token is refused even with the scope: a
call is metered against a box, so it must come from one.

## What reaches the upstream

Request headers are an **allowlist**. A blocklist would have to know the name of
every header a provider ever accepted a key in. The query string is dropped. The
body goes as it came, byte for byte, unless a stream did not ask for usage: then
usage is asked for on the box's behalf, and the chunk that carries it is kept from
a client that would not expect a chunk with no choices in it.

## When the row is written

After the response ends, however it ends:

- **The upstream reports usage:** those are the tokens.
- **A stream is cut, or the box walks away:** nothing will report them, so they
  are estimated from what was sent and seen, at four characters a token, and the
  row says `estimated`. The money was spent, and an unpriced row would stop a
  budget that fails closed for what is usually a person interrupting a turn. The
  upstream is closed so it stops generating.
- **The upstream refuses, or the request never got out:** a row at no cost, with
  the status.
- **It failed after the request may have got out:** a read timeout can mean the
  provider accepted the prompt and is generating, so this is not proof of zero
  spend. The input is estimated and the row says so. The output is unknown, so
  this can undercount, which beats calling a possibly paid call free.

The write goes on a thread of its own and nothing awaits it, because a client
that disconnects cancels this request's task, and with it any `await`. Shutdown
waits for those threads.

**A row is unsettled until it is in the store.** One that cannot be written is
kept and tried again with the same idempotency key and its original moment, while
every budget it would count against refuses calls: otherwise a store that can be
read but not written would show a sum that never moves, and every call would pass.
This is in memory, so one gateway process sees its own rows only, and a restart
loses any it could not write. That loss is logged with the numbers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from flotta import budgets
from flotta.auth import (
    SCOPE_MODEL_CALL,
    AuthError,
    admits,
    resolve_signing_key,
    subject_box,
    verify,
)
from flotta.inference import InvalidModel, is_openrouter, validate_model
from flotta.pricing import EurRate, Price, dollars_to_nano
from flotta.pricing import rate_in_force as pricing_rate_in_force
from flotta.provision import models_for_box
from flotta.settings import layered
from flotta.store import Box, FleetStore, is_terminal

_log = logging.getLogger(__name__)

BASE_URL_ENV = "FLOTTA_MODEL_BASE_URL"
FLEET_KEY_ENV = "FLOTTA_API_KEY"

#: The only request headers that reach the upstream. See the module docstring.
_FORWARDED = frozenset({"content-type", "accept", "user-agent", "http-referer", "x-title"})

#: Response headers that describe this connection, or the upstream's own session.
_DROPPED = frozenset(
    {
        "connection",
        "content-encoding",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "set-cookie",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

#: Models can think for a long time between chunks. A short read timeout would
#: cut exactly the expensive calls.
_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)

#: Failures that prove the request never reached the provider: there was no
#: connection to send it on. Anything else may have got there, so it is not free.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

_RUN_ID = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")
_EVENT_END = re.compile(rb"\r?\n\r?\n")

#: Characters to a token, for a call nothing reported the tokens of.
_CHARS_PER_TOKEN = 4

PRICE_REFRESH_S = 24 * 60 * 60

#: How long a budget check waits for rows still being written, so a loop's next
#: call sees its last one. A database in trouble must not hold every call for long.
_SETTLE_S = 2.0

#: How long before a row that could not be written is tried again.
_WRITE_RETRY_S = 5.0

#: Body fields that could send a call to another model, or add a paid extra to it.
_REFUSED_FIELDS = frozenset(
    {"models", "route", "plugins", "web_search_options", "x_search_filter", "preset"}
)

#: Not text the model wrote, so not counted towards an estimate.
_NOT_TEXT = frozenset({"role", "type", "id", "index"})


@dataclass(frozen=True, slots=True)
class UpstreamKey:
    key: str
    #: Which level paid: `fleet`, `org` or `client` (D19).
    scope: str


StoredKeys = Callable[[str, str, str | None], str | None]


def _nothing_stored(provider: str, org: str, client: str | None) -> str | None:
    """Until `provider_keys` exists (FLOTTA-102), only the fleet has a key."""
    return None


def resolve_key(
    provider: str,
    *,
    org: str,
    client: str,
    env: Mapping[str, str],
    stored: StoredKeys = _nothing_stored,
) -> UpstreamKey | None:
    """Whose key a call goes out on: the client's, the org's, then the fleet's (D19)."""
    own = stored(provider, org, client)
    if own:
        return UpstreamKey(own, "client")
    own = stored(provider, org, None)
    if own:
        return UpstreamKey(own, "org")
    fleet = (env.get(FLEET_KEY_ENV) or "").strip()
    return UpstreamKey(fleet, "fleet") if fleet else None


class _Refused(Exception):
    def __init__(self, status: int, message: str, body: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.body = body

    def response(self) -> JSONResponse:
        if self.body is not None:
            return JSONResponse(self.body, status_code=self.status)
        return _error(self.status, self.message)


def _error(status: int, message: str) -> JSONResponse:
    """The shape an OpenAI client reads, so an agent's log says what happened."""
    return JSONResponse(
        {"error": {"message": message, "type": "flotta_gateway"}}, status_code=status
    )


def _no_repeats(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse a repeated key: see the module docstring."""
    found = dict(pairs)
    if len(found) != len(pairs):
        raise ValueError("a key appears twice")
    return found


def _refused_field(payload: dict[str, Any]) -> str | None:
    """What makes this call more than the one model it names, if anything does."""
    for key, value in payload.items():
        name = key.casefold()
        if name in _REFUSED_FIELDS or (name == "model" and key != "model"):
            return key
        if name == "tools" and isinstance(value, list):
            for tool in value:
                kind = tool.get("type") if isinstance(tool, dict) else None
                if isinstance(kind, str) and kind.casefold().startswith("openrouter:"):
                    return kind
    return None


def _text_length(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_length(item) for item in value)
    if isinstance(value, dict):
        return sum(_text_length(v) for k, v in value.items() if k not in _NOT_TEXT)
    return 0


def _estimated_tokens(chars: int) -> int:
    return (chars + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _reported(usage: Any) -> tuple[int, int, int] | None:
    """Tokens in, out and cached, if `usage` really says."""
    if not isinstance(usage, dict):
        return None
    tokens_in = _count(usage.get("prompt_tokens"))
    tokens_out = _count(usage.get("completion_tokens"))
    if tokens_in is None or tokens_out is None:
        return None
    details = usage.get("prompt_tokens_details")
    cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else None
    return tokens_in, tokens_out, cached or 0


def _reported_cost(usage: Any) -> int | None:
    """What OpenRouter's reply says a call cost, in nanos of a dollar, if it says.

    Its catalogue lists the cheapest provider, while a call is billed at the
    price of the provider that served it, so only the reply knows. On a brought
    key the figure is OpenRouter's fee, with the call billed elsewhere.
    """
    if not isinstance(usage, dict) or usage.get("is_byok") is True:
        return None
    cost = usage.get("cost")
    if not isinstance(cost, int | float):
        return None
    return dollars_to_nano(cost)


class _Events:
    """Reads a server-sent stream as it passes through.

    Forwards whole events, in order and unchanged: a part of an event is no use
    to the client, and holding it back until the rest arrives costs nothing.
    """

    def __init__(self, *, strip_usage: bool) -> None:
        self._strip_usage = strip_usage
        self._buffer = b""
        self.usage: Any = None
        self.chars = 0

    def feed(self, chunk: bytes) -> bytes:
        self._buffer += chunk
        out: list[bytes] = []
        while True:
            found = _EVENT_END.search(self._buffer)
            if found is None:
                return b"".join(out)
            event = self._buffer[: found.end()]
            self._buffer = self._buffer[found.end() :]
            if self._read(event):
                out.append(event)

    def flush(self) -> bytes:
        """What was left after the last whole event, which a cut stream can leave."""
        rest, self._buffer = self._buffer, b""
        return rest if rest and self._read(rest) else b""

    def _read(self, event: bytes) -> bool:
        """Take what it says. Returns whether the client should see it."""
        keep = True
        for line in event.splitlines():
            if not line.startswith(b"data:"):
                continue
            try:
                payload = json.loads(line[len(b"data:") :])
            except ValueError:
                continue  # `[DONE]`, or something only the client can judge.
            if not isinstance(payload, dict):
                continue
            choices = payload.get("choices")
            if payload.get("usage") is not None:
                self.usage = payload["usage"]
                if self._strip_usage and not choices:
                    keep = False
            if isinstance(choices, list):
                self.chars += sum(
                    _text_length(c.get("delta")) for c in choices if isinstance(c, dict)
                )
        return keep


@dataclass(eq=False, slots=True)
class _Entry:
    """A row on its way to the store."""

    box: Box | None
    row: dict[str, Any]
    running: bool = True
    retry_at: float = 0.0
    failed_before: bool = False


class _Ledger:
    """Writes rows on threads that a cancelled request cannot take with it.

    A row stays here until it is written, however long that takes. `settle` says
    which are still missing, because that is what a budget has to know.
    """

    def __init__(
        self,
        write: Callable[..., Any],
        *,
        attempts: int = 3,
        backoff_s: float = 0.5,
        retry_s: float = _WRITE_RETRY_S,
    ) -> None:
        self._write = write
        self._attempts = attempts
        self._backoff_s = backoff_s
        self._retry_s = retry_s
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="flotta-ledger")
        self._pending: set[Future[None]] = set()
        self._lock = threading.Lock()
        self._unsettled: set[_Entry] = set()

    def write(self, *, box: Box | None = None, **row: Any) -> None:
        entry = _Entry(box, row)
        with self._lock:
            self._unsettled.add(entry)
        self._submit(entry)

    def _submit(self, entry: _Entry) -> None:
        try:
            future = self._pool.submit(self._attempt, entry)
        except RuntimeError:
            # Shutting down, with a stream that ended after the pool closed.
            self._attempt(entry)
            return
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)

    def _attempt(self, entry: _Entry) -> None:
        for attempt in range(1, self._attempts + 1):
            try:
                self._write(**entry.row)
            except Exception:
                if attempt == self._attempts:
                    if not entry.failed_before:
                        _log.exception("a usage row could not be written: %s", entry.row)
                    with self._lock:
                        entry.running = False
                        entry.failed_before = True
                        entry.retry_at = time.monotonic() + self._retry_s
                else:
                    time.sleep(self._backoff_s * attempt)
            else:
                with self._lock:
                    self._unsettled.discard(entry)
                return

    def _retry_due(self) -> None:
        now = time.monotonic()
        with self._lock:
            due = [e for e in self._unsettled if not e.running and now >= e.retry_at]
            for entry in due:
                entry.running = True
        for entry in due:
            self._submit(entry)

    async def settle(self, *, timeout: float) -> list[Box]:
        """Try again what is due, wait a moment, and name the boxes still missing rows."""
        self._retry_due()
        pending = set(self._pending)
        if pending:
            await asyncio.to_thread(wait, pending, timeout)
        with self._lock:
            return [entry.box for entry in self._unsettled if entry.box is not None]

    async def drain(self) -> None:
        pending = set(self._pending)
        if pending:
            await asyncio.to_thread(wait, pending)

    def close(self) -> None:
        """One last try at what is left, and say what could not be saved."""
        self._pool.shutdown(wait=True)
        with self._lock:
            left = list(self._unsettled)
        for entry in left:
            try:
                self._write(**entry.row)
            except Exception:
                _log.exception("a usage row is lost: %s", entry.row)


_closing: set[asyncio.Future[None]] = set()


class _MeteredStream(StreamingResponse):
    """A stream whose row is written even if the generator never ran.

    A generator that was never started has no `finally` to run, which is what a
    client that hangs up before the first chunk would leave.
    """

    def __init__(self, content: AsyncIterator[bytes], finish: Callable[[], None], **kw: Any):
        super().__init__(content, **kw)
        self._finish = finish

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._finish()


async def _relay(
    response: httpx.Response, events: _Events, end: Callable[..., None]
) -> AsyncIterator[bytes]:
    """The upstream's stream, then the row, however the stream ended."""
    try:
        async for chunk in response.aiter_raw():
            out = events.feed(chunk)
            if out:
                yield out
        rest = events.flush()
        if rest:
            yield rest
    except httpx.HTTPError as exc:
        # The client sees a stream with no `[DONE]`, which is what happened.
        _log.warning("the upstream's stream ended early: %s", exc)
    finally:
        # The row first, with no `await` before it: see the module docstring.
        end(usage=events.usage, chars=events.chars)
        # On its own task, so being cancelled does not leave a model generating.
        closing = asyncio.ensure_future(response.aclose())
        _closing.add(closing)
        closing.add_done_callback(_closing.discard)
        await asyncio.shield(closing)


def _scrubbed(body: bytes, key: str) -> bytes:
    """A provider's complaint about a key can quote it, and a box holds none."""
    return body.replace(key.encode(), b"[the upstream key]")


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k.lower() not in _DROPPED}


def create_gateway(
    *,
    store_factory: Callable[[], FleetStore] = FleetStore,
    transport: httpx.AsyncBaseTransport | None = None,
    env: Mapping[str, str] | None = None,
    stored_keys: StoredKeys = _nothing_stored,
    price_source: Callable[[], Iterable[Price]] | None = None,
    rate_source: Callable[[], EurRate] | None = None,
    settle_s: float = _SETTLE_S,
    write_backoff_s: float = 0.5,
    write_retry_s: float = _WRITE_RETRY_S,
) -> FastAPI:
    """Build the gateway's ASGI app. The upstream and the store are injected."""
    environment: Mapping[str, str] = os.environ if env is None else env
    signing_key = resolve_signing_key(dict(environment))
    base_url = (environment.get(BASE_URL_ENV) or "").strip().rstrip("/")
    provider = "openrouter" if is_openrouter(base_url) else "custom"

    def rate_in_force(store: FleetStore) -> Decimal | None:
        """The override if there is one, otherwise the latest rate fetched."""
        return pricing_rate_in_force(store, layered(store, environment))

    def write_row(*, box_id: str, **row: Any) -> None:
        store = store_factory()
        try:
            rate = rate_in_force(store)
            store.record_usage(box_id, eur_per_usd=rate, **row)
        finally:
            store.close()

    ledger = _Ledger(write_row, backoff_s=write_backoff_s, retry_s=write_retry_s)

    def refresh_prices() -> None:
        assert price_source is not None
        prices = list(price_source())
        store = store_factory()
        try:
            changed = store.refresh_prices(prices)
        finally:
            store.close()
        _log.info("model prices refreshed: %d models, %d changed", len(prices), changed)

    def refresh_rate() -> None:
        assert rate_source is not None
        rate = rate_source()
        store = store_factory()
        try:
            store.record_eur_rate(rate)
        finally:
            store.close()
        _log.info("euro rate: %s, %s for %s", rate.eur_per_usd, rate.source, rate.as_of)

    async def refresh_once() -> None:
        if price_source is not None:
            try:
                await asyncio.to_thread(refresh_prices)
            except Exception:
                # The last prices stay in force. A model with none is unpriced, which
                # a budget can see, and that beats a gateway that will not start.
                _log.exception("model prices could not be refreshed")
        if rate_source is not None:
            try:
                await asyncio.to_thread(refresh_rate)
            except Exception:
                # Likewise the last rate. Separately, so neither failure costs the other.
                _log.exception("the euro rate could not be refreshed")

    async def refresh_forever() -> None:
        while True:
            await asyncio.sleep(PRICE_REFRESH_S)
            await refresh_once()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.upstream = httpx.AsyncClient(
            transport=transport, timeout=_TIMEOUT, trust_env=False
        )
        refresher = None
        if price_source is not None or rate_source is not None:
            await refresh_once()
            refresher = asyncio.create_task(refresh_forever())
        try:
            yield
        finally:
            if refresher is not None:
                refresher.cancel()
            await app.state.upstream.aclose()
            await ledger.drain()
            ledger.close()

    app = FastAPI(title="Flotta model gateway", lifespan=lifespan)

    def find_box(box_id: str) -> Box | None:
        store = store_factory()
        try:
            return store.get_box(box_id)
        finally:
            store.close()

    async def caller(request: Request) -> Box:
        """The box this request is from, or `_Refused`. Nothing is spent before it."""
        if not signing_key:
            raise _Refused(503, "the gateway has no signing key, so it cannot check a token")
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not presented.strip():
            raise _Refused(401, "this gateway takes a Flotta token as a bearer token")
        try:
            token = verify(presented.strip(), key=signing_key)
        except AuthError as exc:
            raise _Refused(401, str(exc)) from exc
        if not token.allows(SCOPE_MODEL_CALL):
            raise _Refused(403, f"this token does not carry {SCOPE_MODEL_CALL}")
        box_id = subject_box(token.subject)
        if box_id is None:
            raise _Refused(403, "only an agent's own token may call a model through this gateway")
        try:
            box = await asyncio.to_thread(find_box, box_id)
        except Exception as exc:
            # No check, no call. There is nowhere to put an event: only this log.
            _log.exception("the store could not be read")
            refusal = budgets.unchecked()
            raise _Refused(402, refusal.message, refusal.body()) from exc
        if box is None or is_terminal("box", box.status):
            raise _Refused(403, "this agent no longer exists")
        reason = admits(token, box_id=box.id, org=box.org, client=box.client)
        if reason is not None:
            raise _Refused(403, reason)
        return box

    def check_budgets(
        box: Box, model: str, unsettled: list[Box]
    ) -> budgets.Refusal | budgets.Downgrade | None:
        store = store_factory()
        try:
            rate = rate_in_force(store)
            now = datetime.now(UTC)
            return budgets.check_budgets(
                store, box, model=model, eur_per_usd=rate, now=now, unsettled=unsettled
            )
        finally:
            store.close()

    noted: set[tuple[str, ...]] = set()
    noted_lock = threading.Lock()

    def note_once(box: Box, key: tuple[str, ...], kind: str, payload: dict[str, Any]) -> None:
        """One event per refusal per period, so a loop does not fill the timeline.

        In memory: a restart may add one more, which is harmless. Failing to write
        it changes nothing about the refusal.
        """
        key = (box.id, *key)
        with noted_lock:
            if key in noted:
                return
            if len(noted) > 10_000:
                noted.clear()
            noted.add(key)
        try:
            store = store_factory()
            try:
                store.add_event("box", box.id, kind, payload)
            finally:
                store.close()
        except Exception:
            _log.exception("a refusal could not be recorded: %s", kind)

    def note_refusal(box: Box, refusal: budgets.Refusal) -> None:
        note_once(
            box,
            refusal.key,
            "budget_refused",
            {"code": refusal.code, "message": refusal.message, "budget": refusal.budget},
        )

    def read_models(box: Box) -> list[str]:
        store = store_factory()
        try:
            return models_for_box(box.id, store, environment)
        finally:
            store.close()

    async def budget_verdict(
        box: Box, model: str, allowed: list[str]
    ) -> JSONResponse | budgets.Downgrade | None:
        """The 402 if this call may not go out, or the model it has to go to instead.

        Anything unexpected is a 402 too. A fallback off the list is a refusal, never
        the original model: an owner chooses a budget, and the operator pays for it.
        """
        try:
            unsettled = await ledger.settle(timeout=settle_s)
            verdict = await asyncio.to_thread(check_budgets, box, model, unsettled)
        except Exception:
            _log.exception("a budget check failed")
            # Keyed by the minute: an outage is worth more than one event, not thousands.
            verdict = budgets.unchecked(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M"))
        if isinstance(verdict, budgets.Downgrade) and verdict.model not in allowed:
            verdict = budgets.Refusal(
                "fallback_not_allowed",
                f"This budget falls back to {verdict.model}, which isn't one of the fleet's "
                "models, so this call was refused.",
                budget=verdict.budget,
                key=("fallback_not_allowed", verdict.model),
            )
        if not isinstance(verdict, budgets.Refusal):
            return verdict
        await asyncio.to_thread(note_refusal, box, verdict)
        return JSONResponse(verdict.body(), status_code=402)

    def upstream_headers(request: Request, key: str) -> dict[str, str]:
        headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARDED}
        headers["authorization"] = f"Bearer {key}"
        # Usage is read out of the body, so it has to arrive readable.
        headers["accept-encoding"] = "identity"
        return headers

    @app.get("/health")
    def health() -> Any:
        """The gateway's own liveness. Open, and says nothing about the fleet."""
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        """Passed through and not metered: it costs nothing upstream."""
        try:
            box = await caller(request)
        except _Refused as refused:
            return refused.response()
        key = resolve_key(
            provider, org=box.org, client=box.client, env=environment, stored=stored_keys
        )
        if key is None or not base_url:
            return _error(503, "the gateway has no upstream configured")
        try:
            reply = await request.app.state.upstream.get(
                f"{base_url}/models", headers=upstream_headers(request, key.key)
            )
        except httpx.HTTPError as exc:
            _log.warning("the upstream's model list could not be read: %s", exc)
            return _error(502, "the model provider could not be reached")
        body = _scrubbed(reply.content, key.key) if reply.status_code >= 400 else reply.content
        return Response(body, reply.status_code, _response_headers(reply))

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        started = time.monotonic()
        try:
            box = await caller(request)
        except _Refused as refused:
            return refused.response()

        raw = await request.body()
        try:
            payload = json.loads(raw, object_pairs_hook=_no_repeats)
            model = validate_model(payload["model"])
        except (ValueError, KeyError, TypeError, AttributeError, InvalidModel):
            return _error(400, "expected a JSON body naming one model, with no repeated keys")
        refused = _refused_field(payload)
        if refused is not None:
            return _error(
                400,
                f"this gateway calls the one model a request names and adds nothing to it, "
                f"so it refuses {refused!r}",
            )

        try:
            allowed = await asyncio.to_thread(read_models, box)
        except Exception:
            # No list, no call. A 503 and not a 402: Hermes tries it again, and this is
            # nothing to do with anyone's money.
            _log.exception("the models this agent may use could not be read")
            return _error(
                503, "the models this agent may use could not be read, so nothing was sent"
            )
        if model not in allowed:
            listed = ", ".join(allowed) or "none are set"
            await asyncio.to_thread(
                note_once,
                box,
                ("model_refused", model),
                "model_refused",
                {"model": model, "allowed": allowed},
            )
            return JSONResponse(
                {
                    "error": {
                        "message": f"Model not found here: {model} isn't one this agent may use "
                        f"({listed}). The fleet's operator chooses them.",
                        "type": "flotta_gateway",
                        "code": "model_not_found",
                    }
                },
                status_code=404,
            )

        verdict = await budget_verdict(box, model, allowed)
        if isinstance(verdict, JSONResponse):
            return verdict
        requested = None
        if verdict is not None:
            requested, model = model, verdict.model
            payload["model"] = model
            raw = json.dumps(payload).encode()

        key = resolve_key(
            provider, org=box.org, client=box.client, env=environment, stored=stored_keys
        )
        if key is None or not base_url:
            return _error(503, "the gateway has no upstream configured")

        run_id = request.headers.get("x-flotta-run", "").strip()
        call = {
            "box_id": box.id,
            "model": model,
            "requested_model": requested,
            "upstream": provider,
            "key_scope": key.scope,
            "idempotency_key": uuid.uuid4().hex,
            "run_id": run_id if _RUN_ID.match(run_id) else None,
        }

        def write(status: int, *, usage: Any = None, chars: int | None = None) -> None:
            """`chars` is `None` when no tokens were spent at all."""
            reported = _reported(usage)
            if reported is not None:
                tokens = reported
            elif chars is None:
                tokens = (0, 0, 0)
            else:
                tokens = (_estimated_tokens(len(raw)), _estimated_tokens(chars), 0)
            ledger.write(
                box=box,
                **call,
                now=datetime.now(UTC),
                tokens_in=tokens[0],
                tokens_out=tokens[1],
                tokens_cached=tokens[2],
                status=status,
                estimated=reported is None and chars is not None,
                # Only OpenRouter's: nothing says what another endpoint's field means.
                reported_cost_nano=_reported_cost(usage) if provider == "openrouter" else None,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        streaming = isinstance(payload, dict) and payload.get("stream") is True
        options = payload.get("stream_options")
        asked = isinstance(options, dict) and options.get("include_usage") is True
        content = raw
        if streaming and not asked:
            given = options if isinstance(options, dict) else {}
            payload["stream_options"] = {**given, "include_usage": True}
            content = json.dumps(payload).encode()

        client: httpx.AsyncClient = request.app.state.upstream
        outgoing = client.build_request(
            "POST",
            f"{base_url}/chat/completions",
            headers=upstream_headers(request, key.key),
            content=content,
        )
        try:
            reply = await client.send(outgoing, stream=True)
        except httpx.HTTPError as exc:
            _log.warning("the model provider could not be reached: %s", exc)
            if isinstance(exc, _NEVER_SENT):
                write(502)
            else:
                write(502, chars=0)
            return _error(502, "the model provider could not be reached")

        if reply.headers.get("content-type", "").startswith("text/event-stream") and (
            reply.status_code < 400
        ):
            events = _Events(strip_usage=streaming and not asked)
            written = False

            def end(*, usage: Any, chars: int) -> None:
                nonlocal written
                if not written:
                    written = True
                    write(reply.status_code, usage=usage, chars=chars)

            def finish() -> None:
                end(usage=events.usage, chars=events.chars)
                closing = asyncio.ensure_future(reply.aclose())
                _closing.add(closing)
                closing.add_done_callback(_closing.discard)

            return _MeteredStream(
                _relay(reply, events, end),
                finish,
                status_code=reply.status_code,
                headers=_response_headers(reply),
            )

        try:
            body = await reply.aread()
        except httpx.HTTPError as exc:
            _log.warning("the upstream's reply ended early: %s", exc)
            # It may have worked, and nothing says what it used.
            write(502, chars=0 if reply.status_code < 400 else None)
            return _error(502, "the model provider's reply ended early")
        finally:
            await reply.aclose()

        if reply.status_code >= 400:
            write(reply.status_code)
            return Response(_scrubbed(body, key.key), reply.status_code, _response_headers(reply))

        try:
            answer = json.loads(body)
        except ValueError:
            answer = {}
        answer = answer if isinstance(answer, dict) else {}
        choices = answer.get("choices")
        seen = (
            sum(_text_length(c.get("message")) for c in choices if isinstance(c, dict))
            if isinstance(choices, list)
            else 0
        )
        write(reply.status_code, usage=answer.get("usage"), chars=seen)
        return Response(body, reply.status_code, _response_headers(reply))

    return app
