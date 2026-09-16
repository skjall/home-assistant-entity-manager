"""Tests for entity-registry update messages."""

import asyncio

from entity_registry import EntityRegistry


class MockWebSocket:
    """Capture entity-registry requests and return successful responses."""

    def __init__(self, entry: dict = None) -> None:
        """Initialize the captured-message list."""
        self.messages = []
        self.entry = entry if entry is not None else {}
        self.asked = ""

    async def _send_message(self, message: dict) -> int:
        """Capture a WebSocket message and return its ID."""
        self.messages.append(message)
        self.asked = message["type"]
        return 1

    async def _receive_message(self) -> dict:
        """Return a successful entity-registry response.

        A rename is read back afterwards, and a get answers with the entry
        itself rather than the update's wrapper.
        """
        if self.asked == "config/entity_registry/get":
            return {"id": 1, "success": True, "result": self.entry}
        return {"id": 1, "success": True, "result": {"entity_entry": self.entry}}


def test_empty_name_clears_registry_override() -> None:
    """An empty template result clears an existing registry name override.

    Home Assistant only drops the override on ``null``; an empty string is
    stored verbatim and keeps shadowing ``original_name``.
    """
    websocket = MockWebSocket({"entity_id": "light.new", "name": None})

    asyncio.run(EntityRegistry(websocket).update_entity("light.old", new_entity_id="light.new", name=""))

    assert websocket.messages[0] == {
        "type": "config/entity_registry/update",
        "entity_id": "light.old",
        "new_entity_id": "light.new",
        "name": None,
    }
    # And then it asks what was stored, because an acknowledged write is not
    # yet a write that landed as asked.
    assert websocket.messages[1] == {"type": "config/entity_registry/get", "entity_id": "light.new"}
