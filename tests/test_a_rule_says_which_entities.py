"""A rule's count is only useful if the entities behind it can be read.

The rule list said "2 Entitäten" and there was no way to see which two, so a
rule that worded something wrongly could not be checked against the entities it
words - and two update sensors that look alike ended up under two rules with
two different words, with nothing on screen to say why.

Each entity comes back with the three values that settle it: what Home
Assistant supplies, what it is called today, and what the rule would make of
it.
"""

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
    restructurer = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=mappings,
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
    )
    restructurer.floors = {}
    restructurer.areas = {"b": {"area_id": "b", "name": "Büro"}}
    restructurer.devices = {"d": {"id": "d", "name": "Switch 241", "area_id": "b"}}
    restructurer.entities = {
        "update.switch_firmware": {
            "id": "reg-a",
            "entity_id": "update.switch_firmware",
            "device_id": "d",
            "platform": "unifi",
            "original_name": "Firmware",
            "has_entity_name": True,
        },
        "update.sensor_update": {
            "id": "reg-b",
            "entity_id": "update.sensor_update",
            "device_id": "d",
            "platform": "mqtt",
            "original_name": "Update verfügbar",
            "has_entity_name": True,
        },
    }
    for key, value in (
        ("naming_rules", rules),
        ("type_mappings", mappings),
        ("naming_overrides", restructurer.naming_overrides),
        ("naming_templates", restructurer.naming_templates),
        ("restructurer", restructurer),
    ):
        monkeypatch.setitem(web_ui.renamer_state, key, value)
    # The registry is already there; asking Home Assistant for it would hang.
    monkeypatch.setattr(web_ui, "ensure_registry_loaded", _already_loaded, raising=False)
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client(), rules


async def _already_loaded():
    return None


def test_the_entities_behind_the_count_can_be_read(client):
    api, rules = client
    rule = rules.upsert("name", "firmware", None, "de", "Aktualisierung")

    answer = api.get("/api/naming/rules/" + rule["id"] + "/entities").get_json()

    assert [one["entity_id"] for one in answer["entities"]] == ["update.switch_firmware"]
    assert answer["total"] == 1


def test_each_one_says_what_is_supplied_what_it_is_and_what_it_would_be(client):
    api, rules = client
    rule = rules.upsert("name", "firmware", None, "de", "Aktualisierung")

    one = api.get("/api/naming/rules/" + rule["id"] + "/entities").get_json()["entities"][0]

    assert one["supplied"] == "Firmware"
    assert one["proposed"] == "Büro Switch 241 Aktualisierung"
    assert one["platform"] == "unifi"


def test_a_rule_for_another_word_does_not_claim_them(client):
    """The case that started this: two update sensors, two words, two rules."""
    api, rules = client
    rules.upsert("name", "firmware", None, "de", "Aktualisierung")
    other = rules.upsert("name", "update_verfuegbar", None, "de", "Update verfügbar")

    answer = api.get("/api/naming/rules/" + other["id"] + "/entities").get_json()

    assert [one["entity_id"] for one in answer["entities"]] == ["update.sensor_update"]


def test_an_entity_named_one_by_one_is_marked_as_such(client):
    """A filter on a registry id reaches an entity the supplied word does not."""
    api, rules = client
    rule = rules.upsert("name", "brightness", None, "de", "Helligkeit")
    rules.update(rule["id"], filters=[{"registry_id": "reg-b"}])

    answer = api.get("/api/naming/rules/" + rule["id"] + "/entities").get_json()

    assert [one["entity_id"] for one in answer["entities"]] == ["update.sensor_update"]
    assert answer["entities"][0]["by_name"] is True


def test_an_unknown_rule_is_not_an_empty_list(client):
    api, _ = client

    assert api.get("/api/naming/rules/r_nope/entities").status_code == 404
