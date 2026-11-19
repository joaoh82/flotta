"""An agent from the agency's own image (M17.1): the operator's switch, the image, and no Hermes."""

from __future__ import annotations

import ast
import inspect

import pytest

from flotta import provision
from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN, mint
from flotta.backend import BoxHandle
from flotta.control.test_size_api import KEY, MEMBER, OTHER_OWNER, OWNER, _api, _finish
from flotta.control.test_size_api import fleet as fleet  # noqa: F401 - the fixture
from flotta.store import (
    CustomImagesOffError,
    FleetStore,
    InvalidImageError,
    validate_image_ref,
)

IMAGE = "ghcr.io/agency-x/support-agent:1.0"
OPERATOR = {
    "Authorization": "Bearer "
    + mint(scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN}, key=KEY, subject="op")
}


class Machine:
    """Create, start and re-image with no `flyctl`; remembers what it was given."""

    scheme = "fly"
    takes_image = True

    def __init__(self):
        self.specs = []
        self.images: dict[str, str] = {}
        self.secrets: dict[str, str] = {}

    def create(self, spec):
        self.specs.append(spec)
        self.images["fly://flotta-new/m9"] = spec.image
        return BoxHandle(id="m9", endpoint="fly://flotta-new/m9", memory_mb=spec.memory_mb)

    def start(self, endpoint):
        pass

    def state(self, endpoint):
        return "started"

    def existing_endpoint(self, name=None):
        return None

    def image(self, endpoint):
        return self.images.get(endpoint)

    def reimage(self, endpoint, image):
        self.images[endpoint] = image

    def apply_secrets(self, endpoint, secrets):
        self.secrets.update(secrets)


@pytest.fixture
def machine(monkeypatch):
    fake = Machine()
    monkeypatch.setattr(provision, "_default_backend", lambda: fake)
    monkeypatch.setattr(provision, "_resolve_backend", lambda box, backend=None: backend or fake)
    monkeypatch.setattr(provision, "_peek_image", lambda impl, target: fake.images.get(target))
    monkeypatch.setattr(provision, "resolve_fleet_image", lambda: ("registry.fly.io/hermes:v1", ""))
    monkeypatch.setattr(provision, "_fleet_image", lambda: "registry.fly.io/hermes:v1")
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", KEY)
    monkeypatch.setenv("FLOTTA_BOX_PASSWORD", "fleet-password")
    monkeypatch.setenv("FLOTTA_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setenv("FLOTTA_MODEL", "openai/gpt-5-mini")
    monkeypatch.setenv("FLOTTA_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("FLOTTA_API_KEY", "sk-the-fleets-own-key")
    return fake


def _switch(fleet, org="agency-x", on=True):  # noqa: F811
    with FleetStore(fleet) as store:
        store.set_org_custom_images(org, on)


def _create(api, headers, **body):
    return api.post(
        "/api/boxes",
        json={"name": "own", "client": "acme", "runtime": "custom", "image": IMAGE, **body},
        headers=headers,
    )


def _own(fleet):  # noqa: F811
    with FleetStore(fleet) as store:
        return store.get_box_by_name("own--acme--agency-x")


# -- the switch -------------------------------------------------------------


def test_an_org_cannot_run_its_own_image_until_the_operator_says_so(fleet, machine):  # noqa: F811
    with _api(fleet, background=False) as api:
        response = _create(api, OWNER)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "custom_images_off"
    assert machine.specs == [] and _own(fleet) is None


def test_the_store_refuses_it_too_whoever_reserves_the_row(fleet):  # noqa: F811
    """The API's check is the early answer; this is the guarantee."""
    with FleetStore(fleet) as store:
        with pytest.raises(CustomImagesOffError):
            store.create_box("own", org="agency-x", client="acme", runtime="custom", image=IMAGE)
        # No row holds the name: the refusal came before the insert.
        assert store.get_box_by_name("own--acme--agency-x") is None


def test_a_switch_turned_off_mid_create_spends_no_name(fleet, machine, monkeypatch):  # noqa: F811
    """The API's early check passed; the operator turns it off before the row."""
    _switch(fleet)
    real = provision.check_custom_request

    def then_off(*args, **kwargs):
        cleaned = real(*args, **kwargs)
        _switch(fleet, on=False)
        return cleaned

    monkeypatch.setattr(provision, "check_custom_request", then_off)
    with _api(fleet) as api:
        assert _create(api, MEMBER).status_code == 403
    assert _own(fleet) is None and machine.specs == []


def test_only_the_operator_turns_it_on(fleet):  # noqa: F811
    with _api(fleet) as api:
        for who in (OWNER, MEMBER):
            refused = api.put("/api/orgs/agency-x/custom-images", json={"on": True}, headers=who)
            assert refused.status_code == 403, refused.text
        on = api.put("/api/orgs/agency-x/custom-images", json={"on": True}, headers=OPERATOR)
        assert on.status_code == 200, on.text
        assert on.json()["org"]["custom_images"] is True
        assert (
            api.put(
                "/api/orgs/agency-x/custom-images", json={"on": "yes"}, headers=OPERATOR
            ).status_code
            == 422
        )
        assert (
            api.put(
                "/api/orgs/nobody/custom-images", json={"on": True}, headers=OPERATOR
            ).status_code
            == 404
        )
        listed = {o["id"]: o for o in api.get("/api/orgs", headers=OPERATOR).json()["orgs"]}
    assert listed["agency-x"]["custom_images"] is True
    assert listed["agency-y"]["custom_images"] is False


# -- creating one -----------------------------------------------------------


def test_a_member_creates_one_on_its_image_with_the_gateway_and_no_hermes(fleet, machine):  # noqa: F811
    _switch(fleet)
    with _api(fleet, background=False) as api:
        response = _create(api, MEMBER)
    assert response.status_code in (200, 201), response.text
    box = response.json()["box"]
    assert (box["runtime"], box["image"]) == ("custom", IMAGE)
    (spec,) = machine.specs
    assert spec.image == IMAGE
    assert spec.env["FLOTTA_RUNTIME"] == "custom"
    # The door's secret, and the gateway under OpenAI's names...
    assert spec.secrets["FLOTTA_BOX_SECRET"]
    assert spec.secrets["OPENAI_BASE_URL"] == "https://gateway.example/v1"
    assert spec.secrets["OPENAI_API_KEY"] == spec.secrets["FLOTTA_API_KEY"]
    assert spec.secrets["OPENAI_MODEL"] == "openai/gpt-5-mini"
    # ...and nothing of Hermes's, nor the fleet's own key anywhere.
    assert not [k for k in spec.secrets if k.startswith("HERMES_")]
    assert "sk-the-fleets-own-key" not in spec.secrets.values()
    assert "OPENROUTER_API_KEY" not in spec.secrets


def test_the_202_path_carries_the_image_to_the_thread(fleet, machine):  # noqa: F811
    _switch(fleet)
    with _api(fleet) as api:
        response = _create(api, MEMBER)
        assert response.status_code == 202, response.text
        assert response.json()["box"]["runtime"] == "custom"
        _finish()
    (spec,) = machine.specs
    assert spec.image == IMAGE


def test_off_the_gateway_a_custom_agent_gets_no_key_at_all(fleet, machine, monkeypatch):  # noqa: F811
    """A Hermes agent off the gateway holds the fleet's key; the agency's code must not."""
    monkeypatch.delenv("FLOTTA_GATEWAY_URL")
    _switch(fleet)
    with _api(fleet, background=False) as api:
        assert _create(api, MEMBER).status_code in (200, 201)
    (spec,) = machine.specs
    assert "sk-the-fleets-own-key" not in spec.secrets.values()
    assert not {"FLOTTA_API_KEY", "OPENAI_API_KEY"} & set(spec.secrets)
    with FleetStore(fleet) as store:
        missing = [
            e for e in store.get_events("box", _own(fleet).id) if e.type == "fleet_secrets_missing"
        ]
    assert "FLOTTA_GATEWAY_URL" in str(missing[0].payload)


@pytest.mark.parametrize(
    "extra", [{"screen": True}, {"instructions": "be nice"}, {"model": "openai/gpt-5-mini"}]
)
def test_what_only_hermes_has_is_refused_before_a_name_is_spent(fleet, machine, extra):  # noqa: F811
    _switch(fleet)
    with _api(fleet) as api:
        response = _create(api, OWNER, **extra)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "custom_runtime"
    assert machine.specs == [] and _own(fleet) is None


@pytest.mark.parametrize(
    "image",
    [
        "",
        "https://ghcr.io/x/y:1",
        "user:hunter2@ghcr.io/x/y:1",
        "ghcr.io/X/Y:1",
        "ghcr.io/x/y:1 ; rm -rf /",
        "a" * 300,
    ],
)
def test_an_image_that_cannot_be_pulled_or_carries_a_credential_is_a_422(fleet, machine, image):  # noqa: F811
    _switch(fleet)
    with _api(fleet) as api:
        assert _create(api, OWNER, image=image).status_code == 422
    assert machine.specs == []


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/agency-x/agent:1.0",
        "agency/agent",
        "docker.io/library/python:3.12-slim",
        "registry.example.com:5000/a/b/c:latest",
        "ghcr.io/a/b@sha256:" + "a" * 64,
    ],
)
def test_real_references_are_taken(image):
    assert validate_image_ref(f"  {image} ") == image


def test_a_hermes_agent_names_no_image(fleet, machine):  # noqa: F811
    _switch(fleet)
    with _api(fleet) as api:
        response = api.post(
            "/api/boxes", json={"name": "own", "client": "acme", "image": IMAGE}, headers=OWNER
        )
    assert response.status_code == 422
    with pytest.raises(InvalidImageError), FleetStore(fleet) as store:
        store.create_box("other", org="agency-x", client="acme", image=IMAGE)


@pytest.mark.parametrize(
    "image",
    [
        "registry.fly.io/flotta-images:deployment-01J",
        "flotta-images.fly.dev/agent:1",
        "my-registry.internal:5000/agent:1",
        "localhost:5000/agent:1",
        "10.0.0.1:5000/agent:1",
    ],
)
def test_the_fleets_own_registries_are_not_public(image):
    """Fly pulls these with the fleet's credentials: the fleet's Hermes, or another app's."""
    with pytest.raises(InvalidImageError):
        validate_image_ref(image)


def test_creating_a_hermes_agent_is_exactly_as_before(fleet, machine):  # noqa: F811
    with _api(fleet, background=False) as api:
        response = api.post("/api/boxes", json={"name": "own", "client": "acme"}, headers=OWNER)
    assert response.status_code in (200, 201), response.text
    (spec,) = machine.specs
    assert spec.image == "registry.fly.io/hermes:v1"
    assert "FLOTTA_RUNTIME" not in spec.env and "FLOTTA_BOX_SECRET" not in spec.secrets
    assert spec.secrets["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"]
    assert response.json()["box"]["runtime"] == "hermes"


# -- its image, and never Hermes's ------------------------------------------


@pytest.fixture
def own(fleet, machine):  # noqa: F811
    _switch(fleet)
    with _api(fleet, background=False) as api:
        assert _create(api, OWNER).status_code in (200, 201)
    return _own(fleet)


def test_its_image_moves_and_the_timeline_says_from_what(fleet, machine, own):  # noqa: F811
    with _api(fleet) as api:
        response = api.put(
            f"/api/boxes/{own.id}/image",
            json={"image": "ghcr.io/agency-x/support-agent:2.0"},
            headers=MEMBER,
        )
    assert response.status_code == 200, response.text
    assert response.json()["box"]["image"] == "ghcr.io/agency-x/support-agent:2.0"
    assert machine.images[own.endpoint] == "ghcr.io/agency-x/support-agent:2.0"
    with FleetStore(fleet) as store:
        (moved,) = [e for e in store.get_events("box", own.id) if e.type == "reimaged"]
    assert moved.payload["from"] == IMAGE


def test_a_failed_move_leaves_the_row_naming_what_still_runs(fleet, machine, own):  # noqa: F811
    from flotta.backend import BackendError

    def broken(endpoint, image):
        raise BackendError("the registry said no")

    machine.reimage = broken
    with _api(fleet) as api:
        response = api.put(
            f"/api/boxes/{own.id}/image",
            json={"image": "ghcr.io/agency-x/support-agent:2.0"},
            headers=OWNER,
        )
    assert response.status_code == 502
    assert _own(fleet).image == IMAGE


def test_an_upgrade_never_puts_it_on_the_fleets_hermes(fleet, machine, own):  # noqa: F811
    """With no image named, an upgrade means the fleet's: Hermes. Refused."""
    with _api(fleet) as api:
        response = api.post(f"/api/boxes/{own.id}/upgrade", headers=OPERATOR)
        named = api.post(
            f"/api/boxes/{own.id}/upgrade",
            json={"image": "registry.fly.io/hermes:v1"},
            headers=OPERATOR,
        )
    assert response.status_code == 409, response.text
    assert named.status_code == 409, named.text
    assert machine.images[own.endpoint] == IMAGE
    with pytest.raises(provision.CustomRuntimeRefused), FleetStore(fleet) as store:
        provision.upgrade_box(own.id, store=store, image="registry.fly.io/hermes:v1")


def test_the_fleets_hermes_update_passes_it_by(fleet, machine, own, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FLOTTA_FLY_APP", "flotta-images")
    import flotta.images as images

    monkeypatch.setattr(images, "build_box_image", lambda ref, **kw: "registry.fly.io/hermes:v2")
    moved = []
    real = provision.upgrade_box
    monkeypatch.setattr(
        provision,
        "upgrade_box",
        lambda box_id, **kw: moved.append(box_id) or real(box_id, **kw),
    )
    with _api(fleet) as api:
        assert api.post("/api/hermes/update", json={}, headers=OPERATOR).status_code == 202
        _finish()
    with FleetStore(fleet) as store:
        hermes = [b.id for b in store.list_boxes() if b.runtime == "hermes"]
    # Not vacuous: the roll ran, and moved every Hermes agent but this one.
    assert hermes and set(hermes) <= set(moved)
    with _api(fleet) as api:
        (build,) = api.get("/api/hermes/builds", headers=OPERATOR).json()["builds"]
    assert {"box_id": own.id, "reason": "own_image"}.items() <= next(
        s for s in build["skipped"] if s["box_id"] == own.id
    ).items()
    assert own.id not in moved
    assert machine.images[own.endpoint] == IMAGE


def test_a_move_that_timed_out_after_landing_keeps_the_new_image(fleet, machine, own):  # noqa: F811
    from flotta.backend import BackendError

    def landed_then_timed_out(endpoint, image):
        machine.images[endpoint] = image
        raise BackendError("timed out waiting for the machine")

    machine.reimage = landed_then_timed_out
    with _api(fleet) as api:
        response = api.put(
            f"/api/boxes/{own.id}/image",
            json={"image": "ghcr.io/agency-x/support-agent:2.0"},
            headers=OWNER,
        )
    assert response.status_code == 502
    assert _own(fleet).image == "ghcr.io/agency-x/support-agent:2.0"


def test_its_image_does_not_move_once_the_switch_is_off(fleet, machine, own):  # noqa: F811
    _switch(fleet, on=False)
    with FleetStore(fleet) as store, pytest.raises(CustomImagesOffError):
        provision.set_custom_image(own.id, "ghcr.io/agency-x/support-agent:2.0", store=store)
    assert machine.images[own.endpoint] == IMAGE


def test_an_adopted_machine_is_moved_onto_the_agencys_image(fleet, machine):  # noqa: F811
    _switch(fleet)
    machine.images["fly://flotta-new/m9"] = "registry.fly.io/hermes:v1"
    real = machine.create

    def adopting(spec):
        handle = real(spec)
        machine.images[handle.endpoint] = "registry.fly.io/hermes:v1"
        return BoxHandle(id=handle.id, endpoint=handle.endpoint, adopted=True)

    machine.create = adopting
    with _api(fleet, background=False) as api:
        assert _create(api, MEMBER).status_code in (200, 201)
    assert machine.images["fly://flotta-new/m9"] == IMAGE


def test_an_adopted_machine_that_will_not_move_is_stopped_not_left_on_hermes(fleet, machine):  # noqa: F811
    from flotta.backend import BackendError

    _switch(fleet)
    real = machine.create
    stopped = []

    def adopting(spec):
        handle = real(spec)
        machine.images[handle.endpoint] = "registry.fly.io/hermes:v1"
        return BoxHandle(id=handle.id, endpoint=handle.endpoint, adopted=True)

    def refuse(endpoint, image):
        raise BackendError("no")

    machine.create, machine.reimage = adopting, refuse
    machine.stop = stopped.append
    with _api(fleet, background=False) as api:
        response = _create(api, MEMBER)
    assert response.status_code == 201 and "warning" in response.json(), response.text
    assert _own(fleet).status == "stopped" and stopped == ["fly://flotta-new/m9"]


def test_a_rotated_custom_agent_is_said_to_hold_its_own_secret(fleet, machine, own):  # noqa: F811
    with FleetStore(fleet) as store:
        result = provision.rotate_identity(own.id, store=store)
    assert result["own_password"] is True
    assert "FLOTTA_BOX_SECRET" in machine.secrets
    assert not [k for k in machine.secrets if k.startswith("HERMES_")]


def test_a_custom_agents_mcp_config_is_never_written(fleet, machine, own):  # noqa: F811
    with FleetStore(fleet) as store:
        assert provision.rewrite_mcp_config(own.id, store=store, reason="catalogue") is None


def test_a_hermes_agents_image_is_not_the_agencys_to_set(fleet, machine):  # noqa: F811
    _switch(fleet)
    with FleetStore(fleet) as store:
        hermes = store.get_box_by_name("support--acme--agency-x")
    with _api(fleet) as api:
        response = api.put(f"/api/boxes/{hermes.id}/image", json={"image": IMAGE}, headers=OWNER)
    assert response.status_code == 409, response.text


def test_another_orgs_agent_is_a_404(fleet, machine, own):  # noqa: F811
    with _api(fleet) as api:
        response = api.put(f"/api/boxes/{own.id}/image", json={"image": IMAGE}, headers=OTHER_OWNER)
    assert response.status_code == 404


# -- what only Hermes has ---------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "instructions", {"instructions": "be nice"}),
        ("put", "rules", {}),
        ("put", "mcp", {"servers": []}),
        ("put", "model", {"model": "openai/gpt-5-mini"}),
        ("put", "screen", {"screen": True}),
        ("put", "client-screen-control", {"allowed": True}),
    ],
)
def test_hermes_only_verbs_are_a_409_for_a_custom_agent(fleet, machine, own, method, path, body):  # noqa: F811
    with _api(fleet) as api:
        response = getattr(api, method)(f"/api/boxes/{own.id}/{path}", json=body, headers=OWNER)
    assert response.status_code == 409, (path, response.text)


def test_the_gateway_move_is_refused_so_the_fleets_key_never_reaches_it(fleet, machine, own):  # noqa: F811
    with _api(fleet) as api:
        response = api.delete(f"/api/boxes/{own.id}/gateway", headers=OPERATOR)
    assert response.status_code == 409, response.text


#: Every provision verb that works by rewriting Hermes on the box. A new one
#: belongs here, and its first statement is `hermes_only`.
HERMES_ONLY = (
    "set_instructions",
    "set_rules",
    "set_mcp_grants",
    "join_gateway",
    "leave_gateway",
    "set_model",
    "set_screen",
)


@pytest.mark.parametrize("name", HERMES_ONLY)
def test_every_hermes_only_verb_refuses_a_custom_agent_first(name):
    """The ratchet: read the source, so a refactor cannot drop the guard quietly."""
    tree = ast.parse(inspect.getsource(getattr(provision, name)))
    (func,) = tree.body
    statements = [
        s for s in func.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
    ]
    first = statements[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call), name
    assert getattr(first.value.func, "id", None) == "hermes_only", name


def _reaches_exec_unguarded() -> list[str]:
    """Public provision functions that reach a box `exec`, through any private
    helper, without a custom-runtime refusal on the way."""
    tree = ast.parse(inspect.getsource(provision))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def guarded(node: ast.FunctionDef) -> bool:
        if node.name in HERMES_ONLY:
            return True
        src = ast.unparse(node)
        return "hermes_only(" in src or "refuse_custom(" in src or "runtime == 'custom'" in src

    memo: dict[str, bool] = {}

    def reaches(name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name in memo:
            return memo[name]
        node = funcs[name]
        if guarded(node):
            memo[name] = False
            return False
        hit = False
        for n in ast.walk(node):
            if isinstance(n, ast.Attribute) and n.attr == "exec":
                hit = True
                break
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id in funcs
                and n.func.id not in seen | {name}
                and reaches(n.func.id, seen | {name})
            ):
                hit = True
                break
        memo[name] = hit
        return hit

    return sorted(name for name in funcs if not name.startswith("_") and reaches(name))


def test_nothing_execs_into_a_custom_agent_unguarded():
    """The ratchet: any public verb that runs a script on the box, directly or
    through a helper, refuses a custom agent first: every exec rewrites Hermes."""
    assert _reaches_exec_unguarded() == []
