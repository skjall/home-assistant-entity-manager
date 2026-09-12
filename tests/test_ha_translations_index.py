"""The device-class index that makes a name lookup cheap.

Walking every translated resource for each lookup cost a tenth of a second per
request on a real home, so the answers are indexed once per language. Anything
cached has to be dropped when new translations arrive.
"""

import asyncio

from ha_translations import HaTranslations


def resources(temperature: str) -> dict:
    return {
        "component.sensor.entity_component.temperature.name": temperature,
        "component.sensor.entity_component.humidity.name": "Luftfeuchtigkeit",
        "component.binary_sensor.entity_component.door.name": "Tür",
    }


class FakeSocket:
    """Answers the translation request with whatever it was handed."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = 0

    async def _send_message(self, message):
        self.asked += 1
        return 1

    async def _receive_message(self):
        return {"id": 1, "success": True, "result": {"resources": self.answers.pop(0)}}


def load(translations, socket, language="de"):
    asyncio.run(translations.load(socket, language))


def test_a_name_is_found_by_its_key_alone():
    translations = HaTranslations()
    load(translations, FakeSocket([resources("Temperatur")]))

    assert translations.name_for_key("temperature", "de") == "Temperatur"
    assert translations.name_for_key("door", "de") == "Tür"
    assert translations.name_for_key("nonsense", "de") is None


def test_the_index_is_built_once_and_answers_the_same_every_time():
    translations = HaTranslations()
    socket = FakeSocket([resources("Temperatur")])
    load(translations, socket)

    first = [translations.name_for_key("temperature", "de") for _ in range(50)]

    assert set(first) == {"Temperatur"}
    assert socket.asked == 1


def test_new_translations_replace_what_was_indexed():
    """A cache that outlives its data would answer with yesterday's names."""
    translations = HaTranslations()
    load(translations, FakeSocket([resources("Temperatur")]))
    assert translations.name_for_key("temperature", "de") == "Temperatur"

    translations._component.pop("de")
    load(translations, FakeSocket([resources("Raumtemperatur")]))

    assert translations.name_for_key("temperature", "de") == "Raumtemperatur"


def test_every_language_keeps_its_own_index():
    translations = HaTranslations()
    load(translations, FakeSocket([resources("Temperatur")]), "de")
    load(translations, FakeSocket([resources("Temperature")]), "en")

    assert translations.name_for_key("temperature", "de") == "Temperatur"
    assert translations.name_for_key("temperature", "en") == "Temperature"
