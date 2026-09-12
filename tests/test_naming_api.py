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


def test_learn_anchors_on_the_device_class_when_the_name_holds_the_device(client):
    """ "Tomate air humidity" names the device too; only the class is shared."""
    restructurer = web_ui.renamer_state["restructurer"]
    restructurer.entities["sensor.balkon_tomate_luftfeuchtigkeit"] = {
        "id": "reg-plant",
        "entity_id": "sensor.balkon_tomate_luftfeuchtigkeit",
        "device_id": "d",
        "platform": "plant",
        "original_name": "Tomate air humidity",
        "device_class": "humidity",
        "has_entity_name": False,
    }

    response = client.post(
        "/api/naming/learn",
        json={"entity_id": "sensor.balkon_tomate_luftfeuchtigkeit", "value": "Luftfeuchtigkeit"},
    )

    match = response.get_json()["rule"]["match"]
    assert match["kind"] == "device_class"
    assert match["value"] == "humidity"


def test_learn_keeps_the_name_when_the_entity_names_itself(client):
    """A native entity name stands for the type alone and stays the anchor."""
    restructurer = web_ui.renamer_state["restructurer"]
    restructurer.entities["number.a_effect_speed"]["device_class"] = "humidity"

    response = client.post(
        "/api/naming/learn", json={"entity_id": "number.a_effect_speed", "value": "Effektgeschwindigkeit"}
    )

    assert response.get_json()["rule"]["match"]["kind"] == "name"


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
    # Only what this home can use: the number domain is here, a battery is not.
    assert any(entry["key"] == "number" for entry in listed["system"])
    assert not any(entry["key"] == "battery" for entry in listed["system"])

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


def test_backup_download_serves_the_file_from_before_the_migration(tmp_path, monkeypatch):
    legacy = tmp_path / "user_type_mappings.json"
    legacy.write_text('{"user_mappings": {"linkquality": "Verbindungsqualität"}}', encoding="utf-8")
    rules = NamingRules(
        str(tmp_path / "naming_rules.json"),
        legacy_path=str(legacy),
        device_class_keys=DEFAULT_SYSTEM_MAPPINGS["device_class"].keys(),
        default_language="de",
    )
    monkeypatch.setitem(web_ui.renamer_state, "naming_rules", rules)
    web_ui.app.config["TESTING"] = True
    client = web_ui.app.test_client()

    response = client.get("/api/naming/migration/backup")

    assert response.status_code == 200
    assert "Verbindungsqualität" in response.get_data(as_text=True)
    assert "attachment" in response.headers["Content-Disposition"]


def test_backup_download_without_a_migration_is_not_found(client, monkeypatch):
    monkeypatch.setitem(web_ui.renamer_state["naming_rules"].data, "migration", None)

    assert client.get("/api/naming/migration/backup").status_code == 404


@pytest.mark.parametrize(
    "path, section, expected_base",
    [
        ("/settings/naming", "naming", "../"),
        ("/settings/rules", "rules", "../"),
        ("/settings/system", "system", "../"),
        ("/settings/does-not-exist", "naming", "../"),
    ],
)
def test_settings_sections_are_their_own_addresses(client, path, section, expected_base):
    """Each section reloads on its own URL, and says where the app root is."""
    response = client.get(path)
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert f'<base href="{expected_base}">' in body
    assert f"section: '{section}'" in body


def test_bare_settings_goes_to_the_first_section(client):
    """One depth for every section keeps relative asset and API paths valid."""
    response = client.get("/settings")

    assert response.status_code == 302
    assert response.headers["Location"] == "settings/naming"


def test_reach_is_unknown_while_no_entities_can_be_loaded(client, monkeypatch):
    """Zero would brand every rule as useless right after a start; unknown is the truth."""
    restructurer = web_ui.renamer_state["restructurer"]
    client.post("/api/naming/learn", json={"entity_id": "number.a_effect_speed", "value": "Effektgeschwindigkeit"})
    restructurer.entities = {}

    async def no_registry():
        return None

    monkeypatch.setattr(web_ui, "_ensure_registry_loaded", no_registry)
    rules = client.get("/api/naming/rules").get_json()["rules"]

    assert rules and all(rule["affected"] is None for rule in rules)


def test_counting_groups_entities_of_one_type(client):
    """One lookup per type, integration and model instead of one per entity."""
    restructurer = web_ui.renamer_state["restructurer"]
    for index in range(20):
        entity_id = f"number.copy{index}_effect_speed"
        restructurer.entities[entity_id] = {
            "id": f"reg-copy{index}",
            "entity_id": entity_id,
            "device_id": "d",
            "platform": "mqtt",
            "original_name": "Effect speed",
            "has_entity_name": True,
        }
    rules = web_ui.renamer_state["naming_rules"]
    calls = []
    original_find = rules.find

    def counting_find(*args, **kwargs):
        calls.append(args)
        return original_find(*args, **kwargs)

    rules.find = counting_find
    try:
        rule = client.post(
            "/api/naming/learn", json={"entity_id": "number.a_effect_speed", "value": "Effektgeschwindigkeit"}
        ).get_json()["rule"]
    finally:
        rules.find = original_find

    assert rule["affected"] == 22  # the 20 copies plus both spellings in the fixture
    assert len(calls) < 22  # not one lookup per entity


def test_template_sample_comes_from_a_real_entity(client):
    """An example from the user's own home beats "Living room / Thermostat"."""
    data = client.get("/api/naming_templates/sample").get_json()

    assert data["entity_id"] == "number.a_effect_speed"
    assert data["context"]["area"] == "Küche"
    assert data["context"]["device"] == "Deckenleuchte"
    assert data["context"]["entity"] == "Effect speed"
    assert data["context"]["domain"] == "number"
    assert data["context"]["integration"] == "mqtt"


def test_cleanup_also_takes_a_rule_that_repeats_the_standard(client):
    """A rule naming "Effect speed" what is already called that changes nothing."""
    rules = web_ui.renamer_state["naming_rules"]
    same = rules.upsert("name", "effect_speed", None, rules.language, "Effect speed")

    listed = client.get("/api/naming/rules/unused").get_json()["rules"]
    ids = [rule["id"] for rule in listed]

    assert same["id"] in ids
    assert client.delete("/api/naming/rules/unused").get_json()["removed"] == len(ids)
    assert rules.get(same["id"]) is None


def test_unused_rules_can_be_listed_and_removed(client):
    """A rule no entity matches is offered for removal, never removed by itself."""
    rules = web_ui.renamer_state["naming_rules"]
    orphan = rules.upsert("name", "gone_with_the_device", None, rules.language, "Weg")

    listed = client.get("/api/naming/rules/unused").get_json()["rules"]
    assert orphan["id"] in [rule["id"] for rule in listed]
    assert rules.get(orphan["id"]) is not None

    removed = client.delete("/api/naming/rules/unused").get_json()["removed"]

    assert removed == len(listed)
    assert rules.get(orphan["id"]) is None


def test_template_sample_takes_the_entity_it_is_asked_for(client):
    """The user picks the entity the preview runs on."""
    entity_id = next(eid for eid in web_ui.renamer_state["restructurer"].entities if eid != "number.a_effect_speed")
    data = client.get(f"/api/naming_templates/sample?entity_id={entity_id}").get_json()

    assert data["entity_id"] == entity_id


def test_template_sample_ignores_an_entity_it_does_not_know(client):
    data = client.get("/api/naming_templates/sample?entity_id=sensor.nowhere").get_json()

    assert data["entity_id"] == "number.a_effect_speed"


def test_template_sample_entities_carry_a_name_and_an_area(client):
    rows = client.get("/api/naming_templates/sample/entities").get_json()["entities"]

    assert rows
    first = next(row for row in rows if row["entity_id"] == "number.a_effect_speed")
    assert first["area"] == "Küche"
    assert first["name"]


def test_template_sample_falls_back_without_entities(client, monkeypatch):
    web_ui.renamer_state["restructurer"].entities = {}

    async def no_registry():
        return None

    monkeypatch.setattr(web_ui, "_ensure_registry_loaded", no_registry)
    data = client.get("/api/naming_templates/sample").get_json()

    assert data["entity_id"] is None
    assert data["context"]["area"] == "Living room"
