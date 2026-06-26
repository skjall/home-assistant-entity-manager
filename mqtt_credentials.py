#!/usr/bin/env python3
"""
MQTT Credentials - holt die Broker-Zugangsdaten vom Home Assistant Supervisor.

Erfordert `"services": ["mqtt:need"]` in config.json. Der Supervisor liefert dann
unter GET http://supervisor/services/mqtt (Bearer SUPERVISOR_TOKEN) die Broker-
Verbindungsdaten des in HA konfigurierten MQTT-Brokers.

Gibt None zurück, wenn kein Broker verfügbar ist oder die Service-Rolle fehlt
(HTTP 403) - der Z2M-Bridge-Teil deaktiviert sich dann sauber (graceful degradation).
"""

import logging
import os
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)

SUPERVISOR_MQTT_URL = "http://supervisor/services/mqtt"


async def get_mqtt_credentials() -> Optional[Dict[str, Any]]:
    """Holt MQTT-Broker-Zugangsdaten vom Supervisor.

    Returns:
        {"host","port","username","password","ssl"} oder None, wenn nicht verfügbar.
    """
    token = os.getenv("SUPERVISOR_TOKEN") or os.getenv("HA_TOKEN")
    if not token:
        logger.info("No supervisor token available - MQTT/Z2M bridge disabled")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(SUPERVISOR_MQTT_URL, headers={"Authorization": f"Bearer {token}"}) as resp:
                if resp.status != 200:
                    logger.info(
                        "Supervisor MQTT service unavailable (HTTP %s) - MQTT/Z2M bridge disabled "
                        "(needs 'services: [mqtt:need]' in config.json + add-on rebuild)",
                        resp.status,
                    )
                    return None
                payload = await resp.json()
    except Exception as e:  # noqa: BLE001 - fehlende MQTT-Anbindung darf das Add-on nicht stören
        logger.warning("Failed to fetch MQTT credentials: %s", e)
        return None

    data = payload.get("data") or {}
    host = data.get("host")
    if not host:
        logger.info("Supervisor returned no MQTT host - MQTT/Z2M bridge disabled")
        return None

    creds = {
        "host": host,
        "port": int(data.get("port") or 1883),
        "username": data.get("username") or None,
        "password": data.get("password") or None,
        "ssl": bool(data.get("ssl") or False),
    }
    logger.info("MQTT credentials obtained (host=%s port=%s ssl=%s)", creds["host"], creds["port"], creds["ssl"])
    return creds
