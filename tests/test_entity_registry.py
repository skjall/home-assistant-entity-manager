"""Tests for entity-registry update messages."""

import asyncio

from entity_registry import EntityRegistry


class MockWebSocket:
    """Capture entity-registry requests and return successful responses."""

    def __init__(self) -> None:
        """Initialize the captured-message list."""
        self.messages = []

    async def _send_message(self, message: dict) -> int:
        """Capture a WebSocket message and return its ID."""
        self.messages.append(message)
        return 1

    async def _receive_message(self) -> dict:
        """Return a successful entity-registry response."""
        return {"id": 1, "success": True, "result": {}}


def test_empty_name_clears_registry_override() -> None:
    """An empty template result clears an existing registry name override.

    Home Assistant only drops the override on ``null``; an empty string is
    stored verbatim and keeps shadowing ``original_name``.
    """
    websocket = MockWebSocket()

    asyncio.run(EntityRegistry(websocket).update_entity("light.old", new_entity_id="light.new", name=""))

    assert websocket.messages == [
        {
            "type": "config/entity_registry/update",
            "entity_id": "light.old",
            "new_entity_id": "light.new",
            "name": None,
        }
    ]
