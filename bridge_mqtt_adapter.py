#!/usr/bin/env python3
"""
MQTT Z2M Adapter - nativer Rename/Remove für Zigbee2MQTT-Geräte über MQTT.

Greift nur für ECHTE Z2M-Geräte (Identifier ["mqtt", "zigbee2mqtt_0x<ieee>"]).
Andere MQTT-Geräte (z.B. ["mqtt", "[301DEEE4]"]) und die Z2M-Bridge selbst werden
nicht erfasst - die fallen auf den RegistryAdapter zurück.
"""

import logging
from typing import Any, Dict, List, Optional, Set

from integration_bridge import BridgeResult, IntegrationBridgeAdapter, extract_z2m_ieee

logger = logging.getLogger(__name__)


class MqttZ2MAdapter(IntegrationBridgeAdapter):
    """Z2M-Adapter: nativer friendly_name-Rename und natives Entfernen über MQTT."""

    integration_key = "mqtt"

    def __init__(self, mqtt_bridge: Any, device_registry: Any):
        self.mqtt = mqtt_bridge
        self.device_registry = device_registry

    def matches(self, integrations: List[str], device_data: Optional[Dict[str, Any]] = None) -> bool:
        # Nur echte Z2M-Geräte (IEEE aus dem Identifier ableitbar).
        return device_data is not None and extract_z2m_ieee(device_data) is not None

    async def rename_native(self, device_data: Dict[str, Any], new_name: str) -> BridgeResult:
        ieee = extract_z2m_ieee(device_data)
        if not ieee:
            return BridgeResult(success=True, native_supported=False, detail="Not a Z2M device")
        resp = await self.mqtt.rename_device(ieee, new_name)
        if resp.get("status") == "ok":
            return BridgeResult(success=True, native_supported=True, detail=f"Z2M renamed to '{new_name}'")
        return BridgeResult(success=False, native_supported=True, error=resp.get("error") or str(resp))

    async def remove_native(self, device_data: Dict[str, Any], *, force: bool = False) -> BridgeResult:
        ieee = extract_z2m_ieee(device_data)
        if not ieee:
            return BridgeResult(success=True, native_supported=False, detail="Not a Z2M device")
        resp = await self.mqtt.remove_device(ieee, force=force, block=True)
        if resp.get("status") == "ok":
            return BridgeResult(success=True, native_supported=True, detail="Z2M device removed (block=true)")
        return BridgeResult(success=False, native_supported=True, error=resp.get("error") or str(resp))

    @property
    def capabilities(self) -> Set[str]:
        return {"rename", "remove"}
