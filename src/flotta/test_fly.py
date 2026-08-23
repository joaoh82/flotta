"""Tests for the Fly box configuration and the durable-path contract.

Hermetic and $0: nothing here talks to Fly. What is worth pinning is the
*contract* — which paths must survive, and that the org/app resolution cannot
silently fall back to whatever is ambient. The live proof is
`scripts/m2_memory_proof.py`, which needs a real machine.
"""

import pytest

from flotta.fly import (
    DEFAULT_APP,
    DEFAULT_HERMES_HOME,
    DEFAULT_MOUNT_PATH,
    DEFAULT_ORG,
    DURABLE_PATHS,
    FALLBACK_REGION,
    FlyConfig,
    detect_region,
    durable_paths,
)

NO_DOTENV = "/nonexistent/.env"


def cfg(env=None, dotenv=NO_DOTENV):
    return FlyConfig.from_env(env or {}, dotenv=dotenv)


# -- the durable-path contract ----------------------------------------------


def test_hermes_home_is_inside_the_mount():
    """The whole milestone in one assertion.

    If HERMES_HOME ever drifts outside the mounted volume, every other test
    here still passes and the box silently forgets everything again — which is
    exactly the v0.1 bug.
    """
    assert DEFAULT_HERMES_HOME.startswith(DEFAULT_MOUNT_PATH + "/")
    assert cfg().hermes_home.startswith(cfg().mount_path + "/")


def test_hermes_home_is_not_ephemeral():
    """Regression guard against the v0.1 default coming back."""
    assert not DEFAULT_HERMES_HOME.startswith("/tmp")
    assert not cfg().hermes_home.startswith("/tmp")


def test_the_four_things_that_must_survive():
    """SEAM_NOTES Q3 traced these to file:line in Hermes. "Memory survives" is
    four claims, and asserting only the first would let the interesting ones rot.
    """
    assert {p.relative for p in DURABLE_PATHS} == {
        "state.db",  # conversation history
        "memories",  # markdown memory
        "skills",  # the self-improvement surface
        "sessions",  # transcripts
    }


def test_state_db_is_not_labelled_as_conversation_history():
    """Measured, not assumed.

    After a completed headless turn on a live box, `state.db` holds one table
    (`async_delegations`, zero rows) — SEAM_NOTES Q3's sessions/messages/FTS
    schema is written by the gateway/CLI path. Labelling the file "conversation
    history" would promise something M2 does not deliver, and the promise would
    outlive anyone's memory of measuring it.
    """
    state_db = next(p for p in DURABLE_PATHS if p.relative == "state.db")
    assert "conversation history" not in state_db.what
    assert not state_db.is_dir


def test_skills_is_in_the_contract():
    """Named explicitly: `skills/` surviving is what "self-improving" means.

    Conversation history surviving is table stakes; a box that accumulates
    skills across restarts is the actual claim the project is built on.
    """
    skills = next(p for p in DURABLE_PATHS if p.relative == "skills")
    assert skills.is_dir
    assert "self-improvement" in skills.what


def test_durable_paths_are_absolute_and_under_hermes_home():
    paths = durable_paths("/data/hermes")
    assert all(p.startswith("/data/hermes/") for p in paths)
    assert "/data/hermes/state.db" in paths


def test_durable_paths_follow_a_relocated_home():
    assert durable_paths("/mnt/elsewhere")[0] == "/mnt/elsewhere/state.db"


def test_durable_paths_tolerate_a_trailing_slash():
    assert durable_paths("/data/hermes/")[0] == "/data/hermes/state.db"


# -- configuration resolution -----------------------------------------------


def test_defaults_are_safe():
    c = cfg()
    assert c.org == DEFAULT_ORG == "personal"  # every Fly account has one
    assert c.app == DEFAULT_APP
    assert c.region is None  # let Fly pick the nearest
    assert c.volume_gb == 1


def test_env_overrides_every_field():
    c = cfg(
        {
            "FLOTTA_FLY_ORG": "acme",
            "FLOTTA_FLY_APP": "flotta-acme",
            "FLOTTA_FLY_REGION": "gru",
            "FLOTTA_FLY_VOLUME_NAME": "acme_data",
            "FLOTTA_FLY_VOLUME_GB": "5",
        }
    )
    assert (c.org, c.app, c.region, c.volume_name, c.volume_gb) == (
        "acme",
        "flotta-acme",
        "gru",
        "acme_data",
        5,
    )


def test_dotenv_is_read_when_the_env_is_silent(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("FLOTTA_FLY_ORG=from-dotenv\nFLOTTA_FLY_APP=app-from-dotenv\n")
    c = cfg({}, dotenv=dotenv)
    assert c.org == "from-dotenv"
    assert c.app == "app-from-dotenv"


def test_the_environment_beats_the_dotenv(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("FLOTTA_FLY_ORG=from-dotenv\n")
    assert cfg({"FLOTTA_FLY_ORG": "from-env"}, dotenv=dotenv).org == "from-env"


def test_a_blank_value_falls_through_to_the_default():
    """An exported-but-empty variable must not resolve to an empty org."""
    assert cfg({"FLOTTA_FLY_ORG": "   "}).org == DEFAULT_ORG


@pytest.mark.parametrize("bad", ["abc", "1.5", "-1", "0"])
def test_a_bad_volume_size_is_rejected_loudly(bad):
    """Better to fail here than to create a volume of a size nobody meant."""
    with pytest.raises(ValueError, match="FLOTTA_FLY_VOLUME_GB"):
        cfg({"FLOTTA_FLY_VOLUME_GB": bad})


def test_describe_names_the_target_before_anything_acts_on_it():
    """`just fly-whoami` gates every Fly recipe; it has to name all of it."""
    out = cfg({"FLOTTA_FLY_ORG": "acme", "FLOTTA_FLY_APP": "flotta-acme"}).describe()
    assert "acme" in out
    assert "flotta-acme" in out
    assert "/data/hermes" in out


def test_config_carries_its_own_durable_paths():
    c = cfg({"FLOTTA_FLY_VOLUME_GB": "2"})
    assert c.durable_paths == durable_paths(c.hermes_home)


# -- region detection -------------------------------------------------------

# Every Fly edge echoes the region that served a request in a `Fly-Region`
# header, so one unauthenticated request answers "where is nearest?".
_EDGE_RESPONSE = """=== Headers ===
Host: debug.fly.dev
Fly-Region: ams
Fly-Request-Id: 01ABC-ams
"""


def test_region_is_read_from_the_fly_edge_header():
    assert detect_region(lambda: _EDGE_RESPONSE) == "ams"


def test_region_detection_is_case_insensitive():
    assert detect_region(lambda: "fly-region: gru\n") == "gru"


def test_region_detection_returns_none_when_the_header_is_absent():
    assert detect_region(lambda: "Host: example.com\n") is None


def test_region_detection_never_raises():
    """A slow DNS lookup must not kill a provisioning run."""

    def boom():
        raise OSError("network unreachable")

    assert detect_region(boom) is None


def test_resolved_region_is_always_concrete():
    """`fly volumes create` refuses to run without a region when not on a TTY,
    so "unset" cannot mean "decide later" — something must name one."""
    assert cfg().resolved_region(lambda: "") == FALLBACK_REGION
    assert cfg().resolved_region(lambda: _EDGE_RESPONSE) == "ams"


def test_configured_region_beats_detection():
    """An explicit choice is never second-guessed by a network probe."""
    c = cfg({"FLOTTA_FLY_REGION": "gru"})
    assert c.resolved_region(lambda: _EDGE_RESPONSE) == "gru"


def test_describe_says_the_region_is_detected_not_chosen_by_fly():
    """The old wording claimed Fly would pick one, which is false for volumes."""
    assert "detected at provision time" in cfg().describe()
