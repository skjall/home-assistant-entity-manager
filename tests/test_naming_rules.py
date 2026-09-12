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

    def domain_name(self, domain, language):
        return self.device_class_name(domain, "_", language)


def test_a_name_that_says_more_than_its_class_is_kept(restructurer):
    """ "CPU temperature" and "Temperature" sit on one device and must stay apart."""
    restructurer.ha_translations = _FakeHaTranslations({("number", "temperature", "de"): "Temperatur"})
    entity = restructurer.entities["number.x_effect_speed"]
    entity["original_name"] = "CPU Temperatur"
    entity["device_class"] = "temperature"

    restructurer.build_naming_context("number.x_effect_speed", entity)
    resolution = restructurer.last_resolutions["number.x_effect_speed"]

    assert resolution["value"] == "CPU Temperatur"
    assert resolution["won_by"] == "original"


def test_the_english_name_of_a_class_is_translated(restructurer):
    """A class whose English name differs from its key is still recognised."""
    restructurer.ha_translations = _FakeHaTranslations(
        {
            ("number", "signal_strength", "de"): "Signalstärke",
            ("number", "signal_strength", "en"): "Signal strength",
        }
    )
    entity = restructurer.entities["number.x_effect_speed"]
    entity["original_name"] = "Signal strength"
    entity["device_class"] = "signal_strength"

    restructurer.build_naming_context("number.x_effect_speed", entity)

    assert restructurer.last_resolutions["number.x_effect_speed"]["value"] == "Signalstärke"


def test_a_class_fills_in_where_no_name_is_supplied(restructurer):
    restructurer.ha_translations = _FakeHaTranslations({("cover", "shutter", "de"): "Rollladen"})

    supplied = restructurer._home_assistant_name("cover.x", {"device_class": "shutter"}, "de", "")

    assert supplied == "Rollladen"


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


def test_a_name_spells_out_the_domain_but_an_id_does_not(restructurer):
    """ "button" is a word in a name and a slug in an entity ID."""
    restructurer.ha_translations = _FakeHaTranslations({("number", "_", "de"): "Nummer"})
    restructurer.naming_templates.set_templates(
        {
            "device_name": "{area} {device}",
            "entity_name": "{area} {device} {entity} {domain}",
            "entity_id": "{area} {device} {entity} {domain}",
        }
    )
    entity = restructurer.entities["number.x_effect_speed"]

    new_id, new_name = restructurer.generate_new_entity_id("number.x_effect_speed", entity)

    assert new_name.endswith("Nummer")
    assert new_id.endswith("_number")


def test_a_device_class_becomes_its_home_assistant_word(restructurer):
    restructurer.ha_translations = _FakeHaTranslations({("number", "reactive_power", "de"): "Blindleistung"})
    restructurer.naming_templates.set_templates(
        {
            "device_name": "{device}",
            "entity_name": "{entity} {device_class}",
            "entity_id": "{entity} {device_class}",
        }
    )
    entity = restructurer.entities["number.x_effect_speed"]
    entity["device_class"] = "reactive_power"

    new_id, new_name = restructurer.generate_new_entity_id("number.x_effect_speed", entity)

    assert new_name.endswith("Blindleistung")
    assert new_id.endswith("_reactive_power")


def test_an_unknown_technical_value_is_read_as_words(restructurer):
    restructurer.ha_translations = None

    assert restructurer.spell_out("device_class", "power_factor", "sensor.x") == "Power factor"


def test_the_floor_level_is_available_as_a_placeholder(restructurer):
    restructurer.floors = {"f": {"floor_id": "f", "name": "2. Obergeschoss", "level": 2}}
    restructurer.areas = {"k": {"area_id": "k", "name": "Küche", "floor_id": "f"}}

    context = restructurer.build_naming_context("number.x_effect_speed", restructurer.entities["number.x_effect_speed"])

    assert context["floor_level"] == "2"


def test_repair_moves_a_rule_whose_value_is_a_device_class(tmp_path):
    """A rule filed under the name can never match a device class."""
    store = NamingRules(str(tmp_path / "rules.json"), device_class_keys=["battery", "humidity"])
    rule = store.upsert("name", "battery", None, "de", "Akku")

    report = store.repair_kinds(translation_keys=set(), names={"Firmware"})

    assert [item["kind"] for item in report["moved"]] == ["device_class"]
    assert store.get(rule["id"])["match"]["kind"] == "device_class"
    assert store.find("device_class", "battery", None, "de")["targets"]["de"] == "Akku"


def test_repair_moves_a_rule_whose_value_is_a_translation_key(tmp_path):
    store = NamingRules(str(tmp_path / "rules.json"), device_class_keys=["battery"])
    rule = store.upsert("name", "cpu_temperature", None, "de", "Prozessortemperatur")

    store.repair_kinds(translation_keys={"cpu_temperature"}, names={"Firmware"})

    assert store.get(rule["id"])["match"]["kind"] == "translation_key"


def test_repair_leaves_a_rule_that_matches_a_real_name(tmp_path):
    store = NamingRules(str(tmp_path / "rules.json"), device_class_keys=["battery"])
    rule = store.upsert("name", "Battery", None, "de", "Akku")

    store.repair_kinds(translation_keys=set(), names={"Battery"})

    assert store.get(rule["id"])["match"]["kind"] == "name"


def test_repair_runs_only_once(tmp_path):
    store = NamingRules(str(tmp_path / "rules.json"), device_class_keys=["battery"])
    store.upsert("name", "battery", None, "de", "Akku")

    first = store.repair_kinds(translation_keys=set(), names=set())
    second = store.repair_kinds(translation_keys=set(), names=set())

    assert len(first["moved"]) == 1
    assert second is None


def test_delete_many_removes_what_it_is_given(tmp_path):
    store = NamingRules(str(tmp_path / "rules.json"))
    first = store.upsert("name", "one", None, "de", "Eins")
    second = store.upsert("name", "two", None, "de", "Zwei")

    removed = store.delete_many([first["id"], "nothing"])

    assert removed == 1
    assert [rule["id"] for rule in store.rules] == [second["id"]]


class _HaNames:
    """Stands in for Home Assistant's translations, with two classes it knows."""

    KNOWN = {"power_factor": "Leistungsfaktor", "battery": "Akkustand"}

    def name_for_key(self, key, language):
        return self.KNOWN.get(key)

    def device_classes(self, language=None):
        return set(self.KNOWN)


def test_home_assistant_names_a_class_the_embedded_table_misses(tmp_path):
    """The embedded table has no power factor; Home Assistant does."""
    mappings = TypeMappings(user_mappings_path=str(tmp_path / "user.json"), ha_translations=_HaNames())

    assert mappings.find_system_translation("power_factor", "de") == "Leistungsfaktor"


def test_home_assistant_wins_over_the_embedded_table(tmp_path):
    """Where both know a class, the answer comes from Home Assistant."""
    mappings = TypeMappings(user_mappings_path=str(tmp_path / "user.json"), ha_translations=_HaNames())

    assert mappings.find_system_translation("battery", "de") == "Akkustand"
    assert mappings.find_translation("battery", "de") == "Akkustand"


def test_the_embedded_table_still_answers_without_home_assistant(tmp_path):
    mappings = TypeMappings(user_mappings_path=str(tmp_path / "user.json"))

    assert mappings.find_system_translation("battery", "de") == "Batterie"


def test_known_types_list_what_home_assistant_knows(tmp_path):
    mappings = TypeMappings(user_mappings_path=str(tmp_path / "user.json"), ha_translations=_HaNames())

    entries = {entry["key"]: entry["system_default"] for entry in mappings.get_all_known_types("de")}

    assert entries["power_factor"] == "Leistungsfaktor"
    assert entries["battery"] == "Akkustand"


@pytest.mark.parametrize(
    "value, identifier",
    [
        ("WAP-001-230.h01.lh.lan", True),
        ("10.2.10.103", True),
        ("3c:22:fb:01:02:03", True),
        ("PM2.5", False),
        ("CPU Temperatur", False),
        ("Version 2.1", False),
        ("Temperatur", False),
    ],
)
def test_an_identifier_is_told_from_a_name(value, identifier):
    assert EntityRestructurer.is_identifier(value) is identifier


def test_a_supplied_host_name_is_not_used_as_a_name(restructurer):
    """UniFi names its trackers after the host; that is the machine, not the entity."""
    entity = restructurer.entities["number.x_effect_speed"]
    entity["original_name"] = "WAP-001-230.h01.lh.lan"

    restructurer.build_naming_context("number.x_effect_speed", entity)
    resolution = restructurer.last_resolutions["number.x_effect_speed"]

    assert "lh.lan" not in resolution["value"]
    assert resolution["won_by"] in ("device_class", "fallback")
