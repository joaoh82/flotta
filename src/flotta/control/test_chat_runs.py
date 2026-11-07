"""The routes the door records a conversation's turns with (FLOTTA-151, M14.6).

`POST /api/boxes/{id}/chat-runs` when a turn begins and
`POST /api/boxes/{id}/runs/{run}/steps` when it ends, both with the door's
`fleet:route` token. The guard: a turn is ended only on the box it began on,
and only a turn. What the door hears is in `door/test_turns.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_ROUTE, SCOPE_FLEET_WRITE
from flotta.control import test_tenancy
from flotta.control.test_tenancy import X, Y, _bearer, _ids
from flotta.store import FleetStore

DOOR = _bearer(SCOPE_FLEET_ROUTE, subject="door")
AGENCY_X = _bearer(SCOPE_FLEET_WRITE, SCOPE_FLEET_READ, org="agency-x")

fleet = test_tenancy.fleet
api = test_tenancy.api

TOOL = {"kind": "tool", "name": "terminal", "exit_code": 0, "at": "2026-11-07T12:00:00+00:00"}


def _open(api, box=X, headers=DOOR, **body):
    body = {"prompt": "What changed?", "session_id": "live-1", "by": "user:u-1", **body}
    return api.post(f"/api/boxes/{box}/chat-runs", json=body, headers=headers)


def _end(api, run_id, box=X, headers=DOOR, **body):
    body = {"steps": [TOOL], "status": "done", "output": "Nothing.", "approvals": 0, **body}
    return api.post(f"/api/boxes/{box}/runs/{run_id}/steps", json=body, headers=headers)


# -- the guards ----------------------------------------------------------------


def test_a_turn_is_ended_only_on_the_box_it_began_on(api, fleet):
    """The guard: the door names the box whose socket it heard. Another box's
    turn is a 404, and nothing of it changes."""
    mine = _open(api, X).json()

    refused = _end(api, mine["id"], box=Y, output="Mine now.")

    assert refused.status_code == 404
    with FleetStore(fleet) as store:
        assert store.get_run(mine["id"]).status == "running"
    assert _end(api, mine["id"], box=X).status_code == 200


def test_only_a_turn_is_ended_this_way(api, fleet):
    """A run the control plane delivers is its own to end, not the door's."""
    delivered = api.post(f"/api/boxes/{X}/runs", json={"prompt": "Summarise."}, headers=AGENCY_X)
    run_id = delivered.json()["id"]

    assert _end(api, run_id).status_code == 404
    with FleetStore(fleet) as store:
        assert store.get_run(run_id).status == "pending"


def test_only_the_door_records_a_turn(api):
    """`fleet:route`, the operator's: an agency's token cannot open or end one."""
    assert _open(api, headers=AGENCY_X).status_code == 403
    opened = _open(api).json()
    assert _end(api, opened["id"], headers=AGENCY_X).status_code == 403
    tenant_route = _bearer(SCOPE_FLEET_ROUTE, org="agency-y")
    assert _open(api, X, headers=tenant_route).status_code == 404


# -- what a turn is ------------------------------------------------------------


def test_a_turn_is_read_back_as_a_chat_run_with_its_steps(api, fleet):
    opened = _open(api, by="user:u-9", client=True)

    assert opened.status_code == 201, opened.text
    assert opened.json()["status"] == "running"
    ended = _end(api, opened.json()["id"])
    assert ended.status_code == 200 and ended.json()["status"] == "done"

    (run,) = api.get(f"/api/boxes/{X}/runs", headers=AGENCY_X).json()["runs"]
    assert (run["trigger_kind"], run["prompt"], run["output"]) == (
        "chat",
        "What changed?",
        "Nothing.",
    )
    assert run["input"] == {"session_id": "live-1", "by": "user:u-9", "client": True}
    assert run["steps"]["steps"][0]["name"] == "terminal"
    read = api.get(f"/api/runs/{run['id']}", headers=AGENCY_X).json()
    assert read["cost"]["calls"] == 0


def test_a_turn_while_the_agent_does_another_run_is_409(api, fleet):
    with FleetStore(fleet) as store:
        delivered = store.create_run(_ids(fleet)[X], "Summarise.", trigger_kind="api")
        store.start_run(delivered.id)

    assert _open(api).status_code == 409


def test_a_turn_ended_twice_is_409(api):
    opened = _open(api).json()
    assert _end(api, opened["id"]).status_code == 200

    assert _end(api, opened["id"]).status_code == 409


def test_a_turn_without_a_prompt_says_so(api):
    opened = _open(api, prompt="").json()

    (run,) = api.get(f"/api/boxes/{X}/runs", headers=AGENCY_X).json()["runs"]
    assert run["id"] == opened["id"] and "without a prompt" in run["prompt"]


def test_what_the_door_sends_is_checked(api):
    assert _open(api, session_id="").status_code == 422
    assert _open(api, by="x" * 201).status_code == 422
    opened = _open(api).json()
    assert _end(api, opened["id"], status="cancelled").status_code == 422
    assert _end(api, opened["id"], steps=["not a step"]).status_code == 422


def test_a_turns_steps_are_kept_as_a_runs_are():
    """Capped by the store, like a delivered run's."""
    from flotta.runs import MAX_STEPS

    with FleetStore(":memory:") as store:
        box = store.create_box("eng-a")
        turn = store.open_chat_run(box.id, "Loop.", now=datetime.now(UTC) - timedelta(seconds=5))
        ended = store.finish_chat_run(
            turn.id, box.id, "done", steps=[dict(TOOL, n=n) for n in range(MAX_STEPS + 50)]
        )

    assert len(ended.steps["steps"]) == MAX_STEPS and ended.steps["omitted"] == 50


def test_a_turn_keeps_the_times_the_door_heard(api, fleet):
    """Found in review: the door's calls land after the turn, and the run's
    times are the turn's, or its first model call falls outside it."""
    began = datetime.now(UTC) - timedelta(seconds=30)
    ended = began + timedelta(seconds=12)

    opened = _open(api, started_at=began.isoformat()).json()
    _end(api, opened["id"], ended_at=ended.isoformat())

    with FleetStore(fleet) as store:
        run = store.get_run(opened["id"])
    assert datetime.fromisoformat(run.started_at) == began
    assert datetime.fromisoformat(run.finished_at) == ended


def test_a_time_that_is_not_this_turns_is_refused(api):
    now = datetime.now(UTC)
    for bad in (
        "yesterday",
        now.replace(tzinfo=None).isoformat(),
        (now - timedelta(days=1)).isoformat(),
        (now + timedelta(minutes=5)).isoformat(),
    ):
        assert _open(api, started_at=bad).status_code == 422, bad
