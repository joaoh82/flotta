"""Nothing puts an agent to sleep under a person on its screen (FLOTTA-211).

The guard first: **a person holding the screen, with no turn running and nobody
connected, keeps the agent awake** — a takeover is the agent ending its turn to
ask for a sign-in, so the sweep's other signals all read it as idle. Then the
cap that keeps a person who walked away from billing a 4 GB machine for ever,
and the door's reports, written only when they are news.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from flotta.provision import (
    SCREEN_HOLD_CAP_S,
    SCREEN_HOLD_LAPSED_EVENT,
    SCREEN_LEASE_EVENT,
    ScreenHeld,
    idle_boxes,
    record_screen_lease,
    refuse_while_screen_held,
    screen_hold,
    sleep_idle_boxes,
)
from flotta.store import FleetStore
from flotta.test_provision import FakeBackend

TAKEN = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    with FleetStore(tmp_path / "fleet.db") as s:
        yield s


def _quiet(store, box_id, ago_s):
    """Every event of the box `ago_s` seconds old: nothing has happened since."""
    old = (datetime.now(UTC) - timedelta(seconds=ago_s)).isoformat()
    store._conn.execute(
        "UPDATE events SET ts = ? WHERE entity_kind = 'box' AND entity_id = ?", (old, box_id)
    )


def _agent(store, name="eng-a"):
    box = store.create_box(name)
    store.update_box_status(box.id, "running", endpoint=f"fly://app/{name}")
    store.add_event("box", box.id, "running", {"endpoint": f"fly://app/{name}"})
    return box


def _taken_over(store, *, quiet_s, by="user:u-1"):
    box = _agent(store)
    record_screen_lease(store, box.id, holder="human", since=TAKEN, by=by)
    _quiet(store, box.id, quiet_s)
    return box


def _types(store, box_id):
    return [e.type for e in store.get_box_timeline(box_id)]


# -- the guard ------------------------------------------------------------------


def test_a_person_holding_the_screen_with_no_turn_is_never_put_to_sleep(store):
    """An hour of nothing, a half-hour threshold, and a person mid-sign-in."""
    box = _taken_over(store, quiet_s=3600)

    assert sleep_idle_boxes(store, backend=FakeBackend(), idle_after_s=1800) == []
    assert store.get_box(box.id).status == "running"


def test_an_agent_handed_back_to_sleeps_as_any_other(store):
    box = _taken_over(store, quiet_s=0)
    record_screen_lease(store, box.id, holder="agent", since=TAKEN + timedelta(minutes=5))
    _quiet(store, box.id, 3600)

    outcomes = sleep_idle_boxes(store, backend=FakeBackend(), idle_after_s=1800)
    assert [(o["box_id"], o["slept"]) for o in outcomes] == [(box.id, True)]
    assert "screen_hold_lapsed" not in outcomes[0]


# -- the cap --------------------------------------------------------------------


def test_a_person_who_walked_away_holds_it_only_until_the_cap(store):
    box = _taken_over(store, quiet_s=SCREEN_HOLD_CAP_S + 60)

    outcomes = sleep_idle_boxes(store, backend=FakeBackend(), idle_after_s=1800)

    assert [(o["box_id"], o["slept"], o.get("screen_hold_lapsed")) for o in outcomes] == [
        (box.id, True, True)
    ]
    lapsed = [e for e in store.get_box_timeline(box.id) if e.type == SCREEN_HOLD_LAPSED_EVENT]
    assert len(lapsed) == 1
    assert lapsed[0].payload["since"] == TAKEN.isoformat()
    assert lapsed[0].payload["by"] == "user:u-1"
    # After the stop, never instead of it.
    assert _types(store, box.id)[-2:] == ["stopped", SCREEN_HOLD_LAPSED_EVENT]


def test_just_under_the_cap_it_still_holds(store):
    box = _taken_over(store, quiet_s=SCREEN_HOLD_CAP_S - 60)
    assert idle_boxes(store, idle_after_s=1800) == []
    assert screen_hold(store, box.id).lapsed is False


def test_a_lapsed_hold_holds_nothing_after_the_next_wake(store):
    """Or every wake of a forgotten takeover would be four more hours billed."""
    box = _taken_over(store, quiet_s=SCREEN_HOLD_CAP_S + 60)
    sleep_idle_boxes(store, backend=FakeBackend(), idle_after_s=1800)
    store.update_box_status(box.id, "running")
    store.add_event("box", box.id, "addressed", {"via": "front-door"})
    _quiet(store, box.id, 3600)

    assert screen_hold(store, box.id) is None
    assert [b.id for b, _ in idle_boxes(store, idle_after_s=1800)] == [box.id]


def test_a_new_takeover_after_a_lapse_holds_again(store):
    box = _taken_over(store, quiet_s=SCREEN_HOLD_CAP_S + 60)
    sleep_idle_boxes(store, backend=FakeBackend(), idle_after_s=1800)
    store.update_box_status(box.id, "running")
    record_screen_lease(store, box.id, holder="human", since=TAKEN + timedelta(hours=6))
    _quiet(store, box.id, 3600)

    assert idle_boxes(store, idle_after_s=1800) == []


@pytest.mark.parametrize("bookkeeping", ["upgrade_skipped", "screen_hold_forced"])
def test_what_the_fleet_did_about_an_agent_moves_neither_clock(store, bookkeeping):
    """Each press of "Update agents" would otherwise buy a forgotten takeover
    four more hours, and an idle agent another half hour."""
    box = _taken_over(store, quiet_s=SCREEN_HOLD_CAP_S + 60)
    store.add_event("box", box.id, bookkeeping, {"build_id": "bld-1"})

    assert screen_hold(store, box.id) is None or screen_hold(store, box.id).lapsed
    assert [b.id for b, _ in idle_boxes(store, idle_after_s=1800)] == [box.id]


def test_a_force_ends_the_hold(store):
    box = _taken_over(store, quiet_s=60)
    store.add_event("box", box.id, "screen_hold_forced", {"verb": "re-image"})
    assert screen_hold(store, box.id) is None
    # Until the screen changes hands again.
    record_screen_lease(store, box.id, holder="human", since=TAKEN + timedelta(hours=1))
    assert screen_hold(store, box.id).lapsed is False


def test_box_events_of_one_type_are_read_fleet_wide_newest_first(store):
    first, second = _agent(store, "eng-a"), _agent(store, "eng-b")
    store.add_event("box", first.id, "upgrade_skipped", {"n": 1})
    store.add_event("box", second.id, "upgrade_skipped", {"n": 2})
    store.add_event("box", first.id, "addressed", {})

    found = store.box_events_of_type("upgrade_skipped")
    assert [(e.entity_id, e.payload["n"]) for e in found] == [(second.id, 2), (first.id, 1)]
    assert len(store.box_events_of_type("upgrade_skipped", limit=1)) == 1


# -- the door's reports ---------------------------------------------------------


def test_a_report_is_written_only_when_it_is_news(store):
    """Each door socket on the agent hears the same change, and each event is
    activity: written every time, a held screen's agent could never sleep."""
    box = _agent(store)

    assert record_screen_lease(store, box.id, holder="human", since=TAKEN) is True
    assert record_screen_lease(store, box.id, holder="human", since=TAKEN) is False
    # Who took it, from the socket that asked: worth writing, for the refusal.
    assert record_screen_lease(store, box.id, holder="human", since=TAKEN, by="user:u-1") is True
    assert record_screen_lease(store, box.id, holder="human", since=TAKEN) is False
    assert record_screen_lease(store, box.id, holder="agent", since=TAKEN) is True
    assert _types(store, box.id).count(SCREEN_LEASE_EVENT) == 3


def test_an_agent_nobody_ever_took_over_gets_no_events(store):
    box = _agent(store)
    assert record_screen_lease(store, box.id, holder="agent", since=TAKEN) is False
    assert SCREEN_LEASE_EVENT not in _types(store, box.id)


# -- the refusal ----------------------------------------------------------------


def test_the_refusal_says_who_and_since_when(store):
    user = store.create_user("ana@agency.example")
    box = _taken_over(store, quiet_s=60, by=f"user:{user.id}")

    with pytest.raises(ScreenHeld) as refused:
        refuse_while_screen_held(store, store.get_box(box.id), verb="re-image")

    said = str(refused.value)
    assert "ana@agency.example" in said
    assert TAKEN.isoformat() in said
    assert "re-image" in said


def test_an_asleep_agent_is_not_refused(store):
    """Its sign-in ended when it slept; re-imaging it now ends nothing more."""
    box = _taken_over(store, quiet_s=60)
    store.update_box_status(box.id, "stopped")
    refuse_while_screen_held(store, store.get_box(box.id), verb="re-image")


def test_a_lapsed_hold_refuses_nothing(store):
    box = _taken_over(store, quiet_s=SCREEN_HOLD_CAP_S + 60)
    refuse_while_screen_held(store, store.get_box(box.id), verb="put to sleep")
