"""API tests for naming-template settings and previews."""

import pytest

from naming_templates import NamingTemplates
import web_ui


@pytest.fixture
def client(tmp_path, monkeypatch):
    manager = NamingTemplates(str(tmp_path / "templates.json"))
    monkeypatch.setitem(web_ui.renamer_state, "naming_templates", manager)
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client()


def test_get_and_apply_home_assistant_preset(client):
    assert client.get("/api/naming_templates").get_json()["preset"] == "entity_manager"
    response = client.put("/api/naming_templates", json={"preset": "home_assistant"})
    assert response.status_code == 200
    assert response.get_json()["templates"]["entity_name"] == "{entity}"


def test_invalid_template_is_400(client):
    response = client.put(
        "/api/naming_templates",
        json={
            "preset": "custom",
            "templates": {
                "device_name": "{device}",
                "entity_name": "{unknown}",
                "entity_id": "{device} {entity}",
            },
        },
    )
    assert response.status_code == 400
    assert "Unknown placeholders" in response.get_json()["error"]


def test_preview_renders_floor_and_domain(client):
    response = client.post(
        "/api/naming_templates/preview",
        json={
            "templates": {
                "device_name": "{floor} {device}",
                "entity_name": "{area} {entity}",
                "entity_id": "{floor_id} {device} {entity}",
            },
            "context": {
                "floor": "Ground floor",
                "floor_id": "ground",
                "area": "Kitchen",
                "device": "Climate sensor",
                "entity": "Temperature",
                "domain": "sensor",
            },
        },
    )
    assert response.status_code == 200
    assert response.get_json()["rendered"] == {
        "device_name": "Ground floor Climate sensor",
        "entity_name": "Kitchen Temperature",
        "entity_id": "sensor.ground_climate_sensor_temperature",
    }
