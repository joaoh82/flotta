"""A suspended org (FLOTTA-184, M15.2): how a final failed payment ends.

The guard first: **no route wakes a suspended org's agent**. Every route the
control plane serves is sent, with the owner's token and the operator's, at a
suspended org whose agent is asleep on a substrate that records what it is
asked; none of them may start its machine.

Then every other way an agent runs: the door's wake, runs from the API and from
schedules, model calls through the gateway, and machines already running when
the org was suspended. Then nothing is created, sign-in and reads still work,
resuming restores all of it, and the transitions themselves.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from flotta import budgets, runner
from flotta.auth import (
    SCOPE_BOX_CHAT,
    SCOPE_FLEET_READ,
    SCOPE_FLEET_ROUTE,
    SCOPE_FLEET_WRITE,
    SCOPE_ORG_ADMIN,
    SCOPE_ORG_GOVERN,
    mint,
    scopes_for_role,
)
from flotta.control.test_sign_in import KEY, Outbox, _app
from flotta.store import FleetStore, OrgNotActiveError
from flotta.test_provision import FakeBackend

BOX = "support--acme--agency-x"


def _bearer(scopes, **claims):
    return {"Authorization": f"Bearer {mint(subject='t', scopes=scopes, key=KEY, **claims)}"}


#: Everything the operator's own tokens hold, and the door's `fleet:route`, which
#: wakes any org's agent once it has checked the caller.
OPERATOR = _bearer(
    {SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN, SCOPE_BOX_CHAT, SCOPE_FLEET_ROUTE}
)
OWNER = _bearer(scopes_for_role("owner"), org="agency-x")
GOVERNOR = _bearer({SCOPE_ORG_GOVERN})


@pytest.fixture
def machine(monkeypatch):
    """Every box's substrate: records each verb, starting asleep."""
    import flotta.provision as provision

    backend = FakeBackend()
    backend.machine_state = "stopped"
    monkeypatch.setattr(provision, "_backend_for", lambda scheme: backend)
    return backend


@pytest.fixture
def fleet(tmp_path):
    """agency-x, with one agent asleep and one owner; and another org, active."""
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.create_org("agency-x", "Agency X")
        store.create_client("agency-x", "acme", "Acme")
        ana = store.create_user("ana@example.com")
        store.add_membership(ana.id, "agency-x", "owner")
        box = store.create_box("support", org="agency-x", client="acme")
        store.update_box_status(box.id, "running", endpoint="fake://agency/support")
        store.update_box_status(box.id, "stopped")
        store.create_org("other", "Other")
        store.create_client("other", "acme", "Acme")
        other = store.create_box("helper", org="other", client="acme")
        store.update_box_status(other.id, "running", endpoint="fake://other/helper")
    return path


@pytest.fixture
def outbox():
    return Outbox()


@pytest.fixture
def api(fleet, outbox, machine):
    with TestClient(_app(fleet, mailer=outbox)) as client:
        yield client


def _box(fleet, name=BOX):
    with FleetStore(fleet) as store:
        return store.get_box_by_name(name)


def _suspend(fleet, reason="the payment failed"):
    """As billing does when a final payment fails, so billing may lift it (FLOTTA-190)."""
    with FleetStore(fleet) as store:
        return store.suspend_org("agency-x", reason=reason, by="test", as_billing=True)


def _resume(fleet):
    with FleetStore(fleet) as store:
        return store.resume_org("agency-x", by="test")


# -- the guard: nothing wakes it -------------------------------------------------


def _routes():
    from flotta.control.app import create_app

    app = create_app(store_factory=lambda: None, run_loop=False, background=False)
    found = []
    for route in app.routes:
        for method in sorted(getattr(route, "methods", None) or ()):
            if method not in ("HEAD", "OPTIONS"):
                found.append((method, route.path))
    return found


def test_the_sweep_reaches_every_route():
    """A sweep that found nothing would pass for the wrong reason."""
    assert len(_routes()) > 120


@pytest.mark.parametrize("who", ["owner", "operator"])
@pytest.mark.parametrize(("method", "path"), _routes())
def test_no_route_wakes_a_suspended_orgs_agent(api, fleet, machine, who, method, path):
    _suspend(fleet)
    box = _box(fleet)
    with FleetStore(fleet) as store:
        run = store.create_run(box.id, "anything", trigger_kind="api")
    fill = {
        "box_id": box.id,
        "org_id": "agency-x",
        "client_id": "acme",
        "run_id": run.id,
        "task_id": run.id,
        "name": BOX,
        "peer": BOX,
    }
    url = re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: fill.get(m.group(1), "x"), path)
    api.request(
        method,
        url,
        headers=OWNER if who == "owner" else OPERATOR,
        json={"name": "new", "id": "new", "client": "acme", "prompt": "hi", "text": "hi"},
    )
    assert "start" not in machine.calls, f"{method} {path} started a suspended org's agent"
    # By id: destroying it (which wakes nothing, and a suspended org may) frees its name.
    with FleetStore(fleet) as store:
        assert store.get_box(box.id).status != "running"


# -- every way an agent runs ------------------------------------------------------


def test_the_door_is_refused_with_a_reason_the_web_app_can_show(api, fleet, machine, monkeypatch):
    import flotta.control.app as app_module
    from flotta.provision import ADDRESSED_EVENT

    # Every wake would count as activity, not one a minute, so a missing refusal shows.
    monkeypatch.setattr(app_module, "ACTIVITY_FLOOR_S", 0)
    _suspend(fleet)
    answer = api.post(f"/api/boxes/{_box(fleet).id}/wake", headers=OWNER)
    assert answer.status_code == 402, answer.text
    assert "suspended" in answer.json()["detail"]
    assert "billing" in answer.json()["detail"]
    assert machine.calls == []
    # Refused before the activity is recorded, as a paused box is: a tab left open
    # on a suspended org must not keep its agent looking busy.
    with FleetStore(fleet) as store:
        kinds = [e.type for e in store.get_events("box", _box(fleet).id)]
    assert ADDRESSED_EVENT not in kinds


def test_the_operator_cannot_start_it_either(fleet, machine):
    from flotta.provision import OrgSuspended, start_box, wake_box

    _suspend(fleet)
    with FleetStore(fleet) as store:
        with pytest.raises(OrgSuspended):
            start_box(_box(fleet).id, store=store)
        with pytest.raises(OrgSuspended):
            wake_box(_box(fleet).id, store=store)
    assert machine.calls == []


class Door:
    def __init__(self):
        self.sent = []

    async def __call__(self, run, box, talk):
        self.sent.append(run.id)
        return "Done."


def _deliver(fleet, door):
    loop = runner.RunLoop(lambda: FleetStore(fleet), door)

    async def go():
        await loop.sweep()
        await loop.drain()

    asyncio.run(go())


def test_a_run_from_the_api_ends_blocked_and_is_never_delivered(api, fleet):
    _suspend(fleet)
    made = api.post(f"/api/boxes/{_box(fleet).id}/runs", headers=OWNER, json={"prompt": "hi"})
    assert made.status_code == 202, made.text
    door = Door()
    _deliver(fleet, door)
    assert door.sent == []
    with FleetStore(fleet) as store:
        run = store.get_run(made.json()["id"])
        assert (run.status, run.error_class) == ("budget_blocked", "suspended")
        assert "suspended" in (run.output or "")
        assert "budget_blocked" in [e.type for e in store.get_events("task", run.id)]


def test_a_fired_schedule_ends_blocked(fleet):
    _suspend(fleet)
    with FleetStore(fleet) as store:
        run = store.create_run(_box(fleet).id, "the morning report", trigger_kind="schedule")
    door = Door()
    _deliver(fleet, door)
    assert door.sent == []
    with FleetStore(fleet) as store:
        assert store.get_run(run.id).error_class == "suspended"


def test_an_active_orgs_run_is_delivered_beside_it(fleet):
    _suspend(fleet)
    with FleetStore(fleet) as store:
        run = store.create_run(_box(fleet, "helper--acme--other").id, "hi", trigger_kind="api")
    door = Door()
    _deliver(fleet, door)
    assert door.sent == [run.id]


def test_the_gateway_refuses_its_model_calls(fleet):
    _suspend(fleet)
    with FleetStore(fleet) as store:
        refusal = budgets.check_budgets(
            store, _box(fleet), model="any", eur_per_usd=None, now=datetime.now(UTC)
        )
        assert isinstance(refusal, budgets.Refusal)
        assert refusal.code == "org_suspended"
        other = budgets.check_budgets(
            store,
            _box(fleet, "helper--acme--other"),
            model="any",
            eur_per_usd=None,
            now=datetime.now(UTC),
        )
        assert other is None


def test_its_running_agents_are_put_to_sleep_and_their_runs_end(fleet, machine):
    from flotta.provision import sleep_suspended_orgs

    box = _box(fleet)
    with FleetStore(fleet) as store:
        store.update_box_status(box.id, "running")
        run = store.create_run(box.id, "a long job", trigger_kind="api")
        store.start_run(run.id)
    machine.machine_state = "started"
    _suspend(fleet)
    with FleetStore(fleet) as store:
        slept = sleep_suspended_orgs(store)
        assert slept == [{"box_id": box.id, "slept": True}]
        assert store.get_box(box.id).status == "stopped"
        assert store.get_run(run.id).status == "budget_blocked"
        # The other org's running agent is not touched.
        assert store.get_box_by_name("helper--acme--other").status == "running"
        assert sleep_suspended_orgs(store) == []
    assert machine.calls == ["suspend"]


def test_an_agent_a_budget_already_paused_is_put_to_sleep_too(fleet, machine):
    """The review of #220. A budget moves the row to `paused` at once and leaves
    the machine up until it is quiet, and never stops it under a live task. So
    the agency a failed payment finds may well have an agent `paused` with its
    machine up and a run going, which a sweep of `running` boxes never sees."""
    from flotta.provision import sleep_suspended_orgs

    box = _box(fleet)
    with FleetStore(fleet) as store:
        store.update_box_status(box.id, "running")
        run = store.create_run(box.id, "a long job", trigger_kind="api")
        store.start_run(run.id)
        store.update_box_status(box.id, "paused")
    machine.machine_state = "started"
    _suspend(fleet)
    with FleetStore(fleet) as store:
        assert sleep_suspended_orgs(store) == [{"box_id": box.id, "slept": True}]
        assert store.get_run(run.id).status == "budget_blocked"
        # Still the budget's to release: suspension only stopped the machine.
        assert store.get_box(box.id).status == "paused"
        # Asleep now, so the next sweep has nothing to do.
        assert sleep_suspended_orgs(store) == []
    assert machine.calls == ["suspend"]


def test_suspending_through_the_api_sleeps_its_agents_after_answering(api, fleet, machine):
    box = _box(fleet)
    with FleetStore(fleet) as store:
        store.update_box_status(box.id, "running")
    machine.machine_state = "started"
    answer = api.post("/api/orgs/agency-x/suspend", headers=GOVERNOR, json={"reason": "unpaid"})
    assert answer.status_code == 200, answer.text
    assert _box(fleet).status == "stopped"
    assert machine.calls == ["suspend"]


def test_the_reconcile_loop_sleeps_them_each_sweep(fleet):
    from flotta.control.loop import LoopState, run_reconcile_loop

    seen = []
    asyncio.run(
        run_reconcile_loop(
            LoopState(interval_s=0),
            store_factory=lambda: FleetStore(fleet),
            reconcile=lambda store: [],
            sleeper=None,
            box_sweeper=lambda store: [],
            budget_enforcer=lambda store: [],
            suspension_enforcer=lambda store: seen.append(store) or [],
            machine_meter=lambda store: [],
            sleep=lambda s: asyncio.sleep(0),
            max_sweeps=1,
        )
    )
    assert len(seen) == 1


# -- nothing is created, and nothing is taken away --------------------------------


def test_it_creates_no_client_agent_or_invite(api, fleet, outbox):
    _suspend(fleet)
    client = api.post(
        "/api/orgs/agency-x/clients", headers=OWNER, json={"id": "beta", "name": "Beta"}
    )
    assert client.status_code == 403, client.text
    assert "suspended" in client.json()["detail"]
    agent = api.post("/api/boxes", headers=OWNER, json={"name": "new", "client": "acme"})
    assert agent.status_code == 403, agent.text
    invite = api.post(
        "/api/orgs/agency-x/invites",
        headers=OWNER,
        json={"email": "bo@example.com", "role": "member"},
    )
    assert invite.status_code == 403, invite.text
    assert len(outbox) == 0
    with FleetStore(fleet) as store:
        assert [c.id for c in store.list_clients("agency-x")] == ["acme"]
        assert len(store.list_boxes_in("agency-x")) == 1
        with pytest.raises(OrgNotActiveError):
            store.add_person("cy@example.com", "agency-x", "client_viewer", client_id="acme")


def test_an_invite_sent_before_it_was_suspended_joins_nobody_and_is_kept(api, fleet, outbox):
    sent = api.post(
        "/api/orgs/agency-x/invites",
        headers=OWNER,
        json={"email": "bo@example.com", "role": "member"},
    )
    assert sent.status_code == 202, sent.text
    secret = outbox.secret()
    _suspend(fleet)
    refused = api.post("/api/auth/redeem", json={"secret": secret})
    assert refused.status_code in (401, 403), refused.text
    with FleetStore(fleet) as store:
        assert store.get_user_by_email("bo@example.com") is None
        assert store.login_link(secret) is not None
    _resume(fleet)
    assert api.post("/api/auth/redeem", json={"secret": secret}).status_code == 200


def test_nobody_is_moved_into_a_seat(api, fleet):
    """The review of #220: a role change is the other way a free role becomes a
    seat, and the seat limit already counts it as one."""
    with FleetStore(fleet) as store:
        viewer = store.create_user("cy@acme.test")
        store.add_membership(viewer.id, "agency-x", "client_viewer", client_id="acme")
    _suspend(fleet)
    refused = api.patch(
        f"/api/orgs/agency-x/members/{viewer.id}", headers=OWNER, json={"role": "member"}
    )
    assert refused.status_code == 403, refused.text
    assert "suspended" in refused.json()["detail"]
    with FleetStore(fleet) as store:
        assert store.membership(viewer.id, "agency-x").role == "client_viewer"
        with pytest.raises(OrgNotActiveError):
            store.change_membership(viewer.id, "agency-x", "owner")


def test_writing_to_its_agents_says_why_with_a_402(api, fleet, machine):
    """Instructions and rules reach the machine, so they meet the same refusal as
    a wake, and answer it the same way: 402, which is what the web app reads."""
    _suspend(fleet)
    box = _box(fleet)
    answer = api.put(
        f"/api/boxes/{box.id}/instructions", headers=OWNER, json={"instructions": "Be brief."}
    )
    assert answer.status_code == 402, answer.text
    assert "suspended" in answer.json()["detail"]
    rules = api.put(f"/api/boxes/{box.id}/rules", headers=OWNER, json={"always": ["be kind"]})
    assert rules.status_code == 402, rules.text
    assert machine.calls == []


def test_its_people_still_sign_in_and_read(api, fleet):
    _suspend(fleet)
    with FleetStore(fleet) as store:
        secret = store.create_login_link("ana@example.com")
    signed_in = api.post("/api/auth/redeem", json={"secret": secret})
    assert signed_in.status_code == 200, signed_in.text
    me = api.get("/api/me", headers=OWNER)
    assert me.json()["org_status"] == "suspended"
    for path in ("/api/boxes", "/api/orgs/agency-x/limits", f"/api/boxes/{_box(fleet).id}/runs"):
        assert api.get(path, headers=OWNER).status_code == 200, path


# -- resuming ---------------------------------------------------------------------


def test_resuming_restores_it_and_wakes_nothing_itself(api, fleet, machine):
    _suspend(fleet)
    answer = api.post("/api/orgs/agency-x/resume", headers=GOVERNOR)
    assert answer.status_code == 200, answer.text
    assert answer.json()["changed"] is True
    assert machine.calls == []
    woken = api.post(f"/api/boxes/{_box(fleet).id}/wake", headers=OWNER)
    assert woken.status_code == 200, woken.text
    assert machine.calls == ["start"]
    made = api.post(
        "/api/orgs/agency-x/clients", headers=OWNER, json={"id": "beta", "name": "Beta"}
    )
    assert made.status_code == 201, made.text
    with FleetStore(fleet) as store:
        refusal = budgets.check_budgets(
            store, _box(fleet), model="any", eur_per_usd=None, now=datetime.now(UTC)
        )
        assert refusal is None


# -- the transitions, and who is told ---------------------------------------------


def test_suspending_tells_the_owners_once_and_keeps_the_reason(api, fleet, outbox):
    first = api.post("/api/orgs/agency-x/suspend", headers=GOVERNOR, json={"reason": "unpaid"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["changed"], body["told"]) == (True, 1)
    assert (body["org"]["status"], body["org"]["suspend_reason"]) == ("suspended", "unpaid")
    assert body["org"]["suspended_at"]
    assert [m.to for m in outbox] == ["ana@example.com"]
    assert "unpaid" in outbox[0].text and "suspended" in outbox[0].subject

    # Billing's webhook, delivered again.
    again = api.post("/api/orgs/agency-x/suspend", headers=GOVERNOR, json={"reason": "unpaid"})
    assert again.status_code == 200
    assert (again.json()["changed"], again.json()["told"]) == (False, 0)
    assert len(outbox) == 1

    resumed = api.post("/api/orgs/agency-x/resume", headers=OPERATOR)
    assert resumed.json()["org"]["suspend_reason"] is None
    assert resumed.json()["org"]["suspended_at"] is None
    assert [m.subject for m in outbox][1] == "Agency X is running again on Flotta"


def test_its_agents_timelines_say_so(fleet):
    _suspend(fleet, reason="unpaid")
    _resume(fleet)
    with FleetStore(fleet) as store:
        kinds = [e.type for e in store.get_events("box", _box(fleet).id)]
    assert kinds[-2:] == ["org_suspended", "org_resumed"]


@pytest.mark.parametrize("status", ["pending", "declined"])
def test_only_an_active_org_is_suspended(api, fleet, status):
    with FleetStore(fleet) as store:
        store.create_org("new-one", "New", status="pending")
        if status == "declined":
            store.decide_org("new-one", approve=False)
    answer = api.post("/api/orgs/new-one/suspend", headers=GOVERNOR)
    assert answer.status_code == 409, answer.text
    assert api.post("/api/orgs/new-one/resume", headers=GOVERNOR).status_code == 409


def test_the_default_org_is_never_suspended(api):
    answer = api.post("/api/orgs/default/suspend", headers=OPERATOR)
    assert answer.status_code == 409, answer.text


def test_an_owner_cannot_suspend_or_resume_its_own_org(api, fleet):
    assert api.post("/api/orgs/agency-x/suspend", headers=OWNER).status_code == 403
    _suspend(fleet)
    assert api.post("/api/orgs/agency-x/resume", headers=OWNER).status_code == 403


def test_an_unknown_org_is_404_and_a_long_reason_422(api):
    assert api.post("/api/orgs/nope/suspend", headers=GOVERNOR).status_code == 404
    long = api.post("/api/orgs/agency-x/suspend", headers=GOVERNOR, json={"reason": "x" * 501})
    assert long.status_code == 422


# -- who suspended it (FLOTTA-190, M15.8) ---------------------------------------------
#
# Billing lifts only its own suspension (Josh, 11 Nov 2026): an agency the
# operator suspended by hand stays suspended when it next pays, and its owners
# are not told it was resumed.


def _suspend_as(api, who, reason="why"):
    return api.post("/api/orgs/agency-x/suspend", headers=who, json={"reason": reason})


def test_a_suspension_says_who_made_it(api, fleet):
    _suspend_as(api, GOVERNOR)
    assert api.get("/api/orgs", headers=OPERATOR).json()["orgs"][1]["suspended_by"] == "billing"
    with FleetStore(fleet) as store:
        assert store.get_org("agency-x").suspended_by == "billing"


def test_billing_never_lifts_the_operators_suspension(api, fleet, outbox):
    assert _suspend_as(api, OPERATOR, "held by hand").json()["changed"] is True
    told = len(outbox)
    answer = api.post("/api/orgs/agency-x/resume", headers=GOVERNOR)
    assert answer.status_code == 409, answer.text
    assert "operator" in answer.json()["detail"]
    with FleetStore(fleet) as store:
        org = store.get_org("agency-x")
    assert (org.status, org.suspend_reason, org.suspended_by) == (
        "suspended",
        "held by hand",
        "operator",
    )
    assert len(outbox) == told  # nobody is told it was resumed


def test_billing_lifts_its_own(api, fleet):
    _suspend_as(api, GOVERNOR, "the payment failed")
    answer = api.post("/api/orgs/agency-x/resume", headers=GOVERNOR)
    assert answer.status_code == 200 and answer.json()["changed"] is True
    with FleetStore(fleet) as store:
        org = store.get_org("agency-x")
    assert (org.status, org.suspended_by) == ("active", None)


@pytest.mark.parametrize("by", ["billing", "operator"])
def test_the_operator_lifts_either(api, fleet, by):
    _suspend_as(api, GOVERNOR if by == "billing" else OPERATOR)
    assert api.post("/api/orgs/agency-x/resume", headers=OPERATOR).json()["changed"] is True


def test_suspending_again_keeps_who_did_it_first(api, fleet):
    """A billing webhook after the operator's hold must not turn it into billing's."""
    _suspend_as(api, OPERATOR, "held by hand")
    assert _suspend_as(api, GOVERNOR, "the payment failed").json()["changed"] is False
    with FleetStore(fleet) as store:
        org = store.get_org("agency-x")
    assert (org.suspended_by, org.suspend_reason) == ("operator", "held by hand")
    assert api.post("/api/orgs/agency-x/resume", headers=GOVERNOR).status_code == 409


def test_the_operator_holding_billings_suspension_makes_it_theirs(api, fleet, outbox):
    """Billing suspended it for a failed payment; the operator then holds it by hand."""
    _suspend_as(api, GOVERNOR, "the payment failed")
    told = len(outbox)
    answer = _suspend_as(api, OPERATOR, "held for a dispute")
    assert answer.json()["changed"] is False
    with FleetStore(fleet) as store:
        org = store.get_org("agency-x")
    assert (org.status, org.suspended_by, org.suspend_reason) == (
        "suspended",
        "operator",
        "held for a dispute",
    )
    assert len(outbox) == told  # suspended already: nobody is told again
    assert api.post("/api/orgs/agency-x/resume", headers=GOVERNOR).status_code == 409


def test_a_suspension_from_before_the_column_is_the_operators(api, fleet):
    """Billing wasn't deployed before migration 35: any suspension then was made by hand."""
    with FleetStore(fleet) as store:
        store.suspend_org("agency-x", reason="held by hand", by="cli")
        store._conn.execute("UPDATE orgs SET suspended_by = NULL WHERE id = 'agency-x'")
    assert api.post("/api/orgs/agency-x/resume", headers=GOVERNOR).status_code == 409
    assert api.post("/api/orgs/agency-x/resume", headers=OPERATOR).json()["changed"] is True


def test_an_operator_token_named_billing_is_still_the_operator(api, fleet):
    scopes = {SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN}
    named_billing = {"Authorization": f"Bearer {mint(subject='billing', scopes=scopes, key=KEY)}"}
    _suspend_as(api, named_billing, "held by hand")
    with FleetStore(fleet) as store:
        assert store.get_org("agency-x").suspended_by == "operator"
    assert api.post("/api/orgs/agency-x/resume", headers=GOVERNOR).status_code == 409
