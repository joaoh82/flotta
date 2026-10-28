"""A sliding-window count per key, in this process's memory.

For `POST /api/auth/link`, which sends email to an address anyone can type.
In memory rather than in the store: the count must cover addresses that have
no account, because a limit that applied only to real accounts would answer
"is there an account?" with a 429, and the store holds nothing for those.
A restart forgets the counts, which is a burst nobody can trigger at will.
"""

from __future__ import annotations

import contextlib
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
        return hit_all((self, key))

    def _room(self, key: str, now: float) -> bool:
        """Whether one more hit for `key` fits. Counts nothing. Under `_lock`."""
        # Forget every key whose last hit has left the window, so what is held
        # is bounded by the traffic of one window, not of all time.
        for stale in [k for k, v in self._hits.items() if v[-1] <= now - self.window_s]:
            del self._hits[stale]
        hits = self._hits.get(key)
        if hits is None:
            return self.limit > 0
        while hits and hits[0] <= now - self.window_s:
            hits.popleft()
        return len(hits) < self.limit

    def _count(self, key: str, now: float) -> None:
        """One hit for `key`. Under `_lock`, after `_room` said yes.

        The only place a key is added, and always with a hit in it: an empty
        entry would be a key the pruning above cannot read the age of.
        """
        self._hits.setdefault(key, deque()).append(now)


def hit_all(*checks: tuple[RateLimit, str]) -> bool:
    """One hit in each `(limit, key)`, or in none of them.

    Every limit is checked before any is counted. Counting as it went would
    let a request refused by the second limit spend the first: a caller over
    its own limit could still use up a stranger's address, five requests at a
    time, and lock that person out without ever being let through.
    """
    with contextlib.ExitStack() as held:
        # A fixed order, whatever the caller's, so two of these cannot deadlock.
        for limit in sorted({id(c[0]): c[0] for c in checks}.values(), key=id):
            held.enter_context(limit._lock)
        moments = [(limit, key, limit._clock()) for limit, key in checks]
        if not all(limit._room(key, now) for limit, key, now in moments):
            return False
        for limit, key, now in moments:
            limit._count(key, now)
        return True
