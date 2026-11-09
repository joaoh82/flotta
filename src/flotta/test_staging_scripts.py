"""The staging seed, the staging check and the deploy workflows (M14.8).

The scripts reach a real control plane, so what is pinned here is what keeps
them pointed at staging: they refuse anything else, the seeded people are test
addresses, and the workflows deploy each environment with its own names and
its own token.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "env",
    [
        {},
        {
            "FLOTTA_ENVIRONMENT": "staging",
            "FLOTTA_CONTROL_URL": "https://flotta-control.onrender.com",
        },
        {"FLOTTA_CONTROL_URL": "https://flotta-control-staging.onrender.com"},
    ],
)
def test_the_seed_refuses_anything_but_staging(env):
    seed = _script("staging_seed")

    with pytest.raises(SystemExit) as exc:
        seed._refuse_unless_staging(env)

    assert "refusing" in str(exc.value)


def test_the_check_refuses_anything_but_staging(monkeypatch):
    check = _script("staging_check")
    monkeypatch.setenv("FLOTTA_ENVIRONMENT", "staging")
    monkeypatch.setenv("FLOTTA_CONTROL_URL", "https://flotta-control.onrender.com")
    monkeypatch.setenv("FLOTTA_TOKEN", "t")

    with pytest.raises(SystemExit) as exc:
        check.main(["staging_check.py", "abc1234"])

    assert "refusing" in str(exc.value)


def test_every_seeded_person_is_a_test_address():
    seed = _script("staging_seed")

    assert seed.PEOPLE
    assert all(email.endswith("@staging.flotta.dev") for email, *_ in seed.PEOPLE)


def test_a_later_build_containing_the_commit_counts(tmp_path, monkeypatch):
    check = _script("staging_check")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    shas = []
    for n in range(3):
        commit = [*git, "commit", "-q", "--allow-empty", "-m", str(n)]
        subprocess.run(commit, cwd=tmp_path, check=True)
        head = ["git", "rev-parse", "HEAD"]
        shas.append(subprocess.check_output(head, cwd=tmp_path, text=True).strip())
    monkeypatch.chdir(tmp_path)
    first, middle, last = shas

    assert check._contains(middle[:12], middle)
    assert check._contains(last[:12], middle), "a later merge on staging carries this one"
    assert not check._contains(first[:12], middle), "an older build does not"
    assert not check._contains("", middle)
    assert not check._contains("abc", "abc123456")


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_staging_deploys_to_the_staging_org_with_the_staging_token():
    flow = _workflow("staging.yml")

    assert flow["env"]["FLOTTA_FLY_ORG"] == "flotta-staging"
    assert "staging" in flow["env"]["FLOTTA_CONTROL_URL"]
    assert flow["jobs"]["deploy"]["env"]["FLY_API_TOKEN"] == "${{ secrets.FLY_API_TOKEN_STAGING }}"
    assert flow["concurrency"]["cancel-in-progress"] is False


def test_production_deploys_only_what_passed_staging():
    flow = _workflow("production.yml")
    deploy = flow["jobs"]["deploy"]

    assert flow[True]["push"]["branches"] == ["production"]  # `on:` reads as True
    assert flow["env"]["FLOTTA_FLY_ORG"] != "flotta-staging"
    assert "staging" not in flow["env"]["FLOTTA_CONTROL_URL"]
    assert deploy["environment"] == "production"
    first = deploy["steps"][0]
    assert first["name"] == "Refuse a commit that did not pass staging"
    assert 'select(.context == "staging")' in first["run"]


def test_promotion_checks_main_staging_and_fast_forward():
    flow = _workflow("promote.yml")
    choose = next(s for s in flow["jobs"]["promote"]["steps"] if s.get("id") == "choose")["run"]

    assert list(flow[True]) == ["workflow_dispatch"], "promotion is run by hand, never on a push"
    assert 'merge-base --is-ancestor "$sha" origin/main' in choose
    assert '[ "$state" = success ]' in choose
    assert 'merge-base --is-ancestor "$current" "$sha"' in choose
