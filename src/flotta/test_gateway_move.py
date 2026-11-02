"""Agents call models through the gateway, and hold no provider key (FLOTTA-90, M13.4).

The first tests are the guards. **What a box is given, and what its boot step writes
from that, never contains the fleet's provider key.**
"""

from __future__ import annotations

import json

import pytest
import yaml

from flotta.auth import SCOPE_GIT_CREDENTIAL, SCOPE_MODEL_CALL, box_subject, verify
from flotta.backend import BackendError
from flotta.box.inference import apply
from flotta.inference import GATEWAY_PROVIDER, gateway_provider
from flotta.provision import (
    GATEWAY_JOINED,
    GATEWAY_LEFT,
    GATEWAY_MOVE_FAILED,
    MODEL_CHANGE_FAILED,
    MODEL_CHANGE_STARTED,
    MODEL_SET,
    MODEL_TOKEN_TTL_S,
    ProvisionError,
    UpgradeFailed,
    fleet_secrets,
    gateway_url,
    join_gateway,
    leave_gateway,
)
from flotta.store import FleetStore

KEY = "a-signing-key-for-these-tests"
FLEET_KEY = "sk-or-the-fleets-own-key"
GATEWAY = "https://gateway.example"
OPENROUTER = "https://openrouter.ai/api/v1"
MODEL = "anthropic/claude-sonnet-5"


def _env(**overrides):
    env = {
        "FLOTTA_SIGNING_KEY": KEY,
        "FLOTTA_BOX_PASSWORD": "fleet-password",
        "FLOTTA_MODEL": MODEL,
        "FLOTTA_MODEL_BASE_URL": OPENROUTER,
        "FLOTTA_API_KEY": FLEET_KEY,
        "FLOTTA_GATEWAY_URL": GATEWAY,
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def _everything_written(home):
    return "\n".join(p.read_text() for p in home.iterdir() if p.is_file())


# -- the guards ----------------------------------------------------------------


def test_what_a_box_is_given_never_contains_the_fleets_key(tmp_path):
    secrets, missing = fleet_secrets(_env(), box_id="b-1")
    assert missing == []
    assert FLEET_KEY not in json.dumps(secrets)
    assert "OPENROUTER_API_KEY" not in secrets


def test_what_its_boot_step_writes_never_contains_the_fleets_key(tmp_path):
    secrets, _ = fleet_secrets(_env(), box_id="b-1")
    apply(tmp_path, secrets)
    written = _everything_written(tmp_path)
    assert FLEET_KEY not in written
    block = yaml.safe_load(written)["model"]
    assert block["provider"] == "custom"
    assert block["base_url"] == f"{GATEWAY}/v1"
    assert block["default"] == MODEL


def test_the_config_holds_only_the_boxs_own_token_for_the_gateway(tmp_path):
    secrets, _ = fleet_secrets(_env(), box_id="b-1")
    apply(tmp_path, secrets)
    token = verify(yaml.safe_load(_everything_written(tmp_path))["model"]["api_key"], key=KEY)
    assert token.subject == box_subject("b-1")
    # Only this: the file is read by a process that talks to the network.
    assert token.scopes == frozenset({SCOPE_MODEL_CALL})
    assert SCOPE_GIT_CREDENTIAL not in token.scopes


def test_a_box_of_another_org_carries_its_tenancy(tmp_path):
    secrets, _ = fleet_secrets(_env(), box_id="b-1", org="studio-nord", client="acme")
    token = verify(secrets["FLOTTA_API_KEY"], key=KEY)
    assert (token.org, token.client) == ("studio-nord", "acme")


def test_the_token_outlives_the_git_identity(tmp_path):
    """Nothing renews it, and an expired one stops every model call the agent makes.
    The gateway refuses a destroyed agent, so a long life is not a long reach."""
    from flotta.provision import BOX_TOKEN_TTL_S

    secrets, _ = fleet_secrets(_env(), box_id="b-1")
    token = verify(secrets["FLOTTA_API_KEY"], key=KEY)
    assert token.expires_at - token.issued_at == MODEL_TOKEN_TTL_S
    assert MODEL_TOKEN_TTL_S > 10 * BOX_TOKEN_TTL_S


# -- until the switch is thrown, nothing changes -----------------------------------


def test_with_no_gateway_a_box_is_given_what_it_always_was(tmp_path):
    secrets, missing = fleet_secrets(_env(FLOTTA_GATEWAY_URL=None), box_id="b-1")
    assert missing == []
    assert secrets["FLOTTA_API_KEY"] == FLEET_KEY
    assert secrets["OPENROUTER_API_KEY"] == FLEET_KEY
    assert secrets["FLOTTA_MODEL_BASE_URL"] == OPENROUTER
    assert "FLOTTA_GATEWAY_UPSTREAM" not in secrets


def test_with_no_gateway_the_config_is_what_it_always_was(tmp_path):
    secrets, _ = fleet_secrets(_env(FLOTTA_GATEWAY_URL=None), box_id="b-1")
    apply(tmp_path, secrets)
    config = yaml.safe_load(_everything_written(tmp_path))
    assert config["model"]["provider"] == "openrouter"
    assert "providers" not in config


def test_with_a_gateway_the_control_plane_needs_no_provider_key_to_make_a_box():
    secrets, missing = fleet_secrets(
        _env(FLOTTA_API_KEY=None, FLOTTA_MODEL_BASE_URL=None), box_id="b-1"
    )
    assert missing == []
    assert secrets["FLOTTA_MODEL_BASE_URL"] == f"{GATEWAY}/v1"


def test_with_a_gateway_and_no_signing_key_it_says_what_is_missing():
    """Better than an agent that boots with no way to call a model."""
    secrets, missing = fleet_secrets(_env(FLOTTA_SIGNING_KEY=None), box_id="b-1")
    assert any("FLOTTA_SIGNING_KEY" in m for m in missing)
    assert FLEET_KEY not in json.dumps(secrets)
    assert "FLOTTA_API_KEY" not in secrets


def test_with_a_gateway_and_no_model_it_says_what_is_missing():
    _, missing = fleet_secrets(_env(FLOTTA_MODEL=None), box_id="b-1")
    assert any("FLOTTA_MODEL" in m for m in missing)


@pytest.mark.parametrize(
    ("given", "wanted"),
    [
        ("https://gateway.example", "https://gateway.example/v1"),
        ("https://gateway.example/", "https://gateway.example/v1"),
        ("https://gateway.example/v1", "https://gateway.example/v1"),
        ("https://gateway.example/v1/", "https://gateway.example/v1"),
        ("  ", None),
        ("", None),
    ],
)
def test_the_gateway_url_is_the_one_hermes_appends_to(given, wanted):
    assert gateway_url({"FLOTTA_GATEWAY_URL": given}) == wanted


# -- what Hermes stops doing off OpenRouter's URL --------------------------------------


@pytest.mark.parametrize(
    "model", ["anthropic/claude-sonnet-5", "anthropic/claude-opus-5.5", "moonshotai/kimi-k2.6"]
)
def test_prompt_caching_is_asked_for_where_hermes_would_on_openrouter(model):
    provider = gateway_provider(model, f"{GATEWAY}/v1")
    assert provider["api"] == f"{GATEWAY}/v1"
    assert provider["transport"] == "openai_chat"
    assert provider["models"][model]["prompt_caching"] is True


@pytest.mark.parametrize("model", ["z-ai/glm-5.2", "openai/gpt-6", "deepseek/deepseek-chat"])
def test_prompt_caching_is_not_asked_for_elsewhere(model):
    """Hermes's own comment: strict relays reject the block format for other models."""
    provider = gateway_provider(model, f"{GATEWAY}/v1")
    assert "models" not in provider


def test_reasoning_is_kept_for_claude():
    """Hermes sends this itself on OpenRouter's URL, and not on any other."""
    provider = gateway_provider("anthropic/claude-sonnet-5", f"{GATEWAY}/v1")
    assert provider["extra_body"]["reasoning"] == {"enabled": True, "effort": "medium"}


@pytest.mark.parametrize("model", ["z-ai/glm-5.2", "moonshotai/kimi-k2.6"])
def test_reasoning_is_not_forced_on_models_it_might_400_on(model):
    assert "extra_body" not in gateway_provider(model, f"{GATEWAY}/v1")


def test_the_box_writes_that_provider_when_the_gateway_fronts_openrouter(tmp_path):
    secrets, _ = fleet_secrets(_env(), box_id="b-1")
    assert secrets["FLOTTA_GATEWAY_UPSTREAM"] == "openrouter"
    apply(tmp_path, secrets)
    providers = yaml.safe_load(_everything_written(tmp_path))["providers"]
    assert providers[GATEWAY_PROVIDER]["models"][MODEL]["prompt_caching"] is True


def test_a_gateway_in_front_of_something_else_writes_no_such_provider(tmp_path):
    """Cache markers are OpenRouter's rule. A self-hosted model may refuse them."""
    secrets, _ = fleet_secrets(_env(FLOTTA_MODEL_BASE_URL="https://llm.internal/v1"), box_id="b-1")
    assert "FLOTTA_GATEWAY_UPSTREAM" not in secrets
    apply(tmp_path, secrets)
    assert "providers" not in yaml.safe_load(_everything_written(tmp_path))


def test_an_agent_moved_on_an_old_image_gets_the_provider_when_it_is_upgraded(tmp_path):
    """The model block is already right, so only the provider is new. It has to be
    written anyway, or caching stays off after the upgrade that was meant to fix it."""
    secrets, _ = fleet_secrets(_env(), box_id="b-1")
    settled = {k: v for k, v in secrets.items() if k != "FLOTTA_GATEWAY_UPSTREAM"}
    apply(tmp_path, settled)
    assert "providers" not in yaml.safe_load(_everything_written(tmp_path))

    apply(tmp_path, secrets)

    assert GATEWAY_PROVIDER in yaml.safe_load(_everything_written(tmp_path))["providers"]


def test_a_person_s_own_providers_are_kept(tmp_path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"providers": {"mine": {"api": "https://mine.example"}}})
    )
    secrets, _ = fleet_secrets(_env(), box_id="b-1")
    apply(tmp_path, secrets)
    providers = yaml.safe_load(_everything_written(tmp_path))["providers"]
    assert providers["mine"] == {"api": "https://mine.example"}
    assert GATEWAY_PROVIDER in providers


def test_leaving_the_gateway_removes_its_provider_and_its_token(tmp_path):
    secrets, _ = fleet_secrets(_env(), box_id="b-1")
    apply(tmp_path, secrets)
    direct, _ = fleet_secrets(_env(FLOTTA_GATEWAY_URL=None), box_id="b-1")
    apply(tmp_path, direct)
    config = yaml.safe_load(_everything_written(tmp_path))
    assert config["model"]["provider"] == "openrouter"
    assert "api_key" not in config["model"]
    assert "providers" not in config


def test_a_model_change_on_the_gateway_settles_the_provider_again(tmp_path):
    secrets, _ = fleet_secrets(_env(), box_id="b-1")
    apply(tmp_path, secrets)
    apply(tmp_path, {**secrets, "FLOTTA_MODEL": "z-ai/glm-5.2"})
    providers = yaml.safe_load(_everything_written(tmp_path))["providers"]
    assert "models" not in providers[GATEWAY_PROVIDER]


def test_the_boot_line_never_contains_a_token(tmp_path):
    secrets, _ = fleet_secrets(_env(), box_id="b-1")
    assert secrets["FLOTTA_API_KEY"] not in apply(tmp_path, secrets)


# -- moving an existing agent ----------------------------------------------------------


class _Machine:
    def __init__(self, *, apply_fails=False, remove_fails=False):
        self.calls = []
        self.apply_fails = apply_fails
        self.remove_fails = remove_fails

    def apply_secrets(self, box_id, secrets):
        self.calls.append(("apply", box_id, dict(secrets)))
        if self.apply_fails:
            raise BackendError("flyctl had a bad minute")

    def remove_secrets(self, box_id, names):
        self.calls.append(("remove", box_id, sorted(names)))
        if self.remove_fails:
            raise BackendError("flyctl had a bad minute")


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    for key, value in _env().items():
        monkeypatch.setenv(key, value)
    with FleetStore(tmp_path / "fleet.db") as store:
        store.create_org("studio-nord", "Studio Nord")
        store.create_client("studio-nord", "acme", "Acme Dental")
        box = store.create_box("eng", box_id="b-1", org="studio-nord", client="acme")
        store.update_box_status(box.id, "running", endpoint="fly://app/m1")
        store.set_box_model(box.id, "z-ai/glm-5.2")
        yield store


def test_joining_writes_the_gateway_then_removes_the_key_in_that_order(fleet):
    """The other order leaves an agent on OpenRouter with no key to call it with."""
    machine = _Machine()
    join_gateway("b-1", store=fleet, backend=machine)
    assert [call[0] for call in machine.calls] == ["apply", "remove"]
    _, endpoint, secrets = machine.calls[0]
    assert endpoint == "fly://app/m1"
    assert secrets["FLOTTA_MODEL_BASE_URL"] == f"{GATEWAY}/v1"
    assert secrets["FLOTTA_MODEL"] == "z-ai/glm-5.2"
    assert FLEET_KEY not in json.dumps(secrets)
    assert machine.calls[1][2] == ["OPENROUTER_API_KEY"]


def test_joining_mints_for_this_agents_tenancy(fleet):
    machine = _Machine()
    join_gateway("b-1", store=fleet, backend=machine)
    token = verify(machine.calls[0][2]["FLOTTA_API_KEY"], key=KEY)
    assert token.subject == box_subject("b-1")
    assert (token.org, token.client) == ("studio-nord", "acme")


def test_joining_is_on_the_agents_timeline(fleet):
    join_gateway("b-1", store=fleet, backend=_Machine())
    assert GATEWAY_JOINED in [e.type for e in fleet.get_events("box", "b-1")]


def test_a_failed_write_leaves_the_key_where_it_is_and_says_so(fleet):
    machine = _Machine(apply_fails=True)
    with pytest.raises(UpgradeFailed, match="still calls"):
        join_gateway("b-1", store=fleet, backend=machine)
    assert [call[0] for call in machine.calls] == ["apply"]
    types = [e.type for e in fleet.get_events("box", "b-1")]
    assert GATEWAY_MOVE_FAILED in types
    assert GATEWAY_JOINED not in types


def test_a_key_that_could_not_be_removed_is_not_called_done(fleet):
    """It works, and the point of the move has not happened."""
    with pytest.raises(UpgradeFailed, match="still holds"):
        join_gateway("b-1", store=fleet, backend=_Machine(remove_fails=True))
    types = [e.type for e in fleet.get_events("box", "b-1")]
    assert GATEWAY_MOVE_FAILED in types
    assert GATEWAY_JOINED not in types


def test_joining_with_no_gateway_configured_is_refused(fleet, monkeypatch):
    monkeypatch.delenv("FLOTTA_GATEWAY_URL")
    machine = _Machine()
    with pytest.raises(ProvisionError, match="FLOTTA_GATEWAY_URL"):
        join_gateway("b-1", store=fleet, backend=machine)
    assert machine.calls == []


def test_joining_with_no_signing_key_is_refused(fleet, monkeypatch):
    monkeypatch.delenv("FLOTTA_SIGNING_KEY")
    machine = _Machine()
    with pytest.raises(ProvisionError, match="FLOTTA_SIGNING_KEY"):
        join_gateway("b-1", store=fleet, backend=machine)
    assert machine.calls == []


@pytest.mark.parametrize("verb", [join_gateway, leave_gateway])
def test_an_agent_with_no_settled_machine_is_refused(fleet, verb):
    box = fleet.create_box("new", box_id="b-2", org="studio-nord", client="acme")
    machine = _Machine()
    with pytest.raises(ProvisionError):
        verb(box.id, store=fleet, backend=machine)
    assert machine.calls == []


def test_leaving_puts_the_fleets_key_back_then_removes_the_gateways_marker(fleet):
    machine = _Machine()
    leave_gateway("b-1", store=fleet, backend=machine)
    assert [call[0] for call in machine.calls] == ["apply", "remove"]
    secrets = machine.calls[0][2]
    assert secrets["FLOTTA_MODEL_BASE_URL"] == OPENROUTER
    assert secrets["FLOTTA_API_KEY"] == FLEET_KEY
    assert secrets["OPENROUTER_API_KEY"] == FLEET_KEY
    assert machine.calls[1][2] == ["FLOTTA_GATEWAY_UPSTREAM"]
    assert GATEWAY_LEFT in [e.type for e in fleet.get_events("box", "b-1")]


def test_leaving_needs_a_key_to_go_back_to(fleet, monkeypatch):
    monkeypatch.delenv("FLOTTA_API_KEY")
    machine = _Machine()
    with pytest.raises(ProvisionError, match="FLOTTA_API_KEY"):
        leave_gateway("b-1", store=fleet, backend=machine)
    assert machine.calls == []


# -- a move changes the provider, and never the model ---------------------------


GIVEN = "z-ai/glm-5.2"
LATER_DEFAULT = "openai/gpt-6"


@pytest.fixture
def follower(fleet):
    """An agent that follows the fleet's default, given `GIVEN` when it was made."""
    box = fleet.create_box("follower", box_id="b-2", org="studio-nord", client="acme")
    fleet.update_box_status(box.id, "running", endpoint="fly://app/m2")
    fleet.add_event("box", box.id, MODEL_SET, {"model": GIVEN, "source": "fleet"})
    fleet.set_setting("FLOTTA_MODEL", LATER_DEFAULT)
    return box


@pytest.mark.parametrize("verb", [join_gateway, leave_gateway])
def test_a_new_fleet_default_does_not_arrive_with_a_move(fleet, follower, verb):
    """A new default reaches no existing agent, and a move must not be how it does."""
    machine = _Machine()
    moved = verb(follower.id, store=fleet, backend=machine)
    assert machine.calls[0][2]["FLOTTA_MODEL"] == GIVEN
    assert moved["model"] == GIVEN
    assert fleet.models_given([follower.id]) == {follower.id: GIVEN}


@pytest.mark.parametrize(
    ("verb", "event"), [(join_gateway, GATEWAY_JOINED), (leave_gateway, GATEWAY_LEFT)]
)
def test_the_timeline_names_the_model_it_kept(fleet, follower, verb, event):
    verb(follower.id, store=fleet, backend=_Machine())
    (moved,) = [e for e in fleet.get_events("box", follower.id) if e.type == event]
    assert moved.payload["model"] == GIVEN


def test_the_latest_model_given_is_the_one_kept(fleet, follower):
    fleet.add_event("box", follower.id, MODEL_SET, {"model": "openai/gpt-5", "source": "fleet"})
    machine = _Machine()
    join_gateway(follower.id, store=fleet, backend=machine)
    assert machine.calls[0][2]["FLOTTA_MODEL"] == "openai/gpt-5"


@pytest.mark.parametrize("verb", [join_gateway, leave_gateway])
def test_an_agent_older_than_the_record_keeps_the_environments_model(fleet, monkeypatch, verb):
    """It predates FLOTTA-39, so `model_given_by_default` is what it was made with."""
    box = fleet.create_box("old", box_id="b-3", org="studio-nord", client="acme")
    fleet.update_box_status(box.id, "running", endpoint="fly://app/m3")
    monkeypatch.setenv("FLOTTA_MODEL", GIVEN)
    fleet.set_setting("FLOTTA_MODEL", LATER_DEFAULT)
    machine = _Machine()
    verb(box.id, store=fleet, backend=machine)
    assert machine.calls[0][2]["FLOTTA_MODEL"] == GIVEN


@pytest.mark.parametrize("verb", [join_gateway, leave_gateway])
def test_an_agent_whose_model_nothing_records_is_not_given_a_guess(fleet, monkeypatch, verb):
    box = fleet.create_box("old", box_id="b-3", org="studio-nord", client="acme")
    fleet.update_box_status(box.id, "running", endpoint="fly://app/m3")
    monkeypatch.delenv("FLOTTA_MODEL")
    fleet.set_setting("FLOTTA_MODEL", LATER_DEFAULT)
    machine = _Machine()
    with pytest.raises(ProvisionError, match="set its model"):
        verb(box.id, store=fleet, backend=machine)
    assert machine.calls == []


@pytest.mark.parametrize("verb", [join_gateway, leave_gateway])
def test_a_move_waits_for_a_model_change_under_way(fleet, follower, verb):
    """Both write `FLOTTA_MODEL`, so a move landing last would undo the change."""
    fleet.add_event("box", follower.id, MODEL_CHANGE_STARTED, {"model": "openai/gpt-5"})
    machine = _Machine()
    with pytest.raises(ProvisionError, match="openai/gpt-5"):
        verb(follower.id, store=fleet, backend=machine)
    assert machine.calls == []


def test_a_model_change_that_failed_does_not_block_a_move(fleet, follower):
    """The machine never took it, so it still runs what it was last given."""
    fleet.add_event("box", follower.id, MODEL_CHANGE_FAILED, {"model": "openai/gpt-5"})
    machine = _Machine()
    join_gateway(follower.id, store=fleet, backend=machine)
    assert machine.calls[0][2]["FLOTTA_MODEL"] == GIVEN


# -- the moves ------------------------------------------------------------------


def test_a_new_agent_is_created_through_the_gateway(fleet):
    """Under its own tenancy, or the gateway refuses its token. Behaviour, not
    source: reading `create_box` for the words passed with the bug put back."""
    from flotta.provision import create_box

    spec = _create(fleet, create_box)
    assert FLEET_KEY not in json.dumps(spec.secrets)
    assert spec.secrets["FLOTTA_MODEL_BASE_URL"] == f"{GATEWAY}/v1"
    token = verify(spec.secrets["FLOTTA_API_KEY"], key=KEY)
    assert (token.org, token.client) == ("studio-nord", "acme")
    assert token.allows(SCOPE_MODEL_CALL)


def test_with_no_gateway_a_new_agent_is_created_as_it_always_was(fleet, monkeypatch):
    from flotta.provision import create_box

    monkeypatch.delenv("FLOTTA_GATEWAY_URL")
    spec = _create(fleet, create_box)
    assert spec.secrets["FLOTTA_API_KEY"] == FLEET_KEY
    assert spec.secrets["FLOTTA_MODEL_BASE_URL"] == OPENROUTER


def _create(fleet, create_box):
    from flotta.test_provision import Recording

    impl = Recording()
    create_box("fresh", store=fleet, backend=impl, org="studio-nord", client="acme")
    return impl.spec
