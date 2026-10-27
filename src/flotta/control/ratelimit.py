"""A sliding-window count per key, in this process's memory.

For `POST /api/auth/link`, which sends email to an address anyone can type.
In memory rather than in the store: the count must cover addresses that have
no account, because a limit that applied only to real accounts would answer
"is there an account?" with a 429, and the store holds nothing for those.
A restart forgets the counts, which is a burst nobody can trigger at will.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class RateLimit:
    """At most `limit` hits per key in any `window_s` seconds."""

    def __init__(
        self, limit: int, window_s: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Count one hit for `key`. Whether it was within the limit.

        A refused hit is not counted, so someone hammering a limit does not
        push their own window forward forever.
        """
        now = self._clock()
        with self._lock:
            # Forget every key whose last hit has left the window, so what is
            # held is bounded by the traffic of one window, not of all time.
            for stale in [k for k, v in self._hits.items() if v[-1] <= now - self.window_s]:
                del self._hits[stale]
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True
