"""The note on a name says which lookup answered, not which word was asked about.

Home Assistant's name for an entity comes from that entity's translation key or
from its device class. Reporting the supplied name instead made the interface
claim that "every entity called Betriebszustand is called Status", when the
match was Miele's key `status` on this one entity.
"""

import pytest

from entity_restructurer import EntityRestructurer
from naming_rules import NamingRules
from type_mappings import TypeMappings


class FakeTranslations:
    """Answers like Home Assistant: a key from the integration, a class from the core."""

    def __init__(self, keys=None, classes=None):
        self.keys = keys or {}
        self.classes = classes or {}

    def translation_key_name(self, platform, domain, key, language):
        return self.keys.get((platform, key))

    def device_class_name(self, domain, device_class, language):
        return self.classes.get(device_class)


@pytest.fixture
def restructurer(tmp_path):
    made = EntityRestructurer.__new__(EntityRestructurer)
    made.language = "de"
    made.devices = {}
    made.last_resolutions = {}
    rules = NamingRules(str(tmp_path / "rules.json"), default_language="de")
    made.type_mappings = TypeMappings(str(tmp_path / "mappings.json"), rules=rules)
    return made


def test_a_key_is_reported_as_the_key(restructurer):
    restructurer.ha_translations = FakeTranslations(keys={("miele", "status"): "Status"})
    registry = {"platform": "miele", "translation_key": "status"}

    answer = restructurer._resolve_supplied_name("Betriebszustand", "sensor.fridge_status", registry)

    assert answer["value"] == "Status"
    assert answer["matched_on"]["kind"] == "translation_key"
    assert answer["matched_on"]["value"] == "status"
    assert answer["matched_on"]["integration"] == "miele"


def test_a_device_class_is_reported_as_the_class(restructurer):
    restructurer.ha_translations = FakeTranslations(classes={"temperature": "Temperatur"})
    registry = {"platform": "ecoflow_cloud", "original_device_class": "temperature"}

    answer = restructurer._resolve_supplied_name("Temperature", "sensor.plug_temperature", registry)

    assert answer["value"] == "Temperatur"
    assert answer["matched_on"]["kind"] == "device_class"
    assert answer["matched_on"]["value"] == "temperature"


def test_a_name_no_lookup_answers_keeps_its_own_word(restructurer):
    restructurer.ha_translations = FakeTranslations()
    registry = {"platform": "miele", "translation_key": "unknown_key"}

    answer = restructurer._resolve_supplied_name("Betriebszustand", "sensor.fridge_thing", registry)

    assert answer["value"] == "Betriebszustand"
    assert answer["won_by"] == "original"
