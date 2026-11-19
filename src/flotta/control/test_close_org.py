"""Closing an org, and deleting it 30 days later (FLOTTA-224).

Who may close one (the operator, or its own owner, never billing), what closing
does at once (everyone signed out and kept out, links and triggers stopped,
agents destroyed), reopening it before the purge, and the purge itself: every
row of the org goes, another org's and a person who belongs elsewhere stay.
The ratchet at the end fails for a table the purge doesn't know about.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from flotta import budgets
from flotta.auth import (
    SCOPE_FLEET_READ,
    SCOPE_FLEET_WRITE,
    SCOPE_ORG_ADMIN,
    SCOPE_ORG_GOVERN,
    mint,
    scopes_for_role,
)
from flotta.control.test_sign_in import KEY, Outbox, _app
from flotta.pricing import NANO
from flotta.provision import purge_closed_orgs
from flotta.store import (
    PURGED_BY,
    FleetStore,
    InvalidAccountError,
    InvalidTransitionError,
    OrgNotActiveError,
)
from flotta.test_provision import FakeBackend

ORG = "agency-x"


def _bearer(scopes, **claims):
    return {"Authorization": f"Bearer {mint(subject='t', scopes=scopes, key=KEY, **claims)}"}


OPERATOR = _bearer({SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN})
OWNER = _bearer(scopes_for_role("owner"), org=ORG)
MEMBER = _bearer(scopes_for_role("member"), org=ORG)
OTHER_OWNER = _bearer(scopes_for_role("owner"), org="other")
CLIENT_USER = _bearer(scopes_for_role("client_user", client="acme"), org=ORG, client="acme")
GOVERNOR = _bearer({SCOPE_ORG_GOVERN})
#: The operator viewing the agency as its owner: `org:admin` and no write.
VIEWING_AS_OWNER = _bearer({SCOPE_ORG_ADMIN, SCOPE_FLEET_READ}, org=ORG)
CONFIRMED = {"confirm": ORG}


@pytest.fixture
def machine(monkeypatch):
    import flotta.provision as provision

    backend = FakeBackend()
    backend.machine_state = "stopped"
    monkeypatch.setattr(provision, "_backend_for", lambda scheme: backend)
    return backend


@pytest.fixture
def fleet(tmp_path):
    """agency-x with an agent, an owner, a member who is also in `other`, and
    some of everything; and `other`, an active org with an agent of its own."""
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        for org in (ORG, "other"):
            store.create_org(org, org.title())
            store.create_client(org, "acme", "Acme")
        ana = store.create_user("ana@example.com")
        store.add_membership(ana.id, ORG, "owner")
        bo = store.create_user("bo@example.com")
        store.add_membership(bo.id, ORG, "member")
        store.add_membership(bo.id, "other", "owner")
        for org, name in ((ORG, "support"), ("other", "helper")):
            box = store.create_box(name, org=org, client="acme")
            store.update_box_status(box.id, "running", endpoint=f"fake://{org}/{name}")
            store.add_event("box", box.id, "note", {"x": 1})
            store.record_usage(
                box.id,
                model="m",
                upstream="u",
                tokens_in=10,
                tokens_out=10,
                idempotency_key=f"k-{org}",
                eur_per_usd=None,
            )
            store.create_trigger(box.id, prompt="hi", created_by="t", cron="0 9 * * *", tz="UTC")
            store.create_api_key(box.id, name="k")
            store.create_run(box.id, "do it", trigger_kind="api")
            store.set_budget(org, period="month", limit_eur_nano=NANO, action="pause")
        store.create_session(ana.id, ORG)
        store.create_session(bo.id, ORG)
        store.create_session(bo.id, "other")
        store.create_login_link("cy@example.com", purpose="invite", org_id=ORG, role="member")
    return path


@pytest.fixture
def api(fleet, machine):
    with TestClient(_app(fleet, mailer=Outbox())) as client:
        yield client


def _org(fleet, org=ORG):
    with FleetStore(fleet) as store:
        return store.get_org(org)


def _close(api, headers=OWNER, body=CONFIRMED):
    return api.post(f"/api/orgs/{ORG}/close", json=body, headers=headers)


# -- who may close it ---------------------------------------------------------------


def test_its_owner_closes_it_and_its_agents_are_destroyed(api, fleet, machine):
    answer = _close(api)
    assert answer.status_code == 200, answer.text
    view = answer.json()["org"]
    assert view["status"] == "closed" and view["closed_by"] == "owner"
    closed_at = datetime.fromisoformat(view["closed_at"])
    assert datetime.fromisoformat(view["purge_after"]) == closed_at + timedelta(days=30)
    with FleetStore(fleet) as store:
        assert all(b.status == "torn_down" for b in store.list_boxes_in(ORG))
        assert all(b.status != "torn_down" for b in store.list_boxes_in("other"))
    assert any("destroy" in call for call in machine.calls)


def test_the_operator_closes_it_too(api, fleet):
    answer = _close(api, OPERATOR)
    assert answer.status_code == 200
    assert answer.json()["org"]["closed_by"] == "operator"


@pytest.mark.parametrize(
    "who",
    [MEMBER, OTHER_OWNER, CLIENT_USER, VIEWING_AS_OWNER],
    ids=["a member", "another org's owner", "a client's person", "the operator viewing as owner"],
)
def test_nobody_else_closes_it(api, fleet, who):
    assert _close(api, who).status_code in (403, 404)
    assert _org(fleet).status == "active"


def test_billing_does_not_close_an_org(api, fleet):
    """Billing cancels plans; closing an account is a person's choice."""
    assert _close(api, GOVERNOR).status_code == 403
    assert _org(fleet).status == "active"


@pytest.mark.parametrize("body", [None, {}, {"confirm": "agency-y"}, {"confirm": "Agency-X"}])
def test_it_closes_only_when_confirmed_with_its_id(api, fleet, body):
    assert _close(api, body=body).status_code == 422
    assert _org(fleet).status == "active"


def test_the_default_org_is_never_closed(api):
    answer = api.post("/api/orgs/default/close", json={"confirm": "default"}, headers=OPERATOR)
    assert answer.status_code == 409


def test_closing_twice_changes_nothing(api, fleet):
    first = _close(api).json()["org"]["closed_at"]
    again = _close(api, OPERATOR)
    assert again.status_code == 200 and again.json()["changed"] is False
    assert _org(fleet).closed_at == first and _org(fleet).closed_by == "owner"


# -- what closing does at once ------------------------------------------------------------


def test_everyone_is_signed_out_of_it_and_nowhere_else(api, fleet):
    _close(api)
    with FleetStore(fleet) as store:
        rows = store._conn.execute("SELECT org_id FROM sessions").fetchall()
    assert [r["org_id"] for r in rows] == ["other"]


def test_nobody_signs_in_to_it_again(api, fleet):
    _close(api)
    with FleetStore(fleet) as store:
        ana = store.get_user_by_email("ana@example.com")
        with pytest.raises(InvalidAccountError):
            store.create_session(ana.id, ORG)
        link = store.create_login_link("ana@example.com")
    answer = api.post("/api/auth/redeem", json={"secret": link})
    assert answer.status_code == 401


def test_a_person_in_another_org_signs_in_there(api, fleet):
    _close(api)
    with FleetStore(fleet) as store:
        link = store.create_login_link("bo@example.com")
    answer = api.post("/api/auth/redeem", json={"secret": link})
    assert answer.status_code == 200, answer.text
    assert answer.json()["org"] == "other"


def test_its_invites_and_triggers_stop(api, fleet):
    _close(api)
    with FleetStore(fleet) as store:
        links = store._conn.execute(
            "SELECT COUNT(*) AS n FROM login_links WHERE org_id = ?", (ORG,)
        ).fetchone()["n"]
        enabled = {
            r["org_id"]: r["enabled"]
            for r in store._conn.execute("SELECT org_id, enabled FROM triggers").fetchall()
        }
    assert links == 0
    assert enabled == {ORG: 0, "other": 1}


def test_nothing_of_it_runs_or_is_created(api, fleet):
    _close(api)
    with FleetStore(fleet) as store:
        with pytest.raises(OrgNotActiveError, match="closed"):
            store.require_active_org(ORG)
        with pytest.raises(OrgNotActiveError, match="closed"):
            store.create_client(ORG, "globex", "Globex")
        box = store.list_boxes_in(ORG)[0]
        refusal = budgets.check_budgets(
            store, box, model="m", eur_per_usd=None, now=datetime.now(UTC)
        )
    assert isinstance(refusal, budgets.Refusal) and "closed" in refusal.message


def test_billing_neither_suspends_nor_resumes_a_closed_org(api, fleet):
    _close(api)
    for verb in ("suspend", "resume"):
        assert api.post(f"/api/orgs/{ORG}/{verb}", headers=GOVERNOR).status_code == 409
    assert _org(fleet).status == "closed"


def test_the_operator_sees_when_it_will_be_deleted(api, fleet):
    _close(api)
    orgs = {o["id"]: o for o in api.get("/api/orgs", headers=OPERATOR).json()["orgs"]}
    assert orgs[ORG]["purge_after"] is not None
    assert orgs["other"]["purge_after"] is None


# -- reopening it -----------------------------------------------------------------------


def test_the_operator_reopens_it_suspended_with_its_agents_gone(api, fleet):
    _close(api)
    answer = api.post(f"/api/orgs/{ORG}/reopen", headers=OPERATOR)
    assert answer.status_code == 200, answer.text
    org = _org(fleet)
    assert (org.status, org.suspended_by, org.closed_at) == ("suspended", "operator", None)
    with FleetStore(fleet) as store:
        ana = store.get_user_by_email("ana@example.com")
        assert store.create_session(ana.id, ORG)


def test_only_the_operator_reopens_it(api, fleet):
    _close(api)
    for who in (OWNER, GOVERNOR):
        assert api.post(f"/api/orgs/{ORG}/reopen", headers=who).status_code == 403
    assert _org(fleet).status == "closed"


def test_an_org_that_is_not_closed_is_not_reopened(api):
    assert api.post(f"/api/orgs/{ORG}/reopen", headers=OPERATOR).status_code == 409


# -- the purge --------------------------------------------------------------------------


def _counts(fleet):
    """Rows per table that still name agency-x, directly or through its boxes."""
    with FleetStore(fleet) as store:
        conn = store._conn
        boxes = [r["id"] for r in conn.execute("SELECT id FROM boxes").fetchall()]
        mine = {
            r["id"]
            for r in conn.execute("SELECT id FROM boxes WHERE org_id = ?", (ORG,)).fetchall()
        }
        left: dict[str, int] = {}
        for table in _tables(store):
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            n = 0
            if "org_id" in cols:
                n += conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE org_id = ?", (ORG,)
                ).fetchone()["n"]
            if table == "github_installations":
                n += conn.execute(
                    "SELECT COUNT(*) AS n FROM github_installations WHERE org = ?", (ORG,)
                ).fetchone()["n"]
            if table == "orgs":
                n += conn.execute("SELECT COUNT(*) AS n FROM orgs WHERE id = ?", (ORG,)).fetchone()[
                    "n"
                ]
            if "box_id" in cols and mine:
                marks = ",".join("?" * len(mine))
                n += conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE box_id IN ({marks})", tuple(mine)
                ).fetchone()["n"]
            if table == "events" and mine:
                marks = ",".join("?" * len(mine))
                n += conn.execute(
                    f"SELECT COUNT(*) AS n FROM events WHERE entity_id IN ({marks})",
                    tuple(mine),
                ).fetchone()["n"]
            if n:
                left[table] = n
        assert boxes  # the other org's are still there to count against
        return left


def _tables(store):
    return [
        r["name"]
        for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]


def test_after_its_period_every_row_of_it_is_deleted(api, fleet, machine):
    _close(api)
    assert _counts(fleet)  # closed, and still all there
    with FleetStore(fleet) as store:
        outcomes = purge_closed_orgs(store, days=0)
    assert any(o.get("org") == ORG and o.get("purged") for o in outcomes), outcomes
    assert _counts(fleet) == {}
    with FleetStore(fleet) as store:
        assert store.get_org(ORG) is None
        assert store.get_user_by_email("ana@example.com") is None
        # In another org still: kept, with that membership and session.
        bo = store.get_user_by_email("bo@example.com")
        assert bo is not None
        assert [m.org for m in store.memberships_for(bo.id)] == ["other"]
        assert store.get_org("other") is not None
        assert all(b.org == "other" for b in store.list_boxes())


def test_nothing_is_deleted_before_its_period(api, fleet):
    _close(api)
    with FleetStore(fleet) as store:
        purge_closed_orgs(store)  # the default, 30 days
        assert store.get_org(ORG) is not None


def test_the_period_is_counted_from_when_it_closed(api, fleet):
    _close(api)
    closed = datetime.fromisoformat(_org(fleet).closed_at)
    with FleetStore(fleet) as store:
        assert store.orgs_to_purge(days=30, now=closed + timedelta(days=29)) == []
        due = store.orgs_to_purge(days=30, now=closed + timedelta(days=30))
    assert [o.id for o in due] == [ORG]


def test_it_waits_while_an_agent_is_not_destroyed(fleet, machine):
    """A row is the only handle on a machine that may still be billing."""
    with FleetStore(fleet) as store:
        store.close_org(ORG, by="operator")
        with pytest.raises(InvalidTransitionError, match="torn down"):
            store.purge_org(ORG)


def test_the_sweep_destroys_an_agent_a_close_could_not(fleet, machine):
    with FleetStore(fleet) as store:
        store.close_org(ORG, by="operator")  # no route, so nothing destroyed yet
        outcomes = purge_closed_orgs(store, days=0)
    assert any(o.get("org") == ORG and o.get("purged") for o in outcomes), outcomes


def test_only_a_closed_org_is_purged_and_never_the_default(fleet):
    with FleetStore(fleet) as store:
        with pytest.raises(InvalidTransitionError):
            store.purge_org(ORG)
        with pytest.raises(InvalidTransitionError):
            store.purge_org("default")
        with pytest.raises(InvalidTransitionError):
            store.close_org("default", by="operator")


def test_expired_sessions_and_links_go_and_live_ones_stay(fleet):
    now = datetime.now(UTC)
    with FleetStore(fleet) as store:
        ana = store.get_user_by_email("ana@example.com")
        store.create_session(ana.id, ORG, now=now - timedelta(days=60))
        store.create_login_link("ana@example.com", now=now - timedelta(days=2))
        before = {
            t: store._conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("sessions", "login_links")
        }
        gone = store.purge_expired_access(now=now)
        after = {
            t: store._conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("sessions", "login_links")
        }
    assert gone == {"sessions": 1, "login_links": 1}
    assert after == {t: before[t] - 1 for t in before}


def test_every_table_is_known_to_the_purge(fleet):
    """A new table is purged on purpose: add it to `PURGED_BY` and to `purge_org`."""
    with FleetStore(fleet) as store:
        tables = set(_tables(store))
    assert tables - set(PURGED_BY) == set(), "tables the purge doesn't know about"
    assert set(PURGED_BY) - tables == set(), "tables in PURGED_BY that don't exist"


def test_the_default_org_is_never_purged_even_marked_closed(fleet):
    """Belt and braces: a row edited by hand, or a future bug, still deletes
    nothing of the operator's."""
    with FleetStore(fleet) as store:
        store._conn.execute(
            "UPDATE orgs SET status = 'closed', closed_at = ? WHERE id = 'default'",
            ((datetime.now(UTC) - timedelta(days=90)).isoformat(),),
        )
        assert "default" not in [o.id for o in store.orgs_to_purge(days=30)]
        with pytest.raises(InvalidTransitionError, match="never purged"):
            store.purge_org("default")
