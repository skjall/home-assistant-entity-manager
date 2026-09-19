"""Deleting what is already deleted is the state that was asked for.

A row that stays on screen after its entity is gone leaves the user pressing
delete a second time. Home Assistant then answers "not_found", and reporting
that as a failure keeps the row exactly where it is: the disagreement repeats
instead of ending. Gone is gone, and the answer has to say so.
"""

import pytest

from entity_registry import EntityRegistry


class Socket:
    """Answers a removal with whatever the test asked for."""

    def __init__(self, answer):
        self.answer = answer
        self.sent = []
        self._id = 0

    async def _send_message(self, message):
        self.sent.append(message)
        self._id += 1
        return self._id

    async def _receive_message(self):
        return {"id": self._id, **self.answer}


@pytest.mark.asyncio
async def test_an_entity_already_gone_counts_as_removed():
    socket = Socket({"type": "result", "success": False, "error": {"code": "not_found", "message": "Entity not found"}})
    registry = EntityRegistry(socket)

    result = await registry.remove_entity("sensor.one_that_is_gone")

    assert result["success"] is True
    assert result["already_gone"] is True
    assert result["entity_id"] == "sensor.one_that_is_gone"


@pytest.mark.asyncio
async def test_a_removal_that_worked_does_not_claim_it_was_already_gone():
    socket = Socket({"type": "result", "success": True, "result": None})
    registry = EntityRegistry(socket)

    result = await registry.remove_entity("sensor.one_that_was_here")

    assert result["success"] is True
    assert "already_gone" not in result


@pytest.mark.asyncio
async def test_any_other_refusal_is_still_a_failure():
    socket = Socket(
        {"type": "result", "success": False, "error": {"code": "unauthorized", "message": "not allowed"}}
    )
    registry = EntityRegistry(socket)

    with pytest.raises(Exception) as refused:
        await registry.remove_entity("sensor.one_that_stays")

    assert "sensor.one_that_stays" in str(refused.value)
