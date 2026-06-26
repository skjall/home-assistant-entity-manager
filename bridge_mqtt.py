#!/usr/bin/env python3
"""
MQTT Bridge - Verbindung zu Zigbee2MQTT über den HA-MQTT-Broker.

Z2M bietet eine Request/Response-API über MQTT:
- Request:  <base>/bridge/request/<command>   (z.B. device/rename, device/remove)
- Response: <base>/bridge/response/<command>

Antworten werden über ein selbst vergebenes `transaction`-Feld dem Request
zugeordnet. paho läuft in einem eigenen Thread (loop_start); das Warten auf die
Antwort erfolgt über ein threading.Event, das im Executor abgewartet wird, damit
der Flask-Eventloop nicht blockiert.

Wichtige Z2M-Verträge:
- rename: {"from": <ieee>, "to": <name>, "homeassistant_rename": false}
  -> false hält HAs entity_id stabil (die verwaltet der Entity Manager); nur Z2Ms
     friendly_name wird angeglichen.
- remove: {"id": <ieee>, "force": <bool>, "block": true}
  -> block:true verhindert das erneute Beitreten (Rejoin/Re-Population).
"""

import asyncio
import json
import logging
import threading
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt
from paho.mqtt.client import CallbackAPIVersion

logger = logging.getLogger(__name__)


class MqttBridge:
    """Langlebige MQTT-Verbindung mit Request/Response-Korrelation für Z2M."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        ssl: bool = False,
        base_topic: str = "zigbee2mqtt",
    ):
        self.host = host
        self.port = port
        self.base_topic = (base_topic or "zigbee2mqtt").rstrip("/")
        self._client = mqtt.Client(CallbackAPIVersion.VERSION2)
        if username:
            self._client.username_pw_set(username, password)
        if ssl:
            self._client.tls_set()
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._txn = 0
        self._connected = threading.Event()

    # --- Verbindung ---------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> bool:
        """Verbindet und wartet auf das CONNACK. True bei Erfolg."""
        try:
            self._client.connect(self.host, self.port)
            self._client.loop_start()
            return self._connected.wait(timeout)
        except Exception as e:  # noqa: BLE001 - Verbindungsfehler nicht fatal
            logger.error("MQTT connect to %s:%s failed: %s", self.host, self.port, e)
            return False

    def disconnect(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            logger.error("MQTT connection refused: %s", reason_code)
            return
        client.subscribe(f"{self.base_topic}/bridge/response/#")
        self._connected.set()
        logger.info("MQTT connected to %s:%s (base topic '%s')", self.host, self.port, self.base_topic)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:  # noqa: BLE001 - Nicht-JSON ignorieren
            return
        txn = payload.get("transaction")
        if not txn:
            return
        with self._lock:
            slot = self._pending.get(txn)
            if slot is not None:
                slot["result"] = payload
                slot["event"].set()

    # --- Request/Response ---------------------------------------------------

    def _next_transaction(self) -> str:
        with self._lock:
            self._txn += 1
            return f"em_{self._txn}"

    async def _request(self, command: str, payload: Dict[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
        if not self._connected.is_set():
            return {"status": "error", "error": "MQTT not connected"}
        txn = self._next_transaction()
        body = dict(payload, transaction=txn)
        event = threading.Event()
        with self._lock:
            self._pending[txn] = {"event": event, "result": None}
        topic = f"{self.base_topic}/bridge/request/{command}"
        self._client.publish(topic, json.dumps(body))
        loop = asyncio.get_running_loop()
        got = await loop.run_in_executor(None, event.wait, timeout)
        with self._lock:
            slot = self._pending.pop(txn, None)
        if not got or slot is None or slot.get("result") is None:
            return {"status": "error", "error": f"MQTT request timeout ({command})"}
        return slot["result"]

    async def rename_device(self, ieee: str, new_name: str) -> Dict[str, Any]:
        """Benennt ein Z2M-Gerät um (friendly_name), ohne HAs entity_id zu ändern."""
        return await self._request(
            "device/rename",
            {"from": ieee, "to": new_name, "homeassistant_rename": False},
        )

    async def remove_device(self, ieee: str, force: bool = False, block: bool = True) -> Dict[str, Any]:
        """Entfernt ein Z2M-Gerät; block=True verhindert Rejoin."""
        return await self._request(
            "device/remove",
            {"id": ieee, "force": force, "block": block},
        )
