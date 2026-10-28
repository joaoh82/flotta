"""A pending org cannot create an agent, through any route (M12.4).

The guard sign-up rests on. Anyone may sign an agency up, so an agency that
signed itself up must cost nothing until the operator approves it, and the
one thing that costs is an agent: a machine and a volume.

It holds because every way to make an agent writes the box row first, and
`FleetStore.create_box` refuses the row for an org that is not `active`. The
ratchet at the bottom reads the source and fails if a second way to write
that row, or to ask a substrate for a machine, appears. The API's own refusal
is in `control/test_sign_up.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from flotta import migrations
from flotta.store import (
    FleetStore,
    InvalidTransitionError,
    OrgNotActiveError,
    UnknownEntityError,
)
from flotta.test_migrations import target  # noqa: F401

SRC = Path(__file__).parent


@pytest.fixture
def fleet(target):  # noqa: F811
    with FleetStore(target) as store:
        store.create_org("signed-up", "Signed Up", status="pending")
        store.create_client("signed-up", "acme", "Acme")
        yield store


def _boxes(store: FleetStore) -> int:
    return store._conn.execute("SELECT COUNT(*) AS n FROM boxes").fetchone()["n"]


@pytest.mark.parametrize("approve", [None, False], ids=["pending", "declined"])
def test_an_org_that_is_not_active_cannot_create_an_agent(fleet, approve):
    if approve is not None:
        fleet.decide_org("signed-up", approve=approve)

    with pytest.raises(OrgNotActiveError):
        fleet.create_box("support", org="signed-up", client="acme")
    assert _boxes(fleet) == 0


def test_once_approved_it_can(fleet):
    fleet.decide_org("signed-up", approve=True)
    box = fleet.create_box("support", org="signed-up", client="acme")
    assert box.org == "signed-up"


def test_the_refusal_says_what_would_change_it(fleet):
    with pytest.raises(OrgNotActiveError, match="once the operator approves it"):
        fleet.create_box("support", org="signed-up", client="acme")


def test_an_org_is_decided_once(fleet):
    assert fleet.decide_org("signed-up", approve=False).status == "declined"
    with pytest.raises(InvalidTransitionError):
        fleet.decide_org("signed-up", approve=True)
    with pytest.raises(InvalidTransitionError):
        fleet.decide_org("default", approve=False)
    with pytest.raises(UnknownEntityError):
        fleet.decide_org("nobody", approve=True)


def test_an_org_the_operator_makes_is_active(fleet):
    assert fleet.create_org("agency-x", "Agency X").status == "active"
    assert fleet.get_org("default").status == "active"


def test_every_org_from_before_sign_up_is_active(target, monkeypatch):  # noqa: F811
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:2])
    with FleetStore(target) as before:
        before._conn.execute(
            "INSERT INTO orgs (id, name, created_at) VALUES (?, ?, ?)",
            ("agency-x", "Agency X", "2026-10-01T00:00:00+00:00"),
        )
    monkeypatch.undo()

    with FleetStore(target) as after:
        assert {o.id: o.status for o in after.list_orgs()} == {
            "default": "active",
            "agency-x": "active",
        }


# -- provisioning ------------------------------------------------------------------


class Tripwire:
    """A substrate that fails the test if anything at all is asked of it."""

    scheme = "fly"

    def __getattr__(self, name):
        raise AssertionError(f"a pending org's agent reached the substrate: .{name}")


def test_provisioning_refuses_before_the_substrate_is_asked_anything(fleet, monkeypatch):
    """Not even the occupancy probe: on Fly it is a `flyctl` call."""
    from flotta import provision

    def no_probe(*_a, **_k):
        raise AssertionError("a pending org's agent was probed for")

    monkeypatch.setattr(provision, "_peek_endpoint", no_probe)
    with pytest.raises(OrgNotActiveError):
        provision.create_box(
            "support", store=fleet, backend=Tripwire(), org="signed-up", client="acme"
        )
    assert _boxes(fleet) == 0


def test_the_cli_refuses_without_reaching_a_substrate(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from flotta import provision
    from flotta.cli import app

    db = tmp_path / "fleet.db"
    with FleetStore(db) as store:
        store.create_org("signed-up", "Signed Up", status="pending")
        store.create_client("signed-up", "acme", "Acme")

    def no_backend(_scheme):
        raise AssertionError("a pending org's agent reached a substrate")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(provision, "_backend_for", no_backend)
    args = ["create", "support", "--org", "signed-up", "--client", "acme", "--store", str(db)]
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 2, result.output
    assert "approves" in result.output
    with FleetStore(db) as store:
        assert _boxes(store) == 0


# -- the ratchet -------------------------------------------------------------------
#
# The guard is one check in one function, which is only "any route" while that
# function is the only way in. These read the source, so a second way in fails
# here rather than in an invoice.


def _sources() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if not p.name.startswith(("test_", "conftest"))]


def _enclosing(tree: ast.AST, node: ast.AST) -> str | None:
    """The innermost function `node` is in, by name."""
    best = None
    for fn in ast.walk(tree):
        if (
            isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
            and fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno)
            and (best is None or fn.lineno > best.lineno)
        ):
            best = fn
    return best.name if best else None


def _strings(tree: ast.AST, needle: str) -> list[ast.Constant]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and needle in n.value.upper()
    ]


def test_one_place_writes_a_box_row_and_it_asks_first():
    writers = [
        (path.relative_to(SRC).as_posix(), _enclosing(tree, node))
        for path in _sources()
        for tree in [ast.parse(path.read_text(encoding="utf-8"))]
        for node in _strings(tree, "INSERT INTO BOXES")
    ]
    assert writers == [("store.py", "create_box")], (
        f"box rows written from {writers}: each must refuse an org that is not active"
    )

    tree = ast.parse((SRC / "store.py").read_text(encoding="utf-8"))
    create_box = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "create_box"
    )
    asks = [
        n.lineno
        for n in ast.walk(create_box)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "require_active_org"
    ]
    insert = _strings(create_box, "INSERT INTO BOXES")[0].lineno
    assert asks and min(asks) < insert, "create_box must refuse a pending org before the insert"


def test_one_place_asks_a_substrate_for_a_machine_and_only_for_a_row():
    """`Backend.create` is the call that costs money. It is made in
    `provision.create_box`, which makes it only for a row `reserve_box` (and
    so `store.create_box`) wrote — or was handed one it had written."""
    callers = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"impl", "backend", "substrate"}
            ):
                callers.append((path.relative_to(SRC).as_posix(), _enclosing(tree, node)))
    assert callers == [("provision.py", "create_box")], callers
