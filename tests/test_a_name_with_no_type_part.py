"""An entity named entirely by its device has no type part of its own.

Home Assistant marks those with ``has_entity_name`` and no ``original_name``.
Rendered through ``{area} {device} {entity}`` the name ends after the device,
so taking it apart again has to answer that the type part is empty.
"""

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_state import NamingState
from naming_templates import NamingTemplates

TEMPLATES = {
    "device_name": "{area} {device}",
    "entity_id": "{area} {device} {entity}",
    "entity_name": "{area} {device} {entity}",
}

CONTEXT = {"area": "Basement", "device": "Pump"}


@pytest.fixture
def templates(tmp_path):
    built = NamingTemplates(str(tmp_path / "templates.json"))
    built.set_templates(TEMPLATES)
    return built


@pytest.fixture
def restructurer(tmp_path, templates):
    built = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=None,
        naming_templates=templates,
        naming_state=NamingState(str(tmp_path / "naming_state.json")),
    )
    built.floors = {}
    built.areas = {"b": {"area_id": "b", "name": "Basement"}}
    # Renamed by the user; the integration's own name stays on the device.
    built.devices = {"d": {"id": "d", "name": "ACME-4200", "name_by_user": "Basement Pump", "area_id": "b"}}
    built.entities = {
        "switch.a": {
            "id": "reg-a",
            "entity_id": "switch.a",
            "device_id": "d",
            "platform": "matter",
            "name": "Basement Pump",
            "original_name": None,
            "has_entity_name": True,
        }
    }
    return built


def test_the_template_answers_that_the_type_part_is_empty(templates):
    assert templates.extract_field("entity_name", "Basement Pump", "entity", CONTEXT) == ""


def test_a_type_part_that_is_there_still_comes_back(templates):
    assert templates.extract_field("entity_name", "Basement Pump Switch", "entity", CONTEXT) == "Switch"


def test_a_name_no_template_produced_is_still_no_match(templates):
    assert templates.extract_field("entity_name", "", "entity", CONTEXT) is None


def test_the_device_name_is_not_rendered_into_the_name_twice(restructurer):
    context = restructurer.build_naming_context("switch.a", restructurer.entities["switch.a"])

    assert context["device"] == "Pump"
    assert restructurer.naming_templates.render("entity_name", context) != "Basement Pump Basement Pump"


def test_unwinding_an_applied_name_gives_the_empty_type_back(restructurer):
    entity = restructurer.entities["switch.a"]
    context = restructurer.build_naming_context("switch.a", entity)
    prefixes = ("Basement Pump", "Basement", "Pump", "ACME-4200", "")

    assert restructurer._strip_applied_entity_name(entity["name"], prefixes, context) == ""
