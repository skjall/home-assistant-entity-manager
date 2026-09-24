"""A rule can leave the number in a supplied name open.

Some integrations write a serial number into every name: "Heating 12345678",
"Heating 12345679". Each device supplies a name of its own, so a rule on the
exact name reaches one entity, and the next device arrives to be corrected
again. A pattern rule matches every name that differs only in its numbers and
carries the number on into the new name.
"""

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_rules import NamingRuleError, NamingRules, compile_pattern, pattern_of, readable_pattern, target_of
from naming_templates import NamingTemplates
import routes_naming
from type_mappings import DEFAULT_SYSTEM_MAPPINGS, TypeMappings
import web_ui

INTEGRATION = "example_meters"


@pytest.fixture
def rules(tmp_path):
    return NamingRules(str(tmp_path / "rules.json"), default_language="de")


def _pattern_rule(rules, target="Heizkostenverteiler {1}", integration=INTEGRATION):
    regex, _ = pattern_of("Heizung 12345678")
    return rules.add_filter("pattern", regex, "de", target, {"integration": integration})


# ------------------------------------------------------------------ learning


def test_a_name_with_a_number_makes_a_pattern():
    regex, numbers = pattern_of("Heizung 12345678")

    assert numbers == ["12345678"]
    assert readable_pattern(regex) == "Heizung {1}"


def test_a_name_without_a_number_makes_none():
    assert pattern_of("Heizung Gesamt") is None
    assert pattern_of("") is None


def test_every_number_is_left_open():
    regex, numbers = pattern_of("Kanal 2 Zähler 10.5")

    assert numbers == ["2", "10", "5"]
    assert readable_pattern(regex) == "Kanal {1} Zähler {2}.{3}"


def test_the_typed_name_carries_the_number_where_it_repeats_it():
    assert target_of("Heizkostenverteiler 12345678", ["12345678"]) == "Heizkostenverteiler {1}"


def test_a_typed_name_without_the_number_keeps_none():
    assert target_of("Heizkostenverteiler", ["12345678"]) == "Heizkostenverteiler"


def test_a_short_number_is_not_taken_out_of_a_longer_one():
    assert target_of("Kanal 2 Zähler 12", ["2", "12"]) == "Kanal {1} Zähler {2}"


def test_a_hand_written_expression_reads_as_written():
    assert readable_pattern(r"Heizung (\d+)") == r"Heizung (\d+)"


# ------------------------------------------------------------------ matching


def test_the_pattern_answers_for_every_number(rules):
    rule = _pattern_rule(rules)

    for name in ("Heizung 12345678", "Heizung 0042"):
        found = rules.find("pattern", name, INTEGRATION, "de")
        assert found["id"] == rule["id"]
    assert rules.render(rule, "Heizung 0042", "de") == "Heizkostenverteiler 0042"


def test_a_name_that_only_starts_the_same_is_not_caught(rules):
    _pattern_rule(rules)

    assert rules.find("pattern", "Heizung Gesamt", INTEGRATION, "de") is None
    assert rules.find("pattern", "Heizung 12 Gesamt", INTEGRATION, "de") is None


def test_another_integration_is_not_caught(rules):
    _pattern_rule(rules)

    assert rules.find("pattern", "Heizung 12345678", "other", "de") is None


def test_a_pattern_needs_an_integration(rules):
    regex, _ = pattern_of("Heizung 12345678")

    with pytest.raises(NamingRuleError):
        rules.add_filter("pattern", regex, "de", "Heizkostenverteiler {1}", None)
    with pytest.raises(NamingRuleError):
        rules.add_filter("pattern", regex, "de", "Heizkostenverteiler {1}", {"model": "Meter"})


def test_a_placeholder_the_pattern_does_not_capture_is_refused(rules):
    with pytest.raises(NamingRuleError):
        _pattern_rule(rules, target="Heizkostenverteiler {2}")


def test_an_expression_that_does_not_compile_is_refused(rules):
    with pytest.raises(NamingRuleError):
        rules.add_filter("pattern", "Heizung (", "de", "Heizkostenverteiler", {"integration": INTEGRATION})


def test_a_named_group_can_be_carried(rules):
    rule = rules.add_filter(
        "pattern", r"Zone (?P<zone>\w+) Temperatur", "de", "Temperatur {zone}", {"integration": INTEGRATION}
    )

    assert rules.render(rule, "Zone Nord Temperatur", "de") == "Temperatur Nord"


def test_two_patterns_at_the_same_reach_are_not_decided_silently(rules):
    _pattern_rule(rules)
    rules.add_filter("pattern", r"Heizung \d+", "de", "Heizung", {"integration": INTEGRATION})

    assert rules.find("pattern", "Heizung 12345678", INTEGRATION, "de") is None


def test_the_narrower_pattern_wins(rules):
    _pattern_rule(rules)
    narrow = rules.add_filter(
        "pattern", r"Heizung \d+", "de", "Heizung", {"integration": INTEGRATION, "model": "Meter"}
    )

    assert rules.find("pattern", "Heizung 12345678", INTEGRATION, "de", "Meter")["id"] == narrow["id"]


def test_a_pattern_rule_is_never_redundant(rules):
    rule = _pattern_rule(rules, target="Heizung {1}")

    assert rules.is_redundant(rule, "de") is False


def test_why_reads_the_pattern(rules):
    rule = _pattern_rule(rules)

    assert rules.why(rule, INTEGRATION)["label"] == "Heizung {1}"
    assert "label" not in rules.why(rules.upsert("name", "Tür", None, "de", "Kontakt"))


def test_the_setting_is_off_until_switched_on(rules, tmp_path):
    assert rules.pattern_rules is False

    rules.set_pattern_rules(True)

    assert NamingRules(str(tmp_path / "rules.json")).pattern_rules is True


# ------------------------------------------------------------ in a real home


@pytest.fixture
def client(tmp_path, monkeypatch):
    rules = NamingRules(
        str(tmp_path / "naming_rules.json"),
        device_class_keys=DEFAULT_SYSTEM_MAPPINGS["device_class"].keys(),
        default_language="de",
    )
    mappings = TypeMappings(user_mappings_path=str(tmp_path / "unused.json"), rules=rules)
    overrides = NamingOverrides(str(tmp_path / "overrides.json"))
    restructurer = EntityRestructurer(
        client=object(),
        naming_overrides=overrides,
        type_mappings=mappings,
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
    )
    restructurer.floors = {}
    restructurer.areas = {"b": {"area_id": "b", "name": "Bad"}}
    restructurer.devices = {"m": {"id": "m", "name": "Zähler", "area_id": "b"}}
    restructurer.entities = {}
    for registry_id, name in (
        ("reg-1", "Heizung 12345678"),
        ("reg-2", "Heizung 12345679"),
        ("reg-3", "Heizung Gesamt"),
    ):
        entity_id = f"sensor.{registry_id}"
        restructurer.entities[entity_id] = {
            "id": registry_id,
            "entity_id": entity_id,
            "device_id": "m",
            "platform": INTEGRATION,
            "original_name": name,
            "has_entity_name": True,
        }
    monkeypatch.setitem(web_ui.renamer_state, "naming_rules", rules)
    monkeypatch.setitem(web_ui.renamer_state, "type_mappings", mappings)
    monkeypatch.setitem(web_ui.renamer_state, "naming_overrides", overrides)
    monkeypatch.setitem(web_ui.renamer_state, "restructurer", restructurer)
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client()


def _resolved(entity_id):
    restructurer = web_ui.renamer_state["restructurer"]
    restructurer.build_naming_context(entity_id, restructurer.entities[entity_id])
    return restructurer.last_resolutions[entity_id]


def test_learning_a_pattern_renames_every_numbered_name(client):
    response = client.post(
        "/api/naming/learn",
        json={"entity_id": "sensor.reg-1", "value": "Heizkostenverteiler 12345678", "scope": "pattern"},
    )

    assert response.status_code == 200
    rule = response.get_json()["rule"]
    assert rule["match"]["kind"] == "pattern"
    assert rule["label"] == "Heizung {1}"
    assert rule["targets"]["de"] == "Heizkostenverteiler {1}"
    assert rule["filters"] == [{"integration": INTEGRATION}]
    assert rule["affected"] == 2
    assert _resolved("sensor.reg-2")["value"] == "Heizkostenverteiler 12345679"
    assert _resolved("sensor.reg-2")["matched_on"]["label"] == "Heizung {1}"
    assert _resolved("sensor.reg-3")["value"] == "Heizung Gesamt"


def test_an_exact_rule_still_wins_over_the_pattern(client):
    client.post(
        "/api/naming/learn",
        json={"entity_id": "sensor.reg-1", "value": "Heizkostenverteiler 12345678", "scope": "pattern"},
    )
    client.post("/api/naming/learn", json={"entity_id": "sensor.reg-2", "value": "Handtuchheizkörper"})

    assert _resolved("sensor.reg-2")["value"] == "Handtuchheizkörper"
    assert _resolved("sensor.reg-1")["value"] == "Heizkostenverteiler 12345678"


def test_a_name_without_a_number_cannot_be_learned_as_a_pattern(client):
    response = client.post(
        "/api/naming/learn", json={"entity_id": "sensor.reg-3", "value": "Heizung", "scope": "pattern"}
    )

    assert response.status_code == 400


def test_the_scope_is_offered_only_where_it_reaches_more_than_one(client):
    restructurer = web_ui.renamer_state["restructurer"]
    counts = routes_naming.type_pattern_counts(restructurer)

    offered = routes_naming.type_pattern_of(restructurer.entities["sensor.reg-1"], counts)

    assert offered == {"label": "Heizung {1}", "numbers": ["12345678"], "count": 2}
    assert routes_naming.type_pattern_of(restructurer.entities["sensor.reg-3"], counts) is None


def test_the_setting_is_read_and_written(client):
    assert client.get("/api/naming/settings").get_json()["pattern_rules"] is False

    response = client.put("/api/naming/settings", json={"pattern_rules": True})

    assert response.get_json()["pattern_rules"] is True


def test_an_expression_that_repeats_what_repeats_is_refused():
    """ "(a+)+" takes exponentially long on a name that nearly matches.

    Every entity of the integration is matched against the pattern on every
    resolution, so one such expression would stop the add-on answering at all.
    """
    for expression in [r"(a+)+b", r"(a*)*", r"(\d+){2,}", r"((a+))+"]:
        with pytest.raises(NamingRuleError):
            compile_pattern(expression)


def test_the_expressions_this_add_on_writes_are_accepted():
    """A learned pattern quantifies the digits it left open and nothing else."""
    for expression in [r"Heizung\ (?P<n1>\d+)", r"(ab)?c", r"a+b+", r"(?P<n1>[0-9]{1,4})"]:
        assert compile_pattern(expression) is not None


def test_a_number_that_appears_twice_gets_two_placeholders():
    """ "Zone 10 Panel 10" holds the same number twice, and they move apart."""
    assert target_of("Zone 10 Panel 10", ["10", "10"]) == "Zone {1} Panel {2}"


def test_a_repetition_inside_a_class_is_a_character():
    """ "([a+])+" repeats a class of two characters, which finishes in time."""
    assert compile_pattern(r"([a+])+b") is not None


def test_a_group_that_may_match_nothing_may_not_repeat():
    """ "(Sensor ?)+" backtracks as badly as "(a+)+" and was let through."""
    for expression in [r"(Sensor ?)+\d+", r"(a?)+b", r"(ab?)*c"]:
        with pytest.raises(NamingRuleError):
            compile_pattern(expression)


def test_a_placeholder_with_nothing_in_it_leaves_the_name_alone(rules):
    """The target used to come out as the text around a hole."""
    rule = rules.add_filter("pattern", r"Zone\ (?P<n1>\d*)", "de", "Zimmer {1}", {"integration": INTEGRATION})

    assert rules.render(rule, "Zone 4", "de") == "Zimmer 4"
    # No number in the name, so the rule has nothing to put in the target.
    assert rules.render(rule, "Zone ", "de") == "Zimmer {1}"
