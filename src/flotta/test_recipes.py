"""The justfile is code the test suite could not see.

`HERMES_REF` moved from `flotta.worker.image` to `flotta.box.image` when the
shard tier was cut, and three recipes kept importing the old path —
`hermes-check`, `hermes-bump` and **`fly-up`, which is the README's create
path**. Every one would have died with `ModuleNotFoundError` on a fresh
checkout. 419 Python tests passed throughout, because none of them reads a
recipe.

This closes that specific hole: a recipe that reaches into `flotta.*` must
name something that exists. It is hermetic — the imports are resolved, nothing
is executed and no recipe is run, so it needs no network, no credentials and
no `flyctl`.
"""

from __future__ import annotations

import importlib
import pathlib
import re

import pytest

_JUSTFILE = pathlib.Path(__file__).resolve().parents[2] / "justfile"

#: `from flotta.a.b import C, D` inside an inline `python -c` in a recipe.
_IMPORT = re.compile(r"from\s+(flotta(?:\.\w+)*)\s+import\s+([\w,\s]+)")


def _imports() -> list[tuple[str, str]]:
    if not _JUSTFILE.is_file():  # pragma: no cover - only if run outside the repo
        pytest.skip(f"no justfile at {_JUSTFILE}")
    found: list[tuple[str, str]] = []
    for module, names in _IMPORT.findall(_JUSTFILE.read_text(encoding="utf-8")):
        for name in (n.strip() for n in names.split(",")):
            if name:
                found.append((module, name))
    return sorted(set(found))


def test_the_justfile_imports_something():
    """Guard the guard: a regex that silently matches nothing proves nothing."""
    assert _imports(), "no `from flotta… import …` found — has the pattern rotted?"


@pytest.mark.parametrize("module,name", _imports(), ids=lambda v: str(v))
def test_every_recipe_import_resolves(module, name):
    """Named per import so a failure says which recipe to fix, not just 'a recipe'."""
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - the failure this exists for
        pytest.fail(f"the justfile imports `{module}`, which does not exist: {exc}")
    assert hasattr(mod, name), (
        f"the justfile imports `{name}` from `{module}`, which has no such name"
    )


#: `--scope box:chat` anywhere in a recipe.
_SCOPE = re.compile(r"--scope\s+([\w:]+)")


def _scopes() -> list[str]:
    if not _JUSTFILE.is_file():  # pragma: no cover - only if run outside the repo
        pytest.skip(f"no justfile at {_JUSTFILE}")
    return sorted(set(_SCOPE.findall(_JUSTFILE.read_text(encoding="utf-8"))))


def test_the_justfile_mints_something():
    """Guard the guard, as above."""
    assert _scopes(), "no `--scope …` found — has the pattern rotted?"


@pytest.mark.parametrize("scope", _scopes())
def test_every_scope_a_recipe_mints_is_a_real_scope(scope):
    """A typo mints a token that is refused at the first request.

    `flotta token mint` does not validate what it is handed, so the failure
    surfaces as a 403 from the control plane — which reads as "my token is
    wrong" rather than "the recipe asked for a scope that does not exist".
    `just app` mints four of the five, so this is now four chances to misspell
    one in a file nothing else checks.
    """
    from flotta.auth import SCOPES

    assert scope in SCOPES, f"a recipe mints `{scope}`, which is not a scope: {sorted(SCOPES)}"


def test_the_suite_is_hermetic_against_a_developers_env():
    """`FLOTTA_*` must not reach a test from the ambient environment.

    This broke for real: deploying requires `FLOTTA_SIGNING_KEY` in `.env`, the
    justfile loads `.env`, and fifteen control-plane tests started returning
    401 — locally only, because CI has no `.env`. A suite that disagrees with
    itself depending on who runs it is worse than one that simply fails.
    """
    import os

    leaked = sorted(k for k in os.environ if k.startswith("FLOTTA_"))
    assert leaked == [], f"the autouse fixture in conftest.py did not strip: {leaked}"


# -- the Dockerfile is code the suite could not see either -------------------

_DOCKERFILE = _JUSTFILE.parent / "fly" / "Dockerfile"


def test_the_image_records_which_hermes_it_carries():
    """The label is the only place a running agent's Hermes version exists.

    `HERMES_REF` is a build arg: it decides what gets cloned and then
    evaporates. Before this label, nothing on a machine — and nothing anyone
    could ask a machine — said which Hermes was inside it, and the app showed
    the deployment tag in its place, which is a different fact entirely.

    Hermetic, so it does not prove Docker's substitution works; it proves the
    line has not been dropped, which is the failure that would be invisible
    until somebody opened the panel and found a blank where a version was.
    """
    from flotta.backends.fly_backend import HERMES_REF_LABEL

    if not _DOCKERFILE.is_file():  # pragma: no cover - only outside the repo
        pytest.skip(f"no Dockerfile at {_DOCKERFILE}")
    body = _DOCKERFILE.read_text(encoding="utf-8")

    label = next(
        (line for line in body.splitlines() if line.startswith(f"LABEL {HERMES_REF_LABEL}")),
        None,
    )
    assert label, f"fly/Dockerfile no longer labels the image with {HERMES_REF_LABEL}"
    assert "$HERMES_REF" in label, (
        f"{HERMES_REF_LABEL} must carry the build arg, not a literal — "
        f"a hardcoded version is worse than none: {label!r}"
    )
    assert "ARG HERMES_REF" in body, "the label would substitute to empty with no ARG in scope"
