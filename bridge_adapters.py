#!/usr/bin/env python3
"""
Bridge Adapters - konkrete Integrations-Adapter für die IntegrationBridge.

- RegistryAdapter: catch-all (u.a. ZHA, HomeKit). Name wird in HA verwaltet
  (kein nativer Rename nötig); Entfernen via config/device_registry/remove_config_entry.
- MatterAdapter: wie Registry, aber explizit für Matter (kein Eigenname; Entfernen
  über remove_config_entry, was das Decommissioning auslöst).
- MqttZ2MAdapter: wird in einer späteren Ausbaustufe ergänzt (nativer Z2M-Rename
  und -Remove über MQTT).
"""

import logging
from typing import Any, Dict, List, Optional, Set

from device_registry import DeviceRegistry
from integration_bridge import (
    BridgeResult,
    IntegrationBridge,
    IntegrationBridgeAdapter,
    extract_config_entry_id,
)

logger = logging.getLogger(__name__)


class RegistryAdapter(IntegrationBridgeAdapter):
    """Catch-all-Adapter für Integrationen, die vollständig in HA verwaltet werden.

    Der Anzeigename wird über die HA-Registry gesetzt (kein nativer Rename nötig).
    Entfernen erfolgt über remove_config_entry - denselben Pfad, den auch das
    HA-Frontend nutzt.
    """

    integration_key = "registry"

    def __init__(self, device_registry: DeviceRegistry):
        self.device_registry = device_registry

    def matches(self, integrations: List[str]) -> bool:
        # Catch-all: zuständig, wenn kein spezifischerer Adapter gegriffen hat.
        return True

    async def rename_native(self, device_data: Dict[str, Any], new_name: str) -> BridgeResult:
        # Für diese Integrationen ist der HA-Registry-Name maßgeblich; der Aufrufer
        # setzt ihn über device_registry.rename_device. Hier nichts zusätzlich zu tun.
        return BridgeResult(
            success=True,
            native_supported=False,
            detail="HA registry name is authoritative for this integration",
        )

    async def remove_native(self, device_data: Dict[str, Any], *, force: bool = False) -> BridgeResult:
        # Snapshots nutzen "device_id", rohe Registry-Einträge "id" -> beide akzeptieren.
        device_id = device_data.get("device_id") or device_data.get("id")
        config_entry_id = extract_config_entry_id(device_data)
        if not device_id:
            return BridgeResult(
                success=False,
                native_supported=True,
                error="No device_id in device data; cannot remove via registry",
            )
        if not config_entry_id:
            return BridgeResult(
                success=False,
                native_supported=True,
                error="No config_entry found for device; cannot remove via registry",
            )
        try:
            await self.device_registry.remove_config_entry(device_id, config_entry_id)
            return BridgeResult(success=True, native_supported=True, detail="Removed via remove_config_entry")
        except Exception as e:  # noqa: BLE001 - HA-Fehlermeldung an den Aufrufer durchreichen
            return BridgeResult(success=False, native_supported=True, error=str(e))

    @property
    def capabilities(self) -> Set[str]:
        return {"remove"}


class MatterAdapter(RegistryAdapter):
    """Matter: kein Eigenname-Problem; Entfernen löst das Decommissioning aus.

    Matter kann das Entfernen ablehnen ("rejected by integration"), wenn das Gerät
    noch erreichbar/Teil des Fabrics ist - das wird als BridgeResult mit Fehler
    zurückgegeben (vom remove_native der Basis), nicht als Crash.
    """

    integration_key = "matter"

    def matches(self, integrations: List[str]) -> bool:
        return "matter" in integrations


def build_bridge(device_registry: DeviceRegistry, mqtt_bridge: Optional[Any] = None) -> IntegrationBridge:
    """Baut die IntegrationBridge mit den verfügbaren Adaptern.

    Reihenfolge = Priorität; der RegistryAdapter ist catch-all und steht zuletzt.
    Der MQTT/Z2M-Adapter wird nur eingehängt, wenn eine MQTT-Bridge verfügbar ist
    (Ausbaustufe MQTT).
    """
    adapters: List[IntegrationBridgeAdapter] = []

    if mqtt_bridge is not None:
        try:
            from bridge_mqtt_adapter import MqttZ2MAdapter  # lazy: nur wenn MQTT aktiv

            adapters.append(MqttZ2MAdapter(mqtt_bridge, device_registry))
        except ImportError:
            logger.warning("MQTT bridge provided but MqttZ2MAdapter not available; skipping Z2M support")

    adapters.append(MatterAdapter(device_registry))
    adapters.append(RegistryAdapter(device_registry))  # catch-all, muss zuletzt stehen
    return IntegrationBridge(adapters)
