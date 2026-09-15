"""Reading a name back that this add-on wrote.

Before there was a record of it, the only way to get the type part out of an
applied name was to take the rendered name apart along the templates again.
That works for names the templates produced and fails for everything else. With
the record the type part comes back as it was written; the parse stays for what
was named before the record existed.
"""

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_state import NamingState
from naming_templates import NamingTemplates

APPLIED = "Küche Deckenleuchte Verbindungsqualität"


@pytest.fixture
def state(tmp_path):
    return NamingState(str(tmp_path / "naming_state.json"))


@pytest.fixture
def restructurer(tmp_path, state):
    built = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=None,
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
        naming_state=state,
    )
    built.floors = {}
    built.areas = {"k": {"area_id": "k", "name": "Küche"}}
    built.devices = {"d": {"id": "d", "name": "Deckenleuchte", "area_id": "k"}}
    built.entities = {
        "sensor.a": {
            "id": "reg-a",
            "entity_id": "sensor.a",
            "device_id": "d",
            "platform": "mqtt",
            "name": APPLIED,
            "has_entity_name": True,
        }
    }
    return built


def remember(state, applied=APPLIED):
    state.record("reg-a", applied_name=applied, applied_entity_id="sensor.a", base_entity="Verbindungsqualität")


def test_a_name_we_wrote_comes_back_without_being_taken_apart(restructurer, state):
    remember(state)

    context = restructurer.build_naming_context("sensor.a", restructurer.entities["sensor.a"])

    assert context["entity"] == "Verbindungsqualität"
    assert restructurer.last_resolutions["sensor.a"]["won_by"] != "legacy_parse"


def test_without_a_record_the_name_is_still_taken_apart(restructurer):
    """Everything named before the record existed goes the old way."""
    restructurer.build_naming_context("sensor.a", restructurer.entities["sensor.a"])

    assert restructurer.last_resolutions["sensor.a"]["won_by"] == "legacy_parse"


def test_a_name_changed_elsewhere_is_not_read_back_as_ours(restructurer, state):
    """Somebody else's wording is not ours to take apart."""
    remember(state)
    restructurer.entities["sensor.a"]["name"] = "Küche Deckenleuchte Signal"

    restructurer.build_naming_context("sensor.a", restructurer.entities["sensor.a"])

    assert restructurer.last_resolutions["sensor.a"]["won_by"] == "legacy_parse"


def test_a_second_run_proposes_what_is_already_there(restructurer, state):
    """The whole point: applying a name twice must not change it."""
    remember(state)

    first = restructurer.generate_new_entity_id("sensor.a", restructurer.entities["sensor.a"])
    second = restructurer.generate_new_entity_id("sensor.a", restructurer.entities["sensor.a"])

    assert first == second
    assert first[1] == APPLIED


def test_a_name_the_integration_froze_does_not_come_back_to_haunt_the_proposal(restructurer, state):
    """Helpers built in the interface carry the device name of their creation day.

    Home Assistant names such an entity once, in full - "Vorrat Steckdose Kosten"
    - and never again. Stripping the device name off that works only while the
    device is still called what it was called then; afterwards the old name sits
    in the middle of every proposal. The note says what the type part was, and
    is therefore asked before the name the integration supplies.
    """
    restructurer.entities["sensor.a"]["original_name"] = "Vorrat Steckdose Kosten"
    restructurer.entities["sensor.a"]["name"] = "Küche Deckenleuchte Kosten"
    state.record(
        "reg-a",
        applied_name="Küche Deckenleuchte Kosten",
        applied_entity_id="sensor.a",
        base_entity="Kosten",
    )

    new_id, name = restructurer.generate_new_entity_id("sensor.a", restructurer.entities["sensor.a"])

    assert name == "Küche Deckenleuchte Kosten"
    assert new_id == "sensor.kuche_deckenleuchte_kosten"


def test_without_the_note_the_frozen_name_does_come_back(restructurer):
    """What the case above looks like unrecorded - and why the note is asked first."""
    restructurer.entities["sensor.a"]["original_name"] = "Vorrat Steckdose Kosten"
    restructurer.entities["sensor.a"]["name"] = "Küche Deckenleuchte Kosten"

    _, name = restructurer.generate_new_entity_id("sensor.a", restructurer.entities["sensor.a"])

    assert name == "Küche Deckenleuchte Vorrat Steckdose Kosten"


def test_an_older_note_without_a_type_part_still_says_whose_name_it_is(restructurer, state):
    """Notes written before the type part was kept can still be read back.

    The name was rendered by our own templates, so unwinding it gives the type
    part - and it beats the supplied name, which is where the stale device name
    comes from.
    """
    restructurer.entities["sensor.a"]["original_name"] = "Vorrat Steckdose Kosten"
    restructurer.entities["sensor.a"]["name"] = "Küche Deckenleuchte Kosten"
    state.record("reg-a", applied_name="Küche Deckenleuchte Kosten", applied_entity_id="sensor.a")

    _, name = restructurer.generate_new_entity_id("sensor.a", restructurer.entities["sensor.a"])

    assert name == "Küche Deckenleuchte Kosten"
    assert restructurer.last_resolutions["sensor.a"]["won_by"] == "legacy_parse"
