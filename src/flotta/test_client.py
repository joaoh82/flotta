"""Tests for the thin client — the local half of the inversion.

Hermetic: no flyctl, no tunnel, no box. What is worth pinning is that this
module stays *thin*. If it ever grows agent logic the inversion has quietly
reversed, and a test is a cheaper reminder than a code review.
"""

import pytest

from flotta.client import ChatError, free_local_port, wait_until_ready


def test_a_local_port_is_asked_of_the_os_not_guessed():
    """Two chats against different boxes must not collide — and a hardcoded
    9119 locally would also fight a Hermes the operator runs themselves."""
    a, b = free_local_port(), free_local_port()
    assert a > 1024 and b > 1024


def test_wait_until_ready_accepts_any_non_5xx():
    """A 401 from the auth gate proves Hermes is serving.

    Readiness and authorisation are different questions: the box is up the
    moment something answers, and treating "401" as "not ready" would spin for
    the full timeout against a perfectly healthy box.
    """
    import urllib.error

    calls = {"n": 0}

    def fake_urlopen(url, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    import flotta.client as client

    real = client.urllib.request.urlopen
    client.urllib.request.urlopen = fake_urlopen
    try:
        wait_until_ready("http://127.0.0.1:1", timeout_s=5)
    finally:
        client.urllib.request.urlopen = real
    assert calls["n"] == 1, "should return on the first answer, not keep polling"


def test_wait_until_ready_keeps_waiting_through_a_5xx():
    """ "Up but broken" is not ready — a 502 while Hermes boots is normal."""
    import urllib.error

    import flotta.client as client

    def always_502(url, timeout=None):
        raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, None)

    real = client.urllib.request.urlopen
    client.urllib.request.urlopen = always_502
    try:
        with pytest.raises(ChatError, match="did not answer"):
            wait_until_ready("http://127.0.0.1:1", timeout_s=1)
    finally:
        client.urllib.request.urlopen = real


def test_the_client_holds_no_agent_logic():
    """The inversion, as an assertion.

    v0.1 ran the orchestrator locally. If this module ever imports an agent or
    a model client, the laptop has started thinking again and the pivot has
    silently reversed.
    """
    source = (__import__("pathlib").Path(__file__).parent / "client.py").read_text()
    for forbidden in ("run_agent", "AIAgent", "openai", "anthropic", "run_conversation"):
        assert forbidden not in source, f"the thin client must not reference {forbidden!r}"


# -- authenticating against a box -------------------------------------------


def test_credentials_come_from_the_dotenv(tmp_path):
    from flotta.client import USERNAME, credentials

    env = tmp_path / ".env"
    env.write_text("FLOTTA_MODEL=x\nFLOTTA_BOX_PASSWORD=s3cret\n")
    assert credentials(str(env)) == (USERNAME, "s3cret")


def test_missing_credentials_point_at_the_command_that_mints_them(tmp_path):
    from flotta.client import credentials

    env = tmp_path / ".env"
    env.write_text("FLOTTA_MODEL=x\n")
    with pytest.raises(ChatError, match="just fly-auth"):
        credentials(str(env))

    with pytest.raises(ChatError, match="just fly-auth"):
        credentials(str(tmp_path / "nope.env"))


def test_the_cookie_jar_must_be_unsafe_for_ip_hosts():
    """A tunnel is always 127.0.0.1, and aiohttp will not store cookies for an
    IP host by default.

    Without this the login returns 200, the cookie is silently dropped, and the
    next request is anonymous — which surfaces as a bare 401 from
    `/api/auth/ws-ticket` and reads as a credentials problem rather than a
    cookie-jar policy. Cost an entire debugging pass; pinned so it cannot
    regress quietly.
    """
    import asyncio

    from flotta.client import new_session

    async def check():
        session = new_session()
        try:
            assert session.cookie_jar._unsafe is True
        finally:
            await session.close()

    asyncio.run(check())
