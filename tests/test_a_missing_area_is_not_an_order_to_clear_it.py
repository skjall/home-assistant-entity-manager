"""Assigning an area: what a caller must say before a device loses the one it has.

null is a meaningful value here -- it takes a device out of every area -- which
is why a missing key used to reach the registry as "clear it". A caller who
misspells the parameter, or whose field is dropped on the way, says nothing
about areas at all; that is not the same as asking for none.
"""

import pytest

import routes_entities
import web_ui


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://supervisor/core")
    monkeypatch.setenv("HA_TOKEN", "token")
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client()


@pytest.fixture
def registry_calls(monkeypatch):
    """Record what would have been written, without opening a socket."""
    calls = []

    class Socket:
        def __init__(self, *args, **kwargs):
            pass

        async def connect(self):
            calls.append(("connect",))

        async def disconnect(self):
            pass

    class Registry:
        def __init__(self, ws):
            pass

        async def assign_area(self, device_id, area_id):
            calls.append(("assign", device_id, area_id))
            return {"success": True}

    monkeypatch.setattr(routes_entities, "HomeAssistantWebSocket", Socket)
    monkeypatch.setattr(routes_entities, "DeviceRegistry", Registry)
    return calls


def test_a_parameter_that_never_arrived_does_not_clear_the_area(client, registry_calls):
    """The shape that emptied a device: the area went under another key."""
    response = client.post("/api/assign_device_area", json={"device_id": "dev1", "area": "badezimmer"})

    assert response.status_code == 400
    assert "area_id" in response.get_json()["error"]
    assert registry_calls == []


def test_an_area_id_spelled_out_as_null_still_clears_it(client, registry_calls):
    response = client.post("/api/assign_device_area", json={"device_id": "dev1", "area_id": None})

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "device_id": "dev1", "area_id": None}
    assert ("assign", "dev1", None) in registry_calls


def test_an_area_that_cannot_be_read_is_refused_rather_than_emptied(client, registry_calls):
    """A name in place of an id: spaces and umlauts fail the id check."""
    response = client.post("/api/assign_device_area", json={"device_id": "dev1", "area_id": "Wohnzimmer Süd"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid area ID"
    assert registry_calls == []


def test_an_area_that_reads_is_passed_on(client, registry_calls):
    response = client.post("/api/assign_device_area", json={"device_id": "dev1", "area_id": "badezimmer"})

    assert response.status_code == 200
    assert response.get_json()["area_id"] == "badezimmer"
    assert ("assign", "dev1", "badezimmer") in registry_calls
