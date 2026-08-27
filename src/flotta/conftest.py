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
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch):
    """Remove every `FLOTTA_*` variable for the duration of a test.

    Autouse and unconditional. An opt-in version would be applied to the tests
    someone remembered, and the failure mode here is silence — a test that
    passes for a reason its author did not intend.
    """
    for name in [k for k in os.environ if k.startswith("FLOTTA_")]:
        monkeypatch.delenv(name, raising=False)
