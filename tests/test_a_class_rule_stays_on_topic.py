"""A rule on a device class may only word that class, not rename other things.

A device class says what a value measures, and a rule on one settles how it is
worded: "Firmware-Update" and "Firmware Status" both become "Firmware". But
integrations hang a class on entities that are about something else - UniFi
gives its "regenerate password" button the class "update" - and there the rule
is talking about a different thing entirely. It called that button "Firmware".
"""

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_rules import NamingRules
from naming_templates import NamingTemplates
from type_mappings import TypeMappings


class Translations:
    CLASSES = {
        ("button", "update"): {"de": "Update", "en": "Update"},
        ("sensor", "carbon_dioxide"): {"de": "CO2", "en": "Carbon dioxide"},
    }

    def translation_key_name(self, platform, domain, key, language):
        return None

    def device_class_name(self, domain, device_class, language):
        return self.CLASSES.get((domain, device_class), {}).get(language)


@pytest.fixture
def restructurer(tmp_path):
    rules = NamingRules(str(tmp_path / "rules.json"), default_language="de")
    rules.add_filter("device_class", "update", "de", "Firmware", None)
    result = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=TypeMappings(user_mappings_path=str(tmp_path / "mappings.json"), rules=rules),
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
        language="de",
    )
    result.ha_translations = Translations()
    result.devices = {}
    return result


UPDATE_BUTTON = {"platform": "unifi", "original_device_class": "update"}


def test_a_name_that_is_not_about_the_class_is_left_alone(restructurer):
    """ "Passwort neu generieren" has nothing to do with an update."""
    answer = restructurer._resolve_supplied_name(
        "Passwort neu generieren", "button.iot_regenerate_password", UPDATE_BUTTON
    )

    assert answer["value"] == "Passwort neu generieren"


def test_a_name_that_words_the_class_is_still_settled_by_the_rule(restructurer):
    """That is what the rule is for: one wording for all of them."""
    answer = restructurer._resolve_supplied_name("Firmware-Update", "button.router_update", UPDATE_BUTTON)

    assert answer["value"] == "Firmware"


def test_the_class_word_may_sit_anywhere_in_the_name(restructurer):
    answer = restructurer._resolve_supplied_name("Update verfügbar", "button.printer_update", UPDATE_BUTTON)

    assert answer["value"] == "Firmware"


def test_a_name_that_says_it_with_the_rules_own_word_is_settled_too(restructurer):
    """ "Firmware Status" is about an update, it just says so as "Firmware"."""
    answer = restructurer._resolve_supplied_name("Firmware Status", "button.printer_status", UPDATE_BUTTON)

    assert answer["value"] == "Firmware"


def test_a_shorter_word_for_the_class_counts_as_naming_it(restructurer):
    """ "CO2 concentration" is about carbon dioxide, under the rule's own name."""
    rules = restructurer.type_mappings.rules
    rules.add_filter("device_class", "carbon_dioxide", "de", "CO2", None)
    sensor = {"platform": "overkiz", "original_device_class": "carbon_dioxide"}

    answer = restructurer._resolve_supplied_name("CO2 concentration", "sensor.living_co2", sensor)

    assert answer["value"] == "CO2"


def test_an_entity_that_supplies_no_name_is_left_to_the_rule(restructurer):
    assert restructurer._names_the_class("", "button.x", "update", "Firmware") is True


def test_the_english_class_name_counts_too(restructurer):
    """An integration that never translated its names still reaches the rule."""
    assert restructurer._names_the_class("Firmware update", "button.x", "update", "Firmware") is True
