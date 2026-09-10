"""Tests for translating integration-supplied entity names."""

import json

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_templates import NamingTemplates
from type_mappings import TypeMappings


@pytest.fixture
def restructurer(tmp_path):
    """Restructurer with a German user mapping for a Zigbee2MQTT sensor."""
    mappings_path = tmp_path / "user_type_mappings.json"
    mappings_path.write_text(
        json.dumps({"user_mappings": {"linkquality": "Verbindungsqualität"}}),
        encoding="utf-8",
    )
    result = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=TypeMappings(user_mappings_path=str(mappings_path)),
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
        language="de",
    )
    result.floors = {}
    result.areas = {"living": {"area_id": "living", "name": "Wohnzimmer"}}
    result.devices = {"device-1": {"id": "device-1", "name": "Fensterdekoration", "area_id": "living"}}
    result.entities = {
        "sensor.decoration_linkquality": {
            "id": "registry-1",
            "entity_id": "sensor.decoration_linkquality",
            "device_id": "device-1",
            "platform": "mqtt",
            "original_name": "Linkquality",
            "has_entity_name": True,
        }
    }
    return result


def test_configured_mapping_replaces_the_integration_name(restructurer):
    """A mapped name must win over the English one the integration supplies."""
    context = restructurer.build_naming_context(
        "sensor.decoration_linkquality", restructurer.entities["sensor.decoration_linkquality"]
    )

    assert context["entity"] == "Verbindungsqualität"


def test_unmapped_name_is_kept(restructurer):
    """Without a mapping the supplied name stands; it must not become "Sensor"."""
    registry = restructurer.entities["sensor.decoration_linkquality"]
    registry["original_name"] = "Power-on behavior"

    context = restructurer.build_naming_context("sensor.decoration_linkquality", registry)

    assert context["entity"] == "Power-on behavior"


def test_override_is_not_translated(restructurer):
    """An explicit override is the user's own wording and stays untouched."""
    restructurer.naming_overrides.set_entity_override("registry-1", "Linkquality")

    context = restructurer.build_naming_context(
        "sensor.decoration_linkquality", restructurer.entities["sensor.decoration_linkquality"]
    )

    assert context["entity"] == "Linkquality"


def test_find_translation_reports_a_miss(tmp_path):
    """find_translation must not invent the capitalized fallback."""
    mappings = TypeMappings(user_mappings_path=str(tmp_path / "m.json"))

    assert mappings.find_translation("no_such_key_at_all") is None
    assert mappings.get_translation("no_such_key_at_all") == "No Such Key At All"
