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
    """Two proposals for one ID must not both keep it."""
    result = restructurer.deduplicate_entity_ids(
        [
            ("sensor.plug_energy", "sensor.wohnzimmer_steckdose_energie", "Energie"),
            ("sensor.plug_energy_2", "sensor.wohnzimmer_steckdose_energie", "Energie"),
        ]
    )

    assert [new_id for _, new_id, _ in result] == [
        "sensor.wohnzimmer_steckdose_energie",
        "sensor.wohnzimmer_steckdose_energie_2",
    ]


def test_current_holder_keeps_its_id(restructurer):
    """An entity already holding the target must not be renumbered."""
    result = restructurer.deduplicate_entity_ids(
        [
            ("sensor.plug_energy_2", "sensor.plug_energy", "Energie"),
            ("sensor.plug_energy", "sensor.plug_energy", "Energie"),
        ]
    )

    assigned = {entity_id: new_id for entity_id, new_id, _ in result}
    assert assigned["sensor.plug_energy"] == "sensor.plug_energy"
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
        "Wohnzimmer Steckdose Energie",
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
