import asyncio
import json
import logging
from typing import Any, Callable, Dict, Optional

import websockets
from websockets.client import WebSocketClientProtocol

logger = logging.getLogger(__name__)


class HomeAssistantWebSocket:
    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self.websocket: Optional[WebSocketClientProtocol] = None
        self.message_id = 1
        self.pending_messages: Dict[int, asyncio.Future] = {}

    async def connect(self):
        # Erhöhe das max_size Limit auf 10MB für große Entity Registries
        self.websocket = await websockets.connect(self.url, max_size=10 * 1024 * 1024)  # 10MB statt default 1MB

        auth_msg = await self._receive_message()
        if auth_msg["type"] != "auth_required":
            raise Exception(f"Unexpected message type: {auth_msg['type']}")

        await self._send_message({"type": "auth", "access_token": self.token})

        auth_result = await self._receive_message()
        if auth_result["type"] != "auth_ok":
            raise Exception(f"Authentication failed: {auth_result}")

        logger.info("Successfully connected to Home Assistant WebSocket")

    async def disconnect(self):
        if self.websocket:
            await self.websocket.close()

    async def _send_message(self, message: Dict[str, Any]) -> int:
        if "id" not in message and message["type"] != "auth":
            message["id"] = self.message_id
            self.message_id += 1

        await self.websocket.send(json.dumps(message))
        return message.get("id", 0)

    async def _receive_message(self) -> Dict[str, Any]:
        message = await self.websocket.recv()
        return json.loads(message)

    async def send_command(self, message: Dict[str, Any], timeout: float = 30.0) -> Any:
        """Send one command and give back its result.

        Home Assistant answers with events and other traffic in between, so the
        reply is the one carrying this message's id. Everything else is read
        past - which is why this waits with a clock: on a socket that also
        carries a subscription the traffic never stops, and a reply that never
        comes would otherwise be waited for until the process is killed.
        """
        msg_id = await self._send_message(dict(message))

        async def the_answer() -> Dict[str, Any]:
            response = await self._receive_message()
            while response.get("id") != msg_id:
                response = await self._receive_message()
            return response

        try:
            response = await asyncio.wait_for(the_answer(), timeout)
        except asyncio.TimeoutError as expired:
            raise TimeoutError(f"{message['type']} was not answered within {timeout:g}s") from expired

        if not response.get("success"):
            raise Exception(f"{message['type']} failed: {response.get('error') or response}")

        return response.get("result")

    async def call_service(self, domain: str, service: str, data: Optional[Dict] = None) -> Dict[str, Any]:
        message = {
            "type": "call_service",
            "domain": domain,
            "service": service,
        }
        if data:
            message["service_data"] = data

        msg_id = await self._send_message(message)

        response = await self._receive_message()
        while response.get("id") != msg_id:
            response = await self._receive_message()

        if not response.get("success"):
            raise Exception(f"Service call failed: {response}")

        return response.get("result", {})

    async def get_states(self) -> list:
        msg_id = await self._send_message({"type": "get_states"})

        response = await self._receive_message()
        while response.get("id") != msg_id:
            response = await self._receive_message()

        if not response.get("success"):
            raise Exception(f"Failed to get states: {response}")

        return response.get("result", [])

    async def get_config(self) -> Dict[str, Any]:
        msg_id = await self._send_message({"type": "get_config"})

        response = await self._receive_message()
        while response.get("id") != msg_id:
            response = await self._receive_message()

        if not response.get("success"):
            raise Exception(f"Failed to get config: {response}")

        return response.get("result", {})

    async def subscribe_events(self, event_type: str, callback: Callable) -> int:
        msg_id = await self._send_message({"type": "subscribe_events", "event_type": event_type})

        response = await self._receive_message()
        while response.get("id") != msg_id:
            response = await self._receive_message()

        if not response.get("success"):
            raise Exception(f"Failed to subscribe to events: {response}")

        return msg_id

    async def get_entity_registry_entry(self, entity_id: str) -> Dict[str, Any]:
        msg_id = await self._send_message({"type": "config/entity_registry/get", "entity_id": entity_id})

        response = await self._receive_message()
        while response.get("id") != msg_id:
            response = await self._receive_message()

        if not response.get("success"):
            raise Exception(f"Failed to get entity registry entry: {response}")

        return response.get("result", {})
