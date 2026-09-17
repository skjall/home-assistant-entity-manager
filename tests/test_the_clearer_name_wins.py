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
        ("matter", "button", "button"): {"de": "Taste", "en": "Button"},
        ("unifi", "sensor", "battery_voltage"): {"de": "Batteriespannung", "en": "Battery voltage"},
    }
    CLASSES = {
        ("switch", "outlet"): {"de": "Steckdose", "en": "Outlet"},
        ("sensor", "voltage"): {"de": "Spannung", "en": "Voltage"},
        # Home Assistant has no words for a button's device class, so none here.
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


def test_an_outlet_already_named_stays_named(restructurer):
    """An outlet called "Steckdose" must not turn back into "Schalter"."""
    name = restructurer._home_assistant_name("switch.thermomix", OUTLET, "de", supplied="Steckdose")

    assert name == "Steckdose"


def test_a_word_that_says_more_may_still_replace_the_supplied_name(restructurer):
    """That is the whole point of the word: a raw supplied name becomes a real one."""
    name = restructurer._home_assistant_name("sensor.ap_battery", VOLTAGE, "de", supplied="Battery volts")

    assert name == "Batteriespannung"


def test_a_word_that_repeats_the_domain_still_answers_when_nothing_else_does(restructurer):
    """Without a device class it is the best there is, so it keeps answering."""
    without_class = {"platform": "matter", "translation_key": "switch"}

    assert restructurer._home_assistant_name("switch.lamp", without_class, "de") == "Schalter"


def test_a_device_class_nobody_can_put_into_words_does_not_silence_the_word(restructurer):
    """ "button" has no words for its device class, so "Taste" has to stay."""
    button = {"platform": "matter", "translation_key": "button", "original_device_class": "button"}

    assert restructurer._home_assistant_name("button.identify", button, "de") == "Taste"


def test_the_device_class_still_may_not_widen_a_name_that_says_more(restructurer):
    """The older guard, unchanged: a cell voltage must not become "Spannung"."""
    only_class = {"platform": "unifi", "original_device_class": "voltage"}

    assert restructurer._home_assistant_name("sensor.ap", only_class, "de", supplied="Zellspannung") is None


def test_an_entity_with_neither_is_left_to_the_rest_of_the_chain(restructurer):
    assert restructurer._home_assistant_name("switch.lamp", {"platform": "matter"}, "de") is None
