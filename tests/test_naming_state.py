"""Knowing which names came from here, and noticing when one is changed.

The point of the state file is a distinction nothing else in the registry can
make: our own name against one somebody typed in Home Assistant afterwards.
These tests pin that distinction down, including the two ways it could go
wrong - claiming a name we never wrote, and calling a changed name our own.
"""

import asyncio
import json

import pytest

from entity_registry import EntityRegistry
from naming_state import ENTITY_MANAGER, HA_UI, INTEGRATION, UNKNOWN, NamingState


@pytest.fixture
def state(tmp_path):
    return NamingState(str(tmp_path / "naming_state.json"))


def test_an_entity_we_never_named_belongs_to_its_integration(state):
    entry = {"id": "abc", "entity_id": "sensor.x", "name": None}

    assert state.ownership(entry)["name_owner"] == INTEGRATION
    assert state.ownership(entry)["drift"] is False


def test_a_name_from_before_this_file_is_owned_by_nobody_we_know(state):
    """A name override with no entry of ours could be from anywhere."""
    entry = {"id": "abc", "entity_id": "sensor.x", "name": "Küche Temperatur"}

    assert state.ownership(entry)["name_owner"] == UNKNOWN


def test_a_name_we_wrote_stays_ours(state):
    state.record("abc", applied_name="Küche Temperatur", applied_entity_id="sensor.kuche_temperatur")

    owned = state.ownership({"id": "abc", "name": "Küche Temperatur"})

    assert owned["name_owner"] == ENTITY_MANAGER
    assert owned["drift"] is False


def test_a_name_changed_elsewhere_is_reported_as_drift(state):
    state.record("abc", applied_name="Küche Temperatur", applied_entity_id="sensor.kuche_temperatur")

    owned = state.ownership({"id": "abc", "name": "Kühlschrank"})

    assert owned["drift"] is True
    assert owned["name_owner"] == HA_UI
    assert owned["applied_name"] == "Küche Temperatur"


def test_changed_templates_are_not_drift(state):
    """A different proposal after a template change says nothing about who
    owns the name, so it must not look like somebody renamed the entity."""
    state.record("abc", applied_name="Küche Temperatur", applied_entity_id="sensor.x", template_hash="old")

    owned = state.ownership({"id": "abc", "name": "Küche Temperatur"}, template_hash="new")

    assert owned["template_changed"] is True
    assert owned["drift"] is False
    assert owned["name_owner"] == ENTITY_MANAGER


def test_the_type_part_comes_back_without_parsing_the_name(state):
    state.record(
        "abc",
        applied_name="Wohnzimmer Deko Verbindungsqualität",
        applied_entity_id="sensor.x",
        base_entity="Verbindungsqualität",
        won_by="rule:user",
        rule_id="r_01",
    )

    assert state.ownership({"id": "abc", "name": "Wohnzimmer Deko Verbindungsqualität"})["base_entity"] == (
        "Verbindungsqualität"
    )


def test_what_was_written_survives_a_restart(state, tmp_path):
    state.record("abc", applied_name="Küche Temperatur", applied_entity_id="sensor.x")

    again = NamingState(str(tmp_path / "naming_state.json"))

    assert again.get("abc")["applied_name"] == "Küche Temperatur"
    assert json.loads((tmp_path / "naming_state.json").read_text())["version"] == 1


def test_an_unreadable_file_costs_provenance_but_not_the_add_on(tmp_path):
    broken = tmp_path / "naming_state.json"
    broken.write_text("{ this is not json")

    assert NamingState(str(broken)).count() == 0


def test_forgetting_hands_the_name_back(state):
    state.record("abc", applied_name="Küche Temperatur", applied_entity_id="sensor.x")

    assert state.forget("abc") is True
    assert state.ownership({"id": "abc", "name": "Küche Temperatur"})["name_owner"] == UNKNOWN


class MockWebSocket:
    """Answer a registry update the way Home Assistant does."""

    def __init__(self, entry=None):
        self.messages = []
        self.entry = entry or {"id": "abc", "entity_id": "sensor.new", "name": "Küche Temperatur"}

    async def _send_message(self, message):
        self.messages.append(message)
        return 1

    async def _receive_message(self):
        return {"id": 1, "success": True, "result": {"entity_entry": self.entry}}


def test_every_write_notes_itself(state, monkeypatch):
    """Whichever path renames an entity, it goes through update_entity - so
    noting it there is what keeps a new path from forgetting."""
    monkeypatch.setattr(EntityRegistry, "naming_state", state)
    registry = EntityRegistry(MockWebSocket())

    asyncio.run(registry.rename_entity("sensor.old", "sensor.new", "Küche Temperatur"))

    assert state.get("abc")["applied_name"] == "Küche Temperatur"
    assert state.get("abc")["applied_entity_id"] == "sensor.new"


def test_a_write_carries_where_the_name_came_from(state, monkeypatch):
    monkeypatch.setattr(EntityRegistry, "naming_state", state)
    registry = EntityRegistry(MockWebSocket())

    asyncio.run(
        registry.rename_entity(
            "sensor.old",
            "sensor.new",
            "Küche Temperatur",
            provenance={"base_entity": "Temperatur", "won_by": "rule:user", "rule_id": "r_01", "template_hash": "t1"},
        )
    )

    assert state.get("abc")["base_entity"] == "Temperatur"
    assert state.get("abc")["won_by"] == "rule:user"
    assert state.get("abc")["rule_id"] == "r_01"


def test_clearing_a_name_gives_it_back_to_the_integration(state, monkeypatch):
    monkeypatch.setattr(EntityRegistry, "naming_state", state)
    state.record("abc", applied_name="Küche Temperatur", applied_entity_id="sensor.new")
    registry = EntityRegistry(MockWebSocket({"id": "abc", "entity_id": "sensor.new", "name": None}))

    asyncio.run(registry.update_entity("sensor.new", name=""))

    assert state.get("abc") is None


def test_a_write_that_leaves_the_name_alone_claims_nothing(state, monkeypatch):
    """Enabling an entity or setting a label is not naming it."""
    monkeypatch.setattr(EntityRegistry, "naming_state", state)
    registry = EntityRegistry(MockWebSocket())

    asyncio.run(registry.update_entity("sensor.new", enable=True))

    assert state.count() == 0


def test_a_broken_state_file_does_not_break_a_rename(tmp_path, monkeypatch):
    class Refusing:
        def record(self, *args, **kwargs):
            raise OSError("disk full")

        def forget(self, *args, **kwargs):
            raise OSError("disk full")

    monkeypatch.setattr(EntityRegistry, "naming_state", Refusing())
    registry = EntityRegistry(MockWebSocket())

    asyncio.run(registry.rename_entity("sensor.old", "sensor.new", "Küche Temperatur"))
