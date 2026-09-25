"""A rule can leave the number in a supplied name open.

Some integrations write a serial number into every name: "Heating 12345678",
"Heating 12345679". Each device supplies a name of its own, so a rule on the
exact name reaches one entity, and the next device arrives to be corrected
again. A pattern rule matches every name that differs only in its numbers and
carries the number on into the new name.
"""

import logging
import re

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
import naming_rules
from naming_rules import (
    NamingRuleError,
    NamingRules,
    compile_pattern,
    fill_placeholders,
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


def test_a_counted_repetition_is_not_a_runaway():
    """ "(\\d{4})+" bounds what it repeats, so it finishes; "(\\d{2,})+" does not."""
    assert compile_pattern(r"(\d{4})+") is not None
    assert compile_pattern(r"(?:ab{1,3})+c") is not None
    with pytest.raises(NamingRuleError):
        compile_pattern(r"(\d{2,})+")


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


# ------------------------------------------- what only looks like a group


def test_a_backreference_is_not_read_as_a_group(rules):
    """ "(?P=n1)" repeats what a group caught; it does not open one.

    Read as an opener it put a depth on the stack that its own ")" took off
    again, and the quantifier after that ")" was then weighed against the wrong
    group: "((?P=n1)+)+" was refused for repeating something that repeats,
    while what repeats is the outer group.
    """
    assert compile_pattern(r"(?P<n1>\d+) (?P=n1)") is not None
    assert compile_pattern(r"(?P<n1>\d+)(?:x(?P=n1))?") is not None
    # A comment is not a group either.
    assert compile_pattern(r"(?#the serial)(?P<n1>\d+)") is not None
    # And the shape that does run away is still refused, at the depth it is on.
    with pytest.raises(NamingRuleError):
        compile_pattern(r"(?P<n1>\d*)((?P=n1)+)+")


# ------------------------------------------------ what the log has to say


def test_a_placeholder_the_expression_has_no_group_for_is_said_out_loud(caplog):
    """A rule that can never apply to anything looked exactly like one that
    does not apply to this name: both simply never applied.

    Such a target is refused where a rule is written, so this is the net under
    that: a rule out of a hand-edited file, or one whose expression was
    rewritten somewhere the target was not read again.
    """
    match = re.fullmatch(r"Heizung (?P<n1>\d+)", "Heizung 12345678")

    with caplog.at_level(logging.WARNING):
        assert fill_placeholders("Heizung {2}", match) is None

    assert "{2}" in caplog.text
    assert "does not have" in caplog.text


def test_an_empty_capture_is_said_out_loud_as_well(rules, caplog):
    """And not as the same mistake: this rule applies to other names."""
    rule = rules.add_filter("pattern", r"Zone\ (?P<n1>\d*)", "de", "Zimmer {1}", {"integration": INTEGRATION})

    with caplog.at_level(logging.INFO):
        assert rules.render(rule, "Zone ", "de") == ""

    assert "caught nothing" in caplog.text
    assert "Zone " in caplog.text


# ---------------------------------------------- filled once, not twice


def test_the_target_is_filled_once_for_a_name(rules, monkeypatch):
    """Every pattern rule is tried against every supplied name, and the one that
    wins is then asked to render the same name again."""
    rule = _pattern_rule(rules)
    calls = []
    original = naming_rules.fill_placeholders

    def counted(target, match):
        calls.append(target)
        return original(target, match)

    monkeypatch.setattr(naming_rules, "fill_placeholders", counted)

    found = rules.find("pattern", "Heizung 12345678", INTEGRATION, "de")
    assert found["id"] == rule["id"]
    assert rules.render(found, "Heizung 12345678", "de") == "Heizkostenverteiler 12345678"
    assert len(calls) == 1

    # Another name is worked out again rather than answered with this one's.
    assert rules.render(found, "Heizung 87654321", "de") == "Heizkostenverteiler 87654321"
    assert len(calls) == 2


def test_a_rewritten_target_is_filled_again(rules):
    """What was worked out for a name belongs to the rule as it was then."""
    rule = _pattern_rule(rules)
    assert rules.render(rules.find("pattern", "Heizung 12345678", INTEGRATION, "de"), "Heizung 12345678", "de") == (
        "Heizkostenverteiler 12345678"
    )

    rules.update(rule["id"], targets={"de": "Heizkosten {1}"})

    assert rules.render(rules.find("pattern", "Heizung 12345678", INTEGRATION, "de"), "Heizung 12345678", "de") == (
        "Heizkosten 12345678"
    )


def test_a_bounded_count_after_a_backreference_is_bounded(rules):
    """ "((?P=n1){2})+" repeats the backreference twice and finishes; read as an
    open repetition it was refused."""
    assert compile_pattern(r"(?P<n1>\d+)((?P=n1){2})+") is not None
    with pytest.raises(NamingRuleError):
        compile_pattern(r"(?P<n1>\d+)((?P=n1)+)+")


def test_a_bounded_count_on_a_group_that_repeats_is_a_runaway(rules):
    """A count bounds how often a group is tried, not how many ways there are to
    read it: "([\\w ]+){2,5}" can split a hundred characters across five groups
    every way there is, and tries all of them on a name that nearly matches."""
    assert compile_pattern(r"(\d{4}){2,3}") is not None
    for expression in (r"([\w ]+){2,5}", r"(a+){2,3}", r"(a|aa){2,3}"):
        with pytest.raises(NamingRuleError):
            compile_pattern(expression)


def test_a_group_that_caught_nothing_is_not_a_group_that_is_missing(rules, caplog):
    """ "Zone (?P<n1>\\d+)?" against "Zone" answers None for a group it does have.
    Read as a missing group, the log said the rule could never apply to
    anything, when it simply does not apply to this name."""
    match = re.fullmatch(r"Zone (?P<n1>\d+)?", "Zone ")

    with caplog.at_level(logging.INFO):
        assert fill_placeholders("Zimmer {1}", match) is None

    assert "does not have" not in caplog.text
    assert "caught nothing" in caplog.text


def test_a_backreference_counted_twice_is_read_once(rules):
    """The count after it used to be matched here and again by the main walk."""
    assert compile_pattern(r"(?P<n1>\d+)(?P=n1){2,3}") is not None
    assert compile_pattern(r"(?P<n1>\d+)((?P=n1){2})+") is not None


def test_two_threads_filling_targets_do_not_throw_away_each_others_work(rules):
    """They took turns resetting one shared answer, and between them did more
    work than either would have done alone."""
    import threading

    rule = _pattern_rule(rules)
    answers = {}

    def fill(name):
        found = rules.find("pattern", name, INTEGRATION, "de")
        answers[name] = rules.render(found, name, "de")

    one = threading.Thread(target=fill, args=("Heizung 11111111",))
    two = threading.Thread(target=fill, args=("Heizung 22222222",))
    one.start()
    two.start()
    one.join()
    two.join()

    assert answers["Heizung 11111111"] == "Heizkostenverteiler 11111111"
    assert answers["Heizung 22222222"] == "Heizkostenverteiler 22222222"
    assert rule["id"]


def test_a_rewritten_target_is_not_answered_from_what_was_filled(rules):
    """What was worked out belongs to the rule as it was then, and the rules
    moving on has to reach every thread that holds one."""
    rule = _pattern_rule(rules)
    assert rules.render(rules.find("pattern", "Heizung 12345678", INTEGRATION, "de"), "Heizung 12345678", "de") == (
        "Heizkostenverteiler 12345678"
    )

    rules.update(rule["id"], targets={"de": "Heizkosten {1}"})

    assert rules.render(rules.find("pattern", "Heizung 12345678", INTEGRATION, "de"), "Heizung 12345678", "de") == (
        "Heizkosten 12345678"
    )


def test_a_pattern_rule_without_filters_still_applies(rules):
    """An empty filter says two things: a rule with no filters, which covers
    everything, and one whose filters do not cover this entity. Read as the
    second, such a rule applied to nothing at all and said nothing about it.

    A pattern rule names its integration on the way in, so this is one from
    before that was asked for - a hand-edited file, or a rule out of a backup.
    """
    rule = _pattern_rule(rules)
    rule["filters"] = []

    found = rules.find("pattern", "Heizung 12345678", INTEGRATION, "de")

    assert found is not None
    assert found["id"] == rule["id"]
    assert rules.render(found, "Heizung 12345678", "de") == "Heizkostenverteiler 12345678"


def test_the_generation_moves_on_once_per_change(rules):
    """Read and written back, one of two changes at once was lost and a thread
    went on answering from what it had."""
    rule = _pattern_rule(rules)
    seen = {rules._filling_generation}

    for target in ("Heizkosten {1}", "Heizung {1}", "Zähler {1}"):
        rules.update(rule["id"], targets={"de": target})
        seen.add(rules._filling_generation)

    assert len(seen) == 4
