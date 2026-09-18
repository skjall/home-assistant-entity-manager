"""Choosing another word for a place a rule already speaks for.

A type is called one thing in one place, so a second word for that place is not
a second rule - it is a change of mind about the one there. Asking for it came
back as "Rule r_9e6e6268 already covers one of those filters", which is true and
useless: the valve's "Cyclic timed irrigation" was called "Zyklische Bewässerung
(Dauer)" and could not be called "Zyklusprogramm (Zeit)" instead.
"""

import pytest

from naming_rules import NamingRuleError, NamingRules

VALVE = {"integration": "mqtt", "model": "Zigbee smart water valve"}


@pytest.fixture
def rules(tmp_path):
    return NamingRules(str(tmp_path / "naming_rules.json"), default_language="de")


def test_a_place_already_spoken_for_gets_the_new_word(rules):
    first = rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklische Bewässerung (Dauer)", VALVE)

    again = rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklusprogramm (Zeit)", VALVE)

    assert again["id"] == first["id"]
    assert again["targets"]["de"] == "Zyklusprogramm (Zeit)"
    assert len(rules.rules) == 1


def test_everywhere_is_a_place_like_any_other(rules):
    first = rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklische Bewässerung (Dauer)", None)

    again = rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklusprogramm (Zeit)", None)

    assert again["id"] == first["id"]
    assert again["targets"]["de"] == "Zyklusprogramm (Zeit)"


def test_a_narrower_place_is_not_the_same_place(rules):
    """The wider rule keeps its word; the model gets one of its own."""
    everywhere = rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklische Bewässerung (Dauer)", None)

    narrow = rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklusprogramm (Zeit)", VALVE)

    assert narrow["id"] != everywhere["id"]
    assert everywhere["targets"]["de"] == "Zyklische Bewässerung (Dauer)"
    assert rules.find("name", "Cyclic timed irrigation", "mqtt", "de", model=VALVE["model"])["id"] == narrow["id"]


def test_the_other_places_keep_the_word_they_had(rules):
    """The user asked about one place, not about everything the rule covers."""
    rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklische Bewässerung (Dauer)", VALVE)
    held = rules.add_filter(
        "name", "cyclic_timed_irrigation", "de", "Zyklische Bewässerung (Dauer)", {"integration": "tuya"}
    )
    assert len(held["filters"]) == 2

    reworded = rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklusprogramm (Zeit)", VALVE)

    assert reworded["id"] != held["id"]
    assert held["filters"] == [{"integration": "tuya"}]
    assert reworded["filters"] == [VALVE]
    assert (
        rules.find("name", "Cyclic timed irrigation", "tuya", "de")["targets"]["de"] == "Zyklische Bewässerung (Dauer)"
    )


def test_saying_the_same_thing_again_changes_nothing(rules):
    first = rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklische Bewässerung (Dauer)", VALVE)

    again = rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklische Bewässerung (Dauer)", VALVE)

    assert again["id"] == first["id"]
    assert len(rules.rules) == 1


def test_one_entity_named_its_own_way_can_be_renamed_too(rules):
    """A filter on a registry id is a place like any other."""
    one = {"registry_id": "reg-a"}
    first = rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklische Bewässerung (Dauer)", one)

    again = rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklusprogramm (Zeit)", one)

    assert again["id"] == first["id"]
    assert again["targets"]["de"] == "Zyklusprogramm (Zeit)"


def test_a_different_type_in_the_same_place_is_still_its_own_rule(rules):
    rules.add_filter("name", "cyclic_timed_irrigation", "de", "Zyklusprogramm (Zeit)", VALVE)

    other = rules.add_filter("name", "cyclic_quantitative_irrigation", "de", "Zyklische Bewässerung (Menge)", VALVE)

    assert len(rules.rules) == 2
    assert other["match"]["value"] == "cyclic_quantitative_irrigation"


def test_a_rule_still_needs_a_word(rules):
    with pytest.raises(NamingRuleError):
        rules.add_filter("name", "cyclic_timed_irrigation", "de", "   ", VALVE)
