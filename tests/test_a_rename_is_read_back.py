"""A rename counts as done when the registry says so, not when it was accepted.

Home Assistant acknowledges a write and then stores what it likes: it numbers a
colliding entity_id, cuts a name that is too long, and keeps whatever string it
was handed. Without reading the entry back, the list shows the wish while the
registry holds something else - which is how a coffee machine read
"Calc'n'Clean in 5 Tassen" in the interface and "Calc&#x27;n&#x27;Clean in 5
Tassen" in Home Assistant until the next reload.
"""

import pytest

from entity_registry import EntityRegistry


class FakeSocket:
    """Answers an update with success, and a get with what is stored."""

    def __init__(self, stored, *, get_fails=False):
        self.stored = stored
        self.get_fails = get_fails
        self.sent = []
        self._next_id = 0
        self._pending = None

    async def _send_message(self, message):
        self.sent.append(message)
        self._next_id += 1
        self._pending = message
        return self._next_id

    async def _receive_message(self):
        message = self._pending
        if message["type"] == "config/entity_registry/get":
            if self.get_fails:
                return {"id": self._next_id, "success": False, "error": {"message": "not found"}}
            return {"id": self._next_id, "success": True, "result": self.stored}
        return {"id": self._next_id, "success": True, "result": {"entity_entry": self.stored}}


@pytest.fixture
def registry():
    return lambda socket: EntityRegistry(socket)


@pytest.mark.asyncio
async def test_the_entry_is_read_back_after_the_write(registry):
    socket = FakeSocket({"entity_id": "sensor.new", "name": "Küche Herd"})

    await registry(socket).update_entity("sensor.old", new_entity_id="sensor.new", name="Küche Herd")

    assert [message["type"] for message in socket.sent] == [
        "config/entity_registry/update",
        "config/entity_registry/get",
    ]


@pytest.mark.asyncio
async def test_what_was_read_back_is_what_is_returned(registry):
    socket = FakeSocket({"entity_id": "sensor.new", "name": "Küche Herd"})

    result = await registry(socket).update_entity("sensor.old", new_entity_id="sensor.new", name="Küche Herd")

    assert result["entity_entry"] == {"entity_id": "sensor.new", "name": "Küche Herd"}


@pytest.mark.asyncio
async def test_a_name_stored_differently_is_a_failure(registry):
    """Anything else would report the wish as the outcome."""
    socket = FakeSocket({"entity_id": "sensor.new", "name": "Calc&#x27;n&#x27;Clean"})

    with pytest.raises(Exception, match="stored something else"):
        await registry(socket).update_entity("sensor.old", new_entity_id="sensor.new", name="Calc'n'Clean")


@pytest.mark.asyncio
async def test_an_id_home_assistant_numbered_is_a_failure(registry):
    socket = FakeSocket({"entity_id": "sensor.new_2", "name": "Küche Herd"})

    with pytest.raises(Exception, match="stored something else"):
        await registry(socket).update_entity("sensor.old", new_entity_id="sensor.new", name="Küche Herd")


@pytest.mark.asyncio
async def test_clearing_a_name_reads_back_as_nothing(registry):
    """An empty name drops the override, and None is that write landing."""
    socket = FakeSocket({"entity_id": "sensor.old", "name": None})

    result = await registry(socket).update_entity("sensor.old", name="")

    assert result["entity_entry"]["name"] is None


@pytest.mark.asyncio
async def test_a_read_that_fails_does_not_undo_the_write(registry):
    """The name is already written; losing the confirmation must not look worse."""
    socket = FakeSocket({"entity_id": "sensor.new", "name": "Küche Herd"}, get_fails=True)

    result = await registry(socket).update_entity("sensor.old", new_entity_id="sensor.new", name="Küche Herd")

    assert result is not None
