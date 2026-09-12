"""API tests for type rules: learning, listing, previewing and the hierarchy contract."""

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_rules import NamingRules
from naming_templates import NamingTemplates
from type_mappings import DEFAULT_SYSTEM_MAPPINGS, TypeMappings
import web_ui


@pytest.fixture
def client(tmp_path, monkeypatch):
    rules = NamingRules(
        str(tmp_path / "naming_rules.json"),
        device_class_keys=DEFAULT_SYSTEM_MAPPINGS["device_class"].keys(),
        default_language="de",
    )
    mappings = TypeMappings(user_mappings_path=str(tmp_path / "unused.json"), rules=rules)
    overrides = NamingOverrides(str(tmp_path / "overrides.json"))
    restructurer = EntityRestructurer(
        client=object(),
        naming_overrides=overrides,
        type_mappings=mappings,
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
    )
    restructurer.floors = {}
    restructurer.areas = {"k": {"area_id": "k", "name": "Küche"}}
    restructurer.devices = {"d": {"id": "d", "name": "Deckenleuchte", "area_id": "k"}}
    restructurer.entities = {
        "number.a_effect_speed": {
            "id": "reg-a",
            "entity_id": "number.a_effect_speed",
            "device_id": "d",
            "platform": "mqtt",
            "original_name": "Effect speed",
            "has_entity_name": True,
        },
        "number.b_effect_speed": {
            "id": "reg-b",
            "entity_id": "number.b_effect_speed",
            "device_id": "d",
            "platform": "mqtt",
            "original_name": "effect_speed",
            "has_entity_name": True,
        },
    }
    monkeypatch.setitem(web_ui.renamer_state, "naming_rules", rules)
    monkeypatch.setitem(web_ui.renamer_state, "type_mappings", mappings)
    monkeypatch.setitem(web_ui.renamer_state, "naming_overrides", overrides)
    monkeypatch.setitem(web_ui.renamer_state, "restructurer", restructurer)
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client()


def test_learn_derives_the_key_server_side(client):
    response = client.post(
        "/api/naming/learn", json={"entity_id": "number.a_effect_speed", "value": "Effektgeschwindigkeit"}
    )

    assert response.status_code == 200
    rule = response.get_json()["rule"]
    assert rule["match"] == {"kind": "name", "value": "effect_speed", "integration": None, "model": None}
    assert rule["targets"] == {"de": "Effektgeschwindigkeit"}
    # Both spellings of the supplied name are covered by the one rule.
    assert rule["affected"] == 2


def test_learn_scoped_to_integration(client):
    response = client.post(
        "/api/naming/learn",
        json={"entity_id": "number.a_effect_speed", "value": "Effekt-Tempo", "scope": "integration"},
    )

    assert response.get_json()["rule"]["match"]["integration"] == "mqtt"


def test_preview_uses_rule_and_reports_provenance(client):
    client.post("/api/naming/learn", json={"entity_id": "number.a_effect_speed", "value": "Effektgeschwindigkeit"})

    response = client.post("/api/naming/preview", json={"entity_id": "number.b_effect_speed"})

    body = response.get_json()
    assert body["rendered"]["entity_name"] == "Küche Deckenleuchte Effektgeschwindigkeit"
    assert body["resolution"]["won_by"] == "rule:user"


def test_preview_with_replacement_type(client):
    response = client.post("/api/naming/preview", json={"entity_id": "number.a_effect_speed", "type_value": "Tempo"})

    assert response.get_json()["rendered"]["entity_id"] == "number.kuche_deckenleuchte_tempo"


def test_rules_list_delete_and_settings(client):
    client.post("/api/naming/learn", json={"entity_id": "number.a_effect_speed", "value": "Effektgeschwindigkeit"})
    listed = client.get("/api/naming/rules").get_json()
    assert listed["language"] == "de"
    assert len(listed["rules"]) == 1 and listed["rules"][0]["affected"] == 2
    assert any(entry["key"] == "battery" for entry in listed["system"])

    rule_id = listed["rules"][0]["id"]
    updated = client.put(f"/api/naming/rules/{rule_id}", json={"targets": {"de": "Tempo"}}).get_json()["rule"]
    assert updated["targets"]["de"] == "Tempo"

    assert client.delete(f"/api/naming/rules/{rule_id}").status_code == 200
    assert client.get("/api/naming/rules").get_json()["rules"] == []

    assert client.put("/api/naming/settings", json={"language": "en"}).get_json()["language"] == "en"


def test_originals_are_grouped_by_canonical_key(client):
    originals = client.get("/api/naming/originals?q=effect").get_json()["originals"]

    assert originals == [
        {
            "key": "effect_speed",
            "example": "Effect speed",
            "translation_key": None,
            "count": 2,
            "integrations": ["mqtt"],
        }
    ]


def test_legacy_learn_endpoint_still_creates_a_rule(client):
    response = client.post("/api/learn_mapping", json={"type_key": "Factory reset", "translation": "Zurücksetzen"})

    assert response.status_code == 200
    rules = client.get("/api/naming/rules").get_json()["rules"]
    assert rules[0]["match"]["value"] == "factory_reset"


def _add_door_sensors(restructurer):
    for registry_id, platform in (("reg-c", "matter"), ("reg-d", "matter"), ("reg-e", "miele")):
        entity_id = f"binary_sensor.{registry_id}_tur"
        restructurer.entities[entity_id] = {
            "id": registry_id,
            "entity_id": entity_id,
            "device_id": "d",
            "platform": platform,
            "original_name": "Tür",
            "has_entity_name": True,
        }


def test_counts_separate_a_type_by_integration(client):
    """ "Tür" from Matter is a contact sensor, from Miele a fridge door; the counts tell them apart."""
    restructurer = web_ui.renamer_state["restructurer"]
    _add_door_sensors(restructurer)

    per_type = web_ui._type_key_counts(restructurer)
    per_integration = web_ui._type_key_integration_counts(restructurer)

    assert per_type["name:tuer"] == 3
    assert per_integration[("name:tuer", "matter")] == 2
    assert per_integration[("name:tuer", "miele")] == 1


def test_learning_for_one_integration_leaves_the_others_alone(client):
    restructurer = web_ui.renamer_state["restructurer"]
    _add_door_sensors(restructurer)

    response = client.post(
        "/api/naming/learn",
        json={"entity_id": "binary_sensor.reg-c_tur", "value": "Zustand", "scope": "integration"},
    )

    assert response.status_code == 200
    assert response.get_json()["rule"]["match"]["integration"] == "matter"
    assert response.get_json()["rule"]["affected"] == 2
    restructurer.build_naming_context("binary_sensor.reg-c_tur", restructurer.entities["binary_sensor.reg-c_tur"])
    assert restructurer.last_resolutions["binary_sensor.reg-c_tur"]["value"] == "Zustand"
    restructurer.build_naming_context("binary_sensor.reg-e_tur", restructurer.entities["binary_sensor.reg-e_tur"])
    assert restructurer.last_resolutions["binary_sensor.reg-e_tur"]["value"] == "Tür"


def test_learning_for_one_model_leaves_the_other_models_alone(client):
    restructurer = web_ui.renamer_state["restructurer"]
    restructurer.devices = {
        "d": {"id": "d", "name": "Deckenleuchte", "area_id": "k", "model": "MYGGBETT door/window sensor"},
        "e": {"id": "e", "name": "Kühlschrank", "area_id": "k", "model": "Fridge freezer"},
    }
    for registry_id, device_id, platform in (("reg-c", "d", "matter"), ("reg-e", "e", "miele")):
        entity_id = f"binary_sensor.{registry_id}_tur"
        restructurer.entities[entity_id] = {
            "id": registry_id,
            "entity_id": entity_id,
            "device_id": device_id,
            "platform": platform,
            "original_name": "Tür",
            "has_entity_name": True,
        }

    response = client.post(
        "/api/naming/learn",
        json={"entity_id": "binary_sensor.reg-c_tur", "value": "Zustand", "scope": "model"},
    )

    assert response.status_code == 200
    match = response.get_json()["rule"]["match"]
    assert match["integration"] == "matter"
    assert match["model"] == "MYGGBETT door/window sensor"
    assert response.get_json()["rule"]["affected"] == 1
    assert web_ui._type_key_model_counts(restructurer)[("name:tuer", "MYGGBETT door/window sensor")] == 1
