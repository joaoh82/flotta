"""Staging reads `.env.staging` and nothing of production's (M14.8).

`just staging <recipe>` runs any recipe against staging. The danger is not a
missing value, which fails loudly, but a value staging forgot that production's
`.env` quietly supplies: a staging command holding production's token, or
writing staging's key into production's file. So the recipe drops every
`FLOTTA_` variable it inherited, tells `just` (and every `just` it starts) to
load no `.env`, and names the file in `$FLOTTA_DOTENV`, which every reader and
writer of a dotenv in `flotta` follows.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

from flotta import client, dotenv, fleet_key, sealed
from flotta.auth import NoSigningKey, _require_key
from flotta.fly import FlyConfig

_JUSTFILE = pathlib.Path(__file__).resolve().parents[2] / "justfile"

PRODUCTION = (
    "FLOTTA_SIGNING_KEY=production-key\n"
    "FLOTTA_FLY_ORG=personal\n"
    "FLOTTA_BOX_PASSWORD=production-password\n"
    "FLOTTA_TOKEN=production-only-token\n"
)
STAGING = (
    "FLOTTA_ENVIRONMENT=staging\n"
    "FLOTTA_SIGNING_KEY=staging-key\n"
    "FLOTTA_FLY_ORG=flotta-staging\n"
    "FLOTTA_BOX_PASSWORD=staging-password\n"
)


@pytest.fixture
def two_files(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(PRODUCTION)
    (tmp_path / ".env.staging").write_text(STAGING)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_without_a_name_it_is_dot_env(two_files):
    assert dotenv.dotenv_path() == ".env"
    assert dotenv.read_dotenv_value("FLOTTA_SIGNING_KEY") == "production-key"


def test_a_named_file_is_read_and_dot_env_never_is(two_files, monkeypatch):
    monkeypatch.setenv("FLOTTA_DOTENV", ".env.staging")
    loaded: dict[str, str] = {}

    dotenv.load_dotenv(env=loaded)

    assert loaded["FLOTTA_SIGNING_KEY"] == "staging-key"
    assert "FLOTTA_TOKEN" not in loaded, "production's token filled a gap in staging's"
    assert dotenv.read_dotenv_value("FLOTTA_TOKEN") is None
    assert FlyConfig.from_env(env={}).org == "flotta-staging"
    user, password = client.credentials(box_id="b-1")
    _, production = client.credentials(".env", box_id="b-1")
    assert password != production


def test_the_key_generators_write_the_named_file(two_files, monkeypatch):
    monkeypatch.setenv("FLOTTA_DOTENV", ".env.staging")
    (two_files / ".env.staging").write_text("FLOTTA_ENVIRONMENT=staging\n")
    before = (two_files / ".env").read_text()

    fleet_key.main(["generate"])
    sealed.main(["generate"])

    staging = (two_files / ".env.staging").read_text()
    assert "FLOTTA_BOX_PASSWORD=" in staging
    assert "FLOTTA_SECRETS_KEY=" in staging
    assert (two_files / ".env").read_text() == before


def _staging_recipe() -> str:
    text = _JUSTFILE.read_text(encoding="utf-8")
    start = text.index("staging +ARGS:")
    end = text.index("\n\n", start)
    return text[start:end] + "\n"


def _just(tmp_path, *args: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "justfile").write_text(
        "set dotenv-load := true\n\n"
        + _staging_recipe()
        + "\nprobe:\n"
        + '    @echo "key=${FLOTTA_SIGNING_KEY:-} token=${FLOTTA_TOKEN:-} shell=${FLOTTA_LEAK:-}'
        + ' file=${FLOTTA_DOTENV:-}"\n'
        + "    @just nested\n"
        + "\nnested:\n"
        + '    @echo "nested key=${FLOTTA_SIGNING_KEY:-} token=${FLOTTA_TOKEN:-}"\n'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FLOTTA_", "JUST_"))}
    # The recipe parses the file with this checkout's `flotta`, from a
    # justfile that is not in it.
    env["UV_PROJECT"] = str(_JUSTFILE.parent)
    env["FLOTTA_LEAK"] = "from-the-shell"
    env["FLOTTA_SIGNING_KEY"] = "exported-production-key"
    return subprocess.run(
        ["just", *args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )


@pytest.mark.skipif(shutil.which("just") is None, reason="needs `just`, which CI installs")
def test_the_recipe_runs_with_staging_and_nothing_of_production(two_files):
    ran = _just(two_files, "staging", "probe")

    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.splitlines() == [
        "key=staging-key token= shell= file=.env.staging",
        "nested key=staging-key token=",
    ]


@pytest.mark.skipif(shutil.which("just") is None, reason="needs `just`, which CI installs")
def test_the_recipe_refuses_a_file_that_does_not_say_staging(two_files):
    (two_files / ".env.staging").write_text(PRODUCTION)

    ran = _just(two_files, "staging", "probe")

    assert ran.returncode != 0
    assert "FLOTTA_ENVIRONMENT=staging" in ran.stderr
    assert "key=" not in ran.stdout


_NEEDS_JUST = pytest.mark.skipif(
    shutil.which("just") is None, reason="needs `just`, which CI installs"
)


@_NEEDS_JUST
def test_the_file_is_read_as_values_and_never_run(two_files):
    """`.` ran the file: `$(...)` executed and a `$` in a secret killed `set -u`."""
    marker = two_files / "ran"
    (two_files / ".env.staging").write_text(
        "FLOTTA_ENVIRONMENT=staging\n"
        f"FLOTTA_LEAK=$(touch {marker})\n"
        "FLOTTA_SIGNING_KEY=abc$def`id`\n"
    )

    ran = _just(two_files, "staging", "probe")

    assert not marker.exists(), "a command in .env.staging ran"
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.splitlines()[0] == (
        f"key=abc$def`id` token= shell=$(touch {marker}) file=.env.staging"
    )


@_NEEDS_JUST
@pytest.mark.parametrize(
    "line",
    [
        "export FLOTTA_ENVIRONMENT=staging",
        'FLOTTA_ENVIRONMENT="staging"',
        "FLOTTA_ENVIRONMENT='staging'",
    ],
)
def test_the_guard_takes_what_the_parser_takes(two_files, line):
    (two_files / ".env.staging").write_text(line + "\nFLOTTA_SIGNING_KEY=staging-key\n")

    ran = _just(two_files, "staging", "probe")

    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.startswith("key=staging-key ")


def test_exports_quote_every_value_for_a_shell(tmp_path):
    values = {"A": "FlyV1 fm2_x,y", "B": "a$b`c`'d\"e", "C": "$(echo no)", "bad name": "x"}

    script = dotenv.shell_exports(values) + 'printf "%s|%s|%s" "$A" "$B" "$C"'
    out = subprocess.run(["bash", "-euc", script], capture_output=True, text=True, check=True)

    assert out.stdout == "FlyV1 fm2_x,y|a$b`c`'d\"e|$(echo no)"
    assert "bad name" not in dotenv.shell_exports(values)


def test_the_key_hint_names_the_file_that_was_read(two_files, monkeypatch):
    monkeypatch.setenv("FLOTTA_DOTENV", ".env.staging")

    with pytest.raises(NoSigningKey) as exc:
        _require_key(None, env={})

    message = str(exc.value)
    assert "in .env.staging but not" in message
    assert "exports .env.staging" in message
    assert "source .env" not in message
