"""A rename is only carried along if the write can be read back.

Home Assistant answering "ok" is not proof that an automation no longer names
the old entity; a rename that trusted the answer once left an automation broken
and reported success. These tests pin the read-back and what is reported when it
fails or when an automation cannot be reached at all.
"""

import asyncio

import pytest

from dependency_updater import DependencyUpdater


class Recorded(DependencyUpdater):
    """A updater whose reads and writes are lists instead of a Home Assistant."""

    def __init__(self, reads, writes_ok=True):
        super().__init__("http://ha.invalid", "token")
        self.reads = list(reads)
        self.writes_ok = writes_ok
        self.written = []

    async def get_automation_config(self, automation_numeric_id):
        return self.reads.pop(0)

    async def update_automation_config(self, automation_numeric_id, config):
        self.written.append(config)
        return self.writes_ok


def naming(entity_id):
    return {"id": "1", "alias": "Test", "action": [{"entity_id": entity_id}]}


def test_a_write_that_reads_back_changed_counts_as_carried():
    updater = Recorded([naming("sensor.old"), naming("sensor.new")])

    carried = asyncio.run(updater.update_automation_entities("automation.a", "1", "sensor.old", "sensor.new"))

    assert carried is True
    assert updater.written


def test_a_write_that_reads_back_unchanged_is_a_failure():
    """The old id is still there, so the automation is broken, not updated."""
    updater = Recorded([naming("sensor.old"), naming("sensor.old")])

    carried = asyncio.run(updater.update_automation_entities("automation.a", "1", "sensor.old", "sensor.new"))

    assert carried is False


def test_a_config_that_cannot_be_read_back_is_a_failure():
    updater = Recorded([naming("sensor.old"), None])

    carried = asyncio.run(updater.update_automation_entities("automation.a", "1", "sensor.old", "sensor.new"))

    assert carried is False


def test_an_automation_outside_automations_yaml_is_reported_to_the_user():
    """Nothing here can rewrite it, so it has to be said rather than logged."""

    class OutOfReach(DependencyUpdater):
        async def get_states(self):
            return []

        async def get_automation_config(self, automation_numeric_id):
            return None

    updater = OutOfReach("http://ha.invalid", "token")
    states = [
        {
            "entity_id": "automation.packaged",
            "attributes": {"id": "7", "friendly_name": "Packaged"},
        }
    ]

    async def run():
        # The scanner asks Home Assistant what names the entity; here the one
        # automation does, and its config cannot be fetched.
        updater.find_automations_using_entity = lambda entity_id, all_states: [
            {"entity_id": "automation.packaged", "numeric_id": "7", "state": states[0]}
        ]
        return await updater.update_all_dependencies("sensor.old", "sensor.new", states)

    results = asyncio.run(run())

    assert results["automations"]["unreachable"] == []
    assert results["unreachable_configs"] == ["automation.packaged"]


@pytest.mark.parametrize("names_old", [True, False])
def test_unreachable_is_only_a_warning_when_the_old_id_is_actually_there(names_old):
    class OutOfReach(DependencyUpdater):
        async def get_states(self):
            return []

        async def get_automation_config(self, automation_numeric_id):
            return None

    updater = OutOfReach("http://ha.invalid", "token")
    state = {
        "entity_id": "automation.packaged",
        "attributes": {
            "id": "7",
            "friendly_name": "Packaged",
            "entity_id": ["sensor.old"] if names_old else ["sensor.other"],
        },
    }
    updater.find_automations_using_entity = lambda entity_id, all_states: [
        {"entity_id": "automation.packaged", "numeric_id": "7", "state": state}
    ]

    results = asyncio.run(updater.update_all_dependencies("sensor.old", "sensor.new", [state]))

    assert bool(results["automations"]["unreachable"]) is names_old
    assert results["total_unreachable"] == (1 if names_old else 0)
