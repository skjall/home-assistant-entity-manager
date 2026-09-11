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
    assert rule["match"] == {"kind": "name", "value": "effect_speed", "integration": None}
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
