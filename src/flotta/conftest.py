"""Test-suite hermeticity.

The suite's central claim is that it is hermetic and $0 — no network, no
credentials, no substrate. That was true of *injected* dependencies and quietly
untrue of the **environment**: several modules fall back to `os.environ` when a
value is not passed, so a developer's `.env` changed the behaviour under test.

It broke for real the moment deploying required a signing key in `.env`.
`create_app` falls back to `resolve_signing_key()`, the keyless fixture in
`control/test_app.py` assumed there would never be one, and fifteen tests
started returning 401 — locally only. CI stayed green because CI has no `.env`,
which is the worst version of this: the suite disagrees with itself depending on
who runs it.

So every `FLOTTA_*` variable is stripped before each test. A test that wants one
sets it explicitly with `monkeypatch.setenv`, which is also the only way to read
a test and know what environment it runs in.

## What this fixture cannot do

It strips the environment **once**, before the test. It cannot stop code under
test from reading `.env` *again* at run time — and the CLI does exactly that,
loading it in its root callback (#46) relative to the working directory, which
under pytest is the repo root.

So `monkeypatch.delenv("FLOTTA_CONTROL_URL")` followed by a `CliRunner` call is
undone by the CLI one frame later, and the test passes everywhere except on the
machine of someone who has deployed. That is the same shape as the fifteen
tests above, arrived at from the other direction.

The fix is per-test rather than here: a CLI test invokes the app from a
directory with no `.env` (`monkeypatch.chdir(tmp_path)`). Redirecting the
default path globally was rejected — it would make the CLI's own dotenv
behaviour untestable, and that behaviour is what `just box-identity` depends
on. See `test_cli.py::_token_box` and the test directly below it.
"""

from __future__ import annotations

import os

import pytest

#: The one `FLOTTA_*` variable that survives, because it configures the
#: **harness** rather than the code under test.
#:
#: Nothing in `src/flotta` reads it — it selects which engine the store suite
#: runs against. Stripping it therefore buys no hermeticity and costs the whole
#: parameterisation: `test_store.py`'s `_ENGINES` is computed at *import* and so
#: still says `postgres`, while its fixture reads `os.environ` at *fixture*
#: time and finds nothing. That is 94 errors reading `KeyError:
#: 'FLOTTA_TEST_POSTGRES_URL'`, and it is what the first CI run with a service
#: container actually produced.
#:
#: `test_store_postgres.py` escaped it by accident: its `POSTGRES_URL` is a
#: module-level constant captured at import. Relying on that distinction file
#: by file is how this comes back, so the exemption lives here instead.
HARNESS_ENV = frozenset({"FLOTTA_TEST_POSTGRES_URL"})


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch):
    """Remove every `FLOTTA_*` variable for the duration of a test.

    Autouse and unconditional, bar `HARNESS_ENV` above. An opt-in version would
    be applied to the tests someone remembered, and the failure mode here is
    silence — a test that passes for a reason its author did not intend.
    """
    for name in [k for k in os.environ if k.startswith("FLOTTA_")]:
        if name in HARNESS_ENV:
            continue
        monkeypatch.delenv(name, raising=False)


def pytest_sessionstart(session):
    """On CI, refuse to run at all without a Postgres to race against.

    The whole point of `test_store_postgres.py` is that a dropped transaction
    guard is invisible to every other test — three of them lost their guard
    during the M4 migration and all 453 tests still passed. That file was then
    skipped on every PR for want of one environment variable, and **a skip
    reports as a pass**: the check said green while measuring nothing.

    Skipping is right on a laptop, where not everyone has Docker. It is wrong
    on the independent check, which exists precisely so a green tick means
    something. So here the absence is an error rather than a skip — the failure
    is loud, at the start, and says how to fix it.

    Read from the real environment: this runs at session start, before any
    per-test stripping. The variable is exempt from that stripping anyway — see
    `HARNESS_ENV` — because it configures the harness rather than the code
    under test.
    """
    if not os.environ.get("CI"):
        return
    if os.environ.get("FLOTTA_TEST_POSTGRES_URL", "").strip():
        return
    raise pytest.UsageError(
        "FLOTTA_TEST_POSTGRES_URL is unset on CI, so the Postgres transaction "
        "guards would skip — and a skip reports as a pass. Point it at the "
        "workflow's service container, or unset CI to run the hermetic suite "
        "alone."
    )
