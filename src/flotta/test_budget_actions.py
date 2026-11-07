"""What a budget does when it is reached (FLOTTA-91, M13.5).

The guards come first. **A paused box is not woken by anything**: not the door,
the wake route, the operator's start, a write to its disk or the sweeps. And a
downgrade never forwards the model it replaced.
"""

from __future__ import annotations

import dataclasses
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from flotta import provision
from flotta.auth import (
    SCOPE_BOX_CHAT,
    SCOPE_FLEET_READ,
    SCOPE_FLEET_ROUTE,
    SCOPE_FLEET_WRITE,
    SCOPE_MODEL_CALL,
    SCOPE_ORG_ADMIN,
    box_subject,
    mint,
    scopes_for_role,
)
from flotta.backend import BackendError
from flotta.control.app import create_app
from flotta.control.loop import LoopState, run_reconcile_loop
from flotta.door.app import ControlPlane, create_door
from flotta.door.test_app import stub_box  # noqa: F401 - a fixture
from flotta.gateway.app import create_gateway
from flotta.gateway.fake import FAKE_MODEL, FAKE_PRICE, FAKE_UPSTREAM_URL, FakeUpstream
from flotta.pricing import NANO
from flotta.provision import (
    BoxPaused,
    BudgetHolds,
    enforce_budgets,
    movable_box,
    reconcile,
    reconcile_boxes,
    set_instructions,
    sleep_idle_boxes,
    start_box,
    unpause_box,
    wake_box,
)
from flotta.store import FleetStore, InvalidBudgetError, InvalidTransitionError
from flotta.test_provision import FakeBackend

KEY = "a-signing-key-for-these-tests"
RATE = Decimal("0.5")
CHEAP = "fake/cheap"
DOMAIN = "flotta.test"
GATEWAY_ENV = {
    "FLOTTA_SIGNING_KEY": KEY,
    "FLOTTA_API_KEY": "fleet-provider-key",
    "FLOTTA_MODEL_BASE_URL": FAKE_UPSTREAM_URL,
    "FLOTTA_EUR_PER_USD": "0.5",
    "FLOTTA_MODELS": f"{FAKE_MODEL},{CHEAP}",
}
NOON = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
EARLIER = NOON - timedelta(days=30)
CALL = {"model": FAKE_MODEL, "messages": [{"role": "user", "content": "hello"}]}


@pytest.fixture
def path(tmp_path):
    db = str(tmp_path / "fleet.db")
    with FleetStore(db) as fleet:
        fleet.create_org("studio-nord", "Studio Nord")
        fleet.create_client("studio-nord", "acme", "Acme Dental")
        fleet.create_client("studio-nord", "bravo", "Bravo Legal")
        for name, client in (("one", "acme"), ("two", "acme"), ("three", "bravo")):
            box = fleet.create_box(name, box_id=f"b-{name}", org="studio-nord", client=client)
            fleet.update_box_status(box.id, "running", endpoint=f"fake://{name}")
        cheap = dataclasses.replace(FAKE_PRICE, model=CHEAP)
        fleet.refresh_prices([FAKE_PRICE, cheap], now=EARLIER)
    return db


@pytest.fixture
def fleet(path):
    with FleetStore(path) as store:
        yield store


@pytest.fixture
def machine(monkeypatch):
    """Every box's machine, and every verb anything asks of it."""
    fake = FakeBackend()
    monkeypatch.setattr(provision, "_resolve_backend", lambda box, backend=None: backend or fake)
    return fake


def _budget(fleet, *, action="pause", nano=NANO, client="acme", box=None, **kwargs):
    return fleet.set_budget(
        "studio-nord",
        client=client,
        box=box,
        period="day",
        limit_eur_nano=nano,
        action=action,
        **kwargs,
    )


def _spend(fleet, nano=NANO, *, box="b-one", key="k1", now=NOON):
    """`nano` euros spent by `box` at `now`, at a rate of a half."""
    return fleet.record_usage(
        box,
        model=FAKE_MODEL,
        upstream="openrouter",
        tokens_in=1,
        tokens_out=1,
        idempotency_key=key,
        eur_per_usd=RATE,
        reported_cost_nano=nano * 2,
        now=now,
    )


def _status(fleet, box="b-one"):
    return fleet.get_box(box).status


def _events(fleet, box="b-one"):
    return [(e.type, e.payload) for e in fleet.get_events("box", box)]


def _types(fleet, box="b-one"):
    return [kind for kind, _ in _events(fleet, box)]


def _sweep(fleet, now=NOON, idle_after_s=10**9):
    """A sweep at `now`. Idleness is measured against real timestamps, so it stays
    out of a test unless the test is about it."""
    return enforce_budgets(fleet, now=now, idle_after_s=idle_after_s)


# -- the state machine -------------------------------------------------------


def test_a_paused_box_cannot_go_straight_to_running(fleet):
    fleet.update_box_status("b-one", "paused")
    with pytest.raises(InvalidTransitionError):
        fleet.update_box_status("b-one", "running")


def test_a_paused_box_leaves_by_becoming_stopped(fleet):
    fleet.update_box_status("b-one", "paused")
    assert fleet.update_box_status("b-one", "stopped").status == "stopped"


def test_a_paused_box_can_still_be_destroyed(fleet):
    fleet.update_box_status("b-one", "paused")
    assert fleet.update_box_status("b-one", "torn_down").status == "torn_down"


def test_a_sleeping_box_can_be_paused(fleet):
    fleet.update_box_status("b-one", "stopped")
    assert fleet.update_box_status("b-one", "paused").status == "paused"


# -- nothing wakes a paused box ----------------------------------------------


@pytest.fixture
def paused(fleet, machine):
    machine.machine_state = "stopped"
    fleet.update_box_status("b-one", "paused")
    return fleet


def test_the_addressing_path_does_not_wake_a_paused_box(paused, machine):
    with pytest.raises(BoxPaused):
        wake_box("b-one", store=paused)
    assert machine.calls == []
    assert _status(paused) == "paused"


def test_the_operators_start_does_not_wake_a_paused_box(paused, machine):
    with pytest.raises(provision.ProvisionError):
        start_box("b-one", store=paused)
    assert machine.calls == []


def test_writing_to_its_disk_does_not_wake_a_paused_box(paused, machine):
    """`exec` starts a sleeping machine by itself."""
    with pytest.raises(BoxPaused):
        set_instructions("b-one", "be brief", store=paused)
    assert machine.commands == []


def test_moving_its_provider_does_not_wake_a_paused_box(paused):
    """Removing a Fly secret seems to start a stopped machine (FLOTTA-112)."""
    with pytest.raises(BoxPaused):
        movable_box("b-one", paused)


def test_the_idle_sweep_leaves_a_paused_box_alone(paused, machine):
    later = NOON + timedelta(days=1)
    assert sleep_idle_boxes(paused, now=later, idle_after_s=1) == []
    assert _status(paused) == "paused"


def test_the_box_sweep_does_not_correct_a_paused_box(paused, machine):
    assert reconcile_boxes(paused) == []
    assert _status(paused) == "paused"
    assert "start" not in machine.calls


def test_the_task_sweep_does_not_touch_a_paused_box(paused, machine):
    reconcile(paused)
    assert _status(paused) == "paused"
    assert machine.calls == []


def test_another_agent_cannot_find_a_paused_one_to_ask(fleet):
    fleet.update_box_status("b-two", "paused")
    assert "b-two" not in fleet.peers_for_box("b-one")


# -- the wake route and the door ---------------------------------------------


def _bearer(value):
    return {"Authorization": f"Bearer {value}"}


OPERATOR = _bearer(
    mint(
        subject="operator",
        scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN, SCOPE_BOX_CHAT},
        key=KEY,
    )
)
ROUTE = _bearer(mint(subject="door", scopes={SCOPE_FLEET_ROUTE}, key=KEY))


def _as(role, *, client=None, org="studio-nord"):
    return _bearer(
        mint(
            subject="person",
            scopes=scopes_for_role(role, client=client),
            key=KEY,
            org=org,
            client=client,
        )
    )


@pytest.fixture
def control(path):
    return create_app(
        store_factory=lambda: FleetStore(path), run_loop=False, background=False, signing_key=KEY
    )


@pytest.fixture
def api(control):
    with TestClient(control) as client:
        yield client


def test_the_wake_route_answers_402_and_says_which_budget(paused, machine, api):
    budget = _budget(paused)
    _spend(paused)
    reply = api.post("/api/boxes/b-one/wake", headers=ROUTE)
    assert reply.status_code == 402
    assert "paused by budget" in reply.json()["detail"]
    assert budget.period in reply.json()["detail"]
    assert machine.calls == []


def test_a_refused_wake_is_not_activity(paused, machine, api):
    """Or a tab left open would hold a paused machine awake for ever."""
    before = _types(paused)
    api.post("/api/boxes/b-one/wake", headers=ROUTE)
    assert _types(paused) == before


@pytest.fixture
def door(control, stub_box):  # noqa: F811 - the fixture above
    port, seen = stub_box

    class InProcess(ControlPlane):
        async def wake(self, name, *, box_id=None):
            target = await super().wake(name, box_id=box_id)
            return dataclasses.replace(target, host="127.0.0.1", port=port)

    plane = InProcess(
        base_url="http://control",
        token=mint(subject="door", scopes={SCOPE_FLEET_ROUTE}, key=KEY),
        transport=httpx.ASGITransport(app=control),
    )
    app = create_door(
        control=plane, signing_key=KEY, domain=DOMAIN, box_credentials=("flotta", "pw")
    )
    with TestClient(app) as client:
        yield client, seen


def _door_token():
    return _bearer(
        mint(subject="person", scopes={SCOPE_BOX_CHAT}, key=KEY, org="studio-nord", client="acme")
    )


def test_the_door_says_paused_by_budget_and_never_reaches_the_box(paused, machine, door):
    front, seen = door
    reply = front.get(
        "/api/status", headers={**_door_token(), "host": f"one--acme--studio-nord.{DOMAIN}"}
    )
    assert reply.status_code == 402
    assert "paused by budget" in reply.json()["detail"]
    assert machine.calls == []
    assert seen == []


def test_the_door_only_tells_someone_who_may_talk_to_the_agent(paused, machine, door):
    front, _ = door
    someone_else = _bearer(
        mint(subject="person", scopes={SCOPE_BOX_CHAT}, key=KEY, org="other", client="acme")
    )
    reply = front.get(
        "/api/status", headers={**someone_else, "host": f"one--acme--studio-nord.{DOMAIN}"}
    )
    assert reply.status_code in (403, 404)
    assert "paused" not in reply.text


# -- notify ------------------------------------------------------------------


def _gateway(path, upstream, env=GATEWAY_ENV):
    return TestClient(
        create_gateway(store_factory=lambda: FleetStore(path), transport=upstream, env=env)
    )


def _box_auth(box="b-one", client="acme"):
    return _bearer(
        mint(
            subject=box_subject(box),
            scopes={SCOPE_MODEL_CALL},
            key=KEY,
            org="studio-nord",
            client=client,
        )
    )


def test_notify_tells_at_the_soft_limit_once(fleet, machine):
    _budget(fleet, action="notify")
    _spend(fleet, 8 * NANO // 10)
    _sweep(fleet)
    _sweep(fleet)
    assert _types(fleet).count("budget_soft_limit") == 1
    assert "budget_limit" not in _types(fleet)


def test_notify_is_silent_under_the_soft_limit(fleet, machine):
    _budget(fleet, action="notify")
    _spend(fleet, 7 * NANO // 10)
    _sweep(fleet)
    assert fleet.budget_events("studio-nord") == []
    assert "budget_soft_limit" not in _types(fleet)


def test_notify_tells_at_the_limit_once_and_does_nothing_else(fleet, machine):
    _budget(fleet, action="notify")
    _spend(fleet)
    _sweep(fleet)
    _sweep(fleet)
    assert _types(fleet).count("budget_limit") == 1
    assert _status(fleet) == "running"
    assert machine.calls == []


def test_notify_tells_again_the_next_period(fleet, machine):
    _budget(fleet, action="notify")
    _spend(fleet)
    _sweep(fleet)
    tomorrow = NOON + timedelta(days=1)
    _spend(fleet, key="k2", now=tomorrow)
    _sweep(fleet, now=tomorrow)
    assert _types(fleet).count("budget_limit") == 2


def test_notify_lets_calls_through_past_the_limit(path, fleet):
    _budget(fleet, action="notify")
    _spend(fleet, 2 * NANO, now=datetime.now(UTC))
    upstream = FakeUpstream()
    with _gateway(path, upstream) as gateway:
        reply = gateway.post("/v1/chat/completions", json=CALL, headers=_box_auth())
    assert reply.status_code == 200
    assert len(upstream.requests) == 1


def test_every_threshold_lands_on_every_agent_covered_and_the_client(fleet, machine):
    budget = _budget(fleet, action="notify")
    _spend(fleet)
    _sweep(fleet)
    for box in ("b-one", "b-two"):
        assert {"budget_soft_limit", "budget_limit"} <= set(_types(fleet, box))
    assert "budget_limit" not in _types(fleet, "b-three")
    recorded = fleet.budget_events("studio-nord", client="acme")
    assert {e.type for e in recorded} == {"budget_soft_limit", "budget_limit"}
    assert all(e.budget == budget.id for e in recorded)
    assert fleet.budget_events("studio-nord", client="bravo") == []


def test_an_org_wide_budgets_events_are_on_every_clients_timeline(fleet, machine):
    _budget(fleet, action="notify", client=None)
    _spend(fleet)
    _sweep(fleet)
    for client in ("acme", "bravo"):
        assert "budget_limit" in {e.type for e in fleet.budget_events("studio-nord", client=client)}


def test_an_event_says_which_budget_and_how_much(fleet, machine):
    budget = _budget(fleet, action="notify")
    _spend(fleet)
    _sweep(fleet)
    payload = dict(_events(fleet))["budget_limit"]
    assert payload["budget"]["id"] == budget.id
    assert payload["budget"]["spent_eur"] == "1"
    assert payload["budget"]["action"] == "notify"


# -- downgrade ---------------------------------------------------------------


@pytest.fixture
def downgraded(path, fleet):
    _budget(fleet, action="downgrade", fallback_model=CHEAP)
    _spend(fleet, now=datetime.now(UTC))
    return path


def _forwarded(upstream):
    import json

    return [json.loads(r.content)["model"] for r in upstream.requests]


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "stream"])
def test_a_downgrade_never_forwards_the_original_model(downgraded, stream):
    upstream = FakeUpstream()
    with _gateway(downgraded, upstream) as gateway:
        reply = gateway.post(
            "/v1/chat/completions", json={**CALL, "stream": stream}, headers=_box_auth()
        )
        assert reply.status_code == 200
    assert _forwarded(upstream) == [CHEAP]


def test_a_downgrade_records_both_models(downgraded):
    with _gateway(downgraded, FakeUpstream()) as gateway:
        gateway.post("/v1/chat/completions", json=CALL, headers=_box_auth())
    with FleetStore(downgraded) as store:
        row = dict(store._conn.execute("SELECT * FROM usage ORDER BY at DESC LIMIT 1").fetchone())
    assert row["model"] == CHEAP
    assert row["requested_model"] == FAKE_MODEL


def test_under_the_limit_the_model_is_left_alone(path, fleet):
    _budget(fleet, action="downgrade", fallback_model=CHEAP)
    upstream = FakeUpstream()
    with _gateway(path, upstream) as gateway:
        gateway.post("/v1/chat/completions", json=CALL, headers=_box_auth())
    assert _forwarded(upstream) == [FAKE_MODEL]
    with FleetStore(path) as store:
        row = store._conn.execute("SELECT * FROM usage").fetchone()
    assert row["requested_model"] is None


def test_a_call_already_on_the_fallback_is_not_called_a_downgrade(downgraded):
    upstream = FakeUpstream()
    with _gateway(downgraded, upstream) as gateway:
        gateway.post("/v1/chat/completions", json={**CALL, "model": CHEAP}, headers=_box_auth())
    assert _forwarded(upstream) == [CHEAP]
    with FleetStore(downgraded) as store:
        row = store._conn.execute("SELECT * FROM usage ORDER BY at DESC LIMIT 1").fetchone()
    assert row["requested_model"] is None


def test_a_fallback_that_lost_its_price_refuses_rather_than_forwards(path, fleet):
    _budget(fleet, action="downgrade", fallback_model=CHEAP)
    _spend(fleet, now=datetime.now(UTC))
    fleet._conn.execute("DELETE FROM model_prices WHERE model = ?", (CHEAP,))
    upstream = FakeUpstream()
    with _gateway(path, upstream) as gateway:
        reply = gateway.post("/v1/chat/completions", json=CALL, headers=_box_auth())
    assert reply.status_code == 402
    assert upstream.requests == []


def test_a_pause_beats_a_downgrade_covering_the_same_agent(path, fleet):
    _budget(fleet, action="downgrade", fallback_model=CHEAP)
    _budget(fleet, action="pause", client=None)
    _spend(fleet, now=datetime.now(UTC))
    upstream = FakeUpstream()
    with _gateway(path, upstream) as gateway:
        reply = gateway.post("/v1/chat/completions", json=CALL, headers=_box_auth())
    assert reply.status_code == 402
    assert upstream.requests == []


def test_the_narrowest_downgrade_names_the_fallback(path, fleet):
    other = "fake/other"
    fleet.refresh_prices(
        [dataclasses.replace(FAKE_PRICE, model=other)], now=NOON - timedelta(days=1)
    )
    _budget(fleet, action="downgrade", fallback_model=other, client=None)
    _budget(fleet, action="downgrade", fallback_model=CHEAP, box="b-one")
    _spend(fleet, now=datetime.now(UTC))
    upstream = FakeUpstream()
    with _gateway(path, upstream) as gateway:
        gateway.post("/v1/chat/completions", json=CALL, headers=_box_auth())
    assert _forwarded(upstream) == [CHEAP]


def test_a_downgrade_cannot_be_saved_without_a_fallback_in_the_catalogue(fleet):
    with pytest.raises(InvalidBudgetError, match="catalogue"):
        _budget(fleet, action="downgrade", fallback_model="nobody/knows")


def test_any_fallback_named_has_to_be_in_the_catalogue(fleet):
    with pytest.raises(InvalidBudgetError, match="catalogue"):
        _budget(fleet, action="pause", fallback_model="nobody/knows")


def test_a_downgrade_says_so_on_the_timeline(fleet, machine):
    _budget(fleet, action="downgrade", fallback_model=CHEAP)
    _spend(fleet)
    _sweep(fleet)
    payload = dict(_events(fleet))["budget_limit"]
    assert payload["budget"]["action"] == "downgrade"
    assert payload["budget"]["fallback_model"] == CHEAP
    assert _status(fleet) == "running"


# -- pause -------------------------------------------------------------------


def test_a_pause_refuses_calls_the_moment_it_is_reached(path, fleet):
    _budget(fleet, action="pause")
    _spend(fleet, now=datetime.now(UTC))
    upstream = FakeUpstream()
    with _gateway(path, upstream) as gateway:
        reply = gateway.post("/v1/chat/completions", json=CALL, headers=_box_auth())
    assert reply.status_code == 402
    assert reply.json()["error"]["code"] == "budget_exceeded"
    assert upstream.requests == []


def test_a_pause_marks_every_agent_covered_and_no_other(fleet, machine):
    fleet.update_box_status("b-two", "stopped")
    budget = _budget(fleet, action="pause")
    _spend(fleet)
    _sweep(fleet)
    assert _status(fleet, "b-one") == "paused"
    assert _status(fleet, "b-two") == "paused"
    assert _status(fleet, "b-three") == "running"
    assert dict(_events(fleet))["budget_paused"]["budget"]["id"] == budget.id


def test_a_pause_is_written_once(fleet, machine):
    _budget(fleet, action="pause")
    _spend(fleet)
    _sweep(fleet)
    _sweep(fleet)
    assert _types(fleet).count("budget_paused") == 1


def test_a_pause_lets_a_turn_in_progress_end_on_its_own(fleet, machine):
    _budget(fleet, action="pause")
    today = datetime.now(UTC)
    _spend(fleet, now=today)
    _sweep(fleet, now=today, idle_after_s=1800)
    assert machine.calls == []


def test_a_paused_machine_goes_to_sleep_once_quiet(fleet, machine):
    _budget(fleet, action="pause", box="b-one")
    _spend(fleet, now=datetime.now(UTC))
    today = datetime.now(UTC)
    _sweep(fleet, now=today, idle_after_s=1800)
    assert machine.calls == []
    quiet = today + timedelta(seconds=1801)
    # The budget is a day's, and these two tests read the real clock: spent in
    # the sweep's own day too, so a run just before midnight UTC still holds it.
    _spend(fleet, now=quiet, key="k2")
    _sweep(fleet, now=quiet, idle_after_s=1800)
    assert machine.calls == ["suspend"]
    assert _status(fleet) == "paused"
    assert "budget_machine_slept" in _types(fleet)


def test_a_paused_machine_with_a_live_task_is_not_put_to_sleep(fleet, machine):
    """However quiet its timeline: that would strand the watcher holding the call.
    Once the task has finished, the next sweep puts it to sleep."""
    _budget(fleet, action="pause", box="b-one")
    today = datetime.now(UTC)
    _spend(fleet, now=today)
    task = fleet.create_task("b-one", "a long one")
    fleet.update_task_status(task.id, "running")
    later = today + timedelta(seconds=1801)
    # A day's budget and the real clock, as above: spent in each sweep's own day.
    _spend(fleet, now=later, key="k2")
    _sweep(fleet, now=later, idle_after_s=1800)
    assert _status(fleet) == "paused"
    assert machine.calls == []
    fleet.update_task_status(task.id, "done")
    quiet = later + timedelta(seconds=1801)
    _spend(fleet, now=quiet, key="k3")
    _sweep(fleet, now=quiet, idle_after_s=1800)
    assert machine.calls == ["suspend"]


def test_a_kill_cuts_a_live_task_short(fleet, machine):
    _budget(fleet, action="kill", box="b-one")
    _spend(fleet)
    task = fleet.create_task("b-one", "a long one")
    fleet.update_task_status(task.id, "running")
    _sweep(fleet)
    assert machine.calls == ["stop"]


def test_a_paused_machine_that_is_already_asleep_is_left_alone(fleet, machine):
    machine.machine_state = "suspended"
    _budget(fleet, action="pause")
    _spend(fleet, now=datetime.now(UTC))
    today = datetime.now(UTC)
    _sweep(fleet, now=today, idle_after_s=1)
    _sweep(fleet, now=today + timedelta(seconds=10), idle_after_s=1)
    assert machine.calls == []


def test_a_box_still_being_created_is_paused_only_once_it_exists(fleet, machine):
    fleet.create_box("new", box_id="b-new", org="studio-nord", client="acme")
    _budget(fleet, action="pause")
    _spend(fleet)
    _sweep(fleet)
    assert _status(fleet, "b-new") == "provisioning"


def test_a_destroyed_box_stays_destroyed(fleet, machine):
    fleet.update_box_status("b-two", "torn_down")
    _budget(fleet, action="pause")
    _spend(fleet)
    _sweep(fleet)
    assert _status(fleet, "b-two") == "torn_down"


def test_notify_and_downgrade_never_pause(fleet, machine):
    _budget(fleet, action="downgrade", fallback_model=CHEAP)
    _budget(fleet, action="notify", client=None)
    _spend(fleet)
    _sweep(fleet)
    assert _status(fleet) == "running"


def test_another_orgs_spend_pauses_nobody_here(fleet, machine):
    fleet.create_org("other", "Other")
    fleet.create_client("other", "acme", "Another Acme")
    fleet.create_box("four", box_id="b-four", org="other", client="acme")
    fleet.update_box_status("b-four", "running", endpoint="fake://four")
    _budget(fleet, action="pause")
    _spend(fleet, box="b-four")
    _sweep(fleet)
    assert _status(fleet) == "running"


# -- kill --------------------------------------------------------------------


def test_a_kill_ends_the_turn_by_stopping_the_machine_and_names_the_budget(fleet, machine):
    """A stop and not a suspend: a suspended machine would carry on mid-turn when it woke."""
    budget = _budget(fleet, action="kill", box="b-one")
    _spend(fleet)
    _sweep(fleet)
    assert machine.calls.count("stop") == 1
    assert "suspend" not in machine.calls
    assert _status(fleet) == "paused"
    assert dict(_events(fleet))["budget_killed"]["budget"]["id"] == budget.id


def test_a_kill_leaves_a_machine_already_down_alone(fleet, machine):
    machine.machine_state = "stopped"
    _budget(fleet, action="kill")
    _spend(fleet)
    _sweep(fleet)
    assert machine.calls == []
    assert "budget_killed" not in _types(fleet)
    assert _status(fleet) == "paused"


def test_a_kill_that_fails_still_marks_the_box_and_tries_again(fleet, machine):
    _budget(fleet, action="kill", box="b-one")
    _spend(fleet)

    real = machine.stop
    attempts = []

    def fails_once(box_id):
        attempts.append(box_id)
        if len(attempts) == 1:
            raise BackendError("the substrate refused")
        real(box_id)

    machine.stop = fails_once
    outcome = _sweep(fleet)
    assert _status(fleet) == "paused"
    assert "budget_killed" not in _types(fleet)
    assert any(entry.get("error") for entry in outcome)
    _sweep(fleet)
    assert "budget_killed" in _types(fleet)
    assert machine.machine_state == "stopped"


def test_one_machine_refusing_does_not_stop_the_sweep_for_the_rest(fleet, machine):
    _budget(fleet, action="kill")
    _budget(fleet, action="kill", client="bravo")
    _spend(fleet)
    _spend(fleet, box="b-three", key="k2")

    def always(box_id):
        raise BackendError("no")

    machine.stop = always
    _sweep(fleet)
    assert {_status(fleet, b) for b in ("b-one", "b-two", "b-three")} == {"paused"}


# -- coming out of it --------------------------------------------------------


def test_the_period_rolling_over_releases_a_paused_box(fleet, machine):
    machine.machine_state = "stopped"
    _budget(fleet, action="pause")
    _spend(fleet)
    _sweep(fleet)
    assert _status(fleet) == "paused"
    _sweep(fleet, now=NOON + timedelta(days=1))
    assert _status(fleet) == "stopped"
    assert "budget_released" in _types(fleet)
    assert machine.calls == []


def test_raising_the_limit_releases_it(fleet, machine):
    machine.machine_state = "stopped"
    _budget(fleet, action="pause")
    _spend(fleet)
    _sweep(fleet)
    _budget(fleet, action="pause", nano=2 * NANO)
    _sweep(fleet)
    assert _status(fleet) == "stopped"


def test_removing_the_budget_releases_it(fleet, machine):
    machine.machine_state = "stopped"
    _budget(fleet, action="pause")
    _spend(fleet)
    _sweep(fleet)
    fleet.delete_budget("studio-nord", period="day", client="acme")
    _sweep(fleet)
    assert _status(fleet) == "stopped"


def test_a_released_box_still_awake_reads_as_running(fleet, machine):
    """Or the idle sweep, which only looks at `running`, would never put it to sleep."""
    _budget(fleet, action="pause")
    _spend(fleet)
    _sweep(fleet)
    _sweep(fleet, now=NOON + timedelta(days=1))
    assert _status(fleet) == "running"


def test_a_second_budget_still_holds_it(fleet, machine):
    _budget(fleet, action="pause")
    _budget(fleet, action="kill", client=None, nano=NANO)
    _spend(fleet)
    _sweep(fleet)
    fleet.delete_budget("studio-nord", period="day", client="acme")
    _sweep(fleet)
    assert _status(fleet) == "paused"


def test_a_sum_that_cannot_be_trusted_does_not_release_it(fleet, machine):
    _budget(fleet, action="pause")
    _spend(fleet)
    _sweep(fleet)
    tomorrow = NOON + timedelta(days=1)
    fleet.record_usage(
        "b-one",
        model="nobody/knows",
        upstream="openrouter",
        tokens_in=1,
        tokens_out=1,
        idempotency_key="unpriced",
        eur_per_usd=RATE,
        now=tomorrow,
    )
    _sweep(fleet, now=tomorrow)
    assert _status(fleet) == "paused"


def test_a_sum_that_cannot_be_trusted_pauses_nobody_new(fleet, machine):
    _budget(fleet, action="pause")
    fleet.record_usage(
        "b-one",
        model="nobody/knows",
        upstream="openrouter",
        tokens_in=1,
        tokens_out=1,
        idempotency_key="unpriced",
        eur_per_usd=RATE,
        now=NOON,
    )
    _sweep(fleet)
    assert _status(fleet) == "running"


def test_a_box_paused_again_after_release_says_so_again(fleet, machine):
    _budget(fleet, action="pause")
    _spend(fleet)
    _sweep(fleet)
    tomorrow = NOON + timedelta(days=1)
    _sweep(fleet, now=tomorrow)
    _spend(fleet, key="k2", now=tomorrow)
    _sweep(fleet, now=tomorrow)
    assert _types(fleet).count("budget_paused") == 2


def test_unpausing_is_refused_while_a_budget_still_holds_it(fleet, machine):
    _budget(fleet, action="pause")
    _spend(fleet)
    _sweep(fleet)
    with pytest.raises(BudgetHolds):
        unpause_box("b-one", store=fleet, now=NOON)
    assert _status(fleet) == "paused"


def test_unpausing_is_only_for_a_paused_box(fleet, machine):
    assert unpause_box("b-one", store=fleet, now=NOON)["already_unpaused"] is True
    assert _status(fleet) == "running"


def test_the_unpause_route_answers_409_while_held(fleet, machine, api):
    _budget(fleet, action="pause")
    _spend(fleet, now=datetime.now(UTC))
    _sweep(fleet, now=datetime.now(UTC))
    reply = api.post("/api/boxes/b-one/unpause", headers=_as("owner"))
    assert reply.status_code == 409
    assert "budget" in reply.json()["detail"]


def test_the_unpause_route_releases_once_nothing_holds_it(fleet, machine, api):
    machine.machine_state = "stopped"
    fleet.update_box_status("b-one", "paused")
    reply = api.post("/api/boxes/b-one/unpause", headers=_as("owner"))
    assert reply.status_code == 200
    assert reply.json()["box"]["status"] == "stopped"
    assert api.post("/api/boxes/b-one/wake", headers=ROUTE).status_code == 200
    assert machine.calls == ["start"]


def test_only_an_owner_unpauses(fleet, machine, api):
    fleet.update_box_status("b-one", "paused")
    assert api.post("/api/boxes/b-one/unpause", headers=_as("member")).status_code == 403
    viewer = _as("client_viewer", client="acme")
    assert api.post("/api/boxes/b-one/unpause", headers=viewer).status_code == 403
    assert _status(fleet) == "paused"


def test_another_orgs_owner_finds_nothing_to_unpause(fleet, machine, api):
    fleet.update_box_status("b-one", "paused")
    assert (
        api.post("/api/boxes/b-one/unpause", headers=_as("owner", org="other")).status_code == 404
    )


# -- the client's timeline, over the API -------------------------------------


def test_the_agency_reads_what_its_budgets_did(fleet, machine, api):
    _budget(fleet, action="notify")
    _spend(fleet)
    _sweep(fleet)
    reply = api.get(
        "/api/orgs/studio-nord/budget-events", params={"client": "acme"}, headers=_as("owner")
    )
    assert reply.status_code == 200
    assert {e["type"] for e in reply.json()["events"]} == {"budget_soft_limit", "budget_limit"}


def test_another_org_reads_none_of_it(fleet, machine, api):
    reply = api.get("/api/orgs/studio-nord/budget-events", headers=_as("owner", org="other"))
    assert reply.status_code == 404


def test_a_clients_viewer_reads_none_of_it(fleet, machine, api):
    viewer = _as("client_viewer", client="acme")
    assert api.get("/api/orgs/studio-nord/budget-events", headers=viewer).status_code == 403


# -- the sweep runs ----------------------------------------------------------


def test_the_loop_enforces_budgets_and_survives_it_failing(path):
    import asyncio

    calls = []

    def enforcer(store):
        calls.append(store)
        raise RuntimeError("nope")

    state = LoopState(interval_s=0.0)
    asyncio.run(
        run_reconcile_loop(
            state,
            store_factory=lambda: FleetStore(path),
            reconcile=lambda store: [],
            sleeper=None,
            box_sweeper=None,
            budget_enforcer=enforcer,
            max_sweeps=2,
        )
    )
    assert len(calls) == 2
    assert state.sweeps == 2
    assert state.last_error is None


def test_the_loop_uses_the_real_one_by_default():
    import inspect

    source = inspect.getsource(run_reconcile_loop)
    assert "enforce_budgets" in source


def test_the_door_tells_a_conversation_why_it_closed(paused, machine, door):
    front, _ = door
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as closed,
        front.websocket_connect(
            "/api/ws",
            headers={**_door_token(), "host": f"one--acme--studio-nord.{DOMAIN}"},
        ) as socket,
    ):
        socket.receive_text()
    assert closed.value.code == 4402
    assert "paused by budget" in closed.value.reason
    assert machine.calls == []


# -- each action, end to end, on a small budget ------------------------------

#: What one call costs through the fake upstream, at a rate of a half.
ONE_CALL = 1_220_000


def _wait_for_the_row(fleet):
    """The gateway writes a call's row on a thread, after answering."""
    deadline = time.monotonic() + 5
    while not fleet._conn.execute("SELECT id FROM usage").fetchone():
        assert time.monotonic() < deadline, "the call's row was never written"
        time.sleep(0.01)


def _end_to_end(path, fleet, machine, action, **kwargs):
    """A budget one call big, that call through the real gateway, then a sweep."""
    _budget(fleet, action=action, nano=ONE_CALL, box="b-one", **kwargs)
    upstream = FakeUpstream()
    with _gateway(path, upstream) as gateway:
        assert gateway.post("/v1/chat/completions", json=CALL, headers=_box_auth()).is_success
        _wait_for_the_row(fleet)
        enforce_budgets(fleet, now=datetime.now(UTC), idle_after_s=10**9)
        second = gateway.post("/v1/chat/completions", json=CALL, headers=_box_auth())
    return second, upstream


def test_notify_from_end_to_end(path, fleet, machine):
    second, upstream = _end_to_end(path, fleet, machine, "notify")
    assert second.status_code == 200
    assert _forwarded(upstream) == [FAKE_MODEL, FAKE_MODEL]
    assert _status(fleet) == "running"
    assert {"budget_soft_limit", "budget_limit"} <= set(_types(fleet))
    assert machine.calls == []


def test_downgrade_from_end_to_end(path, fleet, machine):
    second, upstream = _end_to_end(path, fleet, machine, "downgrade", fallback_model=CHEAP)
    assert second.status_code == 200
    assert _forwarded(upstream) == [FAKE_MODEL, CHEAP]
    assert _status(fleet) == "running"
    assert "budget_limit" in _types(fleet)


def test_pause_from_end_to_end(path, fleet, machine, door):
    second, upstream = _end_to_end(path, fleet, machine, "pause")
    assert second.status_code == 402
    assert _forwarded(upstream) == [FAKE_MODEL]
    assert _status(fleet) == "paused"
    assert "budget_paused" in _types(fleet)
    assert machine.calls == []
    front, seen = door
    reply = front.get(
        "/api/status", headers={**_door_token(), "host": f"one--acme--studio-nord.{DOMAIN}"}
    )
    assert reply.status_code == 402
    assert "paused by budget" in reply.json()["detail"]
    assert "used up" in reply.json()["detail"]
    assert seen == []


def test_kill_from_end_to_end(path, fleet, machine, door):
    second, upstream = _end_to_end(path, fleet, machine, "kill")
    assert second.status_code == 402
    assert _forwarded(upstream) == [FAKE_MODEL]
    assert _status(fleet) == "paused"
    assert {"budget_paused", "budget_killed"} <= set(_types(fleet))
    assert machine.calls == ["stop"]
    front, _ = door
    reply = front.get(
        "/api/status", headers={**_door_token(), "host": f"one--acme--studio-nord.{DOMAIN}"}
    )
    assert reply.status_code == 402
    assert machine.calls == ["stop"]


def test_a_killed_agent_can_be_destroyed(path, fleet, machine):
    _end_to_end(path, fleet, machine, "kill")
    provision.teardown_box("b-one", store=fleet)
    assert _status(fleet) == "torn_down"


def test_a_fallback_off_the_list_refuses_the_call_and_never_goes_out(downgraded):
    """FLOTTA-106. A budget is the owner's and the bill is the operator's, so neither
    the original model nor the one the budget names may go out."""
    upstream = FakeUpstream()
    with _gateway(
        downgraded, upstream, env={**GATEWAY_ENV, "FLOTTA_MODELS": FAKE_MODEL}
    ) as gateway:
        reply = gateway.post("/v1/chat/completions", json=CALL, headers=_box_auth())
    assert reply.status_code == 402
    assert reply.json()["error"]["code"] == "fallback_not_allowed"
    assert CHEAP in reply.json()["error"]["message"]
    assert _forwarded(upstream) == []
