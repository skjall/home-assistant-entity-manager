"""Tests for applying naming templates to Home Assistant registry data."""

import pytest

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
    assert entity_id == "sensor.living_room_thermostat_temperature"
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
        "event.living_room_thermostat_button_bl",
        "Button BL",
    )
    assert restructurer.generate_new_entity_id("event.button_br_2", {}) == (
        "event.living_room_thermostat_button_br",
        "Button BR",
    )
    assert restructurer.generate_new_entity_id("button.thermostat_reboot", {}) == (
        "button.living_room_thermostat_restart",
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
        "sensor.living_room_thermostat_temperature",
        "Temperature",
    )


def test_composed_state_name_is_reduced_to_native_entity_name(tmp_path):
    restructurer = make_restructurer(tmp_path)
    restructurer.naming_templates.apply_preset("home_assistant")

    assert restructurer.generate_new_entity_id(
        "sensor.old_temperature",
        {"attributes": {"friendly_name": "Living room Thermostat Temperature"}},
    ) == ("sensor.living_room_thermostat_temperature", "Temperature")


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


def test_entity_area_override_wins_over_device_area(tmp_path):
    restructurer = make_restructurer(tmp_path)
    restructurer.areas["office"] = {
        "area_id": "office",
        "name": "Office",
        "floor_id": "ground",
    }
    restructurer.entities["sensor.old_temperature"]["area_id"] = "office"

    context = restructurer.build_naming_context("sensor.old_temperature", {})

    assert context["area_id"] == "office"
    assert context["area"] == "Office"


class FakeRegistryWebSocket:
    def __init__(self):
        self._next_id = 0
        self._responses = []

    async def _send_message(self, message):
        self._next_id += 1
        results = {
            "config/floor_registry/list": [
                {"id": "ground", "name": "Ground floor"},
            ],
            "config/area_registry/list": [
                {"id": "living", "name": "Living room", "floor_id": "ground"},
            ],
            "config/device_registry/list": [
                {"id": "device-1", "name": "Thermostat", "area_id": "living"},
            ],
            "config/entity_registry/list": [
                {
                    "id": "registry-1",
                    "entity_id": "sensor.thermostat_temperature",
                    "device_id": "device-1",
                    "original_name": "Temperature",
                }
            ],
        }
        self._responses.append({"id": self._next_id, "success": True, "result": results[message["type"]]})
        return self._next_id

    async def _receive_message(self):
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_current_ha_registry_ids_load_area_and_floor_names(tmp_path):
    restructurer = make_restructurer(tmp_path)

    await restructurer.load_structure(FakeRegistryWebSocket())

    context = restructurer.build_naming_context("sensor.thermostat_temperature", {})
    assert context["area"] == "Living room"
    assert context["floor"] == "Ground floor"


def test_home_assistant_primary_entity_uses_only_device_name(tmp_path):
    restructurer = make_restructurer(tmp_path)
    restructurer.naming_templates.apply_preset("home_assistant")
    restructurer.entities = {
        "switch.thermostat_switch": {
            "id": "registry-main",
            "entity_id": "switch.thermostat_switch",
            "device_id": "device-1",
            "platform": "matter",
            "original_name": None,
            "has_entity_name": True,
        }
    }

    assert restructurer.generate_new_entity_id(
        "switch.thermostat_switch",
        {"attributes": {"friendly_name": "Thermostat"}},
    ) == ("switch.living_room_thermostat", "")


def test_home_assistant_recreated_ids_match_area_device_entity_example(tmp_path):
    restructurer = make_restructurer(tmp_path)
    restructurer.naming_templates.apply_preset("home_assistant")
    restructurer.areas["hallway"] = {"area_id": "hallway", "name": "Chodba"}
    restructurer.devices["device-1"].update({"name": "Vstupní dveře", "area_id": "hallway"})
    restructurer.entities = {
        "sensor.vstupni_dvere_battery": {
            "id": "registry-battery",
            "entity_id": "sensor.vstupni_dvere_battery",
            "device_id": "device-1",
            "original_name": "Battery",
            "has_entity_name": True,
        }
    }

    assert restructurer.generate_new_entity_id("sensor.vstupni_dvere_battery", {}) == (
        "sensor.chodba_vstupni_dvere_battery",
        "Battery",
    )


def test_home_assistant_device_less_entity_uses_only_entity_name(tmp_path):
    restructurer = make_restructurer(tmp_path)
    restructurer.naming_templates.apply_preset("home_assistant")
    restructurer.entities = {
        "binary_sensor.everyone": {
            "id": "registry-everyone",
            "entity_id": "binary_sensor.everyone",
            "device_id": None,
            "original_name": "Everyone is home",
            "has_entity_name": True,
        }
    }

    assert restructurer.generate_new_entity_id("binary_sensor.everyone", {}) == (
        "binary_sensor.everyone_is_home",
        "Everyone is home",
    )
