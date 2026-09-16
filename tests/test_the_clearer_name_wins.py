"""Which of Home Assistant's two answers names an entity.

An entity can carry both a device class - what it is or measures - and the word
its integration picked for this kind of entity. The word used to win outright,
so a Matter outlet, whose integration calls every switch "switch", came out
"Schalter" although its device class says "outlet". Whichever says more wins
now, and neither may replace a name that already means something else.
"""

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_templates import NamingTemplates
from type_mappings import TypeMappings


class Translations:
    """The answers Home Assistant gives, as a lookup instead of its files."""

    KEYS = {
        ("matter", "switch", "switch"): {"de": "Schalter", "en": "Switch"},
        ("unifi", "sensor", "battery_voltage"): {"de": "Batteriespannung", "en": "Battery voltage"},
    }
    CLASSES = {
        ("switch", "outlet"): {"de": "Steckdose", "en": "Outlet"},
        ("sensor", "voltage"): {"de": "Spannung", "en": "Voltage"},
    }

    def translation_key_name(self, platform, domain, key, language):
        return self.KEYS.get((platform, domain, key), {}).get(language)

    def device_class_name(self, domain, device_class, language):
        return self.CLASSES.get((domain, device_class), {}).get(language)


@pytest.fixture
def restructurer(tmp_path):
    result = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=TypeMappings(user_mappings_path=str(tmp_path / "mappings.json")),
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
        language="de",
    )
    result.ha_translations = Translations()
    return result


OUTLET = {"platform": "matter", "translation_key": "switch", "original_device_class": "outlet"}
VOLTAGE = {"platform": "unifi", "translation_key": "battery_voltage", "original_device_class": "voltage"}


def test_a_word_that_only_repeats_the_domain_loses_to_the_device_class(restructurer):
    """ "switch" on a switch entity says nothing the id does not already say."""
    name = restructurer._home_assistant_name("switch.thermomix", OUTLET, "de")

    assert name == "Steckdose"


def test_a_word_that_says_more_than_the_device_class_still_wins(restructurer):
    name = restructurer._home_assistant_name("sensor.ap_battery", VOLTAGE, "de")

    assert name == "Batteriespannung"


def test_a_name_already_in_hand_is_not_replaced_by_a_different_meaning(restructurer):
    """An outlet called "Steckdose" must not turn back into "Schalter"."""
    name = restructurer._home_assistant_name("switch.thermomix", OUTLET, "de", supplied="Steckdose")

    assert name in (None, "Steckdose")


def test_a_name_already_in_hand_is_restated_where_it_means_the_same(restructurer):
    name = restructurer._home_assistant_name("sensor.ap_battery", VOLTAGE, "de", supplied="Battery voltage")

    assert name == "Batteriespannung"


def test_a_word_that_repeats_the_domain_still_answers_when_nothing_else_does(restructurer):
    """Without a device class it is the best there is, so it keeps answering."""
    without_class = {"platform": "matter", "translation_key": "switch"}

    assert restructurer._home_assistant_name("switch.lamp", without_class, "de") == "Schalter"


def test_an_entity_with_neither_is_left_to_the_rest_of_the_chain(restructurer):
    assert restructurer._home_assistant_name("switch.lamp", {"platform": "matter"}, "de") is None
