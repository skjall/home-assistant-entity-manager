"""Exceptions become rules that name one entity, and stay tidy afterwards."""

import pytest

from entity_restructurer import EntityRestructurer
from naming_exception_adoption import adopt_exceptions, forget_missing
from naming_overrides import NamingOverrides
from naming_rules import NamingRules
from naming_templates import NamingTemplates
from type_mappings import DEFAULT_SYSTEM_MAPPINGS, TypeMappings


@pytest.fixture
def overrides(tmp_path):
    return NamingOverrides(str(tmp_path / "naming_overrides.json"))


@pytest.fixture
def rules(tmp_path):
    return NamingRules(
        str(tmp_path / "naming_rules.json"),
        legacy_path=str(tmp_path / "user_type_mappings.json"),
        device_class_keys=DEFAULT_SYSTEM_MAPPINGS["device_class"].keys(),
        default_language="de",
    )


@pytest.fixture
def restructurer(tmp_path, overrides, rules):
    built = EntityRestructurer(
        client=object(),
        naming_overrides=overrides,
        type_mappings=TypeMappings(user_mappings_path=str(tmp_path / "unused.json"), rules=rules),
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
    )
    built.floors = {}
    built.areas = {"s": {"area_id": "s", "name": "Schlafzimmer"}}
    built.devices = {"d": {"id": "d", "name": "Button", "area_id": "s"}}
    built.entities = {
        "update.schlafzimmer_button_firmware": {
            "id": "reg-1",
            "entity_id": "update.schlafzimmer_button_firmware",
            "device_id": "d",
            "platform": "mqtt",
            "original_name": "Firmware",
            "has_entity_name": True,
        }
    }
    return built


def test_an_exception_becomes_a_rule_for_that_entity(overrides, rules, restructurer):
    overrides.set_entity_override("reg-1", "Aktualisierung")

    report = adopt_exceptions(overrides, rules, restructurer)

    assert report["adopted"] == 1
    assert overrides.get_entity_override("reg-1") is None
    rule = rules.for_entity("reg-1", "de")
    assert rule is not None and rule["targets"]["de"] == "Aktualisierung"
    assert rule["filters"] == [{"registry_id": "reg-1"}]


def test_the_name_still_reaches_the_entity(overrides, rules, restructurer):
    overrides.set_entity_override("reg-1", "Aktualisierung")
    adopt_exceptions(overrides, rules, restructurer)

    _, name = restructurer.generate_new_entity_id(
        "update.schlafzimmer_button_firmware", restructurer.entities["update.schlafzimmer_button_firmware"]
    )

    assert name == "Schlafzimmer Button Aktualisierung"


def test_a_rule_for_one_entity_beats_a_rule_for_its_type(overrides, rules, restructurer):
    """The narrowest filter decides, and none is narrower than one entity."""
    rules.upsert("name", "Firmware", None, "de", "Firmware-Update")
    overrides.set_entity_override("reg-1", "Aktualisierung")
    adopt_exceptions(overrides, rules, restructurer)

    _, name = restructurer.generate_new_entity_id(
        "update.schlafzimmer_button_firmware", restructurer.entities["update.schlafzimmer_button_firmware"]
    )

    assert name == "Schlafzimmer Button Aktualisierung"


def test_an_entity_left_alone_stays_an_override(overrides, rules, restructurer):
    """Keeping the integration's own name is not a name, so it is not a rule."""
    overrides.ignore_entity("reg-1")

    report = adopt_exceptions(overrides, rules, restructurer)

    assert report["adopted"] == 0
    assert overrides.get_entity_override("reg-1")["keep_original"] is True


def test_adoption_happens_once(overrides, rules, restructurer):
    overrides.set_entity_override("reg-1", "Aktualisierung")
    adopt_exceptions(overrides, rules, restructurer)
    overrides.set_entity_override("reg-1", "Von Hand")

    assert adopt_exceptions(overrides, rules, restructurer) is None
    assert overrides.get_entity_override("reg-1")["name"] == "Von Hand"


def test_what_the_registry_no_longer_has_is_forgotten(overrides, rules, restructurer):
    overrides.set_entity_override("reg-gone", "Tür")
    rules.add_filter("name", "tur", "de", "Tür", {"registry_id": "reg-gone"})

    removed = forget_missing(overrides, rules, restructurer)

    assert removed == {"exceptions": 1, "rules": 1}
    assert overrides.get_entity_override("reg-gone") is None
    assert rules.for_entity("reg-gone", "de") is None


def test_an_unread_registry_forgets_nothing(overrides, rules, restructurer):
    """An empty registry is no evidence that an entity is gone."""
    overrides.set_entity_override("reg-1", "Aktualisierung")
    restructurer.entities = {}

    assert forget_missing(overrides, rules, restructurer) == {"exceptions": 0, "rules": 0}
    assert overrides.get_entity_override("reg-1") is not None


def test_one_wording_wanted_twice_is_one_rule_with_two_filters(rules):
    """This is what a filter list is for: a rule applies to a list of places."""
    rules.add_filter("name", "firmware", "de", "Aktualisierung", {"registry_id": "reg-1"})
    rules.add_filter("name", "firmware", "de", "Aktualisierung", {"registry_id": "reg-2"})

    assert len(rules.rules) == 1
    assert rules.rules[0]["filters"] == [{"registry_id": "reg-1"}, {"registry_id": "reg-2"}]


def test_a_different_wording_for_one_type_is_its_own_rule(rules):
    rules.add_filter("name", "firmware", "de", "Aktualisierung", {"registry_id": "reg-1"})
    rules.add_filter("name", "firmware", "de", "Update verfügbar", {"registry_id": "reg-2"})

    assert len(rules.rules) == 2


def test_a_wording_that_already_applies_everywhere_is_left_alone(rules):
    """Narrowing it to the one place just asked about would take it from the rest."""
    rules.upsert("name", "firmware", None, "de", "Aktualisierung")

    rules.add_filter("name", "firmware", "de", "Aktualisierung", {"registry_id": "reg-1"})

    assert len(rules.rules) == 1
    assert rules.rules[0]["filters"] == []


def test_removing_the_last_place_removes_the_rule(rules):
    rule = rules.add_filter("name", "firmware", "de", "Aktualisierung", {"registry_id": "reg-1"})
    rules.add_filter("name", "firmware", "de", "Aktualisierung", {"registry_id": "reg-2"})

    rules.remove_filter(rule["id"], {"registry_id": "reg-1"})
    assert rules.get(rule["id"])["filters"] == [{"registry_id": "reg-2"}]

    rules.remove_filter(rule["id"], {"registry_id": "reg-2"})
    assert rules.get(rule["id"]) is None


def test_rules_that_say_the_same_thing_are_folded_together(rules):
    """What the migration does to rules written before the filter list."""
    rules.add_filter("name", "linkquality", "de", "Verbindungsqualität", {"registry_id": "reg-1"})
    second = rules._make_rule(
        "name", "linkquality", None, {"de": "Verbindungsqualität"}, filters=[{"registry_id": "reg-2"}]
    )
    rules.rules.append(second)

    folded = rules.merge_duplicates()

    assert len(folded) == 1
    assert len(rules.rules) == 1
    assert rules.rules[0]["filters"] == [{"registry_id": "reg-1"}, {"registry_id": "reg-2"}]
