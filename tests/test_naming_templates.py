"""Tests for configurable naming templates."""

import pytest

from naming_templates import NamingTemplateError, NamingTemplates


@pytest.fixture
def manager(tmp_path):
    return NamingTemplates(str(tmp_path / "naming_templates.json"))


def sample_context():
    return {
        "floor": "Ground floor",
        "floor_id": "ground_floor",
        "area": "Living room",
        "area_id": "living_room",
        "device": "Thermostat",
        "device_id": "device_123",
        "entity": "Temperature",
        "entity_id": "thermostat_temperature",
        "domain": "sensor",
        "device_class": "temperature",
        "manufacturer": "Acme",
        "model": "T1000",
        "integration": "matter",
    }


def test_entity_manager_is_default(manager):
    context = sample_context()
    assert manager.get_config()["preset"] == "entity_manager"
    assert manager.render("device_name", context) == "Living room Thermostat"
    assert manager.render("entity_name", context) == "Living room Thermostat Temperature"
    assert manager.render("entity_id", context, normalize=True) == "living_room_thermostat_temperature"


def test_home_assistant_preset_omits_area(manager):
    manager.apply_preset("home_assistant")
    context = sample_context()
    assert manager.render("device_name", context) == "Thermostat"
    assert manager.render("entity_name", context) == "Temperature"
    assert manager.render("entity_id", context, normalize=True) == "thermostat_temperature"


def test_custom_template_supports_extended_context(manager):
    manager.set_templates(
        {
            "device_name": "{floor} / {area} / {manufacturer} {device}",
            "entity_name": "{entity} ({model})",
            "entity_id": "{floor} {area_id} {integration} {device} {device_class}",
        }
    )
    context = sample_context()
    assert manager.render("device_name", context) == "Ground floor / Living room / Acme Thermostat"
    assert manager.render("entity_name", context) == "Temperature (T1000)"
    assert manager.render("entity_id", context, normalize=True) == (
        "ground_floor_living_room_matter_thermostat_temperature"
    )


def test_empty_optional_values_are_cleaned(manager):
    manager.set_templates(
        {
            "device_name": "{floor} - {area} - {device}",
            "entity_name": "{area} - {entity}",
            "entity_id": "{area} - {device} - {entity}",
        }
    )
    context = sample_context()
    context["floor"] = ""
    context["area"] = ""
    assert manager.render("device_name", context) == "Thermostat"
    assert manager.render("entity_name", context) == "Temperature"
    assert manager.render("entity_id", context, normalize=True) == "thermostat_temperature"


def test_unknown_placeholder_is_rejected(manager):
    templates = manager.get_templates()
    templates["device_name"] = "{building} {device}"
    with pytest.raises(NamingTemplateError, match="Unknown placeholders: building"):
        manager.set_templates(templates)


def test_required_component_is_rejected(manager):
    templates = manager.get_templates()
    templates["device_name"] = "{area}"
    with pytest.raises(NamingTemplateError, match=r"must contain \{device\}"):
        manager.set_templates(templates)


def test_previous_template_can_extract_device_base(manager):
    context = sample_context()
    rendered = manager.render("device_name", context)
    manager.apply_preset("home_assistant")
    context["device"] = ""
    assert manager.extract_field("device_name", rendered, "device", context) == "Thermostat"


def test_configuration_persists(manager):
    manager.apply_preset("home_assistant")
    reloaded = NamingTemplates(str(manager.storage_path))
    assert reloaded.get_config()["preset"] == "home_assistant"
    assert reloaded.get_templates()["entity_name"] == "{entity}"
