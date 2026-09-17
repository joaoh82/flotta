"""Does a model id exist, before an agent is pointed at it? (FLOTTA-39)

A typo in a model id does not fail at creation. It fails on every turn
afterwards, as "model not found", from an agent that otherwise looks healthy —
the same class of mistake as a capital letter in a box name (FLOTTA-32), and
refused for the same reason: before anything is spent on it.

Asked of OpenRouter's public catalogue, which needs no key. Other endpoints are
not asked: there is no standard way to list a gateway's models, and guessing
would refuse models that work.

**Refused on a definite no, allowed on an uncertain one** — the rule
`flotta.github.reachable` follows. A catalogue that is down must not stop
somebody choosing a model, and a model can be listed a minute after this looks.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from flotta.inference import is_openrouter

CATALOGUE_URL = "https://openrouter.ai/api/v1/models"

#: The catalogue changes when a provider launches something, not per request.
CACHE_S = 600.0


@dataclass(frozen=True)
class Known:
    """`yes`, `no` or `unknown`, and a sentence a person can act on."""

    verdict: str
    detail: str = ""

    @property
    def refuses(self) -> bool:
        return self.verdict == "no"


_lock = threading.Lock()
_cache: tuple[float, frozenset[str]] | None = None


def _fetch(timeout: float) -> frozenset[str]:
    request = urllib.request.Request(CATALOGUE_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode())
    return frozenset(
        str(item["id"])
        for item in payload.get("data", [])
        if isinstance(item, dict) and "id" in item
    )


def known_model(model: str, base_url: str, *, timeout: float = 5.0) -> Known:
    """Whether `model` exists at `base_url`, as far as can be told."""
    global _cache
    if not is_openrouter(base_url):
        return Known("unknown", "only OpenRouter's catalogue is checked")

    with _lock:
        cached = _cache if _cache and time.monotonic() - _cache[0] < CACHE_S else None
    if cached is None:
        try:
            ids = _fetch(timeout)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            return Known("unknown", f"could not read OpenRouter's catalogue: {exc}")
        if not ids:
            # An empty list is a broken answer, not proof that nothing exists.
            return Known("unknown", "OpenRouter's catalogue came back empty")
        with _lock:
            _cache = (time.monotonic(), ids)
    else:
        ids = cached[1]

    if model in ids:
        return Known("yes")
    close = sorted(i for i in ids if model.split("/")[-1].lower() in i.lower())[:3]
    hint = f" Did you mean {', '.join(close)}?" if close else ""
    return Known(
        "no",
        f"OpenRouter has no model {model!r}.{hint} Model ids are listed at "
        "https://openrouter.ai/models.",
    )
