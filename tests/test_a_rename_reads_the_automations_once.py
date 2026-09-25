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


def test_an_automation_that_cannot_be_read_is_asked_for_again() -> None:
    """A read that failed says nothing: a timeout and a missing configuration look alike.

    Remembering it as "there is none" answered every later entity of the job
    with nothing, and skipped the automation without saying why.
    """
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
    # Both read for the job, and the one asked about read again.
    assert sorted(reads) == ["0", "0", "1"]


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

    async def write(numeric_id: str, config: Dict[str, Any], session: Optional[Any] = None) -> bool:
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


def test_the_automations_are_read_once_even_where_they_are_written() -> None:
    """The reading that is saved is the one before the write, not the proof after it.

    Every automation names the entity here, so each one is rewritten: one read
    per automation for the job, and one read back per write, which is what says
    the old id is gone. Three renames of five automations therefore read twenty
    times, not sixty.
    """
    reads: List[str] = []
    updater = _updater(reads)
    names: Dict[str, str] = {str(i): "sensor.a" for i in range(5)}

    async def fetch(numeric_id: str, session: Optional[Any] = None) -> Dict[str, Any]:
        reads.append(numeric_id)
        return {"id": numeric_id, "action": [{"entity_id": names[numeric_id]}]}

    async def write(numeric_id: str, config: Dict[str, Any], session: Optional[Any] = None) -> bool:
        names[numeric_id] = config["action"][0]["entity_id"]
        return True

    updater.fetch_automation_config = fetch  # type: ignore[assignment]
    updater.update_automation_config = write  # type: ignore[assignment]

    states = _states(5)

    async def run() -> None:
        for old, new in [("sensor.a", "sensor.b"), ("sensor.b", "sensor.c"), ("sensor.c", "sensor.d")]:
            await updater.update_all_dependencies(old, new, states)

    asyncio.run(run())

    # Five automations read once for the job, and read back after each of the
    # three writes each one takes.
    assert len(reads) == 5 + 5 * 3
    assert sorted(set(names.values())) == ["sensor.d"]


def test_a_write_that_fails_leaves_the_store_as_home_assistant_has_it() -> None:
    """Otherwise the next entity of the same job works from a rename that never happened."""
    reads: List[str] = []
    updater = _updater(reads)

    async def fetch(numeric_id: str, session: Optional[Any] = None) -> Dict[str, Any]:
        reads.append(numeric_id)
        return {"id": numeric_id, "action": [{"entity_id": "sensor.old"}]}

    async def write(numeric_id: str, config: Dict[str, Any], session: Optional[Any] = None) -> bool:
        return False

    updater.fetch_automation_config = fetch  # type: ignore[assignment]
    updater.update_automation_config = write  # type: ignore[assignment]

    async def run() -> Optional[Dict[str, Any]]:
        await updater.load_automation_configs(_states(1))
        assert not await updater.update_automation_entities("automation.number_0", "0", "sensor.old", "sensor.new")
        return await updater.get_automation_config("0")

    assert asyncio.run(run()) == {"id": "0", "action": [{"entity_id": "sensor.old"}]}


def test_a_read_back_that_fails_is_not_kept_as_an_answer() -> None:
    """A read that failed says nothing about the automation.

    Keeping it as "there is none" answered every later entity of the job with
    nothing and skipped the automation without saying why.
    """
    reads: List[str] = []
    updater = _updater(reads)
    attempts: List[str] = []

    async def fetch(numeric_id: str, session: Optional[Any] = None) -> Optional[Dict[str, Any]]:
        reads.append(numeric_id)
        attempts.append(numeric_id)
        if len(attempts) == 2:
            return None
        return {"id": numeric_id, "action": [{"entity_id": "sensor.old"}]}

    async def write(numeric_id: str, config: Dict[str, Any], session: Optional[Any] = None) -> bool:
        return True

    updater.fetch_automation_config = fetch  # type: ignore[assignment]
    updater.update_automation_config = write  # type: ignore[assignment]

    async def run() -> Optional[Dict[str, Any]]:
        await updater.load_automation_configs(_states(1))
        assert not await updater.update_automation_entities("automation.number_0", "0", "sensor.old", "sensor.new")
        return await updater.get_automation_config("0")

    # The third read is the one the next entity of the job asks for: it goes
    # out to Home Assistant again rather than being answered with nothing.
    assert asyncio.run(run()) == {"id": "0", "action": [{"entity_id": "sensor.old"}]}
    assert len(reads) == 3


def test_a_read_back_that_still_names_the_old_id_is_not_kept() -> None:
    """The write did not take, so what came back says nothing about what was asked for.

    Keeping it had the rest of the job build on a version Home Assistant may
    never have held.
    """
    reads: List[str] = []
    updater = _updater(reads)

    async def fetch(numeric_id: str, session: Optional[Any] = None) -> Dict[str, Any]:
        reads.append(numeric_id)
        return {"id": numeric_id, "action": [{"entity_id": "sensor.old"}], "alias": "read " + str(len(reads))}

    async def write(numeric_id: str, config: Dict[str, Any], session: Optional[Any] = None) -> bool:
        return True

    updater.fetch_automation_config = fetch  # type: ignore[assignment]
    updater.update_automation_config = write  # type: ignore[assignment]

    async def run() -> Optional[Dict[str, Any]]:
        await updater.load_automation_configs(_states(1))
        assert not await updater.update_automation_entities("automation.number_0", "0", "sensor.old", "sensor.new")
        return await updater.get_automation_config("0")

    # Asked for again rather than answered out of a reading that proved nothing.
    assert asyncio.run(run())["alias"] == "read 3"


def test_the_write_builds_on_the_reading_the_decision_was_taken_on() -> None:
    """It used to read the automation again, and write what the second reading held."""
    reads: List[str] = []
    updater = _updater(reads)
    written: List[Dict[str, Any]] = []

    async def write(numeric_id: str, config: Dict[str, Any], session: Optional[Any] = None) -> bool:
        written.append(config)
        return True

    updater.update_automation_config = write  # type: ignore[assignment]

    supplied = {"id": "0", "action": [{"entity_id": "sensor.old"}]}

    async def run() -> None:
        await updater.update_automation_entities("automation.number_0", "0", "sensor.old", "sensor.new", supplied)

    asyncio.run(run())

    assert written == [{"id": "0", "action": [{"entity_id": "sensor.new"}]}]
    # The supplied reading is left as the caller holds it.
    assert supplied == {"id": "0", "action": [{"entity_id": "sensor.old"}]}
    # Only the read-back; nothing was asked for to decide with.
    assert reads == ["0"]


def test_two_jobs_starting_together_read_the_automations_once() -> None:
    """Both found nothing kept, both read everything, and the second overwrote the first."""
    reads: List[str] = []
    updater = _updater(reads)
    states = _states(4)

    async def run() -> None:
        await asyncio.gather(
            updater.load_automation_configs(states),
            updater.load_automation_configs(states),
        )

    asyncio.run(run())

    assert sorted(reads) == ["0", "1", "2", "3"]


def test_an_automation_that_only_talks_about_the_entity_is_left_alone() -> None:
    """A description naming the old id had the rename reported as a failure."""
    reads: List[str] = []
    updater = _updater(reads)
    written: List[Dict[str, Any]] = []

    async def fetch(numeric_id: str, session: Optional[Any] = None) -> Dict[str, Any]:
        reads.append(numeric_id)
        return {"id": numeric_id, "description": "Watches sensor.old", "action": [{"entity_id": "sensor.other"}]}

    async def write(numeric_id: str, config: Dict[str, Any], session: Optional[Any] = None) -> bool:
        written.append(config)
        return True

    updater.fetch_automation_config = fetch  # type: ignore[assignment]
    updater.update_automation_config = write  # type: ignore[assignment]

    async def run() -> Dict[str, Any]:
        return await updater.update_all_dependencies("sensor.old", "sensor.new", _states(1))

    results = asyncio.run(run())

    assert not written
    assert results["automations"]["failed"] == []
    assert results["total_failed"] == 0


def test_a_write_that_failed_is_not_answered_from_before_it() -> None:
    """Home Assistant may have applied it and reported an error all the same."""
    reads: List[str] = []
    updater = _updater(reads)

    async def fetch(numeric_id: str, session: Optional[Any] = None) -> Dict[str, Any]:
        reads.append(numeric_id)
        return {"id": numeric_id, "action": [{"entity_id": "sensor.old"}], "alias": "read " + str(len(reads))}

    async def write(numeric_id: str, config: Dict[str, Any], session: Optional[Any] = None) -> bool:
        return False

    updater.fetch_automation_config = fetch  # type: ignore[assignment]
    updater.update_automation_config = write  # type: ignore[assignment]

    async def run() -> Optional[Dict[str, Any]]:
        await updater.load_automation_configs(_states(1))
        assert not await updater.update_automation_entities("automation.number_0", "0", "sensor.old", "sensor.new")
        return await updater.get_automation_config("0")

    assert asyncio.run(run())["alias"] == "read 2"


def test_nothing_read_at_all_is_asked_for_again() -> None:
    """Home Assistant not answering is not an installation without automations."""
    reads: List[str] = []
    updater = _updater(reads)

    async def fetch(numeric_id: str, session: Optional[Any] = None) -> Optional[Dict[str, Any]]:
        reads.append(numeric_id)
        return None

    updater.fetch_automation_config = fetch  # type: ignore[assignment]

    async def run() -> None:
        await updater.load_automation_configs(_states(2))
        await updater.load_automation_configs(_states(2))

    asyncio.run(run())

    assert sorted(reads) == ["0", "0", "1", "1"]


def test_an_automation_nobody_can_read_is_only_unreachable_where_it_refers() -> None:
    """Its friendly name may say the id without the automation using it.

    The configuration cannot be read, so the state is all there is; read as
    text it reported every automation whose name mentions the entity as one
    this add-on cannot reach. Read as a reference it reports the one that has
    the id where an id belongs.
    """
    reads: List[str] = []
    updater = _updater(reads)

    async def unreadable(numeric_id: str, session: Optional[Any] = None) -> None:
        reads.append(numeric_id)
        return None

    updater.fetch_automation_config = unreadable  # type: ignore[assignment]

    states = [
        {
            "entity_id": "automation.talks_about_it",
            "attributes": {"id": "1", "friendly_name": "Watches sensor.old on boot"},
        },
        {
            "entity_id": "automation.uses_it",
            "attributes": {"id": "2", "entity_id": ["sensor.old"]},
        },
        {
            "entity_id": "automation.sensor_old_monitor",
            "attributes": {"id": "3", "friendly_name": "sensor.old_monitor"},
        },
    ]

    async def run() -> Dict[str, Any]:
        return await updater.update_all_dependencies("sensor.old", "sensor.new", states)

    results = asyncio.run(run())

    assert results["automations"]["unreachable"] == ["automation.uses_it"]
    assert results["total_unreachable"] == 1
    assert sorted(results["unreachable_configs"]) == ["automation.sensor_old_monitor", "automation.talks_about_it"]


def test_asking_whether_an_automation_names_an_entity_leaves_it_alone() -> None:
    """It used to be asked by rewriting a copy, which cost a copy every time."""
    updater = _updater([])
    config = {"id": "1", "description": "Watches sensor.old", "action": [{"entity_id": "sensor.old"}]}

    assert updater.names_entity(config, "sensor.old") is True
    assert config == {"id": "1", "description": "Watches sensor.old", "action": [{"entity_id": "sensor.old"}]}
    assert updater.names_entity(config, "sensor.oldest") is False


def test_a_script_that_only_talks_about_the_entity_is_left_alone() -> None:
    """Read as text, a script named after the entity was reported as a failure."""
    reads: List[str] = []
    updater = _updater(reads)
    fetched: List[str] = []

    async def script_config(script_id: str) -> Dict[str, Any]:
        fetched.append(script_id)
        return {"sequence": [{"entity_id": "sensor.other"}]}

    updater.get_script_config = script_config  # type: ignore[assignment]

    states = [
        {"entity_id": "script.talks_about_it", "attributes": {"friendly_name": "Manages sensor.old"}},
        {"entity_id": "script.sensor_old_helper", "attributes": {"friendly_name": "sensor.old_helper"}},
    ]

    async def run() -> Dict[str, Any]:
        return await updater.update_all_dependencies("sensor.old", "sensor.new", states)

    results = asyncio.run(run())

    assert fetched == [], "neither script names the entity, so neither is read"
    assert results["scripts"]["failed"] == []
    assert results["total_failed"] == 0


def test_an_automation_that_cannot_be_read_is_asked_for_once_and_not_again() -> None:
    """A handful of unreadable automations cost a request per rename each."""
    reads: List[str] = []
    updater = _updater(reads)

    async def fetch(numeric_id: str, session: Optional[Any] = None) -> Optional[Dict[str, Any]]:
        reads.append(numeric_id)
        return None

    updater.fetch_automation_config = fetch  # type: ignore[assignment]

    async def run() -> None:
        await updater.load_automation_configs(_states(2))
        for _ in range(3):
            assert await updater.get_automation_config("0") is None

    asyncio.run(run())

    # Both read for the job, and the one asked about read one more time - not
    # once for every entity that asks after that.
    assert sorted(reads) == ["0", "0", "1"]


def test_a_write_is_kept_where_the_job_could_read_nothing() -> None:
    """The reading taken after the write is the one version HA is known to hold."""
    reads: List[str] = []
    updater = _updater(reads)
    written: List[Dict[str, Any]] = []

    async def fetch(numeric_id: str, session: Optional[Any] = None) -> Optional[Dict[str, Any]]:
        reads.append(numeric_id)
        # Nothing to be read for the job; a reading only once it was written.
        if not written:
            return None
        return {"id": numeric_id, "action": [{"entity_id": "sensor.new"}]}

    async def write(numeric_id: str, config: Dict[str, Any], session: Optional[Any] = None) -> bool:
        written.append(config)
        return True

    updater.fetch_automation_config = fetch  # type: ignore[assignment]
    updater.update_automation_config = write  # type: ignore[assignment]

    async def run() -> Optional[Dict[str, Any]]:
        await updater.load_automation_configs(_states(1))
        await updater.update_automation_entities(
            "automation.number_0", "0", "sensor.old", "sensor.new", {"action": [{"entity_id": "sensor.old"}]}
        )
        before = len(reads)
        config = await updater.get_automation_config("0")
        assert len(reads) == before, "the written version answers without reading again"
        return config

    config = asyncio.run(run())

    assert written, "the automation naming the old id is written"
    assert config == {"id": "0", "action": [{"entity_id": "sensor.new"}]}


def test_reading_one_automation_does_not_stand_in_for_the_job_reading_them_all() -> None:
    """The individual read used to be written where the job's reading goes.

    load_automation_configs then saw a reading already there and left every
    other automation unread, and each of those came back as "there is none" and
    was skipped without a word.
    """
    reads: List[str] = []
    updater = _updater(reads)

    async def run() -> None:
        # web_ui repairs one reference before any job has read anything.
        await updater.get_automation_config("0")
        await updater.load_automation_configs(_states(3))
        assert await updater.get_automation_config("2") is not None

    asyncio.run(run())

    # The one asked for first, then all three for the job - and the third
    # answered out of that reading rather than asked for again.
    assert sorted(reads) == ["0", "0", "1", "2"]


def test_a_write_is_not_overwritten_by_a_reading_that_was_already_out() -> None:
    """A read that started before the write stored what Home Assistant had then."""
    reads: List[str] = []
    updater = _updater(reads)
    written: List[Dict[str, Any]] = []
    let_it_finish = asyncio.Event()

    async def fetch(numeric_id: str, session: Optional[Any] = None) -> Optional[Dict[str, Any]]:
        reads.append(numeric_id)
        if not written:
            # The reading the slow request comes back with: taken before the
            # write, it still names the old id.
            await let_it_finish.wait()
            return {"id": numeric_id, "action": [{"entity_id": "sensor.old"}]}
        return {"id": numeric_id, "action": [{"entity_id": "sensor.new"}]}

    async def write(numeric_id: str, config: Dict[str, Any], session: Optional[Any] = None) -> bool:
        written.append(config)
        return True

    updater.fetch_automation_config = fetch  # type: ignore[assignment]
    updater.update_automation_config = write  # type: ignore[assignment]

    async def run() -> Optional[Dict[str, Any]]:
        slow = asyncio.ensure_future(updater.read_automation_config("0"))
        await asyncio.sleep(0)
        await updater.update_automation_entities(
            "automation.number_0", "0", "sensor.old", "sensor.new", {"action": [{"entity_id": "sensor.old"}]}
        )
        let_it_finish.set()
        await slow
        return await updater.read_automation_config("0")

    config = asyncio.run(run())

    assert written, "the automation naming the old id is written"
    assert config == {"id": "0", "action": [{"entity_id": "sensor.new"}]}
