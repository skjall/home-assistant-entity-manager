"""Renaming a Zigbee2MQTT device on the bridge, not only in the registry.

Home Assistant's registry and Zigbee2MQTT keep their own name for the same
device. Renaming one without the other leaves the two disagreeing, so a rename
that touches a Z2M device is passed on to the bridge.
"""

import logging

from app_state import ensure_mqtt_bridge, renamer_state
from bridge_adapters import build_bridge

logger = logging.getLogger(__name__)


async def sync_z2m_name(device_registry, device_id: str, new_name: str) -> dict:
    """Gleicht den Z2M-friendly_name an den neuen HA-Namen an (nur Z2M-Geräte).

    Nicht fatal: Ohne MQTT/Z2M oder bei Fehlern wird nur geloggt; der normale
    Rename läuft unabhängig weiter. Gibt einen Status fürs Reporting zurück.
    """
    try:
        device_data = renamer_state["restructurer"].devices.get(device_id)
        if not device_data:
            return {"synced": False, "supported": False, "error": None}
        mqtt_bridge = await ensure_mqtt_bridge()
        bridge = build_bridge(device_registry, mqtt_bridge=mqtt_bridge)
        res = await bridge.rename_native(device_data, new_name)
        if not res.native_supported:
            return {"synced": False, "supported": False, "error": None}
        if res.success:
            logger.info("Z2M name synced for %s -> '%s'", device_id, new_name)
            return {"synced": True, "supported": True, "error": None}
        logger.warning("Z2M name sync failed for %s: %s", device_id, res.error)
        return {"synced": False, "supported": True, "error": res.error}
    except Exception as e:  # noqa: BLE001 - native sync must never block the rename
        logger.warning("Z2M name sync error for %s: %s", device_id, e)
        return {"synced": False, "supported": True, "error": str(e)}
