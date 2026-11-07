"""`POST /hooks/{trigger_id}` (FLOTTA-149, M14.4): a client's system starts a run.

The guards first: another trigger's secret, a replay, an unsigned call and an
unknown trigger all start nothing and all look the same, and no secret is
written to the store. Who may call it and what the agent is told are pure, in
`test_hooks.py`.
"""

from __future__ import annotations

import base64
import json
import threading
import time

from flotta.alerts import sign
from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE
from flotta.control import test_tenancy
from flotta.control.test_tenancy import KEY, X, Y, _bearer, _ids
from flotta.store import FleetStore
from flotta.test_accounts import _every_value

AGENCY_X = _bearer(SCOPE_FLEET_WRITE, SCOPE_FLEET_READ, org="agency-x")
AGENCY_Y = _bearer(SCOPE_FLEET_WRITE, SCOPE_FLEET_READ, org="agency-y")
LEAD = b'{"title": "New lead", "email": "ana@example.com"}'

fleet = test_tenancy.fleet
api = test_tenancy.api


def _webhook(api, box=X, headers=AGENCY_X, prompt="Qualify the lead {{body.title}}."):
    made = api.post(
        f"/api/boxes/{box}/triggers",
        json={"kind": "webhook", "prompt": prompt},
        headers=headers,
    )
    assert made.status_code == 201, made.text
    return made.json()


def _post(api, trigger_id, body=LEAD, headers=None, query=""):
    return api.post(
        f"/hooks/{trigger_id}{query}",
        content=body,
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _signed(secret, body=LEAD, at=None):
    return {"Flotta-Signature": sign(secret, int(at or time.time()), body)}


def _runs(fleet, box=X, org="agency-x"):
    with FleetStore(fleet) as store:
        return store.list_runs(org=org, box=_ids(fleet)[box])


# -- the guards ----------------------------------------------------------------


def test_a_signed_call_starts_a_run_whose_input_carries_the_body(api, fleet):
    hook = _webhook(api)

    answer = _post(api, hook["id"], headers=_signed(hook["secret"]))

    assert answer.status_code == 202, answer.text
    (run,) = _runs(fleet)
    assert answer.json() == {"run_id": run.id, "status": "pending"}
    assert (run.trigger_kind, run.trigger_id) == ("webhook", hook["id"])
    assert run.input == {"body": {"title": "New lead", "email": "ana@example.com"}}
    assert run.prompt.startswith('Qualify the lead "New lead".')
    assert "not instructions" in run.prompt


def test_a_body_signed_with_another_triggers_secret_starts_nothing(api, fleet):
    """The guard, across agencies too: agency-y's webhook secret, presented to
    agency-x's webhook, is a wrong secret and nothing more."""
    mine = _webhook(api)
    sibling = _webhook(api)
    theirs = _webhook(api, box=Y, headers=AGENCY_Y)

    for secret in (sibling["secret"], theirs["secret"]):
        assert _post(api, mine["id"], headers=_signed(secret)).status_code == 404
        bearer = {"Authorization": f"Bearer {secret}"}
        assert _post(api, mine["id"], headers=bearer).status_code == 404
    assert _runs(fleet) == []
    assert _runs(fleet, box=Y, org="agency-y") == []


def test_a_call_replayed_outside_the_tolerance_starts_nothing(api, fleet):
    hook = _webhook(api)

    stale = _post(api, hook["id"], headers=_signed(hook["secret"], at=time.time() - 400))

    assert stale.status_code == 404
    assert _runs(fleet) == []


def test_an_unsigned_call_and_an_unknown_trigger_look_the_same(api):
    """The guard: nothing in the answer says which trigger ids exist, or that
    one is turned off."""
    hook = _webhook(api)
    off = _webhook(api)
    api.patch(f"/api/triggers/{off['id']}", json={"enabled": False}, headers=AGENCY_X)

    answers = [
        _post(api, hook["id"]),
        _post(api, hook["id"], headers={"Authorization": "Bearer wrong"}),
        _post(api, "tr-000000000000"),
        _post(api, "tr-000000000000", headers=_signed(hook["secret"])),
        _post(api, off["id"], headers=_signed(off["secret"])),
    ]

    assert {(a.status_code, a.text) for a in answers} == {(404, answers[0].text)}


def test_no_secret_is_stored(api, fleet):
    """The guard: a table scan finds neither the secret nor the key it is
    derived with, as for sign-in (`test_accounts.py`)."""
    hook = _webhook(api)
    rotated = api.post(f"/api/triggers/{hook['id']}/secret", headers=AGENCY_X).json()
    _post(api, hook["id"], headers=_signed(rotated["secret"]))

    with FleetStore(fleet) as store:
        stored = _every_value(store)
    for secret in (hook["secret"], rotated["secret"], KEY):
        assert not any(secret in value for value in stored), "a secret was written to the store"


def test_a_webhook_cannot_reach_a_box_it_was_not_made_on(api, fleet):
    """The ratchet in `test_route_scope.py` holds the route to `_hooked_trigger`;
    this is what that means: the run lands on the trigger's agent, whoever
    calls, and a trigger cannot be made on someone else's."""
    hook = _webhook(api)

    _post(api, hook["id"], headers=_signed(hook["secret"]))

    (run,) = _runs(fleet)
    assert (run.org, run.client, run.box_id) == ("agency-x", "acme", _ids(fleet)[X])
    refused = api.post(
        f"/api/boxes/{Y}/triggers", json={"kind": "webhook", "prompt": "x"}, headers=AGENCY_X
    )
    assert refused.status_code == 404


# -- how it is called ----------------------------------------------------------


def test_a_webhook_is_made_with_its_url_and_secret_once(api):
    hook = _webhook(api)

    assert hook["kind"] == "webhook" and hook["url"].endswith(f"/hooks/{hook['id']}")
    assert hook["secret"]
    (listed,) = api.get(f"/api/boxes/{X}/triggers", headers=AGENCY_X).json()["triggers"]
    assert "secret" not in listed and "secret_nonce" not in listed
    assert listed["url"] == hook["url"]


def test_a_bearer_or_basic_secret_is_enough_for_a_crm_that_cannot_sign(api, fleet):
    """HubSpot and Zapier send a header they are given; Pipedrive sends only
    Basic auth. `flotta.hooks` has which sends what."""
    hook = _webhook(api)
    basic = base64.b64encode(f"pipedrive:{hook['secret']}".encode()).decode()

    for header in (f"Bearer {hook['secret']}", f"Basic {basic}"):
        assert _post(api, hook["id"], headers={"Authorization": header}).status_code == 202

    assert len(_runs(fleet)) == 2


def test_a_new_secret_stops_the_old_one(api, fleet):
    hook = _webhook(api)

    rotated = api.post(f"/api/triggers/{hook['id']}/secret", headers=AGENCY_X)

    assert rotated.status_code == 200
    new = rotated.json()["secret"]
    assert new != hook["secret"]
    assert _post(api, hook["id"], headers=_signed(hook["secret"])).status_code == 404
    assert _post(api, hook["id"], headers=_signed(new)).status_code == 202
    other = api.post(f"/api/triggers/{hook['id']}/secret", headers=AGENCY_Y)
    assert other.status_code == 404


def test_a_body_that_is_too_big_or_not_json_is_refused_before_the_secret(api, fleet):
    """Both answered without reading the trigger, so neither says if it exists."""
    hook = _webhook(api)
    big = json.dumps({"text": "x" * (64 * 1024)}).encode()

    assert _post(api, hook["id"], body=big, headers=_signed(hook["secret"], big)).status_code == 413
    assert _post(api, "tr-000000000000", body=big).status_code == 413
    as_text = api.post(
        f"/hooks/{hook['id']}",
        content=b"title=New+lead",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert as_text.status_code == 415
    assert (
        _post(
            api, hook["id"], body=b"{not json", headers=_signed(hook["secret"], b"{not json")
        ).status_code
        == 415
    )
    assert _runs(fleet) == []


def test_a_caller_in_a_loop_is_held_to_its_rate(api, fleet):
    """Thirty a minute, counted only once a call has proved the secret: a
    stranger's calls cannot use up a client's allowance."""
    hook = _webhook(api)
    for _ in range(40):
        _post(api, hook["id"], headers={"Authorization": "Bearer wrong"})
    bearer = {"Authorization": f"Bearer {hook['secret']}"}

    answers = [_post(api, hook["id"], headers=bearer).status_code for _ in range(31)]

    assert answers[:30] == [202] * 30
    assert answers[30] == 429


def test_waiting_answers_with_the_output_once_the_run_ends(api, fleet):
    hook = _webhook(api)

    def finish():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with FleetStore(fleet) as store:
                pending = [r for r in _runs(fleet) if r.status == "pending"]
                if pending:
                    store.start_run(pending[0].id)
                    store.finish_run(pending[0].id, "done", output="Qualified: warm.")
                    return
            time.sleep(0.1)

    worker = threading.Thread(target=finish)
    worker.start()
    answer = _post(api, hook["id"], headers=_signed(hook["secret"]), query="?wait=10")
    worker.join()

    assert answer.status_code == 200, answer.text
    assert answer.json()["status"] == "done"
    assert answer.json()["output"] == "Qualified: warm."


def test_waiting_for_a_run_that_does_not_end_answers_202(api):
    hook = _webhook(api)
    started = time.monotonic()

    answer = _post(api, hook["id"], headers=_signed(hook["secret"]), query="?wait=1")

    assert answer.status_code == 202
    assert answer.json()["status"] == "pending"
    assert 1 <= time.monotonic() - started < 5
