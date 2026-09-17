"""Renaming a helper writes the new name into the title it is named by.

Part of the rename, not follow-up work: an interface-built helper reads its
entity name from the title of its config entry. Leaving that behind means the
integration keeps supplying the old name, and the next proposal is built from
it - which is how a socket moved to the kitchen kept proposing "Kammer
Lüftungsanlage Steckdose Energie".

Every rename path goes through update_entity, so this is the one place that
covers the list, the bulk run, the device rename and the device swap.
"""

import pytest

from entity_registry import EntityRegistry


class FakeSocket:
    def __init__(self, stored):
        self.stored = stored
        self.sent = []
        self._id = 0
        self._pending = None

    async def _send_message(self, message):
        self.sent.append(message)
        self._id += 1
        self._pending = message
        return self._id

    async def _receive_message(self):
        kind = self._pending["type"]
        if kind == "config/entity_registry/update":
            # Home Assistant answers with the entry as it now stands.
            self.stored = {**self.stored, "name": self._pending.get("name")}
        if kind == "config/entity_registry/list":
            return {"id": self._id, "success": True, "result": [self.stored]}
        if kind == "config/entity_registry/get":
            return {"id": self._id, "success": True, "result": self.stored}
        if kind == "config_entries/update":
            return {"id": self._id, "success": True, "result": {"config_entry": {}}}
        return {"id": self._id, "success": True, "result": {"entity_entry": self.stored}}


class FakeTitles:
    """Stands in for the config entry reader and writer."""

    def __init__(self, entries):
        self._entries = entries
        self.written = []
        self.reloaded = []

    async def entries(self):
        return self._entries

    async def write_title(self, websocket, entry_id, title):
        self.written.append((entry_id, title))
        return {}

    async def reload(self, entry_id):
        self.reloaded.append(entry_id)
        return True


HELPER = {
    "entity_id": "sensor.kuche_kuhlschrank_steckdose_energie",
    "name": None,
    "original_name": "Kammer Lüftungsanlage Steckdose Energie",
    "config_entry_id": "abc",
}
ITS_ENTRY = {"abc": {"entry_id": "abc", "domain": "integration", "title": "Kammer Lüftungsanlage Steckdose Energie"}}


@pytest.fixture
def registry(monkeypatch):
    def build(titles, entities):
        made = EntityRegistry(FakeSocket(HELPER))
        made.entities = entities
        monkeypatch.setattr(EntityRegistry, "supplied_names", titles)
        return made

    return build


@pytest.mark.asyncio
async def test_the_title_follows_the_new_name(registry):
    titles = FakeTitles(ITS_ENTRY)
    made = registry(titles, {"sensor.kuche_kuhlschrank_steckdose_energie": HELPER})

    await made.update_entity("sensor.kuche_kuhlschrank_steckdose_energie", name="Küche Kühlschrank Steckdose Energie")

    assert titles.written == [("abc", "Küche Kühlschrank Steckdose Energie")]
    assert titles.reloaded == ["abc"]


@pytest.mark.asyncio
async def test_a_name_from_an_integration_leaves_the_title_alone(registry):
    """Miele's title is "Miele@home" and names nothing in particular."""
    titles = FakeTitles({"abc": {"entry_id": "abc", "domain": "miele", "title": "Miele@home"}})
    made = registry(titles, {"sensor.kuche_kuhlschrank_steckdose_energie": HELPER})

    await made.update_entity("sensor.kuche_kuhlschrank_steckdose_energie", name="Küche Kühlschrank Status")

    assert titles.written == []


@pytest.mark.asyncio
async def test_an_entry_with_several_entities_is_left_alone(registry):
    titles = FakeTitles(ITS_ENTRY)
    made = registry(
        titles,
        {
            "sensor.kuche_kuhlschrank_steckdose_energie": HELPER,
            "sensor.something_else": {"config_entry_id": "abc"},
        },
    )

    await made.update_entity("sensor.kuche_kuhlschrank_steckdose_energie", name="Küche Kühlschrank Steckdose Energie")

    assert titles.written == []


@pytest.mark.asyncio
async def test_a_path_that_never_listed_the_entities_still_carries_it(registry):
    """The device swap builds its own registry and lists nothing."""
    titles = FakeTitles(ITS_ENTRY)
    made = registry(titles, {})

    await made.update_entity("sensor.kuche_kuhlschrank_steckdose_energie", name="Küche Kühlschrank Steckdose Energie")

    assert titles.written == [("abc", "Küche Kühlschrank Steckdose Energie")]


@pytest.mark.asyncio
async def test_a_title_that_cannot_be_written_does_not_undo_the_rename(registry):
    class Refusing(FakeTitles):
        async def write_title(self, websocket, entry_id, title):
            raise RuntimeError("no")

    titles = Refusing(ITS_ENTRY)
    made = registry(titles, {"sensor.kuche_kuhlschrank_steckdose_energie": HELPER})

    result = await made.update_entity(
        "sensor.kuche_kuhlschrank_steckdose_energie", name="Küche Kühlschrank Steckdose Energie"
    )

    assert result is not None
    assert titles.reloaded == []
