"""A model change that answers at once and says where it stands (FLOTTA-99).

Found in FLOTTA-86's live check: changing an agent's model showed "the control
plane did not answer" while the change was in fact going fine, because the
route waited for Fly to restart the agent and the web app gives a call ten
seconds. Agents made before FLOTTA-39 also claimed to run whatever default the
operator chose afterwards, so "an earlier fleet default" never appeared.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from flotta.backend import BackendError
from flotta.store import Event, FleetStore

DEFAULT = "z-ai/glm-5.2"
OWN = "anthropic/claude-sonnet-4.5"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOTTA_MODEL", DEFAULT)
    monkeypatch.setenv("FLOTTA_MODELS", OWN)
    with FleetStore(tmp_path / "fleet.db") as s:
        yield s


@pytest.fixture
def box(store):
    created = store.create_box("eng-a")
    store.update_box_status(created.id, "running", endpoint="fake://app/m1")
    return store.get_box(created.id)


class Machine:
    scheme = "fake"

    def __init__(self, *, fails=False):
        self.fails = fails
        self.applied: dict[str, str] = {}

    def apply_secrets(self, endpoint, secrets):
        if self.fails:
            raise BackendError("flyctl secrets import timed out")
        self.applied.update(secrets)


def _event(type_, ts, **payload):
    return Event(id=1, entity_kind="box", entity_id="b-1", ts=ts, type=type_, payload=payload)


# -- starting one -------------------------------------------------------------


def test_a_started_change_is_under_way_until_it_is_applied(store, box):
    from flotta.provision import begin_model_change, model_changes, set_model

    started = begin_model_change(box.id, OWN, store=store)
    assert started == {"box_id": box.id, "name": "eng-a", "model": OWN, "source": "agent"}
    assert model_changes(store, [box.id])[box.id]["state"] == "pending"
    # Nothing is written to the machine or the record until the thread runs.
    assert store.model_for_box(box.id) is None

    set_model(box.id, OWN, store=store, backend=Machine())
    assert model_changes(store, [box.id]) == {}


def test_a_second_change_waits_for_the_first(store, box):
    """Two `secrets import`s racing: the machine would keep whichever landed
    last, whatever the record says."""
    from flotta.provision import ProvisionError, begin_model_change

    begin_model_change(box.id, OWN, store=store)
    with pytest.raises(ProvisionError, match="already switching to anthropic/claude-sonnet-4.5"):
        begin_model_change(box.id, None, store=store)


def test_a_refused_change_is_refused_before_it_is_started(store, box):
    from flotta.inference import InvalidModel
    from flotta.provision import begin_model_change

    with pytest.raises(InvalidModel):
        begin_model_change(box.id, "openai/o1-pro", store=store)
    assert not [e for e in store.get_events("box", box.id) if e.type == "model_change_started"]


def test_a_change_the_machine_refused_says_so(store, box):
    from flotta.provision import UpgradeFailed, begin_model_change, model_changes, set_model

    begin_model_change(box.id, OWN, store=store)
    with pytest.raises(UpgradeFailed):
        set_model(box.id, OWN, store=store, backend=Machine(fails=True))

    change = model_changes(store, [box.id])[box.id]
    assert change["state"] == "failed"
    assert change["model"] == OWN
    assert "timed out" in change["detail"]
    # The next change is not blocked by one that ended.
    begin_model_change(box.id, OWN, store=store)


def test_a_start_with_no_outcome_stops_counting_as_under_way():
    """The control plane restarting mid-change leaves a start and nothing
    else; "restarting" for ever would be the window lying the other way."""
    from flotta.provision import MODEL_CHANGE_STALE_S, model_change_of

    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    recent = (now - timedelta(seconds=30)).isoformat()
    old = (now - timedelta(seconds=MODEL_CHANGE_STALE_S + 1)).isoformat()

    assert model_change_of(_event("model_change_started", recent, model=OWN), now=now) == {
        "model": OWN,
        "at": recent,
        "state": "pending",
    }
    assert model_change_of(_event("model_change_started", old, model=OWN), now=now) is None
    assert model_change_of(_event("model_set", recent, model=OWN), now=now) is None
    assert model_change_of(None, now=now) is None


# -- what an agent with no record runs ----------------------------------------


def test_an_agent_with_no_record_was_given_the_environments_model(monkeypatch):
    from flotta.provision import model_given_by_default

    monkeypatch.setenv("FLOTTA_MODEL", " z-ai/glm-5.2 ")
    assert model_given_by_default() == DEFAULT
    assert model_given_by_default({}) is None
