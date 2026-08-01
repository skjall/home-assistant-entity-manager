"""Tests for applying naming templates to Home Assistant registry data."""

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_templates import NamingTemplates


class FakeTypeMappings:
    """Small deterministic type-mapping test double."""

    @staticmethod
    def detect_integration(entity_id):
        return "matter"

    @staticmethod
    def get_translation(type_key, language, integration, domain):
        return (type_key or domain).replace("_", " ").title()


def make_restructurer(tmp_path):
    templates = NamingTemplates(str(tmp_path / "templates.json"))
    restructurer = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=FakeTypeMappings(),
        naming_templates=templates,
    )
    restructurer.floors = {"ground": {"floor_id": "ground", "name": "Ground floor"}}
    restructurer.areas = {"living": {"area_id": "living", "name": "Living room", "floor_id": "ground"}}
    restructurer.devices = {
        "device-1": {
            "id": "device-1",
            "name": "Thermostat",
            "area_id": "living",
            "manufacturer": "Acme",
            "model": "T1000",
        }
    }
    restructurer.entities = {
        "sensor.old_temperature": {
            "id": "registry-1",
            "entity_id": "sensor.old_temperature",
            "device_id": "device-1",
            "platform": "matter",
            "original_device_class": "temperature",
        }
    }
    return restructurer


def test_entity_manager_generation_is_unchanged(tmp_path):
    restructurer = make_restructurer(tmp_path)
    entity_id, entity_name = restructurer.generate_new_entity_id(
        "sensor.old_temperature", {"attributes": {"device_class": "temperature"}}
    )
    assert entity_id == "sensor.living_room_thermostat_temperature"
    assert entity_name == "Living room Thermostat Temperature"
    assert restructurer.generate_device_name("device-1") == "Living room Thermostat"


def test_home_assistant_generation_uses_registry_context(tmp_path):
    restructurer = make_restructurer(tmp_path)
    restructurer.naming_templates.apply_preset("home_assistant")
    entity_id, entity_name = restructurer.generate_new_entity_id(
        "sensor.old_temperature", {"attributes": {"device_class": "temperature"}}
    )
    assert entity_id == "sensor.thermostat_temperature"
    assert entity_name == "Temperature"
    assert restructurer.generate_device_name("device-1") == "Thermostat"


def test_home_assistant_generation_preserves_native_entity_names(tmp_path):
    restructurer = make_restructurer(tmp_path)
    restructurer.naming_templates.apply_preset("home_assistant")
    restructurer.entities = {
        "event.button_bl_2": {
            "id": "registry-button-bl",
            "entity_id": "event.button_bl_2",
            "device_id": "device-1",
            "platform": "matter",
            "original_name": "Button BL",
        },
        "event.button_br_2": {
            "id": "registry-button-br",
            "entity_id": "event.button_br_2",
            "device_id": "device-1",
            "platform": "matter",
            "original_name": "Button BR",
        },
        "button.thermostat_reboot": {
            "id": "registry-reboot",
            "entity_id": "button.thermostat_reboot",
            "device_id": "device-1",
            "platform": "matter",
            "original_name": "Restart",
        },
    }

    assert restructurer.generate_new_entity_id("event.button_bl_2", {}) == (
        "event.thermostat_button_bl",
        "Button BL",
    )
    assert restructurer.generate_new_entity_id("event.button_br_2", {}) == (
        "event.thermostat_button_br",
        "Button BR",
    )
    assert restructurer.generate_new_entity_id("button.thermostat_reboot", {}) == (
        "button.thermostat_restart",
        "Restart",
    )


def test_original_name_wins_over_existing_registry_name(tmp_path):
    restructurer = make_restructurer(tmp_path)
    restructurer.naming_templates.apply_preset("home_assistant")
    restructurer.entities["sensor.old_temperature"].update(
        {
            "name": "Living room Thermostat Sensor",
            "original_name": "Temperature",
        }
    )

    assert restructurer.generate_new_entity_id("sensor.old_temperature", {}) == (
        "sensor.thermostat_temperature",
        "Temperature",
    )


def test_composed_state_name_is_reduced_to_native_entity_name(tmp_path):
    restructurer = make_restructurer(tmp_path)
    restructurer.naming_templates.apply_preset("home_assistant")

    assert restructurer.generate_new_entity_id(
        "sensor.old_temperature",
        {"attributes": {"friendly_name": "Living room Thermostat Temperature"}},
    ) == ("sensor.thermostat_temperature", "Temperature")


def test_floor_and_metadata_are_available(tmp_path):
    restructurer = make_restructurer(tmp_path)
    restructurer.naming_templates.set_templates(
        {
            "device_name": "{floor} {manufacturer} {device}",
            "entity_name": "{area} {entity} ({model})",
            "entity_id": "{floor_id} {integration} {device} {entity}",
        }
    )
    entity_id, entity_name = restructurer.generate_new_entity_id(
        "sensor.old_temperature", {"attributes": {"device_class": "temperature"}}
    )
    assert entity_id == "sensor.ground_matter_thermostat_temperature"
    assert entity_name == "Living room Temperature (T1000)"
    assert restructurer.generate_device_name("device-1") == "Ground floor Acme Thermostat"
