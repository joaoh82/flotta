"""Where a turn's time goes: model calls, providers, cache (FLOTTA-61, part 3).

Pure. `scripts/latency_probe.py` drives a real agent and collects the numbers;
everything that decides what they *mean* is here, so it is tested rather than
eyeballed.

## Two sources, because each sees what the other cannot

- **The client** sees what a person sees: how long until something appears on
  screen, how long until the answer starts, how long until it is done.
- **Hermes's own log** sees each model call inside the turn. One line per call,
  written by `agent.conversation_loop`, captured off eng-r on 2026-09-14::

      2026-09-14 09:35:54,917 INFO [20260913_205158_59d343] agent.conversation_loop:
      API call #5: model=z-ai/glm-5.2 provider=openrouter in=14497 out=38
      total=14535 latency=29.6s cache=10332/14497 (71%) id=gen-… upstream=StreamLake

  `upstream` is the provider OpenRouter routed that one call to, and the only
  place it appears — which is why the log is read at all. The spikes the
  ticket names coincided with it changing, and that is the hypothesis this
  exists to confirm or kill.

## Why percentiles and not an average

A turn is several calls in a row, so one 30-second call makes the whole turn
feel broken while the average stays at four seconds. p95 and the maximum are
the metric; p50 is there to show the spikes are spikes.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

_CALL = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(?P<ms>\d{3}) \S+ "
    r"\[(?P<session>[^\]]+)\] agent\.conversation_loop: API call #(?P<n>\d+): "
    r"(?P<fields>.*)$"
)
_FIELD = re.compile(r"(\w+)=(\S+)")
_CACHE = re.compile(r"cache=(\d+)/(\d+)")


@dataclass(frozen=True)
class ModelCall:
    """One model call, as Hermes logged it."""

    at: float  # epoch seconds, UTC
    session: str
    number: int
    model: str
    upstream: str
    tokens_in: int
    tokens_out: int
    latency_s: float
    cached_tokens: int | None

    @property
    def cache_pct(self) -> float | None:
        if self.cached_tokens is None or self.tokens_in <= 0:
            return None
        return 100.0 * self.cached_tokens / self.tokens_in


def parse_call(line: str) -> ModelCall | None:
    """One log line → a call, or None for any line that is not one.

    Tolerant of missing fields: `upstream` is absent when a provider is called
    directly rather than through a router, and a line that parses partly is
    still a latency worth counting. A line with no latency is not.
    """
    match = _CALL.match(line.strip())
    if not match:
        return None
    fields = dict(_FIELD.findall(match["fields"]))
    latency = fields.get("latency", "")
    if not latency.endswith("s"):
        return None
    try:
        latency_s = float(latency[:-1])
    except ValueError:
        return None

    cache = _CACHE.search(match["fields"])
    stamp = datetime.strptime(match["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)

    def number(key: str) -> int:
        try:
            return int(fields.get(key, "0"))
        except ValueError:
            return 0

    return ModelCall(
        at=stamp.timestamp() + int(match["ms"]) / 1000,
        session=match["session"],
        number=int(match["n"]),
        model=fields.get("model", ""),
        upstream=fields.get("upstream", "") or fields.get("provider", ""),
        tokens_in=number("in"),
        tokens_out=number("out"),
        latency_s=latency_s,
        cached_tokens=int(cache.group(1)) if cache else None,
    )


def calls_between(lines: list[str], start: float, end: float) -> list[ModelCall]:
    """The calls logged inside a measurement window, in order.

    A window, not a session id: the id in the log is the database's, and the
    socket only ever learns the live one — the two-ids trap `agent.rs`
    documents. A box that is being measured is not being used for anything
    else, which is what makes a time window honest.
    """
    calls = [c for c in (parse_call(line) for line in lines) if c is not None]
    return sorted((c for c in calls if start <= c.at <= end), key=lambda c: c.at)


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile. None for no data rather than a made-up zero."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class Spread:
    n: int
    p50: float | None
    p95: float | None
    max: float | None

    @classmethod
    def of(cls, values: list[float]) -> Spread:
        return cls(
            n=len(values),
            p50=percentile(values, 50),
            p95=percentile(values, 95),
            max=max(values) if values else None,
        )

    def row(self, label: str) -> str:
        def s(v: float | None) -> str:
            return "–" if v is None else f"{v:.1f}s"

        return f"| {label} | {self.n} | {s(self.p50)} | {s(self.p95)} | {s(self.max)} |"


@dataclass(frozen=True)
class TurnTiming:
    """One turn as the person experienced it. Seconds from submit."""

    prompt: str
    first_visible_s: float | None
    first_text_s: float | None
    total_s: float
    tools: int


#: Cache buckets. A cold cache re-bills and re-reads the whole prompt, so it
#: is the other suspect for a slow call, alongside a provider switch.
CACHE_BUCKETS = (
    (0.0, 50.0, "cache < 50%"),
    (50.0, 90.0, "cache 50–90%"),
    (90.0, 101.0, "cache ≥ 90%"),
)


def report(turns: list[TurnTiming], calls: list[ModelCall], *, title: str) -> str:
    """A Markdown report, the shape the ticket records."""
    header = "| | n | p50 | p95 | max |\n|---|---|---|---|---|"
    lines = [f"## {title}", ""]

    lines += ["**What the person waits for** (per turn, from submit)", "", header]
    lines.append(
        Spread.of([t.first_visible_s for t in turns if t.first_visible_s is not None]).row(
            "something on screen"
        )
    )
    lines.append(
        Spread.of([t.first_text_s for t in turns if t.first_text_s is not None]).row(
            "answer starts"
        )
    )
    lines.append(Spread.of([t.total_s for t in turns]).row("turn done"))

    lines += ["", "**Model calls** (Hermes's log)", "", header]
    lines.append(Spread.of([c.latency_s for c in calls]).row("all calls"))
    for upstream in sorted({c.upstream for c in calls}):
        group = [c.latency_s for c in calls if c.upstream == upstream]
        lines.append(Spread.of(group).row(f"upstream {upstream or 'unknown'}"))
    for low, high, label in CACHE_BUCKETS:
        group = [
            c.latency_s for c in calls if c.cache_pct is not None and low <= c.cache_pct < high
        ]
        lines.append(Spread.of(group).row(label))

    switches = sum(1 for a, b in zip(calls, calls[1:], strict=False) if a.upstream != b.upstream)
    lines += ["", f"Upstream changed between consecutive calls {switches} time(s)."]

    slow = sorted(calls, key=lambda c: c.latency_s, reverse=True)[:5]
    if slow:
        lines += [
            "",
            "**Slowest calls**",
            "",
            "| latency | upstream | cache | in | out |",
            "|---|---|---|---|---|",
        ]
        for c in slow:
            cache = "–" if c.cache_pct is None else f"{c.cache_pct:.0f}%"
            lines.append(
                f"| {c.latency_s:.1f}s | {c.upstream or 'unknown'} | {cache} "
                f"| {c.tokens_in} | {c.tokens_out} |"
            )

    lines += [
        "",
        "**Turns**",
        "",
        "| prompt | on screen | answer starts | done | tools |",
        "|---|---|---|---|---|",
    ]
    for t in turns:

        def s(v: float | None) -> str:
            return "–" if v is None else f"{v:.1f}s"

        lines.append(
            f"| {t.prompt[:48]} | {s(t.first_visible_s)} | {s(t.first_text_s)} "
            f"| {t.total_s:.1f}s | {t.tools} |"
        )
    return "\n".join(lines) + "\n"
