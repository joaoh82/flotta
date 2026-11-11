"""An agent's screen (FLOTTA-208): chosen at creation, changed later, and never below 4 GB."""

from __future__ import annotations

import pytest

from flotta.backend import (
    DEFAULT_MEMORY_MB,
    SCREEN_MIN_MEMORY_MB,
    BackendError,
    BoxHandle,
    BoxSpec,
    NotSupported,
)
from flotta.provision import (
    SCREEN_CHANGE_FAILED,
    SCREEN_CHANGE_STARTED,
    SCREEN_SET,
    SIZE_CHANGE_STARTED,
    SIZE_SET,
    ProvisionError,
    ScreenTooSmall,
    UpgradeFailed,
    begin_resize,
    begin_screen_change,
    box_sizes,
    create_box,
    resize_box,
    screen_changes,
    set_screen,
)
from flotta.store import FleetStore
from flotta.test_resize import Sized


@pytest.fixture
def store(tmp_path):
    with FleetStore(tmp_path / "fleet.db") as s:
        yield s


class Screened(Sized):
    """`Sized`, whose resize may carry env in the same restart."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.resized_env: list[dict[str, str] | None] = []
        self.floors: list[bool] = []

    def resize(self, box_id, memory_mb, *, env=None, at_least=False):
        super().resize(box_id, memory_mb)
        self.resized_env.append(env)
        self.floors.append(at_least)
        return memory_mb


def _running(store, name="eng-a", memory_mb=DEFAULT_MEMORY_MB, screen=False):
    box = store.create_box(name)
    store.update_box_status(box.id, "running", endpoint="fake://app/m1")
    if memory_mb is not None:
        store.add_event("box", box.id, SIZE_SET, {"memory_mb": memory_mb, "reason": "created"})
    if screen:
        store.set_box_screen(box.id, True)
    return store.get_box(box.id)


def _types(store, box_id):
    return [e.type for e in store.get_events("box", box_id)]


# --- creation ---------------------------------------------------------------


def test_creating_a_screen_agent_makes_it_the_screens_minimum(store):
    impl = Screened(memory_mb=None)
    out = create_box("eng-s", store=store, backend=impl, screen=True)

    assert impl.spec.memory_mb == SCREEN_MIN_MEMORY_MB == 4096
    assert impl.spec.env["FLOTTA_SCREEN"] == "1"
    assert "FLOTTA_SCREEN" not in impl.spec.secrets
    box = store.get_box(out["box_id"])
    assert box.screen is True
    assert box_sizes(store, [box.id]) == {box.id: 4096}
    assert SCREEN_SET in _types(store, box.id)


def test_creating_without_a_screen_is_exactly_as_before(store):
    """No size named, no `FLOTTA_SCREEN`: the spec today's create builds, so the
    backend's default (`DEFAULT_MEMORY_MB`) decides, and the box reads an absent
    switch as no screen."""
    plain = Screened()
    create_box("eng-p", store=store, backend=plain)
    assert plain.spec.memory_mb is None
    assert "FLOTTA_SCREEN" not in plain.spec.env
    assert "FLOTTA_SCREEN" not in plain.spec.secrets
    assert store.get_box_by_name("eng-p").screen is False
    assert SCREEN_SET not in _types(store, store.get_box_by_name("eng-p").id)


@pytest.mark.parametrize("memory_mb", [1024, 2048])
def test_a_screen_agent_named_smaller_is_refused_and_spends_nothing(store, memory_mb):
    impl = Screened()
    with pytest.raises(ScreenTooSmall, match="4096 MB"):
        create_box("eng-s", store=store, backend=impl, screen=True, memory_mb=memory_mb)
    assert impl.calls == []
    assert store.get_box_by_name("eng-s") is None


def test_a_callers_spec_cannot_make_a_screen_agent_smaller_either(store):
    impl = Screened()
    with pytest.raises(ScreenTooSmall):
        create_box(
            "eng-s",
            store=store,
            backend=impl,
            screen=True,
            spec=BoxSpec(name="eng-s", memory_mb=2048),
        )
    assert impl.calls == []


def test_a_screen_agent_may_be_named_the_minimum(store):
    impl = Screened(memory_mb=4096)
    create_box("eng-s", store=store, backend=impl, screen=True, memory_mb=4096)
    assert impl.spec.memory_mb == 4096


def test_a_reserved_row_carries_its_screen_to_the_provision(store):
    """The 202 path: the API reserves the row, a thread provisions it later."""
    from flotta.provision import reserve_box

    impl = Screened(memory_mb=None)
    box = reserve_box("eng-s", store=store, backend=impl, screen=True)
    assert box.screen is True
    create_box("eng-s", store=store, backend=impl, box=box)
    assert impl.spec.memory_mb == 4096
    assert impl.spec.env["FLOTTA_SCREEN"] == "1"


class Adopts(Screened):
    def __init__(self, *, adopted_memory=1024, **kwargs):
        super().__init__(**kwargs)
        self.adopted_memory = adopted_memory

    def create(self, spec):
        self.calls.append("create")
        self.spec = spec
        return BoxHandle(
            id="m1", endpoint="fake://app/m1", adopted=True, memory_mb=self.adopted_memory
        )


def test_an_adopted_machine_gets_its_screen_and_its_size_in_one_update(store):
    impl = Adopts(adopted_memory=1024)
    out = create_box("eng-s", store=store, backend=impl, screen=True)
    assert impl.resized == [("fake://app/m1", 4096)]
    assert impl.resized_env == [{"FLOTTA_SCREEN": "1"}]
    assert box_sizes(store, [out["box_id"]]) == {out["box_id"]: 4096}
    assert store.get_box(out["box_id"]).screen is True


def test_an_adopted_machine_already_big_enough_only_gets_the_switch(store):
    impl = Adopts(adopted_memory=4096)
    out = create_box("eng-s", store=store, backend=impl, screen=True)
    assert impl.resized == []
    assert impl.env["FLOTTA_SCREEN"] == "1"
    assert store.get_box(out["box_id"]).screen is True


def test_an_adopted_machine_that_refuses_is_left_saying_no_screen(store):
    impl = Adopts(adopted_memory=1024, refuses=BackendError("no capacity"))
    out = create_box("eng-s", store=store, backend=impl, screen=True)
    assert store.get_box(out["box_id"]).screen is False
    assert SCREEN_CHANGE_FAILED in _types(store, out["box_id"])


# --- turning it on and off ----------------------------------------------------


def test_turning_a_screen_on_resizes_in_the_same_restart(store):
    box = _running(store, memory_mb=2048)
    impl = Screened()

    started = begin_screen_change(box.id, True, store=store, backend=impl)
    assert started["resized"] is True and started["memory_mb"] == 4096
    assert {SCREEN_CHANGE_STARTED, SIZE_CHANGE_STARTED} <= set(_types(store, box.id))
    assert screen_changes(store, [box.id])[box.id]["screen"] is True

    out = set_screen(box.id, True, store=store, backend=impl)

    # One `resize` carrying the switch, and no separate env write: one restart.
    assert impl.resized == [("fake://app/m1", 4096)]
    assert impl.resized_env == [{"FLOTTA_SCREEN": "1"}]
    assert impl.floors == [True]
    assert "apply_env" not in impl.calls
    assert out == {
        "box_id": box.id,
        "name": "eng-a",
        "screen": True,
        "memory_mb": 4096,
        "resized": True,
    }
    assert store.get_box(box.id).screen is True
    assert box_sizes(store, [box.id]) == {box.id: 4096}
    assert screen_changes(store, [box.id]) == {}


def test_an_agent_of_unknown_size_is_resized_to_be_sure(store):
    box = _running(store, memory_mb=None)
    impl = Screened()
    set_screen(box.id, True, store=store, backend=impl)
    assert impl.resized == [("fake://app/m1", 4096)]


def test_a_machine_bigger_than_the_store_knew_keeps_its_size(store):
    """The store did not know (None); the machine had 8 GB, set by hand. A floor
    of 4096 leaves it 8 GB, and the timeline says what it really has."""

    class Big(Screened):
        def resize(self, box_id, memory_mb, *, env=None, at_least=False):
            self.calls.append("resize")
            self.resized_env.append(env)
            assert at_least
            return 8192

    box = _running(store, memory_mb=None)
    out = set_screen(box.id, True, store=store, backend=Big())
    assert out["memory_mb"] == 8192 and out["resized"] is False
    assert box_sizes(store, [box.id]) == {box.id: 8192}


def test_a_caller_who_may_not_resize_is_refused_before_anything_is_recorded(store):
    from flotta.provision import ScreenNeedsResize

    small = _running(store, memory_mb=2048)
    with pytest.raises(ScreenNeedsResize, match="only the org's owner"):
        begin_screen_change(small.id, True, store=store, backend=Screened(), may_resize=False)
    assert SCREEN_CHANGE_STARTED not in _types(store, small.id)

    # Big enough already, or turning it off: no size changes, so anyone's.
    big = _running(store, name="eng-b", memory_mb=4096)
    assert begin_screen_change(big.id, True, store=store, backend=Screened(), may_resize=False)
    on = _running(store, name="eng-c", memory_mb=4096, screen=True)
    assert begin_screen_change(on.id, False, store=store, backend=Screened(), may_resize=False)


def test_turning_a_screen_on_for_a_big_agent_only_flips_the_switch(store):
    box = _running(store, memory_mb=4096)
    impl = Screened()
    out = set_screen(box.id, True, store=store, backend=impl)
    assert impl.resized == []
    assert impl.env == {"FLOTTA_SCREEN": "1"}
    assert out["resized"] is False and out["memory_mb"] == 4096


def test_turning_a_screen_off_keeps_the_size(store):
    box = _running(store, memory_mb=4096, screen=True)
    impl = Screened()
    out = set_screen(box.id, False, store=store, backend=impl)
    assert impl.resized == []
    assert impl.env == {"FLOTTA_SCREEN": "0"}
    assert out["memory_mb"] == 4096
    assert store.get_box(box.id).screen is False
    assert box_sizes(store, [box.id]) == {box.id: 4096}


def test_a_substrate_with_no_size_gets_the_screen_without_a_resize(store):
    """A local stand-in is a process: there is no machine to be too small."""
    box = _running(store, memory_mb=None)
    impl = Screened(checks=NotSupported("a local stand-in"))
    started = begin_screen_change(box.id, True, store=store, backend=impl)
    assert started["resized"] is False
    set_screen(box.id, True, store=store, backend=impl)
    assert impl.resized == []
    assert impl.env == {"FLOTTA_SCREEN": "1"}


def test_a_refused_change_leaves_the_screen_as_it_was(store):
    box = _running(store, memory_mb=2048)
    impl = Screened(refuses=BackendError("flyctl: no capacity"))
    begin_screen_change(box.id, True, store=store, backend=impl)
    with pytest.raises(UpgradeFailed, match="still has no screen"):
        set_screen(box.id, True, store=store, backend=impl)
    assert store.get_box(box.id).screen is False
    assert box_sizes(store, [box.id]) == {box.id: 2048}
    assert _types(store, box.id)[-2:] == [SCREEN_CHANGE_FAILED, "size_change_failed"]
    assert screen_changes(store, [box.id]) == {}


def test_a_screen_already_so_is_no_change(store):
    box = _running(store, memory_mb=4096, screen=True)
    out = begin_screen_change(box.id, True, store=store, backend=Screened())
    assert out["already_current"] is True
    assert SCREEN_CHANGE_STARTED not in _types(store, box.id)
    plain = _running(store, name="eng-b")
    assert begin_screen_change(plain.id, False, store=store, backend=Screened())["already_current"]


def test_one_change_at_a_time(store):
    box = _running(store, memory_mb=2048)
    begin_screen_change(box.id, True, store=store, backend=Screened())
    with pytest.raises(ProvisionError, match="already being changed"):
        begin_screen_change(box.id, False, store=store, backend=Screened())
    with pytest.raises(ProvisionError, match="already being resized"):
        begin_resize(box.id, 2048, store=store, backend=Screened())

    # And a screen going off, which resizes nothing, still holds resizes back.
    big = _running(store, name="eng-b", memory_mb=4096, screen=True)
    begin_screen_change(big.id, False, store=store, backend=Screened())
    with pytest.raises(ProvisionError, match="screen is being changed"):
        begin_resize(big.id, 4096, store=store, backend=Screened())


def test_a_box_being_created_or_gone_has_no_screen_to_change(store):
    creating = store.create_box("eng-new")
    with pytest.raises(ProvisionError, match="still being created"):
        begin_screen_change(creating.id, True, store=store, backend=Screened())
    gone = _running(store, "eng-old")
    store.update_box_status(gone.id, "torn_down")
    with pytest.raises(ProvisionError, match="no machine"):
        begin_screen_change(gone.id, True, store=store, backend=Screened())


# --- the guard: a screen agent is never below the minimum ---------------------


@pytest.mark.parametrize("memory_mb", [1024, 2048])
def test_a_screen_agent_cannot_be_resized_below_the_minimum(store, memory_mb):
    box = _running(store, memory_mb=4096, screen=True)
    impl = Screened()
    with pytest.raises(ScreenTooSmall, match="Turn the screen off first"):
        begin_resize(box.id, memory_mb, store=store, backend=impl)
    with pytest.raises(ScreenTooSmall):
        resize_box(box.id, memory_mb, store=store, backend=impl)
    assert impl.resized == []


def test_after_the_screen_goes_the_owner_can_size_down(store):
    box = _running(store, memory_mb=4096, screen=True)
    set_screen(box.id, False, store=store, backend=Screened())
    impl = Screened()
    resize_box(box.id, 2048, store=store, backend=impl)
    assert impl.resized == [("fake://app/m1", 2048)]


# --- the substrate ------------------------------------------------------------


def test_fly_resizes_and_sets_the_switch_in_one_machine_update():
    import subprocess

    from flotta.backends.fly_backend import FlyBackend
    from flotta.test_backend import _offline_config

    issued: list[list[str]] = []

    def runner(cmd, *, timeout, check, stdin=None):
        issued.append(cmd)
        out = ""
        if cmd[1:3] == ["machine", "list"] or cmd[1:3] == ["machines", "list"]:
            out = '[{"id": "m1", "state": "started", "config": {"guest": {"cpu_kind": "shared"}}}]'
        return subprocess.CompletedProcess(cmd, 0, out, "")

    FlyBackend(config=_offline_config(), runner=runner).resize(
        "fly://joaoh82-flotta-eng-s/m1", 4096, env={"FLOTTA_SCREEN": "1"}
    )
    (update,) = [c for c in issued if c[1:3] == ["machine", "update"]]
    assert update[update.index("--vm-memory") + 1] == "4096"
    assert update[update.index("--env") + 1] == "FLOTTA_SCREEN=1"
    assert not any(c[1:3] == ["secrets", "import"] for c in issued)


@pytest.mark.parametrize(("has", "resizes"), [(8192, False), (4096, False), (2048, True)])
def test_fly_never_shrinks_a_machine_to_give_it_a_screen(has, resizes):
    import json
    import subprocess

    from flotta.backends.fly_backend import FlyBackend
    from flotta.test_backend import _offline_config

    issued: list[list[str]] = []
    listing = json.dumps(
        [
            {
                "id": "m1",
                "state": "started",
                "config": {"guest": {"cpu_kind": "shared", "memory_mb": has}},
            }
        ]
    )

    def runner(cmd, *, timeout, check, stdin=None):
        issued.append(cmd)
        listed = cmd[1:3] in (["machine", "list"], ["machines", "list"])
        return subprocess.CompletedProcess(cmd, 0, listing if listed else "", "")

    now = FlyBackend(config=_offline_config(), runner=runner).resize(
        "fly://joaoh82-flotta-eng-s/m1", 4096, env={"FLOTTA_SCREEN": "1"}, at_least=True
    )
    (update,) = [c for c in issued if c[1:3] == ["machine", "update"]]
    assert ("--vm-memory" in update) is resizes
    assert update[update.index("--env") + 1] == "FLOTTA_SCREEN=1"
    assert now == max(has, 4096)
