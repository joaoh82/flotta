"""Fleet settings: what may be set, what wins, and what must never be reachable."""

from __future__ import annotations

import pytest

from flotta.settings import (
    BY_KEY,
    SETTINGS,
    UnknownSettingError,
    describe,
    layered,
    validate,
)
from flotta.store import FleetStore


@pytest.fixture
def store(tmp_path):
    with FleetStore(tmp_path / "fleet.db") as fleet:
        yield fleet


# -- the allowlist is the security boundary ---------------------------------
#
# `layered` shadows environment lookups. Without the catalogue bounding what
# may be written, the settings table would be a way to override *any* variable
# the control plane reads — through an authenticated HTTP call.

SECRETS = [
    "FLOTTA_SIGNING_KEY",
    "FLOTTA_GITHUB_TOKEN",
    "FLOTTA_BOX_PASSWORD",
    "FLOTTA_API_KEY",
    "FLY_API_TOKEN",
]


@pytest.mark.parametrize("key", SECRETS)
def test_a_credential_is_not_a_setting(key):
    """Configuration is settable; credentials are not. The app can never read a
    secret back, and this is the other half: it cannot write one either."""
    assert key not in BY_KEY
    with pytest.raises(UnknownSettingError):
        validate(key, "anything")


@pytest.mark.parametrize("key", SECRETS)
def test_a_row_for_a_credential_would_still_not_shadow_the_environment(store, key):
    """Belt and braces. The API refuses to write these, but if a row appeared
    some other way — a hand-edited database, a future bug — the environment
    must still win. `layered` filters on the catalogue, not on trust."""
    store.set_setting(key, "stolen")
    env = layered(store, {key: "the real one"})
    assert env[key] == "the real one"
    assert env.get(key) == "the real one"


def test_every_catalogued_setting_is_configuration_not_a_credential():
    """If a secret were added to the catalogue, `layered` would start shadowing
    it and the API would start accepting it — one edit, nothing else to
    notice."""
    suspicious = ("KEY", "TOKEN", "PASSWORD", "SECRET", "CREDENTIAL")
    for setting in SETTINGS:
        name = setting.key.upper()
        assert not any(word in name for word in suspicious), (
            f"{setting.key} looks like a credential; settings are configuration only"
        )


@pytest.mark.parametrize("key", SECRETS)
def test_the_catalogue_refuses_a_credential_at_construction(key):
    """The same rule as a runtime failure, not only an assertion.

    A test can be skipped, and this one guards a security boundary: raising in
    `__post_init__` means a module that catalogued a secret would not import at
    all, so the mistake cannot reach a running control plane.
    """
    from flotta.settings import Setting

    with pytest.raises(ValueError, match="credential"):
        Setting(key=key, label="x", help="x", kind="text", default="")


# -- precedence -------------------------------------------------------------


def test_a_stored_value_wins_over_the_environment(store):
    """The whole point: a person changed it in the app, and the deployment
    variable stops deciding."""
    store.set_setting("FLOTTA_IDLE_AFTER_S", "300")
    assert layered(store, {"FLOTTA_IDLE_AFTER_S": "1800"})["FLOTTA_IDLE_AFTER_S"] == "300"


def test_the_environment_still_decides_when_nothing_is_stored(store):
    """Every existing deployment keeps working unchanged — that is what makes
    this safe to land on a live fleet."""
    assert layered(store, {"FLOTTA_IDLE_AFTER_S": "1800"})["FLOTTA_IDLE_AFTER_S"] == "1800"


def test_clearing_a_setting_hands_the_decision_back(store):
    store.set_setting("FLOTTA_IDLE_AFTER_S", "300")
    assert store.clear_setting("FLOTTA_IDLE_AFTER_S") is True
    assert layered(store, {"FLOTTA_IDLE_AFTER_S": "1800"})["FLOTTA_IDLE_AFTER_S"] == "1800"
    assert store.clear_setting("FLOTTA_IDLE_AFTER_S") is False


def test_an_empty_stored_value_is_not_an_override(store):
    """An emptied field means "stop overriding", not "override with nothing" —
    otherwise clearing a box in the app would set the fleet to the empty string
    and every consumer would fall to its default with no way back."""
    store.set_setting("FLOTTA_IDLE_AFTER_S", "   ")
    assert layered(store, {"FLOTTA_IDLE_AFTER_S": "1800"})["FLOTTA_IDLE_AFTER_S"] == "1800"


def test_a_key_nobody_catalogued_passes_straight_through(store):
    """`layered` is a lens over the environment, not a replacement for it —
    everything the control plane reads still resolves normally."""
    env = layered(store, {"PATH": "/usr/bin"})
    assert env["PATH"] == "/usr/bin"
    assert env.get("NOT_SET_ANYWHERE") is None
    with pytest.raises(KeyError):
        env["NOT_SET_ANYWHERE"]


# -- what the app renders ---------------------------------------------------


def test_describe_says_where_each_value_came_from(store):
    """ "Why is my fleet not using the number I set" is otherwise unanswerable
    from outside, and the answer is usually a deployment variable still in
    play."""
    store.set_setting("FLOTTA_IDLE_AFTER_S", "300")
    by_key = {row["key"]: row for row in describe(store, {"FLOTTA_COST_PER_SECOND": "0.001"})}

    assert by_key["FLOTTA_IDLE_AFTER_S"]["value"] == "300"
    assert by_key["FLOTTA_IDLE_AFTER_S"]["source"] == "store"
    assert by_key["FLOTTA_COST_PER_SECOND"]["value"] == "0.001"
    assert by_key["FLOTTA_COST_PER_SECOND"]["source"] == "env"
    assert by_key["FLOTTA_RECONCILE_INTERVAL_S"]["source"] == "default"


def test_describe_carries_the_catalogue_so_the_app_hardcodes_nothing(store):
    """A setting added to `SETTINGS` should appear in the window without the
    app being rebuilt."""
    rows = describe(store, {})
    assert {row["key"] for row in rows} == set(BY_KEY)
    for row in rows:
        assert row["label"] and row["help"] and row["kind"]


# -- validation -------------------------------------------------------------


def test_a_value_that_cannot_be_parsed_is_refused_before_it_is_stored():
    """Stored once, it breaks every read afterwards — and for the sweep
    interval that means at control-plane startup, where it reads as a crash
    loop rather than a bad number."""
    with pytest.raises(ValueError):
        validate("FLOTTA_IDLE_AFTER_S", "half an hour")
    with pytest.raises(ValueError):
        validate("FLOTTA_IDLE_AFTER_S", "-1")


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan", "Infinity", "NaN"])
def test_a_number_that_is_not_a_real_number_is_refused(bad):
    """`float("inf")` parses and is not `< 0`, so it would store.

    As the sweep interval that is `await sleep(inf)` — the loop never ticks
    again, and clearing the field afterwards cannot wake a sleep that never
    ends. As a cost rate it fails `resolve_cost_rate`'s own finiteness check
    and takes down a sweep. `nan` compares false against everything and
    silently becomes whatever floor it meets.
    """
    with pytest.raises(ValueError, match="real number"):
        validate("FLOTTA_IDLE_AFTER_S", bad)
    with pytest.raises(ValueError, match="real number"):
        validate("FLOTTA_COST_PER_SECOND", bad)


def test_several_settings_are_applied_together(store):
    """`set_settings` is one transaction, so a database failure partway cannot
    leave the fleet configured with a prefix of what was asked for."""
    store.set_settings({"FLOTTA_IDLE_AFTER_S": "300", "FLOTTA_RECONCILE_INTERVAL_S": "10"})
    assert store.all_settings() == {
        "FLOTTA_IDLE_AFTER_S": "300",
        "FLOTTA_RECONCILE_INTERVAL_S": "10",
    }

    # An empty value in the same batch clears that one and leaves the other.
    store.set_settings({"FLOTTA_IDLE_AFTER_S": "", "FLOTTA_RECONCILE_INTERVAL_S": "20"})
    assert store.all_settings() == {"FLOTTA_RECONCILE_INTERVAL_S": "20"}


def test_a_good_value_comes_back_clean():
    assert validate("FLOTTA_IDLE_AFTER_S", "  300 ") == "300"
    assert validate("FLOTTA_COST_PER_SECOND", "0.0000131") == "0.0000131"
    assert validate("FLOTTA_IDLE_AFTER_S", "0") == "0"  # disabling is legal


def test_an_empty_value_is_a_clear_not_a_refusal():
    assert validate("FLOTTA_IDLE_AFTER_S", "") == ""
    assert validate("FLOTTA_IDLE_AFTER_S", "   ") == ""
