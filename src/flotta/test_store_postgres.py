"""What the parameterised store suite cannot assert: two connections racing.

`test_store.py` runs its whole ~90-test body against Postgres as well as
SQLite when `$PYTEST_POSTGRES_URL` is set — that is where "the store
behaves identically on both engines" is actually proven, by holding the
behaviour fixed and swapping what is underneath.

This file is only for the claim that needs **concurrency**: the transaction
guard. Every other test is single-threaded, so a dropped guard is invisible to
them — demonstrated during the migration, when three transactions lost their
guard and all 453 tests still passed. `BEGIN IMMEDIATE` on SQLite and
`LOCK TABLE … IN SHARE ROW EXCLUSIVE MODE` on Postgres are different
mechanisms for the same guarantee, and only a race can tell whether either is
doing its job.

    just test-postgres     # spins up a throwaway server and runs this
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

POSTGRES_URL = os.environ.get("PYTEST_POSTGRES_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set PYTEST_POSTGRES_URL to run the concurrency checks",
)


@pytest.fixture
def pg_store():
    """A Postgres FleetStore in a schema of its own."""
    import psycopg

    from flotta.store import FleetStore

    schema = f"c{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(POSTGRES_URL, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    admin.close()

    sep = "&" if "?" in POSTGRES_URL else "?"
    fleet = FleetStore(f"{POSTGRES_URL}{sep}options=-csearch_path%3D{schema}")
    try:
        yield fleet
    finally:
        fleet.close()
        admin = psycopg.connect(POSTGRES_URL, autocommit=True)
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


def _race(store, factory, count=2):
    """Run `factory` on `count` separate connections, released together."""
    from flotta.store import FleetStore

    outcomes: list[str] = []
    barrier = threading.Barrier(count)
    lock = threading.Lock()

    def attempt(index: int) -> None:
        fleet = FleetStore(store._url)
        try:
            barrier.wait(timeout=15)
            factory(fleet, index)
            with lock:
                outcomes.append("created")
        except Exception as exc:  # the cap's refusal, or anything else
            with lock:
                outcomes.append(type(exc).__name__)
        finally:
            fleet.close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=45)
    return outcomes


def test_the_task_cap_survives_a_real_race(pg_store):
    """The assertion the guard exists for, and the one no single-threaded test
    can make: without the table lock both sessions read a live count of zero
    and both insert."""
    box = pg_store.create_box("eng-a")
    outcomes = _race(pg_store, lambda fleet, i: fleet.create_task(box.id, f"t{i}", max_live=1))

    assert sorted(outcomes) == ["ConcurrencyLimitError", "created"], (
        f"expected exactly one winner, got {outcomes} — the cap is racy"
    )
    assert pg_store.count_live_tasks() == 1


def test_one_box_starts_one_run_in_a_real_race(pg_store):
    """`start_run` reads "nothing running" and then moves its run (M14.1). Two
    deliverers doing that at once without the lock both start, and a model
    call that names no run would then have two it could belong to."""
    box = pg_store.create_box("eng-a")
    runs = [pg_store.create_run(box.id, f"r{i}", trigger_kind="api").id for i in range(2)]

    outcomes = _race(pg_store, lambda fleet, i: fleet.start_run(runs[i]))

    assert sorted(outcomes) == ["BoxBusyError", "created"], (
        f"expected exactly one run to start, got {outcomes}"
    )


def test_one_fire_time_makes_one_run_in_a_real_race(pg_store):
    """Two sweeps see the same schedule due at once, a retry or a second
    control plane (M14.3): one makes the run, the other finds it fired."""
    from datetime import UTC, datetime

    box = pg_store.create_box("eng-a")
    trigger = pg_store.create_trigger(
        box.id, cron="0 8 * * *", tz="UTC", prompt="Morning.", created_by="test"
    )
    eight = datetime(2026, 11, 6, 8, tzinfo=UTC)

    def fire(fleet, i):
        if fleet.fire_trigger(trigger.id, eight) is None:
            raise LookupError("already fired")

    outcomes = _race(pg_store, fire)

    assert sorted(outcomes) == ["LookupError", "created"], f"expected one run, got {outcomes}"
    assert len(pg_store.list_runs(org="default", box=box.id)) == 1


def test_a_fire_waits_for_the_disable_and_then_fires_nothing(pg_store):
    """Teardown disables a destroyed agent's schedules while a sweep is
    firing one (M14.3). The fire must take the disable's lock, and read the
    trigger once it has it: on separate locks it read `enabled = 1` and made
    a pending run that teardown had already scanned past."""
    from datetime import UTC, datetime

    from flotta.store import FleetStore

    box = pg_store.create_box("eng-a")
    trigger = pg_store.create_trigger(
        box.id, cron="0 8 * * *", tz="UTC", prompt="Morning.", created_by="test"
    )
    eight = datetime(2026, 11, 6, 8, tzinfo=UTC)
    result: list[object] = []

    def fire():
        fleet = FleetStore(pg_store._url)
        try:
            result.append(fleet.fire_trigger(trigger.id, eight))
        finally:
            fleet.close()

    disabler = FleetStore(pg_store._url)
    try:
        with disabler._conn.transaction(guard="triggers"):
            disabler._conn.execute("UPDATE triggers SET enabled = 0 WHERE box_id = ?", (box.id,))
            sweep = threading.Thread(target=fire)
            sweep.start()
            sweep.join(timeout=2)
            assert sweep.is_alive(), "the fire did not wait for the disable's lock"
        sweep.join(timeout=15)
    finally:
        disabler.close()

    assert result == [None]
    assert pg_store.list_runs(org="default", box=box.id) == []


def test_a_webhook_call_waits_for_the_disable_and_then_makes_nothing(pg_store):
    """The same for a webhook (M14.4): called while its agent is torn down, it
    takes the disable's lock, then finds the trigger off."""
    from flotta.store import FleetStore

    box = pg_store.create_box("eng-a")
    trigger = pg_store.create_trigger(box.id, kind="webhook", prompt="New lead.", created_by="test")
    result: list[object] = []

    def call():
        fleet = FleetStore(pg_store._url)
        try:
            result.append(fleet.fire_webhook(trigger.id, prompt="New lead.", input={"body": {}}))
        finally:
            fleet.close()

    disabler = FleetStore(pg_store._url)
    try:
        with disabler._conn.transaction(guard="triggers"):
            disabler._conn.execute("UPDATE triggers SET enabled = 0 WHERE box_id = ?", (box.id,))
            caller = threading.Thread(target=call)
            caller.start()
            caller.join(timeout=2)
            assert caller.is_alive(), "the webhook did not wait for the disable's lock"
        caller.join(timeout=15)
    finally:
        disabler.close()

    assert result == [None]
    assert pg_store.list_runs(org="default", box=box.id) == []


def test_the_workspace_cap_survives_a_real_race(pg_store):
    """Same guard, the other table — wired ahead of the tier that uses it."""
    box = pg_store.create_box("eng-a")
    outcomes = _race(pg_store, lambda fleet, i: fleet.create_workspace(box.id, max_live=1))

    assert sorted(outcomes) == ["ConcurrencyLimitError", "created"], (
        f"expected exactly one winner, got {outcomes}"
    )
    assert pg_store.count_live_workspaces() == 1


def test_concurrent_status_changes_do_not_both_win(pg_store):
    """`update_box_status` is a read-check-write too.

    Two callers racing `running -> stopped` must not both succeed: the second
    is transitioning from a status it never observed, which is exactly what the
    transition table exists to reject.
    """
    box = pg_store.create_box("eng-a")
    pg_store.update_box_status(box.id, "running")

    outcomes = _race(pg_store, lambda fleet, i: fleet.update_box_status(box.id, "stopped"))

    assert outcomes.count("created") == 1, (
        f"expected one winner and one rejected transition, got {outcomes}"
    )
    assert pg_store.get_box(box.id).status == "stopped"


def test_a_link_clicked_twice_at_once_signs_in_once(pg_store):
    """`redeem_login_link` is a read-check-write as well: two tabs opening the
    same emailed link must not both get a session (M12.1)."""
    secret = pg_store.create_login_link("ana@studionord.example")

    def redeem(fleet, i):
        if fleet.redeem_login_link(secret) is None:
            raise LookupError("already used")

    outcomes = _race(pg_store, redeem)

    assert sorted(outcomes) == ["LookupError", "created"], (
        f"expected exactly one redemption, got {outcomes} — a link can sign in twice"
    )


def test_two_agencies_claiming_one_installation_at_once_leaves_one_owner(pg_store):
    """FLOTTA-22: `connect_github` rests on the primary key, not on a read before it."""
    from flotta.store import GitHubAlreadyConnectedError

    for org in ("agency-a", "agency-b"):
        pg_store.create_org(org, org)
    orgs = ("agency-a", "agency-b")

    def claim(fleet, i):
        fleet.connect_github(
            "77",
            org=orgs[i],
            client=None,
            account="acme",
            account_type="Organization",
            connected_by="someone",
        )

    outcomes = _race(pg_store, claim)

    assert sorted(outcomes) == [GitHubAlreadyConnectedError.__name__, "created"], outcomes
    held = [*pg_store.github_installations("agency-a"), *pg_store.github_installations("agency-b")]
    assert len(held) == 1


def test_a_webhook_changed_while_alerts_are_queued_still_gets_them(pg_store, monkeypatch):
    """FLOTTA-93: without the shared guard, the change misses the row being queued for
    the old URL, and that alert is then never delivered anywhere."""
    from datetime import UTC, datetime

    from flotta.pricing import NANO
    from flotta.store import FleetStore

    old, new = "https://old.example.com/hook", "https://new.example.com/hook"
    pg_store.create_org("studio", "Studio")
    pg_store.create_client("studio", "acme", "Acme")
    pg_store.set_org_alerts("studio", alert_email=None, webhook_url=old, nonce="one")
    budget = pg_store.set_budget(
        "studio", period="day", limit_eur_nano=20 * NANO, action="pause", client="acme"
    )
    pg_store.record_budget_event(
        budget, "budget_limit", period_start=datetime(2026, 11, 4, tzinfo=UTC), payload={}
    )

    def change():
        with FleetStore(pg_store._url) as fleet:
            fleet.set_org_alerts("studio", alert_email=None, webhook_url=new, nonce="two")

    changer = threading.Thread(target=change)
    targets = pg_store._alert_targets

    def change_midway(org):
        found = targets(org)
        changer.start()
        changer.join(timeout=1.0)  # With the guard it is still waiting after this.
        return found

    monkeypatch.setattr(pg_store, "_alert_targets", change_midway)
    pg_store.queue_budget_alerts()
    changer.join(timeout=15)
    assert not changer.is_alive()

    webhooks = {
        d.target: d.status for d in pg_store.alert_deliveries("studio") if d.channel == "webhook"
    }
    assert webhooks == {old: "replaced", new: "pending"}


def test_a_clients_installation_comes_before_its_orgs_on_postgres(pg_store):
    """`ORDER BY client IS NULL` sorts booleans there, where SQLite sorts integers."""
    pg_store.create_org("agency-a", "agency-a")
    pg_store.create_client("agency-a", "acme", "Acme")
    for number, client in (("1", None), ("2", "acme")):
        pg_store.connect_github(
            number,
            org="agency-a",
            client=client,
            account=f"account-{number}",
            account_type="Organization",
            connected_by="someone",
        )

    found = pg_store.github_installations_for("agency-a", "acme")
    assert [i.installation_id for i in found] == ["2", "1"]


def test_a_new_installation_replaces_the_old_one_on_postgres(pg_store):
    pg_store.create_org("agency-a", "agency-a")
    for number in ("1", "2"):
        pg_store.connect_github(
            number,
            org="agency-a",
            client=None,
            account="same",
            account_type="Organization",
            connected_by="someone",
        )

    assert [i.installation_id for i in pg_store.github_installations("agency-a")] == ["2"]


def test_a_grants_source_keeps_the_operators_word_on_postgres(pg_store):
    """The upsert's CASE, and the column the migration adds, on the real engine."""
    box = pg_store.create_box("source-box")
    pg_store.grant_repo(box.id, "same/repo", fleet_token=True)
    pg_store.connect_github(
        "1",
        org=box.org,
        client=box.client,
        account="same",
        account_type="Organization",
        connected_by="someone",
    )
    pg_store.grant_repo(box.id, "same/repo")
    pg_store.grant_repo(box.id, "same/other")

    assert pg_store.may_spend_fleet_token(box.id, "same/repo")
    assert not pg_store.may_spend_fleet_token(box.id, "same/other")
