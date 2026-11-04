"""`/api/orgs/{org}/usage` (FLOTTA-94): sums, never rows, and only the caller's org."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from flotta.auth import mint, scopes_for_role
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.pricing import NANO
from flotta.store import FleetStore

SIGNING_KEY = "a-signing-key-for-these-tests"
ROUTE = "/api/orgs/studio-nord/usage"
#: Costs here come from the upstream's own figure, in nano-dollars, at a rate of 1.
RATE = Decimal("1")
NOW = datetime(2025, 11, 4, 12, tzinfo=UTC)


def _call(store, box, cost, *, key, at=NOW, model="m-good", **overrides):
    store.record_usage(
        box,
        model=model,
        upstream="test",
        tokens_in=10,
        tokens_out=5,
        tokens_cached=2,
        idempotency_key=key,
        eur_per_usd=RATE,
        reported_cost_nano=cost,
        now=at,
        **overrides,
    )


@pytest.fixture
def fleet(tmp_path):
    db = str(tmp_path / "fleet.db")
    with FleetStore(db) as store:
        store.create_org("studio-nord", "Studio Nord")
        store.create_client("studio-nord", "acme", "Acme Dental")
        store.create_client("studio-nord", "bolt", "Bolt Bikes")
        store.create_box("one", box_id="b-one", org="studio-nord", client="acme")
        store.create_box("two", box_id="b-two", org="studio-nord", client="bolt")
        store.create_org("other", "Other Agency")
        store.create_client("other", "acme", "Another Acme")
        store.create_box("four", box_id="b-four", org="other", client="acme")
        _call(store, "b-one", NANO, key="a")
        _call(store, "b-one", NANO // 4, key="b", at=datetime(2025, 11, 3, 23, 59, tzinfo=UTC))
        _call(store, "b-two", NANO // 2, key="c", model="m-cheap", requested_model="m-good")
        _call(store, "b-four", 7 * NANO, key="d")
        store.record_machine_usage(
            "b-one",
            label="shared-cpu-1x:1024MB",
            seconds=3600,
            cost_nano=NANO // 10,
            eur_per_usd=RATE,
            upstream="ams",
            idempotency_key="m",
            at=NOW,
        )
        store.record_machine_usage(
            "b-one",
            label="volume:10gb",
            seconds=3600,
            cost_nano=NANO // 100,
            eur_per_usd=RATE,
            upstream="ams",
            idempotency_key="v",
            at=NOW,
        )
    return db


@pytest.fixture
def api(fleet):
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        reachable=_unchecked,
        known_model=_uncatalogued,
        signing_key=SIGNING_KEY,
    )
    with TestClient(app) as c:
        yield c


def _as(role, org="studio-nord", client=None):
    token = mint(
        subject="cli",
        scopes=scopes_for_role(role, client=client),
        key=SIGNING_KEY,
        org=None if org == "default" else org,
        client=client,
    )
    return {"Authorization": f"Bearer {token}"}


WINDOW = {"from": "2025-11-01", "to": "2025-12-01"}


def _read(api, **params):
    answer = api.get(ROUTE, params={**WINDOW, **params}, headers=_as("owner"))
    assert answer.status_code == 200, answer.text
    return answer.json()


# -- the guards ---------------------------------------------------------------


def test_another_orgs_usage_is_the_404_of_no_org(api):
    answer = api.get("/api/orgs/other/usage", params=WINDOW, headers=_as("owner"))
    assert answer.status_code == 404
    assert "7" not in answer.text


def test_naming_another_orgs_agent_or_client_finds_nothing_of_theirs(api):
    assert _read(api, box="b-four")["total"]["eur"] == "0"
    # Both orgs have a client called acme.
    assert _read(api, client="acme")["total"]["eur"] == "1.36"


@pytest.mark.parametrize(
    ("role", "client"), [("client_viewer", "acme"), ("client_user", "acme"), ("member", "acme")]
)
def test_someone_confined_to_a_client_is_refused(api, role, client):
    answer = api.get(ROUTE, params=WINDOW, headers=_as(role, client=client))
    assert answer.status_code == 403


def test_a_member_of_the_whole_org_reads(api):
    assert api.get(ROUTE, params=WINDOW, headers=_as("member")).status_code == 200


def test_the_operator_reads_any_org(api):
    answer = api.get("/api/orgs/other/usage", params=WINDOW, headers=_as("owner", org="default"))
    assert answer.json()["total"]["eur"] == "7"


def test_no_row_of_the_ledger_leaves(api, fleet):
    """Sums only: no row's id, key, run or rate."""
    body = _read(api, group="box,model")
    with FleetStore(fleet) as store:
        ids = [r["id"] for r in store._conn.execute("SELECT id FROM usage").fetchall()]
    assert ids
    assert not [row_id for row_id in ids if row_id in str(body)]
    for row in body["rows"]:
        assert not {"id", "idempotency_key", "run_id", "eur_per_usd", "upstream"} & set(row)


@pytest.mark.parametrize("group", ["id", "cost_eur_nano", "day;drop table usage", "org", "box,box"])
def test_a_group_that_is_not_on_the_list_is_refused(api, group):
    answer = api.get(ROUTE, params={**WINDOW, "group": group}, headers=_as("owner"))
    assert answer.status_code == 422


# -- the sums -----------------------------------------------------------------


def test_the_total_is_exact_and_splits_model_from_machine(api):
    total = _read(api)["total"]
    assert total["eur"] == "1.86"
    assert total["machine_eur"] == "0.11"
    assert total["model_eur"] == "1.75"
    assert total["calls"] == 3
    assert (total["tokens_in"], total["tokens_out"], total["tokens_cached"]) == (30, 15, 6)
    assert total["unpriced"] == 0


def test_machine_seconds_are_the_machines_not_its_storage_as_well(api):
    assert _read(api)["total"]["machine_seconds"] == 3600


def test_a_day_is_a_utc_day(api):
    rows = _read(api, group="day")["rows"]
    days = {row["day"]: row["eur"] for row in rows}
    assert days == {"2025-11-03": "0.25", "2025-11-04": "1.61"}


def test_by_client_and_by_agent(api):
    clients = {r["client"]: r["eur"] for r in _read(api, group="client")["rows"]}
    assert clients == {"acme": "1.36", "bolt": "0.5"}
    agents = {r["box"]: r for r in _read(api, group="box")["rows"]}
    assert agents["b-one"]["eur"] == "1.36"
    assert agents["b-one"]["box_name"] == "one--acme--studio-nord"


def test_by_model_keeps_what_was_asked_for_beside_what_was_used(api):
    rows = _read(api, group="kind,model,requested_model", box="b-two")["rows"]
    assert [(r["kind"], r["model"], r["requested_model"], r["eur"]) for r in rows] == [
        ("model", "m-cheap", "m-good", "0.5")
    ]


def test_by_model_tells_machine_time_from_storage_and_calls(api):
    rows = _read(api, group="kind,model", box="b-one")["rows"]
    assert {(r["kind"], r["model"]): r["eur"] for r in rows} == {
        ("model", "m-good"): "1.25",
        ("machine", "shared-cpu-1x:1024MB"): "0.1",
        ("machine", "volume:10gb"): "0.01",
    }


def test_several_groups_at_once_are_what_a_csv_needs(api):
    rows = _read(api, group="day,client,box,model")["rows"]
    assert {"day", "client", "box", "box_name", "model", "eur"} <= set(rows[0])
    assert sum(Decimal(r["eur"]) for r in rows) == Decimal("1.86")


def test_a_call_with_no_known_cost_is_counted_not_passed_over(api, fleet):
    with FleetStore(fleet) as store:
        store.record_usage(
            "b-two",
            model="m-unlisted",
            upstream="test",
            tokens_in=10,
            tokens_out=5,
            idempotency_key="u",
            eur_per_usd=None,
            now=NOW,
        )
    assert _read(api)["total"]["unpriced"] == 1
    assert _read(api, group="client")["rows"][1]["unpriced"] == 1


def test_a_destroyed_agent_keeps_its_name(api, fleet):
    with FleetStore(fleet) as store:
        store.release_name("b-two")
    rows = {r["box"]: r for r in _read(api, group="box")["rows"]}
    assert rows["b-two"]["box_name"] == "two--bolt--studio-nord"


def test_the_names_are_only_of_that_orgs_agents(fleet):
    with FleetStore(fleet) as store:
        assert set(store.box_names("studio-nord")) == {"b-one", "b-two"}


def test_an_agent_with_a_display_name_says_it(api, fleet):
    with FleetStore(fleet) as store:
        store.set_box_meta("b-one", display_name="Front desk")
    rows = {r["box"]: r for r in _read(api, group="box")["rows"]}
    assert rows["b-one"]["box_display_name"] == "Front desk"
    assert rows["b-two"]["box_display_name"] is None


# -- the window ---------------------------------------------------------------


def test_from_is_inclusive_and_to_is_not(api):
    assert _read(api, **{"from": "2025-11-04", "to": "2025-11-05"})["total"]["eur"] == "1.61"
    assert _read(api, **{"from": "2025-11-03", "to": "2025-11-04"})["total"]["eur"] == "0.25"


def test_a_call_exactly_on_the_edge_belongs_to_the_window_it_starts(api, fleet):
    with FleetStore(fleet) as store:
        _call(store, "b-two", 9 * NANO, key="edge", at=datetime(2025, 11, 5, tzinfo=UTC))
    assert _read(api, **{"from": "2025-11-05", "to": "2025-11-06"})["total"]["eur"] == "9"
    assert _read(api, **{"from": "2025-11-04", "to": "2025-11-05"})["total"]["eur"] == "1.61"


def test_the_window_is_said_back(api):
    body = _read(api)
    assert body["from"] == "2025-11-01T00:00:00+00:00"
    assert body["to"] == "2025-12-01T00:00:00+00:00"


def test_no_window_is_this_month_so_far(api, fleet):
    with FleetStore(fleet) as store:
        store.record_usage(
            "b-one",
            model="m-good",
            upstream="test",
            tokens_in=1,
            tokens_out=1,
            idempotency_key="now",
            eur_per_usd=RATE,
            reported_cost_nano=3 * NANO,
        )
    body = api.get(ROUTE, headers=_as("owner")).json()
    assert body["from"].endswith("-01T00:00:00+00:00")
    assert body["total"]["eur"] == "3"


@pytest.mark.parametrize(
    "window",
    [
        {"from": "yesterday"},
        {"from": "2025-11-05", "to": "2025-11-05"},
        {"from": "2025-11-05", "to": "2025-11-01"},
        # No timezone, so no particular moment.
        {"from": "2025-11-01T00:00:00", "to": "2025-11-02"},
        {"from": "2025-11-01", "to": "2025-11-02T00:00:00"},
        {"from": "2023-01-01", "to": "2025-01-01"},  # More than a year at once.
    ],
)
def test_a_window_that_makes_no_sense_is_refused(api, window):
    assert api.get(ROUTE, params=window, headers=_as("owner")).status_code == 422


# -- what a budget has spent ---------------------------------------------------


def test_a_listed_budget_says_how_far_it_has_got(api, fleet):
    with FleetStore(fleet) as store:
        store.set_budget(
            "studio-nord", client="bolt", period="month", limit_eur_nano=NANO, action="pause"
        )
    budgets = api.get("/api/orgs/studio-nord/budgets", headers=_as("member")).json()["budgets"]
    assert len(budgets) == 1
    found = budgets[0]
    assert found["limit_eur"] == "1"
    assert found["soft_pct"] == 0.8
    assert "spent_eur" in found
    assert "resets_at" in found
    assert found["unpriced"] == 0
    assert found["reached"] is False
    assert found["soft_reached"] is False


def test_a_listed_budget_at_its_limit_says_so(api, fleet):
    with FleetStore(fleet) as store:
        _call(store, "b-two", 2 * NANO, key="big", at=datetime.now(UTC))
        store.set_budget(
            "studio-nord", client="bolt", period="day", limit_eur_nano=NANO, action="pause"
        )
    found = api.get("/api/orgs/studio-nord/budgets", headers=_as("owner")).json()["budgets"][0]
    assert found["spent_eur"] == "2"
    assert found["reached"] is True
    assert found["soft_reached"] is True
