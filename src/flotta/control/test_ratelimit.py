from __future__ import annotations

from flotta.control.ratelimit import RateLimit


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_key_is_limited_within_the_window_and_free_after_it():
    clock = Clock()
    limit = RateLimit(2, 60, clock=clock)

    assert [limit.hit("a"), limit.hit("a"), limit.hit("a")] == [True, True, False]
    assert limit.hit("b")
    clock.now += 60
    assert limit.hit("a")


def test_a_refused_hit_does_not_extend_the_window():
    clock = Clock()
    limit = RateLimit(1, 60, clock=clock)
    limit.hit("a")
    clock.now += 59
    assert not limit.hit("a")
    clock.now += 1
    assert limit.hit("a")


def test_keys_whose_window_has_passed_are_forgotten():
    clock = Clock()
    limit = RateLimit(1, 60, clock=clock)
    for i in range(100):
        limit.hit(f"k{i}")
    clock.now += 61
    limit.hit("fresh")
    assert set(limit._hits) == {"fresh"}
