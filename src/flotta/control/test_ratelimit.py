from __future__ import annotations

from flotta.control.ratelimit import RateLimit, hit_all


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


def test_a_hit_refused_by_one_limit_is_counted_by_none():
    clock = Clock()
    per_address = RateLimit(5, 60, clock=clock)
    per_caller = RateLimit(1, 60, clock=clock)

    assert hit_all((per_address, "ana"), (per_caller, "attacker"))
    for _ in range(10):
        assert not hit_all((per_address, "ana"), (per_caller, "attacker"))

    # Ana's own five are untouched by the attacker's refused requests: one
    # was let through, so four are left.
    assert [hit_all((per_address, "ana"), (per_caller, f"c{i}")) for i in range(5)] == [
        True,
        True,
        True,
        True,
        False,
    ]


def test_a_refused_hit_leaves_nothing_behind_for_the_other_key():
    """A key checked but not counted must not linger as an empty entry: the
    next pruning reads every entry's newest hit, and an empty one has none."""
    clock = Clock()
    per_address = RateLimit(5, 60, clock=clock)
    per_caller = RateLimit(1, 60, clock=clock)
    hit_all((per_address, "a"), (per_caller, "attacker"))

    assert not hit_all((per_address, "never-counted"), (per_caller, "attacker"))
    assert "never-counted" not in per_address._hits
    assert hit_all((per_address, "b"), (per_caller, "someone-else"))
