"""A token a test module mints when it is imported outlives the whole run.

pytest imports every test module at collection, before any test runs. A token
minted then, as a module constant (`WRITER = _bearer(...)`) or in a
`parametrize` list, starts its clock at collection, not when its tests run.
Minted for 300 seconds, it held while the suite took less than five minutes,
and once the suite took eight, `control/test_triggers_api.py` failed with
"token expired" whenever it ran late. It passed alone, passed on a rerun that
happened to be quicker, and was blamed on whatever branch was being tested.

So this reads every test module's import-time tokens and refuses one that
would not last `OUTLIVE_S`. A token minted inside a test may be as short as
it likes; a test about expiry passes its own `now`.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import time
from collections.abc import Iterator
from typing import Any

import flotta
from flotta.auth import TICKET_PREFIX, TOKEN_PREFIX, _unb64

#: Far longer than any run has taken (about eight minutes in November 2026).
OUTLIVE_S = 6 * 3600


def _test_modules() -> Iterator[Any]:
    for info in pkgutil.walk_packages(flotta.__path__, "flotta."):
        if info.name.rpartition(".")[2].startswith("test_"):
            yield importlib.import_module(info.name)


def _tokens(value: Any, depth: int = 0) -> Iterator[str]:
    """Every token in `value`: bare, as a `Bearer` header, or inside containers."""
    if depth > 5:
        return
    if isinstance(value, str):
        token = value.removeprefix("Bearer ")
        if _expires(token) is not None:
            yield token
    elif isinstance(value, dict):
        for item in value.values():
            yield from _tokens(item, depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _tokens(item, depth + 1)


def _minted_at_import(module: Any) -> Iterator[tuple[str, str]]:
    """(where, token) for each module constant and each `parametrize` value."""
    for name, value in vars(module).items():
        for token in _tokens(value):
            yield name, token
        for mark in getattr(value, "pytestmark", ()):
            for token in _tokens(mark.args):
                yield f"{name} (@{mark.name})", token


def _expires(token: str) -> int | None:
    """When a token expires, or `None` for a string that is not one (`flotta_x` names)."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    body = token.removeprefix(TICKET_PREFIX).removeprefix(TOKEN_PREFIX)
    try:
        exp = json.loads(_unb64(body.split(".")[0]))["exp"]
    except (ValueError, KeyError, TypeError):
        return None
    return int(exp)


def _left(token: str, now: float) -> float:
    expires = _expires(token)
    assert expires is not None
    return expires - now


def test_a_token_minted_at_import_outlives_the_run():
    now = time.time()
    short = sorted(
        f"{module.__name__}.{where}: {_left(token, now) / 60:.0f} minutes"
        for module in _test_modules()
        for where, token in _minted_at_import(module)
        if _left(token, now) < OUTLIVE_S
    )
    assert not short, (
        "These tokens are minted when their module is imported, at collection, and "
        "expire before a slow run reaches them. Mint with the default TTL, or inside "
        "the test:\n" + "\n".join(short)
    )


def test_the_guard_sees_the_ways_a_token_is_held_at_import():
    """Not vacuous: it finds header dicts, bare tokens and parametrize lists."""
    found = {
        (module.__name__, where.split(" ")[0])
        for module in _test_modules()
        for where, _ in _minted_at_import(module)
    }
    assert ("flotta.control.test_triggers_api", "WRITER") in found
    assert ("flotta.door.test_two_orgs", "OPERATOR") in found
    assert len(found) > 20
