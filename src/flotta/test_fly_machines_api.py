"""Listing machines over Fly's Machines API instead of `flyctl` (FLOTTA-182).

Hermetic: the HTTP GET is injected, and every `flyctl` call fails the test,
so a listing that fell back to the subprocess would be caught here.
"""

from __future__ import annotations

import pytest

from flotta.backend import BackendError
from flotta.backends.fly_backend import (
    FlyBackend,
    MachinesApi,
    authorization_for,
)
from flotta.fly import FlyConfig

ENDPOINT = "fly://joaoh82-flotta-eng-g/815990c9246728"
TOKEN = "fm2_the-secret-macaroon"

MACHINE = {
    "id": "815990c9246728",
    "state": "started",
    "region": "ams",
    "private_ip": "fdaa::3",
    "created_at": "2026-10-30T10:00:00Z",
    "updated_at": "2026-11-10T09:00:00Z",
    "host_status": "ok",
    "incomplete_config": None,
    "image_ref": {
        "registry": "registry.fly.io",
        "repository": "joaoh82-flotta-eng-g",
        "tag": "v1",
        "labels": {"dev.flotta.hermes-ref": "v0.22.5"},
    },
    "config": {
        "image": "registry.fly.io/joaoh82-flotta-eng-g:v1",
        "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 2048},
        "mounts": [{"volume": "vol_1", "path": "/data", "size_gb": 3}],
        "env": {"FLOTTA_BOX_ID": "b-1", "FLOTTA_WORKDIR": "/workspace"},
    },
    "events": [{"id": "e1", "status": "started", "timestamp": 1762765200000}],
}


def _offline_config():
    return FlyConfig.from_env({}, dotenv="/nonexistent/.env")


def _no_flyctl(cmd, *, timeout, check, stdin=None):
    raise AssertionError(f"listing must not shell out: {cmd}")


def _fly(*answers):
    """A GET giving each answer in turn, and the requests it was given."""
    seen = []
    queue = list(answers)

    def get(url, *, headers, timeout):
        seen.append({"url": url, "headers": headers, "timeout": timeout})
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return get, seen


def _backend(*answers):
    get, seen = _fly(*answers)
    backend = FlyBackend(
        config=_offline_config(),
        runner=_no_flyctl,
        machines_api=MachinesApi(TOKEN, get=get),
    )
    return backend, seen


# -- the request ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "header"),
    [
        ("FlyV1 fm2_abc,fm2_def", "FlyV1 fm2_abc,fm2_def"),
        ("fm2_abc", "FlyV1 fm2_abc"),
        ("fm1r_abc", "FlyV1 fm1r_abc"),
        ("personal-access-token", "Bearer personal-access-token"),
        ("  fm2_abc\n", "FlyV1 fm2_abc"),
    ],
)
def test_the_header_is_the_one_flyctl_would_send(token, header):
    assert authorization_for(token) == header


def test_one_get_lists_the_apps_machines():
    backend, seen = _backend((200, [MACHINE]))
    backend.inspect(ENDPOINT)

    (request,) = seen
    assert request["url"] == "https://api.machines.dev/v1/apps/joaoh82-flotta-eng-g/machines"
    assert request["headers"] == {"Authorization": "FlyV1 fm2_the-secret-macaroon"}
    assert request["timeout"] > 0


def test_inspect_reads_the_apis_machine_as_it_read_flyctls():
    backend, _ = _backend((200, [MACHINE]))
    info = backend.inspect(ENDPOINT)

    assert info.state == "started"
    assert info.image == "registry.fly.io/joaoh82-flotta-eng-g:v1"
    assert info.hermes_ref == "v0.22.5"
    assert (info.cpu_kind, info.cpus, info.memory_mb) == ("shared", 1, 2048)
    assert (info.volume_id, info.volume_gb, info.volume_path) == ("vol_1", 3, "/data")
    assert info.workdir == "/workspace"
    assert info.env_box_id == "b-1"
    assert info.last_up_at is not None


def test_a_machine_the_listing_lacks_is_gone():
    backend, _ = _backend((200, []))
    assert backend.inspect(ENDPOINT).state == "gone"
    backend, _ = _backend((200, []))
    assert backend.state(ENDPOINT) == "gone"


# -- could not ask is not "not there" (FLOTTA-179) -------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        (401, {"error": "token is not authorized"}),
        (404, {"error": "app not found"}),
        (503, None),
        OSError("connection reset"),
    ],
)
def test_a_failed_listing_raises_for_inspect_and_state(answer):
    backend, _ = _backend(answer)
    with pytest.raises(BackendError, match="could not list machines for joaoh82-flotta-eng-g"):
        backend.inspect(ENDPOINT)
    backend, _ = _backend(answer)
    with pytest.raises(BackendError, match="could not list machines"):
        backend.state(ENDPOINT)


@pytest.mark.parametrize("answer", [(500, None), OSError("connection reset")])
def test_a_failed_listing_is_empty_where_a_lenient_caller_asked(answer):
    backend, _ = _backend(answer)
    assert backend.image_of(ENDPOINT) is None
    backend, _ = _backend(answer)
    assert backend.existing_endpoint("eng-g") is None


def test_fly_s_own_reason_is_kept():
    backend, _ = _backend((401, {"error": "token is not authorized for this app"}))
    with pytest.raises(BackendError, match="Fly answered 401: token is not authorized"):
        backend.inspect(ENDPOINT)


def test_an_answer_in_another_shape_raises_even_where_lenient():
    for body in ({"machines": []}, None):
        backend, _ = _backend((200, body))
        with pytest.raises(BackendError, match="shape may have changed"):
            backend.image_of(ENDPOINT)


@pytest.mark.parametrize(
    "answer",
    [(401, {"error": "nope"}), OSError(f"failed with Authorization: FlyV1 {TOKEN}")],
)
def test_no_error_carries_the_token(answer):
    backend, _ = _backend(answer)
    with pytest.raises(BackendError) as caught:
        backend.inspect(ENDPOINT)
    assert "fm2_" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_the_api_client_names_no_token():
    assert "fm2_" not in repr(MachinesApi(TOKEN))


# -- where it is used -------------------------------------------------------------------


def test_a_token_in_the_environment_makes_an_api_client():
    assert MachinesApi.from_env({"FLY_API_TOKEN": TOKEN}) is not None
    assert MachinesApi.from_env({"FLY_ACCESS_TOKEN": TOKEN}) is not None


@pytest.mark.parametrize("env", [{}, {"FLY_API_TOKEN": "  "}])
def test_without_a_token_listing_stays_on_flyctl(env):
    assert MachinesApi.from_env(env) is None


def test_a_backend_built_from_the_environment_lists_over_http(monkeypatch):
    monkeypatch.setenv("FLY_API_TOKEN", TOKEN)
    monkeypatch.delenv("FLY_ACCESS_TOKEN", raising=False)
    assert FlyBackend(config=_offline_config())._api is not None


def test_a_test_that_fakes_flyctl_keeps_listing_through_it(monkeypatch):
    """Every existing backend test injects a runner; none may start asking Fly."""
    import subprocess

    monkeypatch.setenv("FLY_API_TOKEN", TOKEN)
    listed = []

    def runner(cmd, *, timeout, check, stdin=None):
        listed.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    backend = FlyBackend(config=_offline_config(), runner=runner)
    assert backend.state(ENDPOINT) == "gone"
    assert listed and listed[0][:3] == ["flyctl", "machines", "list"]


def test_writes_still_go_through_flyctl():
    """Only the listing moved: starting a machine is still `flyctl machines start`."""
    import subprocess

    ran = []

    def runner(cmd, *, timeout, check, stdin=None):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    get, seen = _fly(*[(200, [{**MACHINE, "state": "stopped"}])] * 2, (200, [MACHINE]))
    backend = FlyBackend(
        config=_offline_config(), runner=runner, machines_api=MachinesApi(TOKEN, get=get)
    )
    backend.start(ENDPOINT)

    assert any(cmd[:3] == ["flyctl", "machines", "start"] for cmd in ran)
    assert not any(cmd[:3] == ["flyctl", "machines", "list"] for cmd in ran)
    assert seen, "the settle loop listed over HTTP"
