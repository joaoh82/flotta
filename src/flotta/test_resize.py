"""An agent's size (FLOTTA-157): memory, recorded on the timeline, changed by a resize."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from flotta.backend import BackendError, BoxHandle, NotSupported, SizeRefused
from flotta.provision import (
    SIZE_CHANGE_FAILED,
    SIZE_CHANGE_STARTED,
    SIZE_SET,
    InvalidSize,
    ProvisionError,
    ResizeNotSupported,
    UpgradeFailed,
    begin_resize,
    box_sizes,
    create_box,
    resize_box,
    size_change_of,
    size_changes,
)
from flotta.store import FleetStore, MachineMark
from flotta.test_provision import FakeBackend


@pytest.fixture
def store(tmp_path):
    with FleetStore(tmp_path / "fleet.db") as s:
        yield s


class Sized(FakeBackend):
    """A fake whose machine has a size, and which may refuse to change it."""

    def __init__(self, *, memory_mb=None, refuses=None, checks=None, **kwargs):
        super().__init__(**kwargs)
        self.memory_mb = memory_mb
        self.refuses = refuses
        self.checks = checks
        self.resized: list[tuple[str, int]] = []

    def check_resize(self, box_id, memory_mb):
        self.calls.append("check_resize")
        if self.checks:
            raise self.checks

    def create(self, spec):
        self.calls.append("create")
        self.spec = spec
        return BoxHandle(id="m1", endpoint="fake://app/m1", memory_mb=self.memory_mb)

    def resize(self, box_id, memory_mb):
        self.calls.append("resize")
        if self.refuses:
            raise self.refuses
        self.resized.append((box_id, memory_mb))


def _running(store, name="eng-a"):
    box = store.create_box(name)
    store.update_box_status(box.id, "running", endpoint="fake://app/m1")
    return box


def _types(store, box_id):
    return [e.type for e in store.get_events("box", box_id)]


def test_a_resize_reaches_the_machine_and_then_the_timeline(store):
    box = _running(store)
    impl = Sized()

    out = resize_box(box.id, 4096, store=store, backend=impl)

    assert out == {"box_id": box.id, "name": "eng-a", "memory_mb": 4096}
    assert impl.resized == [("fake://app/m1", 4096)]
    assert box_sizes(store, [box.id]) == {box.id: 4096}
    assert _types(store, box.id)[-1] == SIZE_SET


def test_a_refused_resize_leaves_the_size_it_was_and_says_why(store):
    box = _running(store)
    store.add_event("box", box.id, SIZE_SET, {"memory_mb": 1024, "reason": "created"})
    impl = Sized(refuses=BackendError("flyctl: insufficient capacity"))

    with pytest.raises(UpgradeFailed, match="still has 1024 MB"):
        resize_box(box.id, 4096, store=store, backend=impl)

    assert box_sizes(store, [box.id]) == {box.id: 1024}
    last = store.get_events("box", box.id)[-1]
    assert (last.type, last.payload["memory_mb"]) == (SIZE_CHANGE_FAILED, 4096)
    assert "insufficient capacity" in last.payload["error"]


def test_a_substrate_with_no_size_says_so_rather_than_recording_one(store):
    """A local stand-in is a process: `NotSupported`, which is a backend error,
    and the agent's size stays unknown rather than becoming a number."""
    box = _running(store)
    with pytest.raises(UpgradeFailed):
        resize_box(box.id, 2048, store=store, backend=Sized(refuses=NotSupported("no VM")))
    assert box_sizes(store, [box.id]) == {}


@pytest.mark.parametrize("bad", [512, 3000, "2048", None, True])
def test_a_size_off_the_list_is_refused_before_the_substrate(store, bad):
    box = _running(store)
    impl = Sized()
    with pytest.raises(InvalidSize, match="1024, 2048, 4096"):
        begin_resize(box.id, bad, store=store, backend=Sized())
    with pytest.raises(InvalidSize):
        resize_box(box.id, bad, store=store, backend=impl)
    assert impl.calls == []


def test_a_box_being_created_or_gone_cannot_be_resized(store):
    creating = store.create_box("eng-new")
    with pytest.raises(ProvisionError, match="still being created"):
        begin_resize(creating.id, 2048, store=store, backend=Sized())

    gone = _running(store, "eng-old")
    store.update_box_status(gone.id, "torn_down")
    with pytest.raises(ProvisionError, match="no machine to resize"):
        begin_resize(gone.id, 2048, store=store, backend=Sized())


def test_beginning_a_resize_says_it_is_under_way_until_it_lands(store):
    box = _running(store)

    started = begin_resize(box.id, 4096, store=store, backend=Sized())

    assert started == {"box_id": box.id, "name": "eng-a", "memory_mb": 4096}
    change = size_changes(store, [box.id])[box.id]
    assert change["memory_mb"] == 4096 and change["since"]
    with pytest.raises(ProvisionError, match="already being resized"):
        begin_resize(box.id, 2048, store=store, backend=Sized())

    resize_box(box.id, 4096, store=store, backend=Sized())
    assert size_changes(store, [box.id]) == {}


def test_a_failed_resize_is_not_still_under_way(store):
    box = _running(store)
    begin_resize(box.id, 4096, store=store, backend=Sized())
    with pytest.raises(UpgradeFailed):
        resize_box(box.id, 4096, store=store, backend=Sized(refuses=BackendError("no")))
    assert size_changes(store, [box.id]) == {}


def test_a_start_nobody_finished_stops_saying_so():
    """The control plane restarting mid-resize records no outcome; "restarting"
    for ever would be the window lying the other way."""

    class Event:
        type = SIZE_CHANGE_STARTED
        payload = {"memory_mb": 4096}
        ts = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()

    assert size_change_of(Event()) is None
    Event.ts = datetime.now(UTC).isoformat()
    assert size_change_of(Event())["memory_mb"] == 4096


def test_an_agent_already_that_size_records_nothing(store):
    box = _running(store)
    store.add_event("box", box.id, SIZE_SET, {"memory_mb": 2048, "reason": "created"})
    before = _types(store, box.id)

    out = begin_resize(box.id, 2048, store=store, backend=Sized())

    assert out["already_current"] is True
    assert _types(store, box.id) == before


def test_an_agent_older_than_the_event_has_the_size_metering_read(store):
    """Every agent made before FLOTTA-157 has no `size_set`, and the hourly
    sweep has read its machine's size off Fly all along."""
    old = _running(store, "eng-old")
    blank = _running(store, "eng-blank")
    store.set_machine_mark(
        MachineMark(old.id, datetime.now(UTC).isoformat(), "machine:shared-1x-1024", "ams", 1)
    )
    assert box_sizes(store, [old.id, blank.id]) == {old.id: 1024}

    # And a resize is believed over the sweep, which is up to an hour behind.
    resize_box(old.id, 2048, store=store, backend=Sized())
    assert box_sizes(store, [old.id]) == {old.id: 2048}


def test_creating_records_the_size_the_machine_was_given(store):
    impl = Sized(memory_mb=2048)
    out = create_box("eng-a", store=store, backend=impl)
    assert impl.spec.memory_mb is None, "the backend's default, not one invented here"
    assert box_sizes(store, [out["box_id"]]) == {out["box_id"]: 2048}


def test_one_agent_can_be_created_bigger(store):
    impl = Sized(memory_mb=4096)
    out = create_box("eng-big", store=store, backend=impl, memory_mb=4096)
    assert impl.spec.memory_mb == 4096
    resources = [e for e in store.get_events("box", out["box_id"]) if e.type == "resources"]
    assert resources[-1].payload["memory_mb"] == 4096


def test_creating_with_a_size_off_the_list_spends_nothing(store):
    impl = Sized()
    with pytest.raises(InvalidSize):
        create_box("eng-odd", store=store, backend=impl, memory_mb=3000)
    assert impl.calls == []
    assert store.get_box_by_name("eng-odd") is None


def test_a_backend_that_does_not_say_records_no_size(store):
    """A handle without a size (a local stand-in) leaves `memory_mb` unknown."""
    out = create_box("eng-a", store=store, backend=Sized(memory_mb=None))
    assert box_sizes(store, [out["box_id"]]) == {}


def test_a_substrate_that_cannot_resize_is_refused_before_anything_says_resizing(store):
    """A local stand-in: refused up front, so the window never shows a restart."""
    box = _running(store)
    with pytest.raises(ResizeNotSupported, match="no VM"):
        begin_resize(box.id, 4096, store=store, backend=Sized(checks=NotSupported("no VM")))
    assert size_changes(store, [box.id]) == {}
    assert SIZE_CHANGE_STARTED not in _types(store, box.id)


def test_a_size_the_machine_cannot_have_is_refused_before_anything_says_resizing(store):
    box = _running(store)
    refused = SizeRefused("a Fly performance machine with 1 CPU(s) needs at least 2048 MB")
    with pytest.raises(InvalidSize, match="needs at least 2048 MB"):
        begin_resize(box.id, 1024, store=store, backend=Sized(checks=refused))
    assert SIZE_CHANGE_STARTED not in _types(store, box.id)


def _read(store, box_id, label, at):
    store.set_machine_mark(MachineMark(box_id, at.isoformat(), label, "ams", 1))


def _stamp_last(store, box_id, at):
    last = store.get_events("box", box_id)[-1]
    store._conn.execute("UPDATE events SET ts = ? WHERE id = ?", (at.isoformat(), last.id))


def test_a_machine_resized_elsewhere_shows_what_the_sweep_read_and_can_be_put_back(store):
    """`fly machine update` by hand after a resize here: the sweep, which read
    the machine afterwards, wins over the event, and a resize back is not
    answered `already_current`."""
    box = _running(store)
    now = datetime.now(UTC)
    store.add_event("box", box.id, SIZE_SET, {"memory_mb": 2048, "reason": "resized"})
    _stamp_last(store, box.id, now - timedelta(hours=3))
    _read(store, box.id, "machine:shared-2x-4096", now - timedelta(hours=1))

    assert box_sizes(store, [box.id]) == {box.id: 4096}
    out = begin_resize(box.id, 2048, store=store, backend=Sized())
    assert "already_current" not in out


def test_a_resize_here_is_believed_until_the_sweep_reads_the_machine_again(store):
    box = _running(store)
    now = datetime.now(UTC)
    _read(store, box.id, "machine:shared-1x-1024", now - timedelta(minutes=40))
    store.add_event("box", box.id, SIZE_SET, {"memory_mb": 2048, "reason": "resized"})

    assert box_sizes(store, [box.id]) == {box.id: 2048}
    assert begin_resize(box.id, 2048, store=store, backend=Sized())["already_current"] is True


class Adopts(Sized):
    def create(self, spec):
        self.calls.append("create")
        self.spec = spec
        return BoxHandle(id="m1", endpoint="fake://app/m1", adopted=True, memory_mb=1024)


def test_adopting_a_machine_says_the_size_asked_for_was_not_applied(store):
    out = create_box("eng-a", store=store, backend=Adopts(), memory_mb=4096)
    events = {e.type: e.payload for e in store.get_events("box", out["box_id"])}

    assert events[SIZE_SET]["memory_mb"] == 1024
    assert events["size_not_applied"]["requested_memory_mb"] == 4096
    assert events["size_not_applied"]["memory_mb"] == 1024
    assert box_sizes(store, [out["box_id"]]) == {out["box_id"]: 1024}


def test_adopting_a_machine_already_that_size_says_nothing_more(store):
    out = create_box("eng-a", store=store, backend=Adopts(), memory_mb=1024)
    assert "size_not_applied" not in _types(store, out["box_id"])
