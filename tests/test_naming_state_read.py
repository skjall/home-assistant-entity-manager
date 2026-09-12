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
