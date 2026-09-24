"""A note that says the name has no type part is an answer, not a blank.

The type part of an applied name is noted along with it. Where it was empty -
the name is area and device and nothing else - the note holds "". That has to
read back as "no type part", not as "nothing noted", or the type part gets
derived all over again and the supplied name creeps back into the proposal.
"""

import json

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_rules import NamingRules
from naming_state import NamingState
from naming_templates import NamingTemplates
from type_mappings import DEFAULT_SYSTEM_MAPPINGS, TypeMappings

TEMPLATES = {
    "device_name": "{area} {device}",
    "entity_id": "{area} {device} {entity}",
    "entity_name": "{area} {device} {entity}",
}

APPLIED = "Basement Pump"


@pytest.fixture
def state(tmp_path):
    return NamingState(str(tmp_path / "naming_state.json"))


@pytest.fixture
def restructurer(tmp_path, state):
    templates = NamingTemplates(str(tmp_path / "templates.json"))
    templates.set_templates(TEMPLATES)
    built = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=None,
        naming_templates=templates,
        naming_state=state,
    )
    built.floors = {}
    built.areas = {"b": {"area_id": "b", "name": "Basement"}}
    built.devices = {"d": {"id": "d", "name": "Basement Pump", "area_id": "b"}}
    built.entities = {
        "switch.a": {
            "id": "reg-a",
            "entity_id": "switch.a",
            "device_id": "d",
            "platform": "matter",
            "name": APPLIED,
            # What the integration calls it. Older code fell back to this the
            # moment the note read empty, and put "Relay" into the name.
            "original_name": "Relay",
            "has_entity_name": True,
        }
    }
    return built


def note(state, base_entity):
    state.record("reg-a", applied_name=APPLIED, applied_entity_id="switch.a", base_entity=base_entity)


def test_a_note_that_says_nothing_about_the_type_part_reads_as_none(restructurer, state):
    state.record("reg-a", applied_name=APPLIED, applied_entity_id="switch.a")

    assert state.get("reg-a")["base_entity"] is None
    assert restructurer._remembered_type(restructurer.entities["switch.a"]) is None


def test_an_empty_type_part_reads_back_as_empty(restructurer, state):
    note(state, "")

    assert state.get("reg-a")["base_entity"] == ""
    assert restructurer._remembered_type(restructurer.entities["switch.a"]) == ""


def test_a_type_part_that_is_there_reads_back_as_itself(restructurer, state):
    note(state, "Relay")

    assert restructurer._remembered_type(restructurer.entities["switch.a"]) == "Relay"


def test_an_empty_note_keeps_the_name_it_was_written_for(restructurer, state):
    note(state, "")

    _, name = restructurer.generate_new_entity_id("switch.a", restructurer.entities["switch.a"])

    assert name == APPLIED
    assert restructurer.last_resolutions["switch.a"]["value"] == ""
    assert restructurer.last_resolutions["switch.a"]["won_by"] == "original"


def test_an_older_note_still_falls_back_to_taking_the_name_apart(restructurer, state):
    state.record("reg-a", applied_name=APPLIED, applied_entity_id="switch.a")

    _, name = restructurer.generate_new_entity_id("switch.a", restructurer.entities["switch.a"])

    assert name == APPLIED
    assert restructurer.last_resolutions["switch.a"]["won_by"] == "legacy_parse"


def test_ownership_still_reports_a_missing_type_part_as_a_string(restructurer, state):
    state.record("reg-a", applied_name=APPLIED, applied_entity_id="switch.a")

    assert state.ownership(restructurer.entities["switch.a"])["base_entity"] == ""


def write_a_version_one_file(path, applied_name, base_entity):
    """A state file as version 1 wrote it, empty type parts and all."""
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entities": {
                    "reg-a": {
                        "applied_name": applied_name,
                        "applied_entity_id": "switch.a",
                        "base_entity": base_entity,
                        "template_hash": "",
                        "won_by": "",
                        "rule_id": None,
                        "applied_at": "2025-01-01T00:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_an_empty_note_from_before_the_type_part_was_kept_keeps_the_type_part(tmp_path, restructurer):
    """Version 1 recorded an empty type part for every entity.

    Taken at face value those notes would strip the type part off every name
    they cover, and version 1 cannot say which of them meant it. So they say
    nothing, and the name is taken apart once more.
    """
    entity = restructurer.entities["switch.a"]
    entity["name"] = "Basement Pump Relay"
    path = tmp_path / "version_one.json"
    write_a_version_one_file(path, "Basement Pump Relay", "")
    restructurer.naming_state = NamingState(str(path))

    _, name = restructurer.generate_new_entity_id("switch.a", entity)

    assert name == "Basement Pump Relay"
    assert restructurer.last_resolutions["switch.a"]["value"] == "Relay"


def test_a_version_one_type_part_that_is_there_is_still_read(tmp_path):
    """Only the empty ones are ambiguous; a word version 1 recorded still holds."""
    path = tmp_path / "version_one.json"
    write_a_version_one_file(path, "Basement Pump Relay", "Relay")

    assert NamingState(str(path)).get("reg-a")["base_entity"] == "Relay"


def test_an_empty_note_outlives_a_template_that_changes(restructurer, state):
    """The adoption is the user's answer, not a reading of the name.

    Where the template stops rendering the device, the name can be taken apart
    again and yields a word - here the device name. Reading that back would put
    the device name into the type part and keep proposing it, which is the loop
    the note was written to end. The name does follow the new template, but it
    still has no type part.
    """
    note(state, "")
    restructurer.naming_templates.set_templates({**TEMPLATES, "entity_name": "{area} {entity}"})

    _, name = restructurer.generate_new_entity_id("switch.a", restructurer.entities["switch.a"])

    assert name == "Basement"
    assert restructurer.last_resolutions["switch.a"]["value"] == ""
    assert restructurer.last_resolutions["switch.a"]["won_by"] == "original"


class HomeAssistantNames:
    """Home Assistant's own words for a device class, as the add-on asks for them."""

    def device_class_name(self, domain, device_class, language):
        return "Outlet" if device_class == "outlet" else None

    def translation_key_name(self, platform, domain, key, language):
        return None

    def domain_name(self, domain, language):
        return None


@pytest.fixture
def with_rules(tmp_path, state):
    rules = NamingRules(
        str(tmp_path / "naming_rules.json"),
        legacy_path=str(tmp_path / "user_type_mappings.json"),
        device_class_keys=DEFAULT_SYSTEM_MAPPINGS["device_class"].keys(),
        default_language="en",
    )
    templates = NamingTemplates(str(tmp_path / "templates.json"))
    templates.set_templates(TEMPLATES)
    built = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=TypeMappings(user_mappings_path=str(tmp_path / "unused.json"), rules=rules),
        naming_templates=templates,
        naming_state=state,
        ha_translations=HomeAssistantNames(),
    )
    built.floors = {}
    built.areas = {"b": {"area_id": "b", "name": "Basement"}}
    built.devices = {"d": {"id": "d", "name": "Basement Pump", "area_id": "b"}}
    built.entities = {
        "switch.a": {
            "id": "reg-a",
            "entity_id": "switch.a",
            "device_id": "d",
            "platform": "matter",
            "name": APPLIED,
            "original_name": None,
            "translation_key": "switch",
            "device_class": "outlet",
            "has_entity_name": True,
        }
    }
    return built, rules


def test_a_rule_reaches_an_entity_whose_type_part_is_empty(with_rules, state):
    """A rule keys on the translation key, which needs no word to go by."""
    restructurer, rules = with_rules
    note(state, "")
    rules.upsert("translation_key", "switch", "matter", "en", "Relay")

    _, name = restructurer.generate_new_entity_id("switch.a", restructurer.entities["switch.a"])

    assert name == "Basement Pump Relay"
    assert restructurer.last_resolutions["switch.a"]["won_by"] == "rule:user"


def test_home_assistant_does_not_name_an_empty_type_part_after_the_device_class(with_rules, state):
    """Without a rule the name stays as it is.

    Home Assistant answers an empty name with the device class, which would put
    a word back into a name the user chose not to have one in.
    """
    restructurer, _ = with_rules
    note(state, "")

    _, name = restructurer.generate_new_entity_id("switch.a", restructurer.entities["switch.a"])

    assert name == APPLIED
    assert restructurer.last_resolutions["switch.a"]["value"] == ""
