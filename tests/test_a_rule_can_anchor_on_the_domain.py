"""A rule that recognises an entity by its domain.

Every other anchor says what an entity measures: its integration's translation
key, the canonical form of the name it supplies, its device class. Some
entities have none of those. UniFi names each of its device trackers after the
client it found, so fourteen of them carry fourteen different names, no key and
no class. There is no anchor they share except being device trackers, and
without one there is no rule that can reach more than one of them.

The domain is that anchor, and it is the last of them: anything that says what
an entity measures decides before it does.
"""

from pathlib import Path

import pytest

import entity_restructurer
from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_rules import KIND_PRIORITY, NamingRules
from naming_templates import NamingTemplates
import routes_naming
from type_mappings import DEFAULT_SYSTEM_MAPPINGS, TypeMappings
import web_ui

# Taken from a real installation: no two of them are alike.
SUPPLIED = {
    "device_tracker.unifi_default_00_70_07_24_e6_38": "00:70:07:24:e6:38 Dusche Bluetooth Proxy",
    "device_tracker.unifi_default_de_91_e5_f7_12_73": "iPhone",
    "device_tracker.unifi_default_6a_0b_15_00_60_ba": "Watch",
    "device_tracker.unifi_default_1c_af_4a_c7_5d_b1": "JanMacBook-Pro",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A handful of UniFi device trackers, one from elsewhere, and a sensor."""
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
    restructurer.naming_templates.apply_preset("entity_manager")
    restructurer.floors = {}
    restructurer.areas = {"d": {"area_id": "d", "name": "Dusche"}}
    restructurer.devices = {"ap": {"id": "ap", "name": "Access Point", "area_id": "d", "model": "U6"}}
    restructurer.entities = {
        entity_id: {
            "id": f"reg-{index}",
            "entity_id": entity_id,
            "device_id": "ap",
            "platform": "unifi",
            "original_name": supplied,
            "has_entity_name": False,
        }
        for index, (entity_id, supplied) in enumerate(SUPPLIED.items())
    }
    # A device tracker from somewhere else, so a rule scoped to UniFi can be
    # shown to stop at its edge.
    restructurer.entities["device_tracker.jans_iphone"] = {
        "id": "reg-mobile",
        "entity_id": "device_tracker.jans_iphone",
        "device_id": "ap",
        "platform": "mobile_app",
        "original_name": "Jans iPhone",
        "has_entity_name": False,
    }
    # And something that is not a device tracker at all.
    restructurer.entities["sensor.ap_durchsatz"] = {
        "id": "reg-sensor",
        "entity_id": "sensor.ap_durchsatz",
        "device_id": "ap",
        "platform": "unifi",
        "original_name": "Durchsatz",
        "has_entity_name": False,
    }

    monkeypatch.setitem(web_ui.renamer_state, "naming_rules", rules)
    monkeypatch.setitem(web_ui.renamer_state, "type_mappings", mappings)
    monkeypatch.setitem(web_ui.renamer_state, "naming_overrides", overrides)
    monkeypatch.setitem(web_ui.renamer_state, "restructurer", restructurer)
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client(), restructurer, rules


def name_of(restructurer, entity_id):
    return restructurer.generate_new_entity_id(entity_id, restructurer.entities[entity_id])[1]


# --- What was missing -----------------------------------------------------


def test_without_the_domain_anchor_a_rule_reaches_one_of_them(home):
    """Their supplied names have nothing in common, so the anchor taken by
    itself - the name - matches only the entity it was learned from."""
    client, _, _ = home

    answer = client.post(
        "/api/naming/learn",
        json={"entity_id": "device_tracker.unifi_default_de_91_e5_f7_12_73", "value": "Standort"},
    )

    rule = answer.get_json()["rule"]
    assert rule["match"]["kind"] == "name"
    assert rule["affected"] == 1


# --- The anchor -----------------------------------------------------------


def test_the_domain_anchor_reaches_every_device_tracker_of_the_integration(home):
    client, restructurer, _ = home

    answer = client.post(
        "/api/naming/learn",
        json={
            "entity_id": "device_tracker.unifi_default_de_91_e5_f7_12_73",
            "value": "Standort",
            "anchor": "domain",
            "scope": "integration",
        },
    )

    rule = answer.get_json()["rule"]
    assert rule["match"] == {"kind": "domain", "value": "device_tracker"}
    assert rule["filters"] == [{"integration": "unifi"}]
    for entity_id in SUPPLIED:
        assert name_of(restructurer, entity_id) == "Dusche Access Point Standort"


def test_the_rule_stops_at_the_integration_it_was_scoped_to(home):
    client, restructurer, _ = home

    client.post(
        "/api/naming/learn",
        json={
            "entity_id": "device_tracker.unifi_default_de_91_e5_f7_12_73",
            "value": "Standort",
            "anchor": "domain",
            "scope": "integration",
        },
    )

    assert "Standort" not in name_of(restructurer, "device_tracker.jans_iphone")


def test_the_rule_stops_at_its_domain(home):
    """It says what kind of thing an entity is, not what it measures."""
    client, restructurer, _ = home

    client.post(
        "/api/naming/learn",
        json={
            "entity_id": "device_tracker.unifi_default_de_91_e5_f7_12_73",
            "value": "Standort",
            "anchor": "domain",
            "scope": "integration",
        },
    )

    assert "Standort" not in name_of(restructurer, "sensor.ap_durchsatz")


@pytest.mark.parametrize("scope", ["all", "global"])
def test_everywhere_takes_no_filter_at_all(home, scope):
    """ "all" is the word the button sends; "global" is the one the API document
    uses for the same thing, and both have to reach every device tracker.

    One installation per word, or the second would find the rule the first
    made and say nothing about the path it came down.
    """
    client, restructurer, _ = home

    answer = client.post(
        "/api/naming/learn",
        json={
            "entity_id": "device_tracker.unifi_default_de_91_e5_f7_12_73",
            "value": "Standort",
            "anchor": "domain",
            "scope": scope,
        },
    )

    assert answer.get_json()["rule"]["filters"] == []
    assert name_of(restructurer, "device_tracker.jans_iphone") == "Dusche Access Point Standort"


def test_a_name_rule_still_decides_before_the_domain(home):
    """The domain says the least of any anchor, so it decides last."""
    client, restructurer, _ = home
    client.post(
        "/api/naming/learn",
        json={
            "entity_id": "device_tracker.unifi_default_de_91_e5_f7_12_73",
            "value": "Standort",
            "anchor": "domain",
            "scope": "integration",
        },
    )

    client.post(
        "/api/naming/learn",
        json={"entity_id": "device_tracker.unifi_default_1c_af_4a_c7_5d_b1", "value": "Laptop"},
    )

    assert name_of(restructurer, "device_tracker.unifi_default_1c_af_4a_c7_5d_b1") == "Dusche Access Point Laptop"
    assert name_of(restructurer, "device_tracker.unifi_default_6a_0b_15_00_60_ba") == "Dusche Access Point Standort"


def test_an_anchor_nobody_knows_is_refused(home):
    client, _, _ = home

    answer = client.post(
        "/api/naming/learn",
        json={"entity_id": "device_tracker.jans_iphone", "value": "Standort", "anchor": "geraeteklasse"},
    )

    assert answer.status_code == 400


def test_a_domain_rule_cannot_be_put_on_one_model(home):
    """A domain rule is about a kind of entity, not about one model of device.

    Taken, it came back to a form that could not show it: neither row of buttons
    had one to light up, and the next save wrote the same state again.
    """
    client, _, _ = home

    answer = client.post(
        "/api/naming/learn",
        json={
            "entity_id": "device_tracker.jans_iphone",
            "value": "Standort",
            "anchor": "domain",
            "scope": "model",
        },
    )

    assert answer.status_code == 400


def test_the_rule_in_force_is_reported_even_where_it_won_nothing(home):
    """A rule saying what the name already says is still the rule to change.

    Reported by nobody, the form opened as though no rule applied, and the
    correction made a name rule beside the domain rule that names the entity.
    """
    client, restructurer, rules = home
    entity_id = "device_tracker.jans_iphone"
    made = client.post(
        "/api/naming/learn",
        json={"entity_id": entity_id, "value": "Jans iPhone", "anchor": "domain", "scope": "integration"},
    ).get_json()["rule"]

    restructurer.build_naming_context(entity_id, restructurer.entities[entity_id])
    resolution = restructurer.last_resolutions[entity_id]

    # The name is what it always was, so no rule won it - and this one applies.
    assert resolution["rule_id"] is None
    assert resolution["applies"]["rule_id"] == made["id"]
    assert resolution["applies"]["matched_on"]["kind"] == "domain"


# --- How far it reaches ---------------------------------------------------


def test_the_counts_say_how_far_it_would_reach(home):
    """Nothing else warns: a domain rule on `sensor` would catch hundreds."""
    _, restructurer, _ = home

    # Both out of one walk: asked for together on every load, each of them
    # walked the whole entity list for itself.
    by_domain, by_integration = routes_naming.domain_reach(restructurer)

    assert by_domain == {"device_tracker": 5, "sensor": 1}
    assert by_integration == {
        ("device_tracker", "unifi"): 4,
        ("device_tracker", "mobile_app"): 1,
        ("sensor", "unifi"): 1,
    }


def test_the_domain_stands_back_where_a_name_rule_says_nothing_new(home):
    """A rule that only repeats what the name already says still speaks.

    The user looked at this entity and said what it is called; that the word
    matches what was there anyway does not make the rule absent. Read as
    absent, the widest anchor of all stepped in and renamed the entity after
    its domain.
    """
    client, restructurer, _ = home
    client.post(
        "/api/naming/learn",
        json={
            "entity_id": "device_tracker.unifi_default_de_91_e5_f7_12_73",
            "value": "Standort",
            "anchor": "domain",
            "scope": "all",
        },
    )
    # The name rule says what the entity is called anyway.
    client.post(
        "/api/naming/learn",
        json={"entity_id": "device_tracker.jans_iphone", "value": "Jans iPhone"},
    )

    assert name_of(restructurer, "device_tracker.jans_iphone") == "Dusche Access Point Jans iPhone"


def test_a_class_rule_the_naming_holds_back_keeps_the_domain_out(home):
    """The class rule was refused because the name does not name the class.

    That is the naming saying this entity is not what its class says, not an
    invitation for the widest anchor to name it after its domain instead.
    """
    client, restructurer, rules = home
    restructurer.entities["button.passwort_neu"] = {
        "id": "reg-button",
        "entity_id": "button.passwort_neu",
        "device_id": "ap",
        "platform": "unifi",
        "original_name": "Passwort neu erzeugen",
        "device_class": "update",
        "has_entity_name": False,
    }
    rules.upsert("device_class", "update", None, "de", "Aktualisierung")
    client.post(
        "/api/naming/learn",
        json={
            "entity_id": "button.passwort_neu",
            "value": "Taste",
            "anchor": "domain",
            "scope": "integration",
        },
    )

    assert name_of(restructurer, "button.passwort_neu") == "Dusche Access Point Passwort neu erzeugen"


def test_a_rule_the_naming_holds_back_is_not_counted_as_reached(home):
    """The count over a rule and the list under it have to say the same."""
    client, restructurer, _ = home
    restructurer.entities["button.passwort_neu"] = {
        "id": "reg-button",
        "entity_id": "button.passwort_neu",
        "device_id": "ap",
        "platform": "unifi",
        "original_name": "Passwort neu erzeugen",
        "has_entity_name": False,
    }
    client.post(
        "/api/naming/learn",
        json={"entity_id": "button.passwort_neu", "value": "Passwort neu erzeugen"},
    )
    answer = client.post(
        "/api/naming/learn",
        json={
            "entity_id": "button.passwort_neu",
            "value": "Taste",
            "anchor": "domain",
            "scope": "integration",
        },
    )
    domain_rule = answer.get_json()["rule"]["id"]

    behind = restructurer.rule_behind("button.passwort_neu", restructurer.entities["button.passwort_neu"])

    assert (behind or {}).get("rule_id") != domain_rule


# --- What stands back for what --------------------------------------------


def test_every_other_anchor_holds_the_domain_rule_back(home):
    """The domain is the widest anchor, so any of the others speaking about an
    entity takes the decision away from it.

    Written out for each of them, because the naming works the anchors out one
    by one rather than in a loop: a kind added to KIND_PRIORITY and not given
    its own line here would let the domain rule decide over it, and the count
    below is what says a kind was added.
    """
    client, restructurer, rules = home
    assert set(KIND_PRIORITY) == {"translation_key", "name", "pattern", "device_class", "domain"}

    rules.add_filter("domain", "sensor", "de", "Messwert", None)
    # The pattern is the one that was missed: it matched, it renamed the entity
    # to what it is already called, and its value was dropped for saying
    # nothing - after which the domain rule was the only candidate left.
    rules.add_filter("pattern", r"Durchsatz(?P<n1>\d*)", "de", "Durchsatz", {"integration": "unifi"})

    assert name_of(restructurer, "sensor.ap_durchsatz") == "Dusche Access Point Durchsatz"


def test_a_pattern_rule_that_changes_nothing_still_holds_the_domain_back(home):
    """A rule of the user's that renames an entity to what it already says is
    still the user saying which entities this one is about."""
    client, restructurer, rules = home
    rules.add_filter("domain", "sensor", "de", "Messwert", None)

    before = name_of(restructurer, "sensor.ap_durchsatz")
    assert before == "Dusche Access Point Messwert"

    rules.add_filter("pattern", r"Durchsatz(?P<n1>\d*)", "de", "Durchsatz", {"integration": "unifi"})

    assert name_of(restructurer, "sensor.ap_durchsatz") == "Dusche Access Point Durchsatz"


# --- The anchor and the scope have to agree -------------------------------


def test_a_domain_anchor_is_refused_where_the_scope_writes_its_own(home):
    """The pattern scope writes an anchor of its own. Accepted, the call came
    back with a pattern rule while the caller had asked for a domain rule, and
    nothing said the anchor had been dropped."""
    client, _, _ = home

    answer = client.post(
        "/api/naming/learn",
        json={
            "entity_id": "device_tracker.unifi_default_de_91_e5_f7_12_73",
            "value": "Standort",
            "anchor": "domain",
            "scope": "pattern",
        },
    )

    assert answer.status_code == 400
    assert "domain rule" in answer.get_json()["error"]


def test_the_anchors_are_walked_out_of_the_one_place_that_orders_them():
    """rule_behind and the naming both walk KIND_PRIORITY now. Written out by
    hand, the naming missed a kind that rule_behind had - and a domain rule then
    named entities the narrower kind was holding it back from."""
    source = Path(entity_restructurer.__file__).read_text()

    assert source.count("for kind in sorted(asked, key=lambda one: KIND_PRIORITY[one]):") == 2
    # And neither of them asks about a kind by name.
    assert 'rules.find("translation_key"' not in source
    assert 'rules.find("domain"' not in source


def test_the_rule_in_force_is_read_rather_than_looked_up_again(home):
    """The naming has just said which rule applies where one applies and changes
    nothing; asking again looked every anchor up a second time."""
    client, restructurer, rules = home
    rules.add_filter("domain", "device_tracker", "de", "Standort", {"integration": "unifi"})
    entity_id = "device_tracker.unifi_default_de_91_e5_f7_12_73"
    registry = restructurer.entities[entity_id]
    # A name rule that says what the entity is called already: it wins nothing
    # and it holds the domain rule back.
    rules.add_filter("name", "iPhone", "de", "iPhone", None)

    behind = restructurer.rule_behind(entity_id, registry)
    resolution = restructurer.last_resolutions[entity_id]

    assert resolution["applies"] is not None
    assert behind["rule_id"] == resolution["applies"]["rule_id"]


def test_a_row_reopened_holds_no_anchor_of_its_own():
    """Kept while the reach was read again, a row whose domain rule had been
    deleted came back with the domain button lit beside a reach worked out for a
    rule that is not there - and the save wrote a new domain rule."""
    markup = (Path(__file__).parent.parent / "templates" / "index.html").read_text()
    at = markup.index("toggleEntityExpand(entity) {")
    body = markup[at : markup.index("updateEntityPreview(entity, suffix) {", at)]

    assert "entity._ruleScope = undefined;" in body
    assert "entity._ruleAnchor = null;" in body


def test_a_rule_that_changes_the_spelling_is_reported_as_the_source(home):
    """A target that differs from the shown name at all - in case, in spacing -
    is a name the rule changes, so the rule wins it and is reported as its
    source. The form opens on it from there, and "applies" is for the rule that
    changes nothing."""
    client, restructurer, rules = home
    rule = rules.add_filter("domain", "device_tracker", "de", "standort", {"integration": "unifi"})
    entity_id = "device_tracker.unifi_default_de_91_e5_f7_12_73"

    restructurer.build_naming_context(entity_id, restructurer.entities[entity_id])
    resolution = restructurer.last_resolutions[entity_id]

    assert resolution["rule_id"] == rule["id"]
    assert restructurer.rule_behind(entity_id, restructurer.entities[entity_id])["rule_id"] == rule["id"]


def test_an_integration_scope_needs_an_integration(home):
    """There is nothing to narrow the rule to, and a rule stored without that
    filter applies everywhere - which is not what was asked for. Said out loud
    rather than written that way."""
    client, restructurer, _ = home
    restructurer.entities["sensor.by_hand"] = {
        "id": "reg-by-hand",
        "entity_id": "sensor.by_hand",
        "device_id": "ap",
        "original_name": "Zähler",
        "has_entity_name": False,
    }

    answer = client.post(
        "/api/naming/learn",
        json={"entity_id": "sensor.by_hand", "value": "Messwert", "scope": "integration"},
    )

    assert answer.status_code == 400
    assert "integration" in answer.get_json()["error"]


def test_the_domain_is_not_looked_up_where_it_cannot_win(home):
    """The lookup ran for every entity that has a narrower rule in force, and its
    answer was thrown away two lines later."""
    client, restructurer, rules = home
    rules.add_filter("domain", "device_tracker", "de", "Standort", {"integration": "unifi"})
    rules.add_filter("name", "iPhone", "de", "Telefon", None)
    entity_id = "device_tracker.unifi_default_de_91_e5_f7_12_73"

    asked = []
    original = rules.find

    def counted(kind, value, *args, **kwargs):
        asked.append(kind)
        return original(kind, value, *args, **kwargs)

    rules.find = counted
    try:
        behind = restructurer.rule_behind(entity_id, restructurer.entities[entity_id])
    finally:
        rules.find = original

    assert behind["kind"] == "name"
    assert "domain" not in asked


def test_a_rule_that_names_one_entity_and_an_integration_is_not_read_as_naming_this_one(home):
    """Caught through the integration, it was offered as "this entity only" - a
    reach it does not have - because the check read every filter the rule holds
    rather than what caught this entity."""
    markup = (Path(__file__).parent.parent / "templates" / "index.html").read_text()
    at = markup.index("scopeThatApplied(entity) {")
    body = markup[at : markup.index("toggleEntityExpand(entity) {", at)]

    assert "const caught = !!(match.integration || match.model || match.domain);" in body
    assert "const named = !caught" in body


def test_one_place_says_which_kinds_the_table_cannot_answer_for():
    """Two readings of it drifted: one counted domain rules as used and the other
    looked their wording up anyway."""
    source = (Path(__file__).parent.parent / "routes_naming.py").read_text()

    assert source.count("def _not_a_word(") == 1
    # Both readings ask it, rather than each naming the kind for itself.
    assert source.count("_not_a_word(rule)") == 2


def test_the_domain_stands_back_where_a_held_back_rule_speaks(home):
    """A device-class rule is held back where the entity's own name says more than
    its class does. A domain rule says less still, so letting it answer there would
    put back exactly the name that was kept - the user said something narrower
    about this entity, and the widest anchor does not step in over it."""
    client, restructurer, rules = home
    rules.add_filter("domain", "device_tracker", "de", "Standort", {"integration": "unifi"})
    entity_id = "device_tracker.unifi_default_de_91_e5_f7_12_73"
    registry = restructurer.entities[entity_id]
    registry["device_class"] = "connectivity"
    # A class rule the naming holds back: the name "iPhone" says more than
    # "connectivity" does.
    rules.add_filter("device_class", "connectivity", "de", "Verbindung", None)

    behind = restructurer.rule_behind(entity_id, registry)
    resolution = restructurer.last_resolutions[entity_id]

    assert behind is None
    assert resolution["rule_id"] is None
    assert "Standort" not in [one["value"] for one in resolution["candidates"]]


def test_the_domain_is_read_once_where_it_cannot_win():
    """Nothing between the two readings touches what they read, so the second could
    never fire - and it said it guarded against something."""
    source = Path(entity_restructurer.__file__).read_text()

    at = source.index("def rule_behind(")
    assert source.count('if kind == "domain" and narrower:', at) == 1


def test_the_narrower_of_two_rules_saying_one_word_is_the_one_in_force(home):
    """Two rules of the user's saying the same word about one entity: the narrower
    one is in force, so that is the one the form opens on. Read the other way, a
    correction went to the rule the naming does not ask first."""
    client, restructurer, rules = home
    entity_id = "device_tracker.unifi_default_de_91_e5_f7_12_73"
    registry = restructurer.entities[entity_id]
    registry["translation_key"] = "client"
    # Both say what the entity is called already, so neither wins the name.
    key = rules.add_filter("translation_key", "client", "de", "iPhone", None)
    rules.add_filter("name", "iPhone", "de", "iPhone", None)

    behind = restructurer.rule_behind(entity_id, registry)

    assert behind["rule_id"] == key["id"]
    assert behind["kind"] == "translation_key"


def test_a_rule_of_the_domain_scope_lights_the_domain_button():
    """A rule of that scope carries the model as well - "this model's entities of
    one domain". Read model-first, the row lit the model button beside it, and
    saving from there wrote a model-only rule over the rule that had the domain."""
    markup = (Path(__file__).parent.parent / "templates" / "index.html").read_text()
    at = markup.index("scopeThatApplied(entity) {")
    body = markup[at : markup.index("toggleEntityExpand(entity) {", at)]

    assert body.index("if (match.domain) return 'domain';") < body.index("if (match.model) return 'model';")
    assert body.index("if (match.model) return 'model';") < body.index("if (match.integration) return 'integration';")


def test_a_rule_is_answered_for_with_one_value_either_way(home):
    """Read out of what the naming worked out, a rule came back spelled as the rule
    holds it; read out of the walk below, spelled as the entity supplied it. The
    same rule, asked the two ways, answered with two values."""
    client, restructurer, rules = home
    entity_id = "device_tracker.unifi_default_de_91_e5_f7_12_73"
    registry = restructurer.entities[entity_id]
    # One rule, and it changes the name - so the naming reports it as the source
    # and the reading out of the resolution is the one that answers.
    rule = rules.add_filter("name", "iPhone", "de", "Telefon", None)

    from_the_naming = restructurer.rule_behind(entity_id, registry)

    # And again where the naming named no rule at all - a resolution that says
    # what went in and nothing about who answered - which is the walk below.
    original = restructurer.build_naming_context

    def says_nothing(*args, **kwargs):
        restructurer.last_resolutions[entity_id] = {"input": "iPhone", "rule_id": None, "applies": None}

    restructurer.build_naming_context = says_nothing
    try:
        from_the_walk = restructurer.rule_behind(entity_id, registry)
    finally:
        restructurer.build_naming_context = original

    assert from_the_naming["value"] == rule["match"]["value"]
    assert from_the_walk["value"] == rule["match"]["value"]


def test_a_row_reopened_does_not_ask_what_it_has_just_unset():
    """The reset above it has just unset the reach, so asking again could only get
    the answer written two lines up - and the check read as a guard against a reach
    that was already there."""
    markup = (Path(__file__).parent.parent / "templates" / "index.html").read_text()
    at = markup.index("toggleEntityExpand(entity) {")
    body = markup[at : markup.index("updateEntityPreview(entity, suffix) {", at)]

    assert "if (entity._expanded && entity._ruleScope === undefined) {" not in body
    assert "entity._ruleScope = undefined;" in body


def test_a_domain_rule_is_not_read_as_a_rule_that_does_nothing(home):
    """A domain is not a name: "device_tracker" is what the entities are, not what
    they are called. Judged against that word as a supplied name would be, a rule
    reading "Device tracker" looked like a rule that changes nothing - and it was
    offered for deletion while it renames every one of them."""
    client, restructurer, rules = home
    rule = rules.add_filter("domain", "device_tracker", "de", "Device tracker", {"integration": "unifi"})

    assert rules.is_redundant(rule, "de") is False

    # And a name rule whose target is that name as the display spells it does
    # change nothing, which is what redundant means.
    named = rules.add_filter("name", "sensor", "de", "Sensor", None)
    assert rules.is_redundant(named, "de") is True


def test_a_device_moved_does_not_write_its_entities_own_areas():
    """An entity that follows its device carries no area of its own - that is what
    following is - and the names read the device's where it does. Written through, an
    entity pinned to another area was shown in the one the device went to and named
    for it while Home Assistant had left it where it was, and every follower was
    given an area of its own."""
    markup = (Path(__file__).parent.parent / "templates" / "index.html").read_text()
    at = markup.index("async assignDeviceArea(deviceId, areaId) {")
    body = markup[at : markup.index("async syncZ2mDrift(", at)]

    moved = body[body.index("const mine = this.hierarchy.entities.filter(") :]
    assert "area_id" not in moved[: moved.index("});")]
    # The names the server computed are still dropped: those are about the area
    # the device has left.
    assert "e.suggested_name = null;" in body
    assert "e.suggested_entity_id = null;" in body
    # The device's own area is the device's to write.
    assert "device.area_id = areaId || null;" in body
