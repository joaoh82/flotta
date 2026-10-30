"""The fleet's password stays off every agent, the first one included (FLOTTA-96)."""

from __future__ import annotations

from flotta.fleet_key import ENV, PASSWORD_ENV, bootstrap_secrets, main, record


def test_the_first_machine_never_gets_the_fleets_password(tmp_path, monkeypatch, capsys):
    """`fly-auth` used to install it as that machine's login. Root there could
    then derive every agent's password from an id."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{ENV}=the-fleet-key\n")
    monkeypatch.setenv(ENV, "the-fleet-key")

    assert main(["bootstrap"]) == 0
    sent = capsys.readouterr().out
    assert "the-fleet-key" not in sent
    pairs = dict(line.split("=", 1) for line in sent.splitlines())
    assert sorted(pairs) == sorted(bootstrap_secrets())
    assert pairs[PASSWORD_ENV], "the machine still refuses an empty login"


def test_each_bootstrap_login_is_new():
    assert bootstrap_secrets()[PASSWORD_ENV] != bootstrap_secrets()[PASSWORD_ENV]


def test_a_fleet_password_is_written_when_there_is_none(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("FLOTTA_DOMAIN=flotta.dev\n")
    assert record(dotenv, replace=False) is True
    lines = dotenv.read_text().splitlines()
    assert lines[0] == "FLOTTA_DOMAIN=flotta.dev"
    assert lines[1].startswith(f"{ENV}=") and len(lines[1]) > len(ENV) + 20


def test_fly_auth_keeps_the_fleet_password_it_already_has(tmp_path):
    """A new one would lock the door out of every agent until each is rotated."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{ENV}=kept\n")
    assert record(dotenv, replace=False) is False
    assert dotenv.read_text() == f"{ENV}=kept\n"


def test_a_fleet_password_can_be_replaced_and_nothing_else_moves(tmp_path, capsys):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"A=1\n{ENV}=old\nB=2\n")
    assert main(["generate", "--replace", "--dotenv", str(dotenv)]) == 0
    lines = dotenv.read_text().splitlines()
    assert lines[:2] == ["A=1", "B=2"]
    assert lines[2].startswith(f"{ENV}=") and lines[2] != f"{ENV}=old"
    assert lines[2].split("=", 1)[1] not in capsys.readouterr().out, "never echoed"
