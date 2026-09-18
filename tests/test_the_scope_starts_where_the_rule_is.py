"""Changing a type offers the reach the rule in force already has.

Renaming a type is a change to the rule that names it, so starting the choice
at "everywhere" when the rule in force covers one device model offers to rename
entities the user was not looking at - and, until a rule could be reworded at
all, simply failed. The buttons now start where the rule is.

The reach comes from what the naming reported about this entity, so the two
cannot disagree; these tests hold that report to what the page reads out of it.
"""

import os

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_rules import NamingRules
from naming_templates import NamingTemplates
from type_mappings import DEFAULT_SYSTEM_MAPPINGS, TypeMappings

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALVE = {"integration": "mqtt", "model": "Zigbee smart water valve"}


@pytest.fixture(scope="module")
def markup():
    with open(os.path.join(HERE, "templates", "index.html"), encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture
def restructurer(tmp_path):
    rules = NamingRules(
        str(tmp_path / "naming_rules.json"),
        device_class_keys=DEFAULT_SYSTEM_MAPPINGS["device_class"].keys(),
        default_language="de",
    )
    built = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=TypeMappings(user_mappings_path=str(tmp_path / "unused.json"), rules=rules),
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
    )
    built.floors = {}
    built.areas = {"b": {"area_id": "b", "name": "Badezimmer"}}
    built.devices = {"d": {"id": "d", "name": "Zulauf Ventil", "area_id": "b", "model": VALVE["model"]}}
    built.entities = {
        "sensor.a": {
            "id": "reg-a",
            "entity_id": "sensor.a",
            "device_id": "d",
            "platform": "mqtt",
            "original_name": "Cyclic timed irrigation",
            "has_entity_name": True,
        }
    }
    built.rules = rules
    return built


def resolution_of(restructurer):
    restructurer.build_naming_context("sensor.a", restructurer.entities["sensor.a"])
    return restructurer.last_resolutions["sensor.a"]


def test_a_rule_on_one_model_reports_that_model(restructurer):
    restructurer.rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklusprogramm (Zeit)", VALVE)

    match = resolution_of(restructurer)["matched_on"]

    assert match["model"] == VALVE["model"]
    assert match["integration"] == "mqtt"


def test_a_rule_on_one_integration_reports_no_model(restructurer):
    restructurer.rules.add_filter(
        "name", "cyclic_timed_irrigation", "de", "Zyklusprogramm (Zeit)", {"integration": "mqtt"}
    )

    match = resolution_of(restructurer)["matched_on"]

    assert match["integration"] == "mqtt"
    assert not match["model"]


def test_a_rule_everywhere_reports_neither(restructurer):
    restructurer.rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklusprogramm (Zeit)", None)

    match = resolution_of(restructurer)["matched_on"]

    assert not match["integration"]
    assert not match["model"]


def test_a_rule_written_for_one_entity_names_it_in_its_filters(restructurer):
    """The page tells this from the others by finding the entity's own id."""
    restructurer.rules.add_filter(
        "name", "cyclic_timed_irrigation", "de", "Zyklusprogramm (Zeit)", {"registry_id": "reg-a"}
    )

    match = resolution_of(restructurer)["matched_on"]

    assert {"registry_id": "reg-a"} in match["filters"]


def test_the_page_starts_the_choice_where_the_rule_is(markup):
    assert "const already = this.scopeThatApplied(entity);" in markup
    body = markup[markup.index("scopeThatApplied(entity) {") : markup.index("toggleEntityExpand(entity) {")]
    assert "one.registry_id === entity.registry_id" in body
    assert "if (match.model) return 'model';" in body
    assert "if (match.integration) return 'integration';" in body


def test_a_name_no_rule_of_the_users_decided_still_guesses(markup):
    body = markup[markup.index("scopeThatApplied(entity) {") : markup.index("toggleEntityExpand(entity) {")]
    assert "resolution.won_by !== 'rule:user'" in body
    assert "entity.type_integration_count" in markup
