"""Tests for canonical keys, the rules store, its migration and rule resolution."""

import json

import pytest

from entity_restructurer import EntityRestructurer
from ha_translations import HaTranslations
from naming_canon import canon
from naming_display import normalize_display
from naming_overrides import NamingOverrides
from naming_rules import NamingRuleError, NamingRules
from naming_templates import NamingTemplates
from type_mappings import DEFAULT_SYSTEM_MAPPINGS, TypeMappings


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Effect speed", "effect_speed"),
        ("effect_speed", "effect_speed"),
        ("Effect-Speed", "effect_speed"),
        ("  Factory  reset ", "factory_reset"),
        ("Verbindungsqualität", "verbindungsqualitaet"),
        ("Température", "temperature"),
        ("", ""),
        ("___", ""),
    ],
)
def test_canon_folds_spelling(value, expected):
    assert canon(value) == expected


def _rules(tmp_path, legacy=None, language="de"):
    legacy_path = tmp_path / "user_type_mappings.json"
    if legacy is not None:
        legacy_path.write_text(json.dumps({"user_mappings": legacy}), encoding="utf-8")
    return NamingRules(
        str(tmp_path / "naming_rules.json"),
        legacy_path=str(legacy_path),
        device_class_keys=DEFAULT_SYSTEM_MAPPINGS["device_class"].keys(),
        default_language=language,
    )


def test_migration_merges_spellings_and_reports_conflicts(tmp_path):
    rules = _rules(
        tmp_path,
        legacy={
            "effect_speed": "Effektgeschwindigkeit",
            "Effect speed": "Effektgeschwindigkeit",
            "factory reset": "Zurücksetzen",
            "factory_reset": "Werksreset",
            "battery": "Akku",
        },
    )

    report = rules.migration_report
    by_key = {rule["match"]["value"]: rule for rule in rules.rules}

    assert set(by_key) == {"effect_speed", "factory_reset", "battery"}
    assert by_key["effect_speed"]["targets"] == {"de": "Effektgeschwindigkeit"}
    assert sorted(by_key["effect_speed"]["legacy_keys"]) == ["Effect speed", "effect_speed"]
    assert by_key["battery"]["match"]["kind"] == "device_class"
    assert len(report["conflicts"]) == 1 and report["conflicts"][0]["key"] == "factory_reset"
    assert by_key["factory_reset"]["alternatives"]
    assert (tmp_path / "migrations").exists()
    # The legacy file is untouched so a rollback is a file copy.
    assert (tmp_path / "user_type_mappings.json").exists()


def test_migration_runs_once(tmp_path):
    _rules(tmp_path, legacy={"linkquality": "Verbindungsqualität"})
    again = NamingRules(str(tmp_path / "naming_rules.json"), legacy_path=str(tmp_path / "user_type_mappings.json"))

    assert len(again.rules) == 1


def test_find_is_spelling_and_scope_aware(tmp_path):
    rules = _rules(tmp_path, legacy={})
    rules.upsert("name", "Effect speed", None, "de", "Effektgeschwindigkeit")
    rules.upsert("name", "effect_speed", "mqtt", "de", "Effekt-Tempo")

    assert rules.find("name", "EFFECT-SPEED", None, "de")["targets"]["de"] == "Effektgeschwindigkeit"
    assert rules.find("name", "Effect speed", "mqtt", "de")["targets"]["de"] == "Effekt-Tempo"
    assert rules.find("name", "Effect speed", "zha", "de")["targets"]["de"] == "Effektgeschwindigkeit"
    assert rules.find("name", "Effect speed", None, "en") is None


def test_upsert_updates_instead_of_duplicating(tmp_path):
    rules = _rules(tmp_path, legacy={})
    first = rules.upsert("name", "Link quality", None, "de", "Verbindungsqualität")
    second = rules.upsert("name", "link_quality", None, "de", "Signalqualität")

    assert first["id"] == second["id"]
    assert len(rules.rules) == 1
    assert second["targets"]["de"] == "Signalqualität"


def test_migrated_targets_follow_the_first_language_change(tmp_path):
    """Migrated rules were recorded under the default language; they move with the user's choice."""
    rules = _rules(tmp_path, legacy={"linkquality": "Verbindungsqualität"}, language="en")
    rules.upsert("name", "effect", None, "en", "Effekt", source="user")

    rules.set_language("de")

    migrated = next(rule for rule in rules.rules if rule["source"] == "migrated")
    manual = next(rule for rule in rules.rules if rule["source"] == "user")
    assert migrated["targets"] == {"de": "Verbindungsqualität"}
    assert manual["targets"] == {"en": "Effekt"}
    assert rules.find("name", "Linkquality", None, "de")["targets"]["de"] == "Verbindungsqualität"


def test_choose_alternative_resolves_conflict(tmp_path):
    rules = _rules(tmp_path, legacy={"factory reset": "Zurücksetzen", "factory_reset": "Werksreset"})
    rule = next(rule for rule in rules.rules if rule["match"]["value"] == "factory_reset")

    resolved = rules.choose_alternative(rule["id"], "Zurücksetzen")

    assert resolved["targets"]["de"] == "Zurücksetzen"
    assert resolved["alternatives"] == ["Werksreset"]
    assert rules.migration_report["conflicts"][0]["resolved"] is True
    with pytest.raises(NamingRuleError):
        rules.choose_alternative(rule["id"], "Nie gesehen")


@pytest.fixture
def restructurer(tmp_path):
    rules = _rules(tmp_path, legacy={"effect_speed": "Effektgeschwindigkeit"})
    result = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=TypeMappings(user_mappings_path=str(tmp_path / "unused.json"), rules=rules),
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
    )
    result.floors = {}
    result.areas = {"k": {"area_id": "k", "name": "Küche"}}
    result.devices = {"d": {"id": "d", "name": "Deckenleuchte", "area_id": "k"}}
    result.entities = {
        "number.x_effect_speed": {
            "id": "reg-1",
            "entity_id": "number.x_effect_speed",
            "device_id": "d",
            "platform": "mqtt",
            "original_name": "Effect speed",
            "has_entity_name": True,
        }
    }
    return result


def test_rule_matches_supplied_name_regardless_of_spelling(restructurer):
    context = restructurer.build_naming_context("number.x_effect_speed", restructurer.entities["number.x_effect_speed"])
    resolution = restructurer.last_resolutions["number.x_effect_speed"]

    assert context["entity"] == "Effektgeschwindigkeit"
    assert resolution["won_by"] == "rule:user"
    assert resolution["matched_on"]["kind"] == "name"


def test_translation_key_rule_beats_name_rule(restructurer):
    rules = restructurer.type_mappings.rules
    rules.upsert("translation_key", "effect_speed_tk", None, "de", "Effekttempo (TK)")
    restructurer.entities["number.x_effect_speed"]["translation_key"] = "effect_speed_tk"

    restructurer.build_naming_context("number.x_effect_speed", restructurer.entities["number.x_effect_speed"])
    resolution = restructurer.last_resolutions["number.x_effect_speed"]

    assert resolution["value"] == "Effekttempo (TK)"
    assert resolution["matched_on"]["kind"] == "translation_key"


def test_unmapped_name_reports_original(restructurer):
    restructurer.entities["number.x_effect_speed"]["original_name"] = "Power-on behavior"

    restructurer.build_naming_context("number.x_effect_speed", restructurer.entities["number.x_effect_speed"])
    resolution = restructurer.last_resolutions["number.x_effect_speed"]

    assert resolution["value"] == "Power-on behavior"
    assert resolution["won_by"] == "original"


def test_override_that_is_a_bare_type_key_uses_the_builtin_wording(restructurer):
    """Older versions stored keys like "cover" as exceptions; those still read as words."""
    restructurer.naming_overrides.set_entity_override("reg-1", "cover")

    restructurer.build_naming_context("number.x_effect_speed", restructurer.entities["number.x_effect_speed"])
    resolution = restructurer.last_resolutions["number.x_effect_speed"]

    assert resolution["won_by"] == "override"
    assert resolution["value"] == "Abdeckung"


def test_override_wording_is_kept_verbatim(restructurer):
    restructurer.naming_overrides.set_entity_override("reg-1", "Effect speed")

    restructurer.build_naming_context("number.x_effect_speed", restructurer.entities["number.x_effect_speed"])

    assert restructurer.last_resolutions["number.x_effect_speed"]["value"] == "Effect speed"


def test_language_follows_rules_store(restructurer):
    assert restructurer.language == "de"
    restructurer.type_mappings.rules.set_language("en")
    assert restructurer.language == "en"


def test_legacy_mapping_api_still_works_through_rules(tmp_path):
    rules = _rules(tmp_path, legacy={})
    mappings = TypeMappings(user_mappings_path=str(tmp_path / "unused.json"), rules=rules)

    mappings.set_user_mapping("Gradient scene", "Verlaufsszene")

    assert mappings.get_user_mapping("gradient_scene") == "Verlaufsszene"
    assert mappings.get_all_user_mappings() == {"gradient_scene": "Verlaufsszene"}
    assert mappings.remove_user_mapping("GRADIENT SCENE") is True
    assert mappings.get_all_user_mappings() == {}


def test_slug_style_original_name_reads_as_words(restructurer):
    """zigbee2mqtt scenes supply names like "nacht_rot"; the friendly name says "Nacht Rot"."""
    restructurer.entities["number.x_effect_speed"]["original_name"] = "nacht_rot"

    restructurer.build_naming_context("number.x_effect_speed", restructurer.entities["number.x_effect_speed"])
    resolution = restructurer.last_resolutions["number.x_effect_speed"]

    assert resolution["value"] == "Nacht Rot"
    assert resolution["won_by"] == "original"


def test_rule_on_slug_style_original_still_wins(restructurer):
    restructurer.type_mappings.rules.upsert("name", "nacht_rot", None, "de", "Nachtlicht rot")
    restructurer.entities["number.x_effect_speed"]["original_name"] = "nacht_rot"

    restructurer.build_naming_context("number.x_effect_speed", restructurer.entities["number.x_effect_speed"])

    assert restructurer.last_resolutions["number.x_effect_speed"]["value"] == "Nachtlicht rot"


@pytest.mark.parametrize(
    "value, mode, expected",
    [
        ("firmware", "first_word", "Firmware"),
        ("FIRMWARE", "first_word", "Firmware"),
        ("Battery LOW", "first_word", "Battery LOW"),
        ("BATTERY LOW", "first_word", "Battery low"),
        ("BSSID", "first_word", "BSSID"),
        ("FRITZ!OS", "sentence", "FRITZ!OS"),
        ("02:84:ED:CF:49:CB", "sentence", "02:84:ED:CF:49:CB"),
        ("Wohnzimmer HCHO", "sentence", "Wohnzimmer HCHO"),
        ("Allow Inter-DMZ HTTPS", "first_word", "Allow Inter-DMZ HTTPS"),
        ("Mac", "first_word", "Mac"),
        ("mac", "first_word", "MAC"),
        ("ble battery C47C", "first_word", "BLE battery C47C"),
        ("AUX Input", "first_word", "AUX Input"),
        ("FIRMWARE", "sentence", "Firmware"),
        ("Link Quality", "first_word", "Link Quality"),
        ("Link Quality", "sentence", "Link quality"),
        ("led indicator", "sentence", "LED indicator"),
        ("CO2 Level", "sentence", "CO2 level"),
        ("pm2.5", "first_word", "PM2.5"),
        ("ZigBee Channel", "sentence", "ZigBee channel"),
        ("Power-on behavior", "sentence", "Power-on behavior"),
        ("POWER-ON BEHAVIOR", "sentence", "Power-on behavior"),
        ("Spot 2", "sentence", "Spot 2"),
        ("nacht_rot", "off", "Nacht Rot"),
        ("firmware", "off", "firmware"),
        ("Verbindungsqualität", "sentence", "Verbindungsqualität"),
        ("", "sentence", ""),
    ],
)
def test_normalize_display(value, mode, expected):
    assert normalize_display(value, mode) == expected
    assert normalize_display(expected, mode) == expected  # idempotent


def test_migration_skips_mappings_that_only_repeat_the_spelling(tmp_path):
    rules = _rules(tmp_path, legacy={"firmware": "Firmware", "linkquality": "Verbindungsqualität"})

    assert [rule["match"]["value"] for rule in rules.rules] == ["linkquality"]
    assert rules.migration_report["skipped"] == [{"key": "firmware", "value": "Firmware", "legacy_keys": ["firmware"]}]


def test_spelling_variants_resolve_without_a_rule(restructurer):
    restructurer.entities["number.x_effect_speed"]["original_name"] = "firmware"

    restructurer.build_naming_context("number.x_effect_speed", restructurer.entities["number.x_effect_speed"])
    resolution = restructurer.last_resolutions["number.x_effect_speed"]

    assert resolution["value"] == "Firmware"
    assert resolution["won_by"] == "original"
    assert resolution["normalized"] is True
    assert resolution["platform"] == "mqtt"


def test_rule_that_repeats_the_spelling_is_not_reported(restructurer):
    rules = restructurer.type_mappings.rules
    rules.upsert("name", "firmware", None, "de", "Firmware")
    restructurer.entities["number.x_effect_speed"]["original_name"] = "firmware"

    restructurer.build_naming_context("number.x_effect_speed", restructurer.entities["number.x_effect_speed"])
    resolution = restructurer.last_resolutions["number.x_effect_speed"]

    assert resolution["won_by"] == "original"
    assert rules.is_redundant(rules.rules[-1], "de") is True
    assert rules.is_redundant(rules.rules[0], "de") is False  # effect_speed -> Effektgeschwindigkeit


def test_display_case_setting_changes_resolution(restructurer):
    rules = restructurer.type_mappings.rules
    restructurer.entities["number.x_effect_speed"]["original_name"] = "Link Quality"

    rules.set_display_case("sentence")
    restructurer.build_naming_context("number.x_effect_speed", restructurer.entities["number.x_effect_speed"])
    assert restructurer.last_resolutions["number.x_effect_speed"]["value"] == "Link quality"

    with pytest.raises(NamingRuleError):
        rules.set_display_case("shouting")


def test_model_scope_wins_over_integration_and_global(tmp_path):
    rules = _rules(tmp_path, legacy={})
    rules.upsert("name", "Tür", None, "de", "Zustand")
    rules.upsert("name", "Tür", "matter", "de", "Kontakt")
    rules.upsert("name", "Tür", "matter", "de", "Fensterkontakt", model="MYGGBETT door/window sensor")

    assert rules.find("name", "Tür", "matter", "de", "MYGGBETT door/window sensor")["targets"]["de"] == "Fensterkontakt"
    assert rules.find("name", "Tür", "matter", "de", "Eve Door 20EBN9901")["targets"]["de"] == "Kontakt"
    assert rules.find("name", "Tür", "miele", "de", "Fridge freezer")["targets"]["de"] == "Zustand"
    assert len(rules.rules) == 3


def test_model_rule_is_matched_regardless_of_spelling(tmp_path):
    rules = _rules(tmp_path, legacy={})
    rules.upsert("name", "Tür", "matter", "de", "Fensterkontakt", model="MYGGBETT door/window sensor")

    assert rules.find("name", "Tür", "matter", "de", "myggbett door/window SENSOR") is not None


def test_resolution_uses_the_device_model(restructurer):
    rules = restructurer.type_mappings.rules
    restructurer.devices["d"]["model"] = "Eve Door 20EBN9901"
    restructurer.entities["number.x_effect_speed"]["original_name"] = "Tür"
    rules.upsert("name", "Tür", "mqtt", "de", "Kontakt", model="Eve Door 20EBN9901")

    restructurer.build_naming_context("number.x_effect_speed", restructurer.entities["number.x_effect_speed"])
    resolution = restructurer.last_resolutions["number.x_effect_speed"]

    assert resolution["value"] == "Kontakt"
    assert resolution["matched_on"]["model"] == "Eve Door 20EBN9901"


class _FakeHaTranslations:
    """Stands in for Home Assistant's own names."""

    def __init__(self, component=None, entity=None):
        self.component = component or {}
        self.entity = entity or {}

    def device_class_name(self, domain, device_class, language):
        return HaTranslations._usable(self.component.get((domain, device_class, language)))

    def translation_key_name(self, platform, domain, key, language):
        return HaTranslations._usable(self.entity.get((platform, domain, key, language)))


def test_home_assistant_names_a_device_class_in_its_own_language(restructurer):
    """A user whose Home Assistant speaks Italian needs no rule for a door sensor."""
    restructurer.ha_translations = _FakeHaTranslations({("number", "door", "de"): "Tür"})
    entity = restructurer.entities["number.x_effect_speed"]
    entity["original_name"] = "Door"
    entity["device_class"] = "door"

    restructurer.build_naming_context("number.x_effect_speed", entity)
    resolution = restructurer.last_resolutions["number.x_effect_speed"]

    assert resolution["value"] == "Tür"
    assert resolution["won_by"] == "rule:system"


def test_a_translation_key_beats_the_device_class(restructurer):
    restructurer.ha_translations = _FakeHaTranslations(
        {("number", "door", "de"): "Tür"},
        {("mqtt", "number", "reactive_current", "de"): "Blindstrom"},
    )
    entity = restructurer.entities["number.x_effect_speed"]
    entity["original_name"] = "Reactive current"
    entity["device_class"] = "door"
    entity["translation_key"] = "reactive_current"

    restructurer.build_naming_context("number.x_effect_speed", entity)

    assert restructurer.last_resolutions["number.x_effect_speed"]["value"] == "Blindstrom"


def test_a_user_rule_still_beats_home_assistant(restructurer):
    restructurer.ha_translations = _FakeHaTranslations({("number", "door", "de"): "Tür"})
    restructurer.type_mappings.rules.upsert("name", "Door", None, "de", "Zustand")
    entity = restructurer.entities["number.x_effect_speed"]
    entity["original_name"] = "Door"
    entity["device_class"] = "door"

    restructurer.build_naming_context("number.x_effect_speed", entity)
    resolution = restructurer.last_resolutions["number.x_effect_speed"]

    assert resolution["value"] == "Zustand"
    assert resolution["won_by"] == "rule:user"


def test_a_name_with_a_placeholder_is_not_used(restructurer):
    """Home Assistant fills "Warnung {slot_id}" itself; the original is the better name."""
    restructurer.ha_translations = _FakeHaTranslations({("number", "problem", "de"): "Warnung {slot_id}"})
    entity = restructurer.entities["number.x_effect_speed"]
    entity["original_name"] = "Warnung 1"
    entity["device_class"] = "problem"

    restructurer.build_naming_context("number.x_effect_speed", entity)
    resolution = restructurer.last_resolutions["number.x_effect_speed"]

    assert resolution["value"] == "Warnung 1"
    assert resolution["won_by"] == "original"
