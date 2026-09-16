"""An entity that already carries a note must still follow the rules.

The note holds the type part a written name was built from. Which of the two
possible readings it holds decides whether a rule the user edits afterwards
ever reaches the entity again: the word that went in does, the word that came
out does not - no rule matches its own output. Older notes hold the output, so
that reading has to be recognised and undone on the way back.
"""

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_rules import NamingRules
from naming_state import NamingState
from naming_templates import NamingTemplates
from type_mappings import DEFAULT_SYSTEM_MAPPINGS, TypeMappings

APPLIED = "Küche Dunstabzugshaube LED-Helligkeit"


@pytest.fixture
def state(tmp_path):
    return NamingState(str(tmp_path / "naming_state.json"))


@pytest.fixture
def rules(tmp_path):
    built = NamingRules(
        str(tmp_path / "naming_rules.json"),
        legacy_path=str(tmp_path / "user_type_mappings.json"),
        device_class_keys=DEFAULT_SYSTEM_MAPPINGS["device_class"].keys(),
        default_language="de",
    )
    built.upsert("name", "Brightness", "ecoflow_cloud", "de", "LED-Helligkeit")
    return built


@pytest.fixture
def restructurer(tmp_path, state, rules):
    built = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=TypeMappings(user_mappings_path=str(tmp_path / "unused.json"), rules=rules),
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
        naming_state=state,
    )
    built.floors = {}
    built.areas = {"k": {"area_id": "k", "name": "Küche"}}
    built.devices = {"d": {"id": "d", "name": "Dunstabzugshaube", "area_id": "k"}}
    built.entities = {
        "number.led": {
            "id": "reg-led",
            "entity_id": "number.led",
            "device_id": "d",
            "platform": "ecoflow_cloud",
            "original_name": "Brightness",
            "name": APPLIED,
            "has_entity_name": True,
        }
    }
    return built


def test_a_rule_edit_reaches_an_entity_whose_note_holds_what_went_in(restructurer, state, rules):
    state.record("reg-led", applied_name=APPLIED, applied_entity_id="number.led", base_entity="Brightness")

    rules.upsert("name", "Brightness", "ecoflow_cloud", "de", "LED-Leuchtstärke")
    _, name = restructurer.generate_new_entity_id("number.led", restructurer.entities["number.led"])

    assert name == "Küche Dunstabzugshaube LED-Leuchtstärke"


def test_a_rule_edit_reaches_an_entity_whose_note_holds_what_came_out(restructurer, state, rules):
    """Notes written before this held the rendered word, and were a dead end.

    Putting the supplied name through the rules says as much: it renders to
    exactly what the note holds, so the note was that rendering and the supplied
    name was its input. That correction is written back, because once the rule
    is edited the two words no longer agree and it could not be made again.
    """
    state.record("reg-led", applied_name=APPLIED, applied_entity_id="number.led", base_entity="LED-Helligkeit")

    restructurer.generate_new_entity_id("number.led", restructurer.entities["number.led"])
    assert state.get("reg-led")["base_entity"] == "Brightness"

    rules.upsert("name", "Brightness", "ecoflow_cloud", "de", "LED-Leuchtstärke")
    _, name = restructurer.generate_new_entity_id("number.led", restructurer.entities["number.led"])

    assert name == "Küche Dunstabzugshaube LED-Leuchtstärke"
    assert restructurer.last_resolutions["number.led"]["rule_id"] is not None


def test_a_supplied_name_that_has_gone_stale_leaves_the_note_standing(restructurer, state):
    """The supplied name is only the input where it still renders to the note.

    A helper built in the interface froze the device name of its creation day
    into the name the integration supplies. That renders to something else
    entirely, so the note stays what the name is read back from.
    """
    restructurer.entities["number.led"]["original_name"] = "Vorrat Steckdose Kosten"
    restructurer.entities["number.led"]["name"] = "Küche Dunstabzugshaube Kosten"
    state.record(
        "reg-led",
        applied_name="Küche Dunstabzugshaube Kosten",
        applied_entity_id="number.led",
        base_entity="Kosten",
    )

    _, name = restructurer.generate_new_entity_id("number.led", restructurer.entities["number.led"])

    assert name == "Küche Dunstabzugshaube Kosten"
