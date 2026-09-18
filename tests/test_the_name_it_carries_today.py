"""What the entity is called now, as a type on its own.

The list says what an entity would be called. Keeping a name it already has -
the German one an English-speaking integration would overwrite - needs the
other answer: the name in the registry with the hierarchy taken off and no
rule applied.
"""

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_templates import NamingTemplates


class NoMappings:
    """Nothing is translated here; a name passes through as written."""

    @staticmethod
    def detect_integration(entity_id):
        return "example"

    @staticmethod
    def get_translation(type_key, language, integration=None, domain=None):
        return (type_key or domain or "").replace("_", " ").title()

    @staticmethod
    def find_translation(type_key, language="en", integration=None, domain=None):
        return None


@pytest.fixture
def restructurer(tmp_path):
    result = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=NoMappings(),
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
    )
    result.areas = {"balcony": {"area_id": "balcony", "name": "Balcony"}}
    result.devices = {
        "device-1": {
            "id": "device-1",
            "name": "Solar plant",
            "area_id": "balcony",
            "manufacturer": "Acme",
            "model": "Model 1",
        }
    }
    result.entities = {
        "sensor.plant_temperature": {
            "id": "registry-1",
            "entity_id": "sensor.plant_temperature",
            "device_id": "device-1",
            "platform": "example",
            "original_name": "Temperature",
            "name": "Balcony Solar plant Resonanzwandler Temperatur",
            "has_entity_name": True,
        }
    }
    return result


def test_the_hierarchy_is_taken_off_the_written_name(restructurer):
    kept = restructurer.type_in_the_current_name("sensor.plant_temperature")

    assert kept == "Resonanzwandler Temperatur"


def test_an_entity_with_no_written_name_has_nothing_to_keep(restructurer):
    restructurer.entities["sensor.plant_temperature"].pop("name")

    assert restructurer.type_in_the_current_name("sensor.plant_temperature") == ""


def test_a_name_that_is_only_the_hierarchy_leaves_nothing(restructurer):
    restructurer.entities["sensor.plant_temperature"]["name"] = "Balcony Solar plant"

    assert restructurer.type_in_the_current_name("sensor.plant_temperature") == ""


def test_an_entity_nobody_knows_is_not_an_error(restructurer):
    assert restructurer.type_in_the_current_name("sensor.nothing_here") == ""


def test_the_device_name_of_the_day_is_taken_off_too(restructurer):
    """An integration may have written the device's old name into the entity."""
    restructurer.devices["device-1"]["name_by_user"] = "Solar plant"
    restructurer.devices["device-1"]["name"] = "SUN-2000-9152"
    restructurer.entities["sensor.plant_temperature"]["name"] = "SUN-2000-9152 Resonanzwandler Temperatur"

    assert restructurer.type_in_the_current_name("sensor.plant_temperature") == "Resonanzwandler Temperatur"
