"""Machine time and storage in `usage`, written by the sweep (FLOTTA-92, M13.6).

The guards come first. **Two sweeps over the same hour count it once, and a restart
of the control plane neither loses nor doubles it.** And a log that missed a suspend
doesn't bill for ever: what Fly reports caps it, and the gap is written down.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from flotta import metering, provision
from flotta.backend import BackendError, MachineInfo
from flotta.budgets import standing
from flotta.metering import meter_machines
from flotta.pricing import NANO
from flotta.provision import enforce_budgets
from flotta.store import FleetStore
from flotta.test_migrations import target  # noqa: F401
from flotta.test_provision import FakeBackend

#: A whole hour, so a window starts there.
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
ENV = {"FLOTTA_EUR_PER_USD": "0.5"}
#: $6.70 a month for 1 GB in `iad`, over 30 days. Quoted to the nano in a test.
AN_HOUR_IAD = 9_305_556
#: $0.15 a GB a month, for 1 GB.
A_GB_HOUR = 208_333


class Machine(FakeBackend):
    """What Fly says about the machine, down to its size."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.size = {"cpu_kind": "shared", "cpus": 1, "memory_mb": 1024}
        self.volume_gb = 1
        self.region = "iad"
        self.last_down_at = None
        self.last_up_at = None
        self.inspected = 0
        self.unreadable = False

    def inspect(self, box_id):
        self.inspected += 1
        if self.unreadable:
            raise BackendError("flyctl is logged out")
        return MachineInfo(
            state=self.machine_state,
            region=self.region,
            volume_gb=self.volume_gb,
            last_down_at=self.last_down_at,
            last_up_at=self.last_up_at,
            **self.size,
        )


@pytest.fixture
def fleet(target):  # noqa: F811
    with FleetStore(target) as store:
        store.create_org("studio-nord", "Studio Nord")
        store.create_client("studio-nord", "acme", "Acme Dental")
        box = store.create_box("one", box_id="b-one", org="studio-nord", client="acme")
        store.update_box_status(box.id, "running", endpoint="fake://one")
        yield store


@pytest.fixture
def machine(monkeypatch):
    fake = Machine()
    monkeypatch.setattr(metering, "_resolve_backend", lambda box, backend=None: backend or fake)
    return fake


def _at(fleet, kind, moment, box="b-one", **payload):
    """A lifecycle event as it would be written at `moment`."""
    fleet.add_event("box", box, kind, payload)
    last = fleet.get_events("box", box)[-1]
    fleet._conn.execute("UPDATE events SET ts = ? WHERE id = ?", (moment.isoformat(), last.id))


def _sweep(fleet, now, env=ENV):
    return meter_machines(fleet, now=now, env=env)


def _rows(fleet, prefix=""):
    rows = fleet._conn.execute(
        "SELECT * FROM usage WHERE kind = 'machine' ORDER BY at, model"
    ).fetchall()
    return [dict(r) for r in rows if str(r["model"]).startswith(prefix)]


def _machine_rows(fleet):
    return _rows(fleet, "machine:")


def _volume_rows(fleet):
    return _rows(fleet, "volume:")


def _started(fleet, machine, now=T0):
    """The first sweep marks where counting starts, and running from there."""
    _sweep(fleet, now)
    _at(fleet, "running", now)


# -- the guards --------------------------------------------------------------


def test_two_sweeps_over_the_same_hour_count_it_once(fleet, machine):
    _started(fleet, machine)
    _sweep(fleet, T0 + HOUR + timedelta(seconds=1))
    _sweep(fleet, T0 + HOUR + timedelta(seconds=61))
    assert [r["seconds"] for r in _machine_rows(fleet)] == [3600]


def test_a_mark_that_was_lost_still_counts_an_hour_once(fleet, machine):
    """A crash between writing the rows and moving the mark runs the same hour again."""
    _started(fleet, machine)
    _sweep(fleet, T0 + HOUR)
    fleet._conn.execute("UPDATE machine_meter_marks SET metered_to = ?", (T0.isoformat(),))
    _sweep(fleet, T0 + HOUR)
    assert [r["seconds"] for r in _machine_rows(fleet)] == [3600]
    assert [r["seconds"] for r in _volume_rows(fleet)] == [3600]


def test_a_restart_mid_hour_neither_loses_nor_doubles_it(fleet, machine, target):  # noqa: F811
    _started(fleet, machine)
    _sweep(fleet, T0 + timedelta(minutes=30))
    with FleetStore(target) as after_restart:
        _sweep(after_restart, T0 + HOUR + timedelta(seconds=1))
    assert [r["seconds"] for r in _machine_rows(fleet)] == [3600]


def test_a_control_plane_down_for_hours_catches_up(fleet, machine):
    _started(fleet, machine)
    _sweep(fleet, T0 + 3 * HOUR + timedelta(minutes=5))
    assert [r["seconds"] for r in _machine_rows(fleet)] == [3600, 3600, 3600]
    assert machine.inspected == 1 + 1


def test_a_missing_suspend_does_not_bill_for_ever(fleet, machine):
    _started(fleet, machine)
    machine.machine_state = "suspended"
    machine.last_down_at = (T0 + timedelta(minutes=10)).isoformat()
    _sweep(fleet, T0 + 5 * HOUR)
    assert [r["seconds"] for r in _machine_rows(fleet)] == [600]


def test_the_gap_is_written_down(fleet, machine):
    _started(fleet, machine)
    machine.machine_state = "stopped"
    machine.last_down_at = (T0 + timedelta(minutes=10)).isoformat()
    _sweep(fleet, T0 + HOUR)
    events = {e.type: e.payload for e in fleet.get_events("box", "b-one")}
    gap = events["machine_down_observed"]
    assert gap["machine_down_at"] == machine.last_down_at
    assert gap["gap_s"] == 3000
    _sweep(fleet, T0 + 2 * HOUR)
    types = [e.type for e in fleet.get_events("box", "b-one")]
    assert types.count("machine_down_observed") == 1


def test_a_machine_reported_down_with_no_time_stops_counting_now(fleet, machine):
    _started(fleet, machine)
    machine.machine_state = "stopped"
    _sweep(fleet, T0 + HOUR)
    assert [r["seconds"] for r in _machine_rows(fleet)] == [3600]
    _sweep(fleet, T0 + 2 * HOUR)
    assert [r["seconds"] for r in _machine_rows(fleet)] == [3600]


def test_fly_being_unreadable_closes_nothing_and_loses_nothing(fleet, machine):
    _started(fleet, machine)
    machine.unreadable = True
    outcome = _sweep(fleet, T0 + 2 * HOUR)
    assert _rows(fleet) == []
    assert any(entry.get("error") for entry in outcome)
    machine.unreadable = False
    _sweep(fleet, T0 + 2 * HOUR)
    assert [r["seconds"] for r in _machine_rows(fleet)] == [3600, 3600]


def test_a_machine_awake_with_no_event_is_noticed_and_counted(fleet, machine):
    """Removing a Fly secret seems to start a stopped machine, and nothing logs it."""
    _sweep(fleet, T0)
    _at(fleet, "stopped", T0)
    fleet.update_box_status("b-one", "stopped")
    machine.machine_state = "started"
    machine.last_up_at = (T0 + timedelta(minutes=45)).isoformat()
    _sweep(fleet, T0 + HOUR)
    assert [r["seconds"] for r in _machine_rows(fleet)] == [900]
    assert "machine_up_observed" in [e.type for e in fleet.get_events("box", "b-one")]


def test_a_stopped_row_with_a_machine_up_becomes_running_so_it_can_sleep(fleet, machine):
    """The idle sweep only looks at `running`, so this machine would bill for ever."""
    _sweep(fleet, T0)
    _at(fleet, "stopped", T0)
    fleet.update_box_status("b-one", "stopped")
    machine.machine_state = "started"
    _sweep(fleet, T0 + HOUR)
    assert fleet.get_box("b-one").status == "running"


def test_a_paused_box_with_a_machine_up_stays_paused(fleet, machine):
    _sweep(fleet, T0)
    _at(fleet, "stopped", T0)
    fleet.update_box_status("b-one", "paused")
    machine.machine_state = "started"
    machine.last_up_at = T0.isoformat()
    _sweep(fleet, T0 + HOUR)
    assert fleet.get_box("b-one").status == "paused"
    assert [r["seconds"] for r in _machine_rows(fleet)] == [3600]


# -- what counts as running --------------------------------------------------


def test_nothing_before_the_first_sweep_is_billed(fleet, machine):
    _at(fleet, "running", T0 - 100 * HOUR)
    _sweep(fleet, T0 + timedelta(minutes=5))
    assert _rows(fleet) == []


def test_an_hour_still_open_is_not_closed(fleet, machine):
    _started(fleet, machine)
    _sweep(fleet, T0 + timedelta(minutes=59))
    assert _rows(fleet) == []


def test_only_the_time_it_ran_is_counted(fleet, machine):
    _sweep(fleet, T0)
    _at(fleet, "running", T0 + timedelta(minutes=10))
    _at(fleet, "stopped", T0 + timedelta(minutes=25), method="suspend")
    _at(fleet, "running", T0 + timedelta(minutes=40))
    _at(fleet, "stopped", T0 + timedelta(minutes=50), method="suspend")
    machine.machine_state = "suspended"
    _sweep(fleet, T0 + HOUR)
    assert [r["seconds"] for r in _machine_rows(fleet)] == [25 * 60]


def test_a_run_across_hours_is_split_between_them(fleet, machine):
    _sweep(fleet, T0)
    _at(fleet, "running", T0 + timedelta(minutes=50))
    _sweep(fleet, T0 + 2 * HOUR)
    assert [r["seconds"] for r in _machine_rows(fleet)] == [600, 3600]
    assert _machine_rows(fleet)[0]["at"] == T0.isoformat()


def test_a_run_begun_before_counting_started_counts_from_there(fleet, machine):
    _at(fleet, "running", T0 - 5 * HOUR)
    _sweep(fleet, T0)
    _sweep(fleet, T0 + HOUR)
    assert [r["seconds"] for r in _machine_rows(fleet)] == [3600]


@pytest.mark.parametrize("kind", ["stopped", "budget_killed", "budget_machine_slept", "torn_down"])
def test_each_way_down_stops_the_count(fleet, machine, kind):
    _started(fleet, machine)
    _at(fleet, kind, T0 + timedelta(minutes=30))
    machine.machine_state = "stopped"
    _sweep(fleet, T0 + 2 * HOUR)
    assert [r["seconds"] for r in _machine_rows(fleet)] == [1800]


def test_an_asleep_machine_costs_no_machine_time(fleet, machine):
    _sweep(fleet, T0)
    _at(fleet, "stopped", T0)
    fleet.update_box_status("b-one", "stopped")
    machine.machine_state = "suspended"
    _sweep(fleet, T0 + HOUR)
    assert _machine_rows(fleet) == []
    assert len(_volume_rows(fleet)) == 1


def test_a_box_being_created_is_not_counted_yet(fleet, machine):
    fleet.create_box("new", box_id="b-new", org="studio-nord", client="acme")
    _sweep(fleet, T0)
    _sweep(fleet, T0 + HOUR)
    assert [r for r in _rows(fleet) if r["box_id"] == "b-new"] == []


# -- what it costs -----------------------------------------------------------


def test_machine_time_is_priced_by_size_and_stored(fleet, machine):
    _started(fleet, machine)
    _sweep(fleet, T0 + HOUR)
    row = _machine_rows(fleet)[0]
    assert row["model"] == "machine:shared-1x-1024"
    assert row["upstream_cost_nano"] == AN_HOUR_IAD
    assert row["currency"] == "USD"
    assert row["eur_per_usd"] == "0.5"
    assert row["cost_eur_nano"] == AN_HOUR_IAD // 2
    assert row["org_id"] == "studio-nord"
    assert row["client_id"] == "acme"
    assert row["kind"] == "machine"


@pytest.mark.parametrize(("memory", "monthly"), [(256, "2.19"), (512, "3.69"), (1024, "6.70")])
def test_each_published_size_has_its_price(fleet, machine, memory, monthly):
    machine.size = {"cpu_kind": "shared", "cpus": 1, "memory_mb": memory}
    _started(fleet, machine)
    _sweep(fleet, T0 + HOUR)
    expected = round(Decimal(monthly) * NANO * 3600 / 2_592_000)
    assert _machine_rows(fleet)[0]["upstream_cost_nano"] == expected


def test_a_machine_costs_what_its_region_costs(fleet, machine):
    machine.region = "ams"
    _started(fleet, machine)
    _sweep(fleet, T0 + HOUR)
    assert _machine_rows(fleet)[0]["upstream_cost_nano"] == 9_663_462


def test_an_unknown_region_is_priced_at_the_base(fleet, machine):
    machine.region = "xyz"
    _started(fleet, machine)
    _sweep(fleet, T0 + HOUR)
    assert _machine_rows(fleet)[0]["upstream_cost_nano"] == AN_HOUR_IAD


def test_a_setting_overrides_the_published_price(fleet, machine):
    _started(fleet, machine)
    env = {**ENV, "FLOTTA_MACHINE_PRICES": "shared-1x-1024=25.92, shared-1x-512=1"}
    _sweep(fleet, T0 + HOUR, env=env)
    assert _machine_rows(fleet)[0]["upstream_cost_nano"] == 36_000_000


def test_the_sweep_reads_the_settings_stored_on_the_fleet_page(fleet, machine):
    """What the loop does, with no `env` of its own: otherwise the field decides nothing."""
    fleet.set_setting("FLOTTA_EUR_PER_USD", "0.5")
    fleet.set_setting("FLOTTA_MACHINE_PRICES", "shared-1x-1024=25.92")
    fleet.set_setting("FLOTTA_VOLUME_USD_GB_MONTH", "0.72")
    meter_machines(fleet, now=T0)
    _at(fleet, "running", T0)
    meter_machines(fleet, now=T0 + HOUR)
    assert _machine_rows(fleet)[0]["upstream_cost_nano"] == 36_000_000
    assert _volume_rows(fleet)[0]["upstream_cost_nano"] == 1_000_000


def test_a_setting_that_makes_no_sense_is_ignored(fleet, machine):
    _started(fleet, machine)
    env = {**ENV, "FLOTTA_MACHINE_PRICES": "shared-1x-1024=soon"}
    _sweep(fleet, T0 + HOUR, env=env)
    assert _machine_rows(fleet)[0]["upstream_cost_nano"] == AN_HOUR_IAD


def test_a_size_with_no_price_is_kept_and_counted_as_unpriced(fleet, machine):
    machine.size = {"cpu_kind": "performance", "cpus": 8, "memory_mb": 16384}
    _started(fleet, machine)
    _sweep(fleet, T0 + HOUR)
    row = _machine_rows(fleet)[0]
    assert row["model"] == "machine:performance-8x-16384"
    assert row["cost_eur_nano"] is None
    assert row["seconds"] == 3600


def test_no_exchange_rate_counts_as_unpriced(fleet, machine):
    _started(fleet, machine)
    _sweep(fleet, T0 + HOUR, env={})
    row = _machine_rows(fleet)[0]
    assert row["upstream_cost_nano"] == AN_HOUR_IAD
    assert row["cost_eur_nano"] is None


def test_a_stored_cost_does_not_change_with_the_price(fleet, machine):
    _started(fleet, machine)
    _sweep(fleet, T0 + HOUR)
    env = {**ENV, "FLOTTA_MACHINE_PRICES": "shared-1x-1024=999"}
    _sweep(fleet, T0 + 2 * HOUR, env=env)
    first, second = _machine_rows(fleet)
    assert first["upstream_cost_nano"] == AN_HOUR_IAD
    assert second["upstream_cost_nano"] > AN_HOUR_IAD


def test_a_local_stand_in_costs_nothing(fleet, machine):
    machine.size = {"cpu_kind": None, "cpus": None, "memory_mb": None}
    machine.region = "local"
    machine.volume_gb = None
    _started(fleet, machine)
    _sweep(fleet, T0 + HOUR)
    row = _machine_rows(fleet)[0]
    assert row["model"] == "machine:local"
    assert (row["upstream_cost_nano"], row["cost_eur_nano"]) == (0, 0)
    assert _volume_rows(fleet) == []


# -- storage -----------------------------------------------------------------


def test_storage_is_its_own_line_awake_or_asleep(fleet, machine):
    machine.volume_gb = 2
    _sweep(fleet, T0)
    _at(fleet, "stopped", T0)
    machine.machine_state = "suspended"
    _sweep(fleet, T0 + HOUR)
    row = _volume_rows(fleet)[0]
    assert row["model"] == "volume:2gb"
    assert row["seconds"] == 3600
    assert row["upstream_cost_nano"] == 416_667
    assert row["currency"] == "USD"


def test_a_setting_overrides_the_price_of_storage(fleet, machine):
    _sweep(fleet, T0)
    env = {**ENV, "FLOTTA_VOLUME_USD_GB_MONTH": "0.72"}
    _sweep(fleet, T0 + HOUR, env=env)
    assert _volume_rows(fleet)[0]["upstream_cost_nano"] == 1_000_000


def test_a_destroyed_box_stops_costing_storage_when_it_was_destroyed(fleet, machine):
    _started(fleet, machine)
    _sweep(fleet, T0 + HOUR)
    _at(fleet, "torn_down", T0 + HOUR + timedelta(minutes=30))
    fleet.update_box_status("b-one", "torn_down")
    fleet._conn.execute(
        "UPDATE boxes SET destroyed_at = ? WHERE id = 'b-one'",
        ((T0 + HOUR + timedelta(minutes=30)).isoformat(),),
    )
    machine.unreadable = True
    _sweep(fleet, T0 + 4 * HOUR)
    assert [r["seconds"] for r in _volume_rows(fleet)] == [3600, 1800]
    assert [r["seconds"] for r in _machine_rows(fleet)] == [3600, 1800]
    # And it is finished with: nothing more is read or written for it.
    assert fleet.machine_mark("b-one") is None
    before = len(_rows(fleet))
    _sweep(fleet, T0 + 9 * HOUR)
    assert len(_rows(fleet)) == before


def test_a_machine_gone_keeps_what_it_was_known_to_be(fleet, machine):
    _started(fleet, machine)
    machine.size = {"cpu_kind": None, "cpus": None, "memory_mb": None}
    machine.volume_gb = None
    machine.machine_state = "gone"
    _sweep(fleet, T0 + HOUR)
    _sweep(fleet, T0 + 2 * HOUR)
    assert fleet.machine_mark("b-one").machine_label == "machine:shared-1x-1024"
    assert [r["model"] for r in _volume_rows(fleet)] == ["volume:1gb", "volume:1gb"]


def test_a_box_never_metered_and_destroyed_is_left_alone(fleet, machine):
    fleet.update_box_status("b-one", "torn_down")
    machine.unreadable = True
    assert _sweep(fleet, T0 + HOUR) == []
    assert _rows(fleet) == []


# -- reading Fly as seldom as it can -----------------------------------------


def test_fly_is_read_once_an_hour_per_box(fleet, machine):
    _started(fleet, machine)
    assert machine.inspected == 1
    _sweep(fleet, T0 + timedelta(minutes=5))
    _sweep(fleet, T0 + timedelta(minutes=30))
    assert machine.inspected == 1
    _sweep(fleet, T0 + HOUR)
    _sweep(fleet, T0 + HOUR + timedelta(minutes=1))
    assert machine.inspected == 2


# -- budgets -----------------------------------------------------------------


def test_machine_spend_counts_in_the_sum_a_budget_reads(fleet, machine):
    budget = fleet.set_budget(
        "studio-nord", client="acme", period="day", limit_eur_nano=NANO, action="kill"
    )
    _started(fleet, machine)
    _sweep(fleet, T0 + HOUR)
    spent = standing(fleet, budget, T0 + HOUR).spent_nano
    assert spent == 4_652_778 + 104_167


def test_a_budget_reached_on_machine_time_alone_acts(fleet, machine, monkeypatch):
    monkeypatch.setattr(provision, "_resolve_backend", lambda box, backend=None: machine)
    fleet.set_budget(
        "studio-nord", client="acme", period="day", limit_eur_nano=1_000_000, action="kill"
    )
    _started(fleet, machine)
    _sweep(fleet, T0 + HOUR)
    enforce_budgets(fleet, now=T0 + HOUR, idle_after_s=10**9)
    assert fleet.get_box("b-one").status == "paused"
    assert "stop" in machine.calls


def test_an_hour_counts_on_the_day_it_ran(fleet, machine):
    midnight = T0.replace(hour=23)
    _sweep(fleet, midnight)
    _at(fleet, "running", midnight)
    _sweep(fleet, midnight + HOUR + timedelta(seconds=1))
    next_day = midnight + HOUR
    assert fleet.spend(org="studio-nord", since=next_day).eur_nano == 0


def test_the_loop_meters_and_survives_it_failing(fleet, target):  # noqa: F811
    import asyncio

    from flotta.control.loop import LoopState, run_reconcile_loop

    calls = []

    def meter(store):
        calls.append(store)
        raise RuntimeError("nope")

    state = LoopState(interval_s=0.0)
    asyncio.run(
        run_reconcile_loop(
            state,
            store_factory=lambda: FleetStore(target),
            reconcile=lambda store: [],
            sleeper=None,
            box_sweeper=None,
            budget_enforcer=lambda store: [],
            machine_meter=meter,
            max_sweeps=1,
        )
    )
    assert len(calls) == 1
    assert state.last_error is None


def test_a_row_is_keyed_by_its_hour(fleet, machine):
    _started(fleet, machine)
    _sweep(fleet, T0 + HOUR)
    keys = {r["idempotency_key"] for r in _rows(fleet)}
    assert keys == {f"machine:{T0.isoformat()}", f"volume:{T0.isoformat()}"}


def test_the_box_sweep_keeps_the_time_fly_says_it_went_down(fleet, machine):
    """A control plane that was down corrects the row late, and shouldn't bill for that."""
    _started(fleet, machine)
    machine.machine_state = "suspended"
    machine.last_down_at = (T0 + timedelta(minutes=10)).isoformat()
    provision.reconcile_boxes(fleet, backend_for=lambda endpoint: machine)
    _sweep(fleet, T0 + 3 * HOUR)
    assert [r["seconds"] for r in _machine_rows(fleet)] == [600]
