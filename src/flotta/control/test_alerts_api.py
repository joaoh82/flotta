"""`/api/orgs/{org}/alerts` (FLOTTA-93): owners write, the secret is shown once."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flotta.auth import scopes_for_role, webhook_secret
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.store import FleetStore

SIGNING_KEY = "a-signing-key-for-these-tests"
HOOK = "https://hooks.example.com/flotta"
PATH = "/api/orgs/studio-nord/alerts"


@pytest.fixture
def fleet(tmp_path):
    db = str(tmp_path / "fleet.db")
    with FleetStore(db) as store:
        store.create_org("studio-nord", "Studio Nord")
        store.create_client("studio-nord", "acme", "Acme Dental")
        store.create_org("other", "Other Agency")
    return db


@pytest.fixture
def api(fleet, monkeypatch):
    monkeypatch.delenv("FLOTTA_ALLOW_LOCAL_WEBHOOKS", raising=False)
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        reachable=_unchecked,
        known_model=_uncatalogued,
        signing_key=SIGNING_KEY,
    )
    with TestClient(app) as client:
        yield client


def _as(role, org="studio-nord", client=None):
    from flotta.auth import mint

    token = mint(
        subject="cli",
        scopes=scopes_for_role(role, client=client),
        key=SIGNING_KEY,
        org=None if org == "default" else org,
        client=client,
    )
    return {"Authorization": f"Bearer {token}"}


def test_setting_a_webhook_answers_with_its_secret_once(api, fleet):
    answer = api.put(PATH, json={"webhook_url": HOOK}, headers=_as("owner"))
    assert answer.status_code == 200
    secret = answer.json()["webhook_secret"]
    with FleetStore(fleet) as store:
        settings = store.org_alerts("studio-nord")
    assert secret == webhook_secret(SIGNING_KEY, "studio-nord", settings.webhook_nonce)

    read = api.get(PATH, headers=_as("owner"))
    assert read.status_code == 200
    assert read.json()["webhook_url"] == HOOK
    assert secret not in read.text
    assert "webhook_secret" not in read.text
    assert "nonce" not in read.text


def test_the_secret_is_not_in_the_database(api, fleet):
    secret = api.put(PATH, json={"webhook_url": HOOK}, headers=_as("owner")).json()[
        "webhook_secret"
    ]
    with FleetStore(fleet) as store:
        assert store.org_alerts("studio-nord").webhook_nonce != secret


def test_setting_it_again_makes_a_new_secret(api):
    first = api.put(PATH, json={"webhook_url": HOOK}, headers=_as("owner")).json()
    second = api.put(PATH, json={"webhook_url": HOOK}, headers=_as("owner")).json()
    assert first["webhook_secret"] != second["webhook_secret"]


def test_changing_only_the_address_keeps_the_webhook_and_its_secret(api, fleet):
    api.put(PATH, json={"webhook_url": HOOK}, headers=_as("owner"))
    with FleetStore(fleet) as store:
        nonce = store.org_alerts("studio-nord").webhook_nonce
    answer = api.put(PATH, json={"alert_email": "Ops@Example.com "}, headers=_as("owner")).json()
    assert answer == {"alert_email": "ops@example.com", "webhook_url": HOOK}
    with FleetStore(fleet) as store:
        assert store.org_alerts("studio-nord").webhook_nonce == nonce


def test_null_removes_either(api):
    api.put(
        PATH, json={"webhook_url": HOOK, "alert_email": "ops@example.com"}, headers=_as("owner")
    )
    answer = api.put(PATH, json={"webhook_url": None, "alert_email": None}, headers=_as("owner"))
    assert answer.json() == {"alert_email": None, "webhook_url": None}


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/",
        "https://127.0.0.1/",
        "http://127.0.0.1:8184/",
        "https://10.0.0.8/",
        "https://169.254.169.254/",
        "https://[::1]/",
        "https://localhost/",
    ],
)
def test_a_webhook_that_could_reach_something_private_is_a_422(api, fleet, url):
    assert api.put(PATH, json={"webhook_url": url}, headers=_as("owner")).status_code == 422
    with FleetStore(fleet) as store:
        assert store.org_alerts("studio-nord") is None


def test_an_address_that_is_not_one_is_a_422(api):
    answer = api.put(PATH, json={"alert_email": "nobody"}, headers=_as("owner"))
    assert answer.status_code == 422


def test_a_member_reads_and_cannot_write(api):
    assert api.get(PATH, headers=_as("member")).status_code == 200
    assert api.put(PATH, json={"webhook_url": HOOK}, headers=_as("member")).status_code == 403


def test_a_clients_viewer_sees_none_of_it(api):
    assert api.get(PATH, headers=_as("client_viewer", client="acme")).status_code == 403


@pytest.mark.parametrize("role", ["owner", "member"])
def test_another_orgs_alerts_are_a_404(api, role):
    assert api.get(PATH, headers=_as(role, org="other")).status_code == 404


def test_an_owner_cannot_point_another_orgs_alerts_anywhere(api):
    answer = api.put(PATH, json={"webhook_url": HOOK}, headers=_as("owner", org="other"))
    assert answer.status_code == 404


def test_a_keyless_control_plane_is_on_a_laptop_and_may_have_a_local_webhook(fleet, monkeypatch):
    monkeypatch.delenv("FLOTTA_SIGNING_KEY", raising=False)
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        reachable=_unchecked,
        known_model=_uncatalogued,
    )
    with TestClient(app) as client:
        answer = client.put(PATH, json={"webhook_url": "http://127.0.0.1:8184/"})
    assert answer.status_code == 200
    assert answer.json()["webhook_secret"]


def test_the_alert_loop_runs_when_the_reconcile_loop_does(fleet, monkeypatch):
    started = []

    async def loop(store_factory, **kwargs):
        started.append(kwargs)

    async def runner(state, *, store_factory):
        return None

    monkeypatch.setattr("flotta.control.app.run_alert_loop", loop)
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=True,
        loop_runner=runner,
        background=False,
        signing_key=SIGNING_KEY,
        mailer=print,
        app_url="https://app.example.com",
    )
    with TestClient(app):
        pass
    assert started == [
        {
            "mailer": print,
            "app_url": "https://app.example.com",
            "signing_key": SIGNING_KEY,
            "allow_local": False,
        }
    ]
