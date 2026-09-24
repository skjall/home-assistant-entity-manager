"""Tests for assigning unique entity IDs when a template renders duplicates."""

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_templates import NamingTemplates


@pytest.fixture
def restructurer(tmp_path):
    """Restructurer holding one plug with two identically named energy sensors."""
    result = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=None,
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
    )
    result.floors = {}
    result.areas = {}
    result.devices = {}
    result.entities = {
        "sensor.plug_energy": {"id": "r1", "entity_id": "sensor.plug_energy"},
        "sensor.plug_energy_2": {"id": "r2", "entity_id": "sensor.plug_energy_2"},
        "sensor.unrelated_energie_2": {"id": "r3", "entity_id": "sensor.unrelated_energie_2"},
    }
    return result


def test_duplicate_targets_are_numbered(restructurer):
    """Two proposals for one ID are both numbered, from one.

    Home Assistant would leave the first unnumbered and call the second _2. In
    a list that reads as though only the second were a duplicate, and an entity
    its integration already calls "(1)" would lose its one.
    """
    result = restructurer.deduplicate_entity_ids(
        [
            ("sensor.plug_energy", "sensor.wohnzimmer_steckdose_energie", "Energie"),
            ("sensor.plug_energy_2", "sensor.wohnzimmer_steckdose_energie", "Energie"),
        ]
    )

    assert [new_id for _, new_id, _ in result] == [
        "sensor.wohnzimmer_steckdose_energie_1",
        "sensor.wohnzimmer_steckdose_energie_2",
    ]


def test_the_current_holder_of_the_plain_id_becomes_the_first(restructurer):
    """Where two want one name, the one holding it plainly becomes number one.

    It keeps its place at the head of the group, so the numbers do not shuffle
    between runs; what it loses is the unnumbered name, which said nothing
    about which of the two it is.
    """
    result = restructurer.deduplicate_entity_ids(
        [
            ("sensor.plug_energy_2", "sensor.plug_energy", "Energie"),
            ("sensor.plug_energy", "sensor.plug_energy", "Energie"),
        ]
    )

    assigned = {entity_id: new_id for entity_id, new_id, _ in result}
    assert assigned["sensor.plug_energy"] == "sensor.plug_energy_1"
    assert assigned["sensor.plug_energy_2"] == "sensor.plug_energy_2"


def test_id_held_by_an_untouched_entity_is_avoided(restructurer):
    """A target belonging to an entity outside the batch is off limits."""
    result = restructurer.deduplicate_entity_ids([("sensor.plug_energy", "sensor.unrelated_energie_2", "Energie")])

    assert result[0][1] == "sensor.unrelated_energie_2_2"


def test_friendly_name_carries_the_number(restructurer):
    """A numbered ID needs a numbered name, or the rename is proposed forever."""
    result = restructurer.deduplicate_entity_ids(
        [
            ("sensor.plug_energy", "sensor.wohnzimmer_steckdose_energie", "Wohnzimmer Steckdose Energie"),
            ("sensor.plug_energy_2", "sensor.wohnzimmer_steckdose_energie", "Wohnzimmer Steckdose Energie"),
        ]
    )

    assert [name for _, _, name in result] == [
        "Wohnzimmer Steckdose Energie 1",
        "Wohnzimmer Steckdose Energie 2",
    ]


def test_order_and_friendly_names_are_preserved(restructurer):
    """Only the IDs change; order and friendly names stay as they were."""
    proposals = [
        ("sensor.plug_energy", "sensor.a", "Erste"),
        ("sensor.plug_energy_2", "sensor.b", "Zweite"),
    ]

    result = restructurer.deduplicate_entity_ids(proposals)

    assert [entity_id for entity_id, _, _ in result] == [p[0] for p in proposals]
    assert [name for _, _, name in result] == ["Erste", "Zweite"]


def test_numbering_is_stable_across_runs(restructurer):
    """Repeating the run must not swap the numbers between entities."""
    proposals = [
        ("sensor.plug_energy_2", "sensor.wohnzimmer_steckdose_energie", "Energie"),
        ("sensor.plug_energy", "sensor.wohnzimmer_steckdose_energie", "Energie"),
    ]

    first = restructurer.deduplicate_entity_ids(proposals)
    second = restructurer.deduplicate_entity_ids(list(reversed(proposals)))

    assert {e: n for e, n, _ in first} == {e: n for e, n, _ in second}


def test_a_numbered_proposal_says_who_holds_the_plain_id(restructurer):
    """The number keeps the rename possible but says nothing about what the
    two entities are; the case has to reach the interface to be decided."""
    restructurer.deduplicate_entity_ids(
        [
            ("sensor.plug_energy", "sensor.wohnzimmer_steckdose_energie", "Energie"),
            ("sensor.plug_energy_2", "sensor.wohnzimmer_steckdose_energie", "Energie"),
        ]
    )

    first = restructurer.last_numbering["sensor.plug_energy"]
    assert first["wanted"] == "sensor.wohnzimmer_steckdose_energie"
    assert first["suffix"] == 1
    # A note pointing at itself says nothing, so the first names the next one.
    assert first["holder"] == "sensor.plug_energy_2"

    second = restructurer.last_numbering["sensor.plug_energy_2"]
    assert second["wanted"] == "sensor.wohnzimmer_steckdose_energie"
    assert second["holder"] == "sensor.plug_energy"
    assert second["suffix"] == 2


def test_nothing_is_reported_when_every_proposal_fits(restructurer):
    restructurer.deduplicate_entity_ids([("sensor.plug_energy", "sensor.kuche_energie", "Energie")])

    assert restructurer.last_numbering == {}


def test_a_single_proposal_keeps_the_plain_name(restructurer):
    """Numbering is for a crowd. One entity alone is not one of several."""
    result = restructurer.deduplicate_entity_ids([("sensor.plug_energy", "sensor.kuche_energie", "Energie")])

    assert result[0][1] == "sensor.kuche_energie"
    assert result[0][2] == "Energie"
    assert restructurer.last_numbering == {}


def test_three_of_a_kind_count_one_two_three(restructurer):
    """The button from the report: two keys, and a third would be three."""
    restructurer.entities["event.button_taste_3"] = {"id": "r4", "entity_id": "event.button_taste_3"}
    result = restructurer.deduplicate_entity_ids(
        [
            ("event.button_taste_1", "event.schlafzimmer_button_taste", "Taste"),
            ("event.button_taste_2", "event.schlafzimmer_button_taste", "Taste"),
            ("event.button_taste_3", "event.schlafzimmer_button_taste", "Taste"),
        ]
    )

    assert [new_id for _, new_id, _ in result] == [
        "event.schlafzimmer_button_taste_1",
        "event.schlafzimmer_button_taste_2",
        "event.schlafzimmer_button_taste_3",
    ]
    assert [name for _, _, name in result] == ["Taste 1", "Taste 2", "Taste 3"]


def test_the_endpoint_answers_with_the_ids_a_rename_would_write(restructurer, monkeypatch):
    """The interface used to work the numbering out a second time.

    Two rows out of one name: which of them keeps the plain id, and from which
    number the others count, is the rename's own reckoning. Asked for here, so a
    preview and the write it leads to cannot disagree - and what the numbering
    tells the user about stays the rename they asked for, not this question.
    """
    import web_ui

    monkeypatch.setitem(web_ui.renamer_state, "restructurer", restructurer)
    restructurer.last_numbering = {"sensor.untouched": {"wanted": "x", "holder": "y"}}
    web_ui.app.config["TESTING"] = True
    client = web_ui.app.test_client()

    answer = client.post(
        "/api/normalize",
        json={
            "names": ["Wohnzimmer Steckdose Energie", "Wohnzimmer Steckdose Energie"],
            "for": ["sensor.plug_energy", "sensor.plug_energy_2"],
        },
    ).get_json()

    assert answer["normalized"] == ["wohnzimmer_steckdose_energie"] * 2
    assert answer["ids"] == ["sensor.wohnzimmer_steckdose_energie_1", "sensor.wohnzimmer_steckdose_energie_2"]
    assert answer["names"] == ["Wohnzimmer Steckdose Energie 1", "Wohnzimmer Steckdose Energie 2"]
    assert restructurer.last_numbering == {"sensor.untouched": {"wanted": "x", "holder": "y"}}


def test_the_endpoint_still_answers_names_alone(restructurer, monkeypatch):
    """Without "for" it says what it always said."""
    import web_ui

    monkeypatch.setitem(web_ui.renamer_state, "restructurer", restructurer)
    web_ui.app.config["TESTING"] = True
    client = web_ui.app.test_client()

    answer = client.post("/api/normalize", json={"names": ["Küche Lampe"]}).get_json()

    assert answer == {"normalized": ["kuche_lampe"]}
