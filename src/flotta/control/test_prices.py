"""`POST /api/prices/refresh` (FLOTTA-87): the deployed prices, from a laptop.

Who may call it is in `test_tenancy.py`'s operator table and `test_view_as.py`'s
sweep of every route. This is what it does for the operator.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from flotta import model_catalog
from flotta.control.app import create_app
from flotta.store import FleetStore

ENTRY = {"id": "m/x", "pricing": {"prompt": "0.000003", "completion": "0.000015"}}
ROUTER = {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}}


@pytest.fixture
def fleet(tmp_path):
    return str(tmp_path / "fleet.db")


@pytest.fixture
def client(fleet):
    app = create_app(store_factory=lambda: FleetStore(fleet), run_loop=False)
    with TestClient(app) as c:
        yield c


def test_a_refresh_stores_the_catalogues_prices_and_names_what_it_skipped(
    client, fleet, monkeypatch
):
    monkeypatch.setattr(model_catalog, "_entries", lambda timeout: [ENTRY, ROUTER])

    response = client.post("/api/prices/refresh")

    assert response.status_code == 200, response.text
    assert response.json() == {"models": 1, "changed": 1, "skipped": ["openrouter/auto"]}
    with FleetStore(fleet) as store:
        price = store.price_of("m/x", at=datetime.now(UTC))
    assert price is not None
    assert price.currency == "USD"


def test_refreshing_again_changes_nothing(client, monkeypatch):
    monkeypatch.setattr(model_catalog, "_entries", lambda timeout: [ENTRY])
    client.post("/api/prices/refresh")
    assert client.post("/api/prices/refresh").json()["changed"] == 0


def test_a_catalogue_that_cannot_be_read_is_a_502_and_changes_nothing(client, fleet, monkeypatch):
    def down(timeout):
        raise OSError("no route")

    monkeypatch.setattr(model_catalog, "_entries", down)

    response = client.post("/api/prices/refresh")

    assert response.status_code == 502
    assert "no route" in response.json()["detail"]
    with FleetStore(fleet) as store:
        assert store.price_of("m/x", at=datetime.now(UTC)) is None
