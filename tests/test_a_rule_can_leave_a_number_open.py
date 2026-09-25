"""A rule can leave the number in a supplied name open.

Some integrations write a serial number into every name: "Heating 12345678",
"Heating 12345679". Each device supplies a name of its own, so a rule on the
exact name reaches one entity, and the next device arrives to be corrected
again. A pattern rule matches every name that differs only in its numbers and
carries the number on into the new name.
"""

import os

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_rules import (
    NamingRuleError,
    NamingRules,
    UnknownRuleError,
    compile_pattern,
    pattern_of,
    readable_pattern,
    target_of,
)
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


# ------------------------------------------------------------------- editing


def test_the_expression_of_a_pattern_rule_can_be_changed(rules):
    """A pattern is written to be adjusted: the first one rarely fits exactly."""
    rule = _pattern_rule(rules)

    changed = rules.update(rule["id"], value=r"Heizkosten\ (?P<n1>\d+)")

    assert changed["match"]["value"] == r"Heizkosten\ (?P<n1>\d+)"
    assert readable_pattern(changed["match"]["value"]) == "Heizkosten {1}"


def test_an_expression_that_cannot_be_read_is_refused(rules):
    rule = _pattern_rule(rules)

    with pytest.raises(NamingRuleError):
        rules.update(rule["id"], value="Heizung (?P<n1>")

    assert rules.get(rule["id"])["match"]["value"] == pattern_of("Heizung 12345678")[0]


def test_an_expression_losing_a_placeholder_the_target_uses_is_refused(rules):
    """The name would come out with a hole where the number belongs."""
    rule = _pattern_rule(rules)

    with pytest.raises(NamingRuleError):
        rules.update(rule["id"], value=r"Heizung\ \d+")

    assert rules.get(rule["id"])["targets"]["de"] == "Heizkostenverteiler {1}"


def test_a_new_expression_and_a_new_target_are_judged_together(rules):
    """The target that goes with the new expression, not the one before it."""
    rule = _pattern_rule(rules)

    changed = rules.update(
        rule["id"],
        value=r"Heizung\ (?P<meter>\d+)",
        targets={"de": "Heizkostenverteiler {meter}"},
    )

    assert changed["targets"]["de"] == "Heizkostenverteiler {meter}"


def test_an_expression_another_rule_already_claims_is_refused(rules):
    first = _pattern_rule(rules)
    second = rules.add_filter("pattern", r"Wasser\ (?P<n1>\d+)", "de", "Wasserzähler {1}", {"integration": INTEGRATION})

    with pytest.raises(NamingRuleError):
        rules.update(second["id"], value=first["match"]["value"])


def test_only_a_pattern_rule_is_matched_on_an_expression(rules):
    """A name rule is matched on the word an integration supplies, not one the user wrote."""
    rule = rules.add_filter("name", "Heizung", "de", "Heizkostenverteiler", {"integration": INTEGRATION})

    with pytest.raises(NamingRuleError):
        rules.update(rule["id"], value=r"Heizung\ (?P<n1>\d+)")


def test_the_endpoint_carries_the_new_expression(client):
    rules = web_ui.renamer_state["naming_rules"]
    rule = _pattern_rule(rules)

    response = client.put(
        f"/api/naming/rules/{rule['id']}",
        json={"targets": {"de": "Heizkostenverteiler {1}"}, "match_value": r"Heizkosten\ (?P<n1>\d+)"},
    )

    assert response.status_code == 200
    assert response.get_json()["rule"]["label"] == "Heizkosten {1}"


def test_the_endpoint_says_why_an_expression_is_refused(client):
    rules = web_ui.renamer_state["naming_rules"]
    rule = _pattern_rule(rules)

    response = client.put(
        f"/api/naming/rules/{rule['id']}",
        json={"targets": {"de": "Heizkostenverteiler {1}"}, "match_value": "Heizung (?P<n1>"},
    )

    assert response.status_code == 400
    assert "pattern" in response.get_json()["error"].lower()


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


def test_an_update_that_asks_for_nothing_changes_nothing(rules):
    """It used to stamp the rule as changed and write the file for it."""
    rule = _pattern_rule(rules)
    before = dict(rule)

    unchanged = rules.update(rule["id"])

    assert unchanged == before


def test_the_endpoint_refuses_a_body_that_asks_for_nothing(client):
    rules = web_ui.renamer_state["naming_rules"]
    rule = _pattern_rule(rules)

    response = client.put(f"/api/naming/rules/{rule['id']}", json={})

    assert response.status_code == 400


def test_a_number_that_appears_twice_gets_two_placeholders():
    """ "Zone 10 Panel 10" holds the same number twice, and they move apart."""
    assert target_of("Zone 10 Panel 10", ["10", "10"]) == "Zone {1} Panel {2}"


def test_a_repetition_inside_a_class_is_a_character():
    """ "([a+])+" repeats a class of two characters, which finishes in time."""
    assert compile_pattern(r"([a+])+b") is not None


def test_a_target_in_another_language_survives_an_edit(rules):
    """The writer edits the language in front of it and says nothing about the rest."""
    rule = _pattern_rule(rules)
    rules.update(rule["id"], targets={"en": "Heat meter {1}"})

    changed = rules.update(rule["id"], targets={"de": "Heizkosten {1}"})

    assert changed["targets"] == {"de": "Heizkosten {1}", "en": "Heat meter {1}"}


def test_a_target_is_refused_when_the_expression_captures_nothing_for_it(rules):
    """The check used to be skipped where only the targets were supplied."""
    rule = _pattern_rule(rules)

    with pytest.raises(NamingRuleError):
        rules.update(rule["id"], targets={"de": "Heizkosten {2}"})


def test_the_endpoint_refuses_targets_that_are_not_a_mapping(client):
    """A list where a mapping belongs used to come back as a 500."""
    rules = web_ui.renamer_state["naming_rules"]
    rule = _pattern_rule(rules)

    response = client.put(f"/api/naming/rules/{rule['id']}", json={"targets": ["Heizung"]})

    assert response.status_code == 400


def test_the_endpoint_refuses_an_expression_of_whitespace(client):
    """It reached compile_pattern and failed about the pattern, not the field.

    Sent alongside targets it was dropped without a word and the targets were
    written, so the field is answered for rather than passed on empty.
    """
    rules = web_ui.renamer_state["naming_rules"]
    rule = _pattern_rule(rules)

    for body in ({"match_value": "   "}, {"targets": {"de": "Heizkosten {1}"}, "match_value": "   "}):
        response = client.put(f"/api/naming/rules/{rule['id']}", json=body)

        assert response.status_code == 400
        assert "expression" in response.get_json()["error"]


def test_the_endpoint_refuses_an_expression_over_the_limit_rather_than_cutting_it(client):
    """It used to be cut to 500 characters and stored with a 200."""
    rules = web_ui.renamer_state["naming_rules"]
    rule = _pattern_rule(rules)
    too_long = "a" * 501

    response = client.put(f"/api/naming/rules/{rule['id']}", json={"match_value": too_long})

    assert response.status_code == 400
    assert "500" in response.get_json()["error"]
    assert rules.get(rule["id"])["match"]["value"] == rule["match"]["value"]


def test_a_group_that_may_match_nothing_may_not_repeat():
    """ "(Sensor ?)+" backtracks as badly as "(a+)+" and was let through."""
    for expression in [r"(Sensor ?)+\d+", r"(a?)+b", r"(ab?)*c"]:
        with pytest.raises(NamingRuleError):
            compile_pattern(expression)


def test_a_counted_repetition_is_not_a_runaway():
    """ "(\\d{4})+" bounds what it repeats, so it finishes; "(\\d{2,})+" does not."""
    assert compile_pattern(r"(\d{4})+") is not None
    assert compile_pattern(r"(?:ab{1,3})+c") is not None
    assert compile_pattern(r"(\d{4}){2,3}") is not None
    with pytest.raises(NamingRuleError):
        compile_pattern(r"(\d{2,})+")


def test_a_bounded_count_on_a_group_that_repeats_is_a_runaway():
    """A count bounds how often the group is tried, not how many ways there are
    to read what is inside it: "([\\w ]+){2,5}" can split a hundred characters
    across five groups every way there is, and tries all of them on a name that
    nearly matches. The bound was read as making any group safe."""
    for expression in (r"([\w ]+){2,5}", r"(a+){2,3}"):
        with pytest.raises(NamingRuleError):
            compile_pattern(expression)


def test_a_bounded_count_over_a_choice_is_bounded():
    """A choice counted a fixed number of times can be read a fixed number of
    ways, overlapping alternatives and all: "(open|closed){1,2}" is a name with
    one of two words in it twice, and refusing it said it would never finish."""
    assert compile_pattern(r"(open|closed){1,2}") is not None
    assert compile_pattern(r"(a|aa){2,3}") is not None
    # Unbounded is the one that does not finish.
    with pytest.raises(NamingRuleError):
        compile_pattern(r"(a|aa)+")


def test_a_placeholder_with_nothing_in_it_leaves_the_name_alone(rules):
    """The target used to come out as the text around a hole."""
    rule = rules.add_filter("pattern", r"Zone\ (?P<n1>\d*)", "de", "Zimmer {1}", {"integration": INTEGRATION})

    assert rules.render(rule, "Zone 4", "de") == "Zimmer 4"
    # No number in the name, so the rule has nothing to put in the target and
    # says nothing rather than its own template.
    assert rules.render(rule, "Zone ", "de") == ""
    assert rules.find("pattern", "Zone ", INTEGRATION, "de") is None


def test_a_refused_target_is_not_left_standing_in_the_rule(rules):
    """It was written first and refused afterwards, and the next save put it on disk."""
    rule = _pattern_rule(rules)
    before = dict(rule["targets"])

    with pytest.raises(NamingRuleError):
        rules.upsert("pattern", rule["match"]["value"], INTEGRATION, "de", "Heizkosten {2}")

    assert rules.get(rule["id"])["targets"] == before


def test_an_optional_group_inside_a_repeated_one_is_refused():
    """ "((ab)?)+" repeats a group that can match nothing, and that is the runaway shape."""
    with pytest.raises(NamingRuleError):
        compile_pattern(r"((ab)?)+X")


def test_the_endpoint_says_a_mapping_of_nothing_is_not_a_target(client):
    """Passed on it came back as "a rule needs at least one target".

    Which reads as though the rule had lost the targets it has, where what
    happened is that the mapping sent held no language with text in it.
    """
    rules = web_ui.renamer_state["naming_rules"]
    rule = _pattern_rule(rules)

    for targets in ({}, {"de": "   "}, {"de": None}):
        response = client.put(f"/api/naming/rules/{rule['id']}", json={"targets": targets})

        assert response.status_code == 400
        assert "language" in response.get_json()["error"]


def test_an_expression_of_nothing_is_refused_for_being_nothing():
    """The length limit is a different mistake and used to be named for both."""
    with pytest.raises(NamingRuleError) as refused:
        compile_pattern("")

    assert "at most" not in str(refused.value)

    with pytest.raises(NamingRuleError) as too_long:
        compile_pattern("a" * 501)

    assert "at most" in str(too_long.value)


def test_a_refused_target_is_not_left_standing_when_the_rule_is_edited(rules):
    """update() wrote the targets and judged them afterwards."""
    rule = _pattern_rule(rules)
    before = dict(rule["targets"])

    with pytest.raises(NamingRuleError):
        rules.update(rule["id"], targets={"de": "Heizkosten {2}"})

    assert rules.get(rule["id"])["targets"] == before


def test_a_refused_target_is_not_left_standing_when_the_rule_is_reworded(rules):
    """Rewording the rule that holds the place skipped the judgement entirely."""
    rule = _pattern_rule(rules)
    before = dict(rule["targets"])

    with pytest.raises(NamingRuleError):
        rules.add_filter("pattern", rule["match"]["value"], "de", "Heizkosten {2}", {"integration": INTEGRATION})

    assert rules.get(rule["id"])["targets"] == before


def test_a_tie_is_settled_by_the_rule_that_reaches_less_far(rules):
    """Two patterns contradicting each other said nothing about the wider rule.

    The user wrote that wider rule too, and nothing contradicts it, so it is
    what the entity is called - it used to come out with no name at all.
    """
    wide = _pattern_rule(rules, target="Weit {1}")
    for target in ("Eng {1}", "Auch eng {1}"):
        regex, _ = pattern_of("Heizung 12345678")
        rules.add_filter(
            "pattern", regex + f"(?#{target})", "de", target, {"integration": INTEGRATION, "model": "Meter"}
        )

    found = rules.find("pattern", "Heizung 12345678", INTEGRATION, "de", "Meter")

    assert found["id"] == wide["id"]


def test_a_backreference_is_not_read_as_a_quantifier():
    """ "(?P=n1)" repeats nothing by itself; the pattern around it decides."""
    assert compile_pattern(r"(?P<n1>\d)-(?P=n1)") is not None
    # The digits inside the repeated group are the runaway shape, backreference
    # or not.
    with pytest.raises(NamingRuleError):
        compile_pattern(r"((?P<n1>\d+)-(?P=n1))+")


def test_a_choice_inside_a_repeated_group_is_refused():
    """ "(a|aa)+" reads one stretch of text in as many ways as it can be cut up.

    It tries all of them on a name that nearly matches, which is the same
    runaway as "(a+)+" and was let through.
    """
    for expression in [r"(a|aa)+", r"((a|aa))+", r"(He|Heating)*X"]:
        with pytest.raises(NamingRuleError):
            compile_pattern(expression)


def test_a_choice_that_is_not_repeated_is_accepted():
    """A choice is only a runaway where something repeats it."""
    for expression in [r"(a|b)", r"(a|b)?c", r"(?:Heizung|Kuehlung) (?P<n1>\d+)"]:
        assert compile_pattern(expression) is not None


# --------------------------------------- what an edit to a rule is judged on


def test_a_target_edit_is_not_refused_over_another_language(rules):
    """An edit says what one language reads and nothing about the others.

    A rule can hold a target in a language whose placeholders the expression has
    no group for - written before the expression was corrected, or put there by
    hand. Judged as a whole rule, that language refused every edit to every
    other one, the edit that would have mended it among them.
    """
    rule = _pattern_rule(rules)
    # Put there past the check on the way in, because the state that has to be
    # editable is not the state that can be created.
    rule["targets"]["en"] = "Heat meter {9}"

    updated = rules.update(rule["id"], targets={"de": "Heizkosten {1}"})

    assert updated["targets"]["de"] == "Heizkosten {1}"
    assert updated["targets"]["en"] == "Heat meter {9}"


def test_a_target_edit_is_still_judged_against_the_expression(rules):
    """The language being written is: a placeholder with no group behind it
    would put a hole in every name the rule makes."""
    rule = _pattern_rule(rules)

    with pytest.raises(NamingRuleError):
        rules.update(rule["id"], targets={"de": "Heizkosten {9}"})


def test_a_new_expression_is_judged_against_every_target(rules):
    """An expression says which entities the rule is about, so the whole rule is
    judged again - a target left without its group is the rule not applying."""
    rule = _pattern_rule(rules, target="Heizkostenverteiler {1}")

    with pytest.raises(NamingRuleError):
        rules.update(rule["id"], value=r"Heizung\ \d+")


def test_an_edit_writes_only_what_it_changes(rules):
    """Assigning all three fields wrote one its own value, and the rollback then
    put back something that had never moved."""
    rule = _pattern_rule(rules)
    match_before = rule["match"]
    filters_before = rule["filters"]

    updated = rules.update(rule["id"], targets={"de": "Heizkosten {1}"})

    assert updated["match"] is match_before
    assert updated["filters"] is filters_before


# ------------------------------------------- what the route says beforehand


def test_the_route_reads_the_expression_as_a_pattern(client):
    """Length is the smaller half of what makes an expression usable. Told that a
    499-character expression was within bounds, the caller then got an answer
    about repeated groups out of the rules as though from somewhere else."""
    rule = client.post(
        "/api/naming/learn",
        json={"entity_id": "sensor.reg-1", "value": "Heizkostenverteiler 12345678", "scope": "pattern"},
    ).get_json()["rule"]

    answer = client.put(f"/api/naming/rules/{rule['id']}", json={"match_value": r"(\d+)+"})

    assert answer.status_code == 400
    assert "repeat" in answer.get_json()["error"]


def test_the_route_refuses_an_expression_for_a_rule_that_has_none(client):
    """Passed on, the whole call was refused by the rules - the targets sent with
    it among them - and nothing said which half of it was the problem."""
    rule = client.post(
        "/api/naming/learn",
        json={"entity_id": "sensor.reg-3", "value": "Heizung gesamt"},
    ).get_json()["rule"]
    assert rule["match"]["kind"] != "pattern"

    answer = client.put(
        f"/api/naming/rules/{rule['id']}",
        json={"targets": {"de": "Heizung gesamt"}, "match_value": r"Heizung\ \d+"},
    )

    assert answer.status_code == 400
    assert "pattern rule" in answer.get_json()["error"]


def test_an_expression_for_a_rule_that_is_not_there_says_so(client):
    answer = client.put("/api/naming/rules/nothing", json={"match_value": r"Heizung\ \d+"})

    assert answer.status_code == 404


def test_a_target_for_a_rule_that_is_not_there_says_so_as_well(client):
    """The same answer whichever field was sent: a caller that named a rule which
    is not there has not sent anything wrong, and 400 said it had."""
    answer = client.put("/api/naming/rules/nothing", json={"targets": {"de": "Heizkosten"}})

    assert answer.status_code == 404


def test_a_rule_deleted_between_the_reading_and_the_write_is_still_a_404(rules):
    """No amount of checking beforehand rules that out, so the write says it in
    its own words."""
    rule = _pattern_rule(rules)
    rules.delete(rule["id"])

    with pytest.raises(UnknownRuleError):
        rules.update(rule["id"], targets={"de": "Heizkosten {1}"})


def test_a_stored_expression_that_cannot_be_read_says_whose_it_is(rules):
    """The caller editing a target did not send that expression and cannot mend
    it from where it is standing; read out as it was, the answer to "this target
    is wrong" was a complaint about something nobody had touched."""
    rule = _pattern_rule(rules)
    rule["match"]["value"] = r"Heizung ("

    with pytest.raises(NamingRuleError) as refused:
        rules.update(rule["id"], targets={"de": "Heizkosten {1}"})

    assert "this rule's own expression" in str(refused.value).lower()


def test_a_refused_save_keeps_what_was_typed():
    """Closed on the answer, an expression the server would not take was gone,
    and the only way back to it was to write it again from memory."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "templates", "settings.html"), encoding="utf-8") as handle:
        markup = handle.read()

    at = markup.index("async saveEdit(row) {")
    body = markup[at : markup.index("async addMapping()", at)]
    assert "alert(" not in body
    assert "} finally {" not in body
    assert "this.editError = error.error || this.t('settings.save_failed');" in body


# ---------------------------------------------- what the page says out loud


def test_an_empty_expression_is_answered_in_the_row():
    """The rest of the page answers where the question was asked. An alert stops
    the reactivity and every answer still on its way until it is dismissed."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "templates", "settings.html"), encoding="utf-8") as handle:
        markup = handle.read()

    at = markup.index("async saveEdit(row) {")
    body = markup[at : at + 4000]
    assert "alert(this.t('settings.pattern_needs_an_expression'))" not in body
    assert "this.editError = this.t('settings.pattern_needs_an_expression');" in body
    assert 'x-text="editError"' in markup


def test_an_expression_is_read_once_for_one_write(rules, monkeypatch):
    """It is read where it arrives, to answer about it, and again where the rules
    judge what the targets ask of it: the runaway walk and the compilation twice
    for one write."""
    import naming_rules

    naming_rules._read_pattern.cache_clear()
    walked = []
    original = naming_rules._refuse_runaway
    monkeypatch.setattr(naming_rules, "_refuse_runaway", lambda regex: walked.append(regex) or original(regex))

    expression = r"Heizung\ (?P<n1>\d+)"
    assert naming_rules.compile_pattern(expression) is naming_rules.compile_pattern(expression)
    assert walked == [expression]


def test_an_expression_that_cannot_be_read_is_refused_every_time(rules):
    """Only what can be read is kept; a refusal is worked out again, which is
    what a refusal costs."""
    import naming_rules

    for _ in range(2):
        with pytest.raises(NamingRuleError):
            naming_rules.compile_pattern(r"Heizung (")


def test_a_rule_missing_a_field_is_answered_with_a_rule(rules):
    """One out of a backup, or built in a test, may be missing a field this
    never wrote - and an edit to its target came back as a KeyError."""
    rule = _pattern_rule(rules)
    rule.pop("filters")

    updated = rules.update(rule["id"], targets={"de": "Heizkosten {1}"})

    assert updated["targets"]["de"] == "Heizkosten {1}"


def test_a_save_that_went_through_closes_the_row():
    """The reading back afterwards is a second request: letting it fall into the
    same catch left the row open saying "not saved" about a rule that was."""
    import os

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "templates", "settings.html"), encoding="utf-8") as handle:
        markup = handle.read()

    at = markup.index("async saveEdit(row) {")
    body = markup[at : markup.index("async addMapping()", at)]
    assert body.index("this.cancelEdit();") < body.index("await this.loadMappings();")


def test_a_broken_expression_is_answered_before_the_filters(rules):
    """Asked about the filters first, a rule sent with both a broken expression
    and no integration was answered about the integration, and the expression
    only on the next try."""
    with pytest.raises(NamingRuleError) as refused:
        rules.add_filter("pattern", "Heizung (", "de", "Heizkosten", None)

    assert "valid pattern" in str(refused.value)


def test_a_field_this_call_adds_is_taken_off_again_where_the_write_fails(rules, monkeypatch):
    """A rule missing a field - one out of a backup - was given it here, and a
    write that failed left the rule in memory carrying what the file does not."""
    rule = _pattern_rule(rules)
    rule.pop("filters")

    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(rules, "_write", refuse)

    with pytest.raises(OSError):
        rules.update(rule["id"], filters=[{"integration": INTEGRATION}])

    assert "filters" not in rule


def test_a_rule_missing_its_targets_is_answered_rather_than_crashing(rules):
    """One out of a backup may have no targets at all. Merged into what is not
    there, an edit came back as a KeyError."""
    rule = _pattern_rule(rules)
    rule.pop("targets")

    updated = rules.update(rule["id"], targets={"de": "Heizkosten {1}"})

    assert updated["targets"] == {"de": "Heizkosten {1}"}


def test_an_expression_for_a_rule_missing_its_targets_is_written(rules):
    """The same rule, and an expression instead of a target: it reaches the check
    that reads the targets against the expression, which came back as a KeyError.
    There is nothing to read, so there is nothing to refuse."""
    rule = _pattern_rule(rules)
    rule.pop("targets")

    updated = rules.update(rule["id"], value=r"Heizung\ (?P<n1>\d+)x")

    assert updated["match"]["value"] == r"Heizung\ (?P<n1>\d+)x"


def test_the_rules_answer_for_the_expression_themselves(rules):
    """Asked in the route first, the answer came out of a reading nothing held
    still - the rule can be rewritten or deleted between that reading and the
    write - and the same questions were asked again a moment later somewhere
    else, where the two could drift apart."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "routes_naming.py"), encoding="utf-8") as handle:
        source = handle.read()
    at = source.index('@naming.route("/api/naming/rules/<rule_id>"')
    body = source[at : source.index("@naming.route", at + 10)]

    assert "compile_pattern(" not in body
    assert "rules.get(rule_id)" not in body
    assert "except NotAPatternRuleError as error:" in body


def test_a_count_around_a_count_is_weighed_as_what_the_two_come_to(rules):
    """Two words counted four times read sixteen ways, which is as far as a counted
    choice may reach. Weighed a level at a time, each count of
    "(?:(?:a|aa){4}){4}" passed on its own and the whole read 65536 ways."""
    for expression in [r"(?:(?:a|aa){4}){4}", r"((a|aa){3}){3}", r"(?:(?:a|aa){2}){3}"]:
        with pytest.raises(NamingRuleError, match="repeat a choice"):
            compile_pattern(expression)

    # And what stayed within it is still read: sixteen ways at most, however it
    # is written.
    assert compile_pattern(r"(?:a|aa){4}") is not None
    assert compile_pattern(r"(a|aa){2,3}") is not None
    assert compile_pattern(r"(open|closed){1,2}") is not None
    assert compile_pattern(r"(?:Heizung|Kuehlung) (?P<n1>\d+)") is not None
