"""The control plane's routes, read as source: is every one of them tenant-scoped?

M11 puts more than one org on one control plane, and the door's subject check
(M11.1) only guards the path *through the door*. The fleet API is reachable
directly, and today 28 of its 29 routes take their token as `_` and look boxes
up with `store.get_box` — so a token for org A that names org B's box id gets
org B's box. Nothing in the behavioural suite notices, because every test runs
one org, and in one org an unscoped lookup and a scoped one return the same row.

So this reads the source and insists, as `test_every_read_check_write_is_guarded`
does for transactions. A route is **scoped** when:

1. it binds its principal — the `needs_*` dependency is a named parameter the
   body actually reads, not `_` and not dead; and
2. nothing it reaches looks a box up unscoped — no call to a fleet-wide read
   (`UNSCOPED_READS`), directly or through a helper, except inside a function
   in `SCOPE_FUNCTIONS`, which is where the tenant check is made.

**Or** it is an operator route (`OPERATOR_ROUTES`): one that acts on the whole
fleet on purpose — settings, image builds, rolling Hermes — and so may read it
whole, provided its first statement is `_require_operator(<its principal>)`.
First, so nothing the route does happens before the refusal.

What it cannot see: functions imported from other modules. `provision.wake_box`
reads a box by the id it is given, which is safe exactly when that id came from
a scoped lookup — and this reader takes the route's word for it.

The routes not yet scoped are in `UNSCOPED_ROUTES`. **That list is a ratchet**:
a new route that is not scoped fails, and so does a listed route that has become
scoped and was not taken off — otherwise it could quietly regress. M11.6a
scoped every box route and M11.6b the two a box calls to reach its colleagues,
so it is empty, and a route that would go on it is a decision a reviewer sees.
"""

from __future__ import annotations

import ast
import inspect

import pytest

#: Store reads that answer for the whole fleet. Each one, called from a route,
#: is a way to reach a box without asking whose it is. (`peers_for_box` and
#: `may_message` were here until they kept to the box's own client.)
UNSCOPED_READS = frozenset({"get_box", "get_box_by_name", "list_boxes"})

#: Where a box lookup is allowed to happen, because the tenant check happens
#: there too. Adding a name is a decision a reviewer must see: everything it
#: reaches is exempt from this test. Each has behavioural tests of its own in
#: `test_tenancy.py`.
SCOPE_FUNCTIONS: frozenset[str] = frozenset(
    {
        "_scoped_box",  # one box, if the token's tenant may act on it
        "_scoped_boxes",  # the token's org's boxes, or its client's
        "_routable_box",  # `_scoped_box`, or any box for the door's `fleet:route`
        "_operated_box",  # `_scoped_box`, or any box for the operator (FLOTTA-86)
        "_occupant",  # fleet-wide on purpose (a machine is held or not); names scoped
    }
)

#: Routes that act on the whole fleet and are the operator's alone. Each must
#: open with `_require_operator(<principal>)`; after that it may read anything.
OPERATOR_ROUTES = frozenset(
    {
        "get_settings",
        "put_settings",
        "hermes_versions",
        "list_builds",
        "update_hermes",
        "create_org",
        "add_user",
        "approve_org",
        "decline_org",
        # The operator's view (M12.9): everyone, every org's agents, and a
        # read-only session in an agency.
        "view_as",
        "list_users",
        "list_fleet_boxes",
    }
)

#: Routes that read the fleet's configuration, not any box, for anyone who can
#: read the fleet: their answer is the same whoever asks, so they need not read
#: their token. They still take one, so an unauthenticated caller is refused,
#: and they may not reach a box any more than any other route.
CONFIG_READS = frozenset({"list_models"})

#: Routes with no principal at all, on purpose. They may not reach a box.
#: The sign-in and sign-up routes are how a person without a credential gets one.
OPEN_ROUTES = frozenset({"health", "request_link", "redeem_link", "sign_up"})

#: The routes not yet scoped. May only shrink — and is empty since M11.6b.
UNSCOPED_ROUTES: frozenset[str] = frozenset()

#: Every `FastAPI` method that registers the function it decorates.
_ROUTE_DECORATORS = frozenset(
    {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "trace",
        "api_route",
        "websocket",
        "websocket_route",
    }
)

#: Every other way to put a route on the app — none of them a decorator, so
#: none of them seen by this reader. `app.py` may not use them.
_UNREAD_REGISTRATIONS = frozenset(
    {
        "add_api_route",
        "add_api_websocket_route",
        "add_route",
        "include_router",
        "mount",
        "host",
        "frontend",
    }
)

#: `FastAPI`'s remaining public names, none of which adds a route.
_NOT_ROUTES = frozenset(
    {
        "add_exception_handler",
        "add_middleware",
        "build_middleware_stack",
        "exception_handler",
        "middleware",
        "on_event",
        "openapi",
        "routes",
        "setup",
        "url_path_for",
    }
)

_Function = ast.FunctionDef | ast.AsyncFunctionDef


def _is_route(node: _Function) -> bool:
    return any(
        isinstance(d, ast.Call)
        and isinstance(d.func, ast.Attribute)
        and isinstance(d.func.value, ast.Name)
        and d.func.value.id == "app"
        and d.func.attr in _ROUTE_DECORATORS
        for d in node.decorator_list
    )


def _functions(tree: ast.AST) -> list[_Function]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]


def _principal(node: _Function) -> str | None:
    """The parameter a `needs_*` dependency fills, if there is one."""
    positional = node.args.posonlyargs + node.args.args
    defaulted = positional[len(positional) - len(node.args.defaults) :]
    pairs = list(zip(defaulted, node.args.defaults, strict=True))
    pairs += [(a, d) for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True) if d]
    for arg, default in pairs:
        if isinstance(default, ast.Name) and default.id.startswith("needs_"):
            return arg.arg
    return None


def _names_read(node: _Function) -> set[str]:
    return {
        n.id
        for stmt in node.body
        for n in ast.walk(stmt)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


def _unscoped_reads(node: _Function) -> set[str]:
    """Every fleet-wide read `node` names — called, aliased or handed on.

    A reference, not only a call: `lookup = store.get_box` and
    `Thread(target=store.get_box)` read the fleet just as a call does.
    """
    return {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute) and n.attr in UNSCOPED_READS
    }


def _guards_first(node: _Function, principal: str) -> bool:
    """Whether the body opens (after a docstring) with `_require_operator(principal, …)`."""
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if not body or not isinstance(body[0], ast.Expr) or not isinstance(body[0].value, ast.Call):
        return False
    call = body[0].value
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "_require_operator"
        and bool(call.args)
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == principal
    )


def _unscoped_routes(
    source: str,
    *,
    open_routes: frozenset[str] = OPEN_ROUTES,
    scope_functions: frozenset[str] = SCOPE_FUNCTIONS,
    operator_routes: frozenset[str] = OPERATOR_ROUTES,
    config_reads: frozenset[str] = CONFIG_READS,
) -> dict[str, list[str]]:
    """Every route that is not scoped, with why. Empty means all of them are.

    A function counts if it is *referenced* at all, not only called: one handed
    to a thread does its lookups on the route's behalf just the same. Routes
    are functions too — a new route calling an old unscoped one inherits its
    reads, or the list would let it in on the old one's entry.
    """
    functions = _functions(ast.parse(source))
    routes = [f for f in functions if _is_route(f)]

    # Keyed by name, as references are; same-named functions are merged, which
    # can only over-report.
    graph: dict[str, list[_Function]] = {}
    for f in functions:
        if f.name not in scope_functions:
            graph.setdefault(f.name, []).append(f)

    # What each function reaches, to a fixpoint: functions call functions.
    reach = {
        name: {(read, name) for f in fs for read in _unscoped_reads(f)}
        for name, fs in graph.items()
    }
    changed = True
    while changed:
        changed = False
        for name, fs in graph.items():
            for f in fs:
                for callee in _names_read(f) & graph.keys():
                    new = reach[callee] - reach[name]
                    if new:
                        reach[name] |= new
                        changed = True

    unscoped: dict[str, list[str]] = {}
    seen: set[str] = set()
    for route in routes:
        reasons = []
        if route.name in seen:
            reasons.append("shares its name with another route, so a list of names is ambiguous")
        seen.add(route.name)

        principal = _principal(route)
        if principal is None:
            if route.name not in open_routes:
                reasons.append("takes no principal")
        elif principal == "_":
            reasons.append("discards its token as `_`")
        elif principal not in _names_read(route) and route.name not in config_reads:
            reasons.append(f"never reads its token `{principal}`")

        if route.name in operator_routes:
            if principal is None or not _guards_first(route, principal):
                reasons.append("is an operator route that does not open with `_require_operator`")
            if reasons:
                unscoped[route.name] = reasons
            # Fleet-wide reads are what an operator route is for.
            continue

        found = {(read, None) for read in _unscoped_reads(route)}
        for callee in _names_read(route) & graph.keys():
            found |= reach[callee]
        for read, via in sorted(found, key=lambda pair: (pair[0], pair[1] or "")):
            reasons.append(f"calls `{read}`" + (f" via `{via}`" if via else ""))

        if reasons:
            unscoped[route.name] = reasons
    return unscoped


def _app_source() -> str:
    from flotta.control import app

    return inspect.getsource(app)


def test_every_route_resolves_boxes_through_the_tenant_scope():
    unscoped = _unscoped_routes(_app_source())

    new = {name: why for name, why in unscoped.items() if name not in UNSCOPED_ROUTES}
    assert not new, (
        "route(s) that do not go through the tenant scope: "
        + "; ".join(f"{name}: {', '.join(why)}" for name, why in sorted(new.items()))
        + ". With more than one org on the control plane, an unscoped lookup "
        "hands one org's token another org's box. Bind the token and resolve "
        "the box through a function in SCOPE_FUNCTIONS."
    )

    fixed = UNSCOPED_ROUTES - unscoped.keys()
    assert not fixed, (
        f"{sorted(fixed)} now scoped (or gone) — take them off UNSCOPED_ROUTES, "
        "or a later change can unscope them again and nothing will notice."
    )


def test_the_unscoped_allowlist_names_real_routes():
    """A stale name protects nothing, and a renamed route escapes the list."""
    tree = ast.parse(_app_source())
    functions = {f.name: f for f in _functions(tree)}
    routes = {f.name for f in _functions(tree) if _is_route(f)}

    # Whether the reader still finds routes at all: if `app` were renamed,
    # every list below would be checked against nothing and pass.
    assert {"health", "box_address", "peer_ask"} <= routes

    assert not UNSCOPED_ROUTES - routes, f"not routes: {sorted(UNSCOPED_ROUTES - routes)}"
    assert not OPEN_ROUTES - routes, f"not routes: {sorted(OPEN_ROUTES - routes)}"
    assert not OPERATOR_ROUTES - routes, f"not routes: {sorted(OPERATOR_ROUTES - routes)}"
    assert not UNSCOPED_ROUTES & OPEN_ROUTES
    assert not OPERATOR_ROUTES & (UNSCOPED_ROUTES | OPEN_ROUTES)

    assert not SCOPE_FUNCTIONS - functions.keys(), (
        f"not functions: {sorted(SCOPE_FUNCTIONS - functions.keys())}"
    )
    for name in SCOPE_FUNCTIONS:
        annotations = [ast.unparse(a.annotation) for a in functions[name].args.args if a.annotation]
        # Not proof the check is right — its own tests are — but a scope
        # function that is never handed a token cannot be making one.
        assert any("Token" in a for a in annotations), f"{name} takes no token"


def test_every_route_is_registered_where_the_reader_looks():
    """Only decorators are read, so only decorators may register a route.

    `app.include_router(...)` or `app.add_api_route("/x", leak)` would put an
    endpoint on the fleet API that the test above never sees.
    """
    tree = ast.parse(_app_source())
    used = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in _UNREAD_REGISTRATIONS
    }
    assert not used, f"app.py registers routes the reader cannot see: {sorted(used)}"


def test_every_way_fastapi_adds_a_route_is_accounted_for():
    """A FastAPI upgrade that adds one fails here, rather than going unread."""
    from fastapi import FastAPI

    public = {name for name in dir(FastAPI) if not name.startswith("_")}
    known = _ROUTE_DECORATORS | _UNREAD_REGISTRATIONS | _NOT_ROUTES
    assert not public - known, f"unclassified FastAPI method(s): {sorted(public - known)}"


# -- the reader itself, against source written to break it ---------------------


# Spelled out, not `sorted(_ROUTE_DECORATORS)`: a case list drawn from the set
# under test loses the case along with the entry, and passes.
@pytest.mark.parametrize(
    "method",
    [
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "trace",
        "api_route",
        "websocket",
        "websocket_route",
    ],
)
def test_the_reader_finds_every_kind_of_route(method):
    route = f"@app.{method}('/boxes')\ndef leak():\n    return store.list_boxes()"
    assert _unscoped_routes(route, open_routes=frozenset()) == {
        "leak": ["takes no principal", "calls `list_boxes`"]
    }


_HEADER = """
needs_read = object()
def _scoped_box(store, token: Token, ref):
    return store.get_box(ref)
"""


@pytest.mark.parametrize(
    ("route", "reason"),
    [
        (
            "def r(box_id: str, _: Token | None = needs_read):\n"
            "    return _scoped_box(store, _, box_id)",
            "discards its token as `_`",
        ),
        (
            "def r(box_id: str, token: Token | None = needs_read):\n"
            "    return _scoped_box(store, None, box_id)",
            "never reads its token `token`",
        ),
        ("def r(box_id: str):\n    return box_id", "takes no principal"),
        (
            "def r(box_id: str, token: Token | None = needs_read):\n"
            "    return token and store.get_box(box_id)",
            "calls `get_box`",
        ),
        (
            "def _lookup(store, ref):\n"
            "    return store.get_box_by_name(ref)\n"
            "def _indirect(store, ref):\n"
            "    return _lookup(store, ref)\n"
            "@app.get('/x')\n"
            "def r(box_id: str, token: Token | None = needs_read):\n"
            "    return token and _indirect(store, box_id)",
            "calls `get_box_by_name` via `_lookup`",
        ),
        (
            "def _later(box_id):\n"
            "    store.list_boxes()\n"
            "@app.post('/x')\n"
            "async def r(token: Token | None = needs_read):\n"
            "    threading.Thread(target=_later, args=(token,)).start()",
            "calls `list_boxes` via `_later`",
        ),
        (
            "def r(box_id: str, token: Token | None = needs_read):\n"
            "    lookup = store.get_box\n"
            "    return token and lookup(box_id)",
            "calls `get_box`",
        ),
        (
            "def r(box_id: str, token: Token | None = needs_read):\n"
            "    threading.Thread(target=store.get_box, args=(token,)).start()",
            "calls `get_box`",
        ),
        (
            "@app.get('/legacy')\n"
            "def legacy(box_id: str, _: Token | None = needs_read):\n"
            "    return store.get_box(box_id)\n"
            "@app.get('/x')\n"
            "def r(box_id: str, token: Token | None = needs_read):\n"
            "    return token.subject and legacy(box_id, token)",
            "calls `get_box` via `legacy`",
        ),
    ],
    ids=[
        "discarded",
        "unread",
        "no-principal",
        "direct",
        "through-helpers",
        "handed-to-a-thread",
        "bound-method-alias",
        "bound-method-thread-target",
        "through-another-route",
    ],
)
def test_the_reader_catches(route, reason):
    """Each way a route can skip the scope, written out and caught.

    The test above passing on today's source says nothing if this reader is
    blind. These are the guard reintroduced, kept.
    """
    if "@app." not in route:
        route = "@app.get('/x')\n" + route
    unscoped = _unscoped_routes(
        _HEADER + route, open_routes=frozenset(), scope_functions=frozenset({"_scoped_box"})
    )
    assert reason in unscoped.get("r", []), unscoped


def test_the_reader_passes_a_scoped_route():
    route = (
        "@app.get('/x')\n"
        "def r(box_id: str, token: Token | None = needs_read):\n"
        "    return _scoped_box(store, token, box_id)"
    )
    unscoped = _unscoped_routes(
        _HEADER + route, open_routes=frozenset(), scope_functions=frozenset({"_scoped_box"})
    )
    assert unscoped == {}


def test_an_open_route_may_not_reach_a_box():
    route = "@app.get('/health')\ndef health():\n    return store.list_boxes()"
    unscoped = _unscoped_routes(route, open_routes=frozenset({"health"}))
    assert unscoped == {"health": ["calls `list_boxes`"]}


@pytest.mark.parametrize(
    "route",
    [
        "def ops(token: Token | None = needs_read):\n    return store.list_boxes()",
        "def ops(token: Token | None = needs_read):\n"
        "    boxes = store.list_boxes()\n"
        "    _require_operator(token, 'x')\n"
        "    return boxes",
        "def ops(token: Token | None = needs_read):\n"
        "    _require_operator(None, 'x')\n"
        "    return token and store.list_boxes()",
    ],
    ids=["no-guard", "guard-after-the-read", "guard-on-another-value"],
)
def test_an_operator_route_must_refuse_before_it_reads(route):
    unscoped = _unscoped_routes(
        "@app.get('/x')\n" + route,
        open_routes=frozenset(),
        scope_functions=frozenset(),
        operator_routes=frozenset({"ops"}),
    )
    assert "is an operator route that does not open with `_require_operator`" in unscoped["ops"]


def test_an_operator_route_that_refuses_first_may_read_the_fleet():
    route = (
        "@app.get('/x')\n"
        "def ops(token: Token | None = needs_read):\n"
        '    """Docstring first is fine."""\n'
        "    _require_operator(token, 'x')\n"
        "    return store.list_boxes()"
    )
    assert (
        _unscoped_routes(
            route,
            open_routes=frozenset(),
            scope_functions=frozenset(),
            operator_routes=frozenset({"ops"}),
        )
        == {}
    )
