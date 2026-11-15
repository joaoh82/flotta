"""The CLI beside a fleet that is not production (FLOTTA-16).

Found running the README's quickstart on `just local`: `flotta chat` dialled
`https://<agent>.flotta.dev` with a locally minted token, `flotta create` wrote
`./fleet.db` beside a control plane, `flotta logs` read that file, and the
operator's CLI answered 404 for every agent outside the default org.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
from typer.testing import CliRunner

from flotta import cli
from flotta.cli import ControlPlaneError, app
from flotta.client import ChatError, door_url

KEY = "k" * 32


@pytest.fixture(autouse=True)
def _no_dotenv(tmp_path, monkeypatch):
    """`door_url` and the CLI read `.env` as well as the environment, and a
    deployed one names flotta.dev; these tests are about what the CLI does
    without it."""
    monkeypatch.chdir(tmp_path)


# -- where `flotta chat` dials ---------------------------------------------


def test_chat_dials_the_door_url_without_tls_for_a_local_door():
    env = {"FLOTTA_DOOR_URL": "ws://localhost:8081"}

    assert door_url("dev--acme--studio", env=env) == "http://dev--acme--studio.localhost:8081"


def test_a_wss_door_url_is_dialled_over_tls():
    env = {"FLOTTA_DOOR_URL": "wss://boxes.example.org"}

    assert door_url("eng-a", env=env) == "https://eng-a.boxes.example.org"


def test_the_door_url_wins_over_the_domain():
    env = {"FLOTTA_DOOR_URL": "ws://localhost:8381", "FLOTTA_DOMAIN": "flotta.dev"}

    assert door_url("eng-a", env=env) == "http://eng-a.localhost:8381"


def test_a_door_url_that_is_not_one_is_refused_in_words():
    with pytest.raises(ChatError, match="ws:// or wss://"):
        door_url("eng-a", env={"FLOTTA_DOOR_URL": "http://localhost:8081/x"})


@pytest.mark.parametrize(
    "control", ["http://127.0.0.1:8080", "http://localhost:8080", "http://[::1]:8080"]
)
def test_a_local_fleet_never_falls_back_to_the_production_domain(control):
    """The token in hand was minted for this machine; flotta.dev is not its door."""
    with pytest.raises(ChatError, match="FLOTTA_DOOR_URL"):
        door_url("dev--acme--studio", env={"FLOTTA_CONTROL_URL": control})


def test_a_deployed_fleet_still_defaults_to_flotta_dev():
    env = {"FLOTTA_CONTROL_URL": "https://control.flotta.dev"}

    assert door_url("eng-a", env=env) == "https://eng-a.flotta.dev"


def test_a_local_fleet_with_a_domain_set_uses_it():
    env = {"FLOTTA_CONTROL_URL": "http://127.0.0.1:8080", "FLOTTA_DOMAIN": "boxes.example.org"}

    assert door_url("eng-a", env=env) == "https://eng-a.boxes.example.org"


def test_flotta_chat_sends_its_token_to_the_local_door(monkeypatch, tmp_path):
    """The bug itself: the token went to `https://<agent>.flotta.dev`."""
    from flotta import client

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLOTTA_DOOR_URL", "ws://localhost:8381")
    monkeypatch.setenv("FLOTTA_TOKEN", "a-local-token")
    dialled: list[str] = []

    async def login(session, base_url, *args, **kwargs):
        dialled.append(base_url)
        raise ChatError("stop here")

    monkeypatch.setattr(client, "login", login)

    result = CliRunner().invoke(app, ["chat", "dev--acme--studio", "hello"])

    assert result.exit_code == 1
    assert dialled == ["http://dev--acme--studio.localhost:8381"]


def test_subdomains_of_localhost_resolve_to_this_machine():
    """RFC 6761; macOS's resolver answers nothing for them, so the client does."""
    from flotta.client import _LocalhostResolver

    async def resolve():
        resolver = _LocalhostResolver()
        try:
            return await resolver.resolve("dev--acme--studio.localhost", 8081, socket.AF_UNSPEC)
        finally:
            await resolver.close()

    found = asyncio.run(resolve())

    assert {(r["host"], r["port"]) for r in found} == {("127.0.0.1", 8081), ("::1", 8081)}


def test_other_names_are_left_to_the_system_resolver():
    from flotta.client import _LocalhostResolver

    asked: list[str] = []

    class Inner:
        async def resolve(self, host, port, family):
            asked.append(host)
            return []

        async def close(self):
            pass

    async def resolve():
        resolver = _LocalhostResolver()
        await resolver._inner.close()
        resolver._inner = Inner()
        await resolver.resolve("eng-a.flotta.dev", 443)

    asyncio.run(resolve())

    assert asked == ["eng-a.flotta.dev"]


# -- whose token the CLI mints ---------------------------------------------


def test_a_minted_token_acts_in_the_org_it_is_minted_for():
    from flotta.auth import verify

    env = {"FLOTTA_SIGNING_KEY": KEY}

    assert verify(cli.control_token("fleet:write", env=env, org="studio"), env=env).org == "studio"
    assert verify(cli.control_token("fleet:write", env=env, org="default"), env=env).org is None
    assert verify(cli.control_token("fleet:write", env=env), env=env).org is None


def test_a_supplied_token_is_used_whatever_org_is_asked_for():
    env = {"FLOTTA_CONTROL_TOKEN": "supplied", "FLOTTA_SIGNING_KEY": KEY}

    assert cli.control_token("fleet:write", env=env, org="studio") == "supplied"


@pytest.fixture
def remote(monkeypatch, tmp_path):
    """A fleet API that knows one agency agent, `dev--acme--studio` (`b-dev`).

    Like the real one, it answers the operator's token (no org) with a 404 for
    it, and finds any org's agent for `fleet:route`.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", KEY)
    monkeypatch.setenv("FLOTTA_CONTROL_URL", "http://127.0.0.1:8080")
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    calls: list[dict] = []
    box = {"id": "b-dev", "name": "dev--acme--studio", "org": "studio", "client": "acme"}

    def fake(method, path, *, scopes, body=None, org=None, **kwargs):
        calls.append({"method": method, "path": path, "scopes": set(scopes), "org": org})
        if path.endswith("/address"):
            return {"box": box}
        if path == "/api/boxes":
            return {"box": {**box, "status": "provisioning"}, "box_id": "b-dev"}
        if "b-dev" in path or "dev--acme--studio" in path:
            if org != "studio":
                raise ControlPlaneError(f"{method} {path} -> 404: no box", status=404)
            if path.endswith("/runs"):
                return {"id": "t-1", "status": "pending", "prompt": "hi"}
            if path.endswith("/events"):
                return {
                    "events": [
                        {
                            "id": 1,
                            "entity_kind": "box",
                            "entity_id": "b-dev",
                            "ts": "2026-11-14T10:00:00+00:00",
                            "type": "running",
                            "payload": None,
                        }
                    ]
                }
            return {"box": {**box, "status": "running"}, "tasks": []}
        raise ControlPlaneError(f"{method} {path} -> 404: no box", status=404)

    monkeypatch.setattr(cli, "control_request", fake)
    return calls


def test_run_by_address_mints_in_the_agents_org_without_asking(remote):
    result = CliRunner().invoke(app, ["run", "dev--acme--studio", "hi"])

    assert result.exit_code == 0, result.output
    assert [(c["path"], c["org"]) for c in remote] == [
        ("/api/boxes/dev--acme--studio/runs", "studio")
    ]


def test_run_by_id_in_another_org_finds_the_org_and_asks_again(remote):
    result = CliRunner().invoke(app, ["run", "b-dev", "hi"])

    assert result.exit_code == 0, result.output
    assert [(c["path"], c["org"]) for c in remote] == [
        ("/api/boxes/b-dev/runs", None),
        ("/api/boxes/b-dev/address", None),
        ("/api/boxes/b-dev/runs", "studio"),
    ]
    assert remote[1]["scopes"] == {"fleet:route"}


def test_an_agent_nobody_has_is_still_the_same_404(remote):
    result = CliRunner().invoke(app, ["run", "nope", "hi"])

    assert result.exit_code == 1
    assert "404" in result.output


def test_a_supplied_token_is_not_second_guessed(remote, monkeypatch):
    monkeypatch.setenv("FLOTTA_CONTROL_TOKEN", "an-owners-token")

    result = CliRunner().invoke(app, ["run", "b-dev", "hi"])

    assert result.exit_code == 1
    assert [c["path"] for c in remote] == ["/api/boxes/b-dev/runs"]


# -- `flotta create` and `flotta logs` through the control plane -----------


def test_create_goes_to_the_control_plane_and_writes_no_store(remote, tmp_path):
    result = CliRunner().invoke(
        app, ["create", "dev", "--org", "studio", "--client", "acme", "--description", "Dev"]
    )

    assert result.exit_code == 0, result.output
    assert [(c["method"], c["path"], c["org"]) for c in remote] == [
        ("POST", "/api/boxes", "studio")
    ]
    assert not (tmp_path / "fleet.db").exists()
    assert "dev--acme--studio" in result.output


def test_create_sends_what_it_was_given(monkeypatch, remote):
    sent: list[dict] = []
    real = cli.control_request

    def capture(method, path, *, scopes, body=None, org=None, **kwargs):
        sent.append(body)
        return real(method, path, scopes=scopes, body=body, org=org, **kwargs)

    monkeypatch.setattr(cli, "control_request", capture)

    CliRunner().invoke(app, ["create", "dev", "--org", "studio", "--client", "acme", "--screen"])

    assert sent == [{"name": "dev", "org": "studio", "client": "acme", "screen": True}]


def test_create_refuses_an_image_the_control_plane_cannot_use(remote):
    result = CliRunner().invoke(app, ["create", "dev", "--image", "registry/x:1"])

    assert result.exit_code == 2
    assert "--store" in result.output
    assert remote == []


def test_logs_reads_the_timeline_from_the_control_plane(remote, tmp_path):
    result = CliRunner().invoke(app, ["logs", "dev--acme--studio"])

    assert result.exit_code == 0, result.output
    assert [(c["path"], c["org"]) for c in remote] == [
        ("/api/boxes/dev--acme--studio", "studio"),
        ("/api/boxes/b-dev/events", "studio"),
    ]
    assert "b-dev  dev--acme--studio  running" in result.output
    assert "running" in result.output.splitlines()[-1]
    assert not (tmp_path / "fleet.db").exists()
