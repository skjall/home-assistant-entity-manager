"""A written name has to remember what went into it.

Only the device path passed a note along; the batch path and the direct one a
mass rename takes did not. Without it the supplied type that went into the name
is gone the moment it is written, no later rule can find the entity again, and
the next proposal starts from nothing - which is how one switch came out
"Steckdose" once and "Schalter" the next time.
"""

import ast
from pathlib import Path

import pytest

import web_ui


class Restructurer:
    """Answers a name the way the real one does, and remembers how."""

    def __init__(self, proposed="Küche Thermomix Steckdose", supplied="Steckdose", rendered=None):
        self.proposed = proposed
        self.supplied = supplied
        # What the rule renders the supplied word to. The same word unless a
        # rule or a translation changes it, which is the interesting case.
        self.rendered = supplied if rendered is None else rendered
        self.last_resolutions = {}

    def generate_new_entity_id(self, entity_id, state, entity_name=None):
        self.last_resolutions[entity_id] = {
            "input": self.supplied,
            "value": self.rendered,
            "won_by": "rule:user",
            "rule_id": "r_01",
        }
        return "switch.kuche_thermomix_steckdose", self.proposed


class Templates:
    def fingerprint(self):
        return "t1"


@pytest.fixture
def named(monkeypatch):
    restructurer = Restructurer()
    monkeypatch.setitem(web_ui.renamer_state, "restructurer", restructurer)
    monkeypatch.setitem(web_ui.renamer_state, "naming_templates", Templates())
    return restructurer


STATE = {"entity_id": "switch.old", "attributes": {}}


def test_the_supplied_type_is_noted_with_the_name(named):
    note = web_ui._note_for("switch.old", STATE, "Küche Thermomix Steckdose")

    assert note["base_entity"] == "Steckdose"
    assert note["won_by"] == "rule:user"
    assert note["rule_id"] == "r_01"


def test_a_name_the_user_typed_is_not_credited_to_a_rule(named):
    """The supplied type still holds; the claim that a rule decided it does not."""
    note = web_ui._note_for("switch.old", STATE, "Thermomix an der Wand")

    assert note["base_entity"] == "Steckdose"
    assert note["won_by"] == "user"
    assert note["rule_id"] is None


def test_an_entity_without_a_state_is_noted_as_nothing_rather_than_wrongly(named):
    assert web_ui._note_for("switch.old", None, "Küche Thermomix Steckdose") is None


def test_a_restructurer_that_throws_costs_the_note_but_not_the_rename(named, monkeypatch):
    def refuse(entity_id, state):
        raise RuntimeError("no")

    monkeypatch.setattr(named, "generate_new_entity_id", refuse)

    assert web_ui._note_for("switch.old", STATE, "Küche Thermomix Steckdose") is None


def test_every_path_that_writes_a_name_passes_the_note():
    """The bug was one path forgetting, so the question is asked of all of them."""
    source = Path(web_ui.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    forgot = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        if method not in ("rename_entity", "update_entity"):
            continue
        given = {keyword.arg for keyword in node.keywords}
        # update_entity also enables or labels an entity, which writes no name.
        writes_a_name = method == "rename_entity" or "name" in given or len(node.args) > 2
        if writes_a_name and "provenance" not in given:
            forgot.append(f"{method} at line {node.lineno}")

    assert not forgot, "these write a name without noting what went into it: " + ", ".join(forgot)


def test_the_note_keeps_what_went_in_where_a_rule_changed_it(monkeypatch):
    """Every test had the two words alike, so swapping them would have gone unseen.

    The reading is left behind by the naming itself, as provenance_for requires
    of its caller, rather than written into the resolver by hand: a note built
    on a reading nothing produced would say nothing about the real path.
    """
    restructurer = Restructurer(proposed="Küche Thermomix Zustand", supplied="Tuer", rendered="Zustand")
    monkeypatch.setitem(web_ui.renamer_state, "restructurer", restructurer)
    # Like every other test here: the fingerprint comes from a stand-in, not
    # from whatever the installation happens to hold.
    monkeypatch.setitem(web_ui.renamer_state, "naming_templates", Templates())

    note = web_ui._note_for("switch.old", STATE, "Küche Thermomix Zustand")

    assert note["base_entity"] == "Tuer"
    assert note["won_by"] == "rule:user"
    assert note["rule_id"] == "r_01"
