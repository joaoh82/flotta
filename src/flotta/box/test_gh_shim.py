"""The box's `gh`, run: what it does around pull requests (FLOTTA-171).

`test_image.py` reads the shim; these run it, under bash, against a fake `gh`,
a fake `git` and a fake `flotta-pr` that write down how they were called. The
shim's own path to the real `gh` is swapped for the fake's in a copy, so the
file that ships keeps `/usr/bin/gh` as `test_image.py` insists.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SHIM = Path(__file__).resolve().parents[3] / "fly" / "gh_shim.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _script(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def box(tmp_path):
    """A PATH with fakes on it, and a function that runs the shim there."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "log"
    _script(
        bin_ / "real-gh",
        f'echo "gh $* token=${{GH_TOKEN:-}}" >> {log}\n'
        'if [ "$1 $2" = "pr view" ]; then echo "${FAKE_PR_URL:-}"; fi\n'
        'exit "${FAKE_GH_STATUS:-0}"\n',
    )
    _script(
        bin_ / "git",
        'case "$1 $2" in\n'
        '  "config --get") echo "https://github.com/acme/app.git" ;;\n'
        '  "credential fill") cat >/dev/null; printf "username=x\\npassword=tok-1\\n" ;;\n'
        "esac\n",
    )
    _script(
        bin_ / "flotta-pr",
        f'echo "flotta-pr $*" >> {log}\n'
        'if [ "$1" = may-merge ]; then echo "${FAKE_MAY_MERGE_SAYS:-}"; '
        'exit "${FAKE_MAY_MERGE:-1}"; fi\n'
        'exit "${FAKE_REPORT_STATUS:-0}"\n',
    )
    shim = tmp_path / "gh"
    shim.write_text(
        _SHIM.read_text(encoding="utf-8").replace(
            "REAL_GH=/usr/bin/gh", f"REAL_GH={bin_ / 'real-gh'}"
        ),
        encoding="utf-8",
    )
    shim.chmod(0o755)

    def run(*args: str, **env: str) -> tuple[int, str, list[str]]:
        environ = {
            "PATH": f"{bin_}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            **env,
        }
        done = subprocess.run(
            ["bash", str(shim), *args], env=environ, capture_output=True, text=True, timeout=20
        )
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return done.returncode, done.stderr, calls

    return run


def test_a_pull_request_it_opens_is_reported_to_the_agency(box):
    code, err, calls = box(
        "pr",
        "create",
        "--title",
        "Fix",
        "--body",
        "x",
        FAKE_PR_URL="https://github.com/acme/app/pull/7",
    )
    assert code == 0
    assert err == ""
    assert calls[0] == "gh pr create --title Fix --body x token=tok-1"
    assert "flotta-pr report https://github.com/acme/app/pull/7" in calls


def test_the_report_looks_up_the_branch_and_repository_it_was_given(box):
    _, _, calls = box(
        "pr",
        "create",
        "-H",
        "fix-1",
        "--repo=acme/app",
        "--fill",
        FAKE_PR_URL="https://github.com/acme/app/pull/8",
    )
    assert any(c.startswith("gh pr view fix-1 --repo acme/app --json url") for c in calls)


def test_a_failed_create_reports_nothing_and_keeps_its_status(box):
    code, _, calls = box("pr", "create", "--fill", FAKE_GH_STATUS="4")
    assert code == 4
    assert not any(c.startswith("flotta-pr") for c in calls)


def test_a_report_that_fails_says_so_and_the_pr_stands(box):
    code, err, _ = box(
        "pr",
        "create",
        "--fill",
        FAKE_PR_URL="https://github.com/acme/app/pull/9",
        FAKE_REPORT_STATUS="2",
    )
    assert code == 0
    assert "flotta-pr report <its url>" in err


def test_a_dry_run_reports_nothing(box):
    _, _, calls = box("pr", "create", "--dry-run", FAKE_PR_URL="https://github.com/acme/app/pull/1")
    assert not any(c.startswith("flotta-pr") for c in calls)


def test_merging_is_refused_when_the_agency_has_not_allowed_it(box):
    code, err, calls = box(
        "pr",
        "merge",
        "7",
        "--squash",
        FAKE_MAY_MERGE="1",
        FAKE_MAY_MERGE_SAYS="the agency merges them.",
    )
    assert code == 1
    assert "gh pr merge refused" in err and "the agency merges them." in err
    assert not any(c.startswith("gh pr merge") for c in calls)


def test_merging_is_refused_when_nobody_could_be_asked(box):
    """A merge nobody could check is not one to make."""
    code, err, calls = box("pr", "merge", "7", FAKE_MAY_MERGE="2")
    assert code == 1
    assert not any(c.startswith("gh pr merge") for c in calls)


def test_merging_goes_ahead_when_the_agency_allows_it(box):
    code, _, calls = box("pr", "merge", "7", "--squash", FAKE_MAY_MERGE="0")
    assert code == 0
    assert "gh pr merge 7 --squash token=tok-1" in calls


def test_asking_how_to_merge_asks_nobody(box):
    code, _, calls = box("pr", "merge", "--help", FAKE_MAY_MERGE="1")
    assert code == 0
    assert "flotta-pr may-merge" not in calls


def test_the_merge_guard_holds_with_a_token_of_its_own(box):
    """A shell that set `GH_TOKEN` skips the credential trade, not the guard."""
    code, _, calls = box("pr", "merge", "7", GH_TOKEN="mine", FAKE_MAY_MERGE="1")
    assert code == 1
    assert not any(c.startswith("gh pr merge") for c in calls)


def test_everything_else_is_untouched(box):
    code, err, calls = box("pr", "list")
    assert code == 0 and err == ""
    assert calls == ["gh pr list token=tok-1"]
