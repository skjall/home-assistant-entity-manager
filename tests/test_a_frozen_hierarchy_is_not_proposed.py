"""A supplied name that froze an old room and device is not the type part.

Home Assistant names a helper built in its interface once, in full, and never
again. Move the socket behind it and the entity is called "Küche Kühlschrank
Steckdose Energie" while the integration still supplies "Kammer Lüftungsanlage
Steckdose Energie". Taking the supplied one as the type part renders the old
room and device inside the new ones, which is how a proposal came out as
"Küche Kühlschrank Steckdose Kammer Lüftungsanlage Steckdose Energie".

What gives it away is the shape: the name the entity carries today is the last
thing the supplied name says, and everything in front of it named somewhere
else.
"""

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_templates import NamingTemplates


@pytest.fixture
def restructurer(tmp_path):
    built = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=None,
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
    )
    built.floors = {}
    built.areas = {"k": {"area_id": "k", "name": "Küche"}}
    built.devices = {"d": {"id": "d", "name": "Kühlschrank Steckdose", "area_id": "k"}}
    built.entities = {
        "sensor.a": {
            "id": "reg-a",
            "entity_id": "sensor.a",
            "device_id": "d",
            "platform": "integration",
            "has_entity_name": True,
        }
    }
    return built


def name_of(restructurer):
    _, name = restructurer.generate_new_entity_id("sensor.a", restructurer.entities["sensor.a"])
    return name


def test_the_old_room_and_device_do_not_come_back(restructurer):
    restructurer.entities["sensor.a"]["original_name"] = "Kammer Lüftungsanlage Steckdose Energie"
    restructurer.entities["sensor.a"]["name"] = "Küche Kühlschrank Steckdose Energie"

    assert name_of(restructurer) == "Küche Kühlschrank Steckdose Energie"


def test_a_supplied_name_that_says_something_else_is_kept(restructurer):
    """Only a name the standing one ends is a frozen hierarchy."""
    restructurer.entities["sensor.a"]["original_name"] = "Tagesenergie"
    restructurer.entities["sensor.a"]["name"] = "Küche Kühlschrank Steckdose Energie"

    assert name_of(restructurer) == "Küche Kühlschrank Steckdose Tagesenergie"


def test_a_supplied_name_that_is_already_the_type_part_is_kept(restructurer):
    restructurer.entities["sensor.a"]["original_name"] = "Energie"
    restructurer.entities["sensor.a"]["name"] = "Küche Kühlschrank Steckdose Energie"

    assert name_of(restructurer) == "Küche Kühlschrank Steckdose Energie"


def test_a_word_is_not_matched_inside_another_word():
    """ "Energie" does not end "Tagesenergie" - that is a different type."""
    assert not EntityRestructurer._only_puts_something_in_front("Tagesenergie", "Energie")


def test_the_match_runs_word_for_word_from_the_back():
    assert EntityRestructurer._only_puts_something_in_front("Kammer Lüftungsanlage Steckdose Energie", "Energie")
    assert EntityRestructurer._only_puts_something_in_front("Vorrat Steckdose Kosten", "Kosten")
    assert not EntityRestructurer._only_puts_something_in_front("Energie", "Energie")
    assert not EntityRestructurer._only_puts_something_in_front("Energie", "Kammer Energie")


def test_spelling_does_not_break_the_match():
    assert EntityRestructurer._only_puts_something_in_front("Kammer Steckdose Spannung (Effektivwert)", "Effektivwert")
