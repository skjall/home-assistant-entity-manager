"""Tests that a rename reads the automations once, not once per entity.

Only the configuration API can say whether an automation names an entity, and
it hands out one automation at a time. Reading them again for every entity of a
device is what made a rename of a handful of entities take minutes.
"""

import asyncio
from typing import Any, Dict, List, Optional

from dependency_updater import DependencyUpdater


def _updater(reads: List[str]) -> DependencyUpdater:
    """An updater whose reads are counted and whose writes go nowhere."""
    updater = DependencyUpdater("http://home-assistant.invalid", "token")

    async def fetch(numeric_id: str, session: Optional[Any] = None) -> Dict[str, Any]:
        reads.append(numeric_id)
        return {"id": numeric_id, "alias": f"Automation {numeric_id}", "action": []}

    async def no_helpers(old_entity_id: str, new_entity_id: str) -> Dict[str, List[str]]:
        return {"success": [], "failed": []}

    updater.fetch_automation_config = fetch  # type: ignore[assignment]
    updater.helpers.rename = no_helpers  # type: ignore[assignment]
    return updater


def _states(count: int) -> List[Dict[str, Any]]:
    return [{"entity_id": f"automation.number_{i}", "attributes": {"id": str(i)}} for i in range(count)]


def test_three_renames_read_the_automations_once() -> None:
    reads: List[str] = []
    updater = _updater(reads)
    states = _states(5)

    async def run() -> None:
        for old, new in [("sensor.a", "sensor.b"), ("sensor.c", "sensor.d"), ("sensor.e", "sensor.f")]:
            await updater.update_all_dependencies(old, new, states)

    asyncio.run(run())

    assert sorted(reads) == ["0", "1", "2", "3", "4"]


def test_the_configurations_are_answered_from_what_was_read() -> None:
    """Asking again costs nothing once the job has read them."""
    reads: List[str] = []
    updater = _updater(reads)

    async def run() -> Optional[Dict[str, Any]]:
        await updater.load_automation_configs(_states(3))
        return await updater.get_automation_config("1")

    config = asyncio.run(run())

    assert config is not None and config["id"] == "1"
    assert sorted(reads) == ["0", "1", "2"]


def test_an_automation_that_cannot_be_read_is_remembered_as_such() -> None:
    """One unreadable automation is not the job, and is not read again either."""
    reads: List[str] = []
    updater = _updater(reads)

    async def fetch(numeric_id: str, session: Optional[Any] = None) -> Optional[Dict[str, Any]]:
        reads.append(numeric_id)
        return None

    updater.fetch_automation_config = fetch  # type: ignore[assignment]

    async def run() -> Optional[Dict[str, Any]]:
        await updater.load_automation_configs(_states(2))
        return await updater.get_automation_config("0")

    assert asyncio.run(run()) is None
    assert sorted(reads) == ["0", "1"]


def test_a_written_automation_is_kept_as_it_was_written() -> None:
    """The next entity of the rename has to see the version just written."""
    reads: List[str] = []
    updater = _updater(reads)
    written: List[Dict[str, Any]] = []

    async def fetch(numeric_id: str, session: Optional[Any] = None) -> Dict[str, Any]:
        reads.append(numeric_id)
        # Before the write it names the old id; afterwards the new one.
        entity = "sensor.new" if written else "sensor.old"
        return {"id": numeric_id, "action": [{"entity_id": entity}]}

    async def write(numeric_id: str, config: Dict[str, Any]) -> bool:
        written.append(config)
        return True

    updater.fetch_automation_config = fetch  # type: ignore[assignment]
    updater.update_automation_config = write  # type: ignore[assignment]

    async def run() -> Optional[Dict[str, Any]]:
        await updater.load_automation_configs(_states(1))
        await updater.update_automation_entities("automation.number_0", "0", "sensor.old", "sensor.new")
        return await updater.get_automation_config("0")

    config = asyncio.run(run())

    assert written, "the automation naming the old id is written"
    assert config == {"id": "0", "action": [{"entity_id": "sensor.new"}]}
