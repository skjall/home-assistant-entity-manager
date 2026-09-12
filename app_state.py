"""The stores and the mutable state the whole add-on shares.

Every route, job handler and tool works on the same registries and the same
JSON-backed stores; they live here so no part has to import the web layer to
reach them.
"""

import asyncio
import logging
import os

from api_token_store import ApiTokenStore
from device_swap import SwapJobStore
from entity_registry import EntityRegistry
from entity_restructurer import EntityRestructurer
from ha_client import HomeAssistantClient
from ha_translations import HaTranslations
from jobs import TERMINAL_STATES, JobStore, JobWorker
from json_store import new_lock
from naming_overrides import NamingOverrides
from naming_rules import NamingRules
from naming_templates import NamingTemplates
from rename_log import RenameLog
from type_mappings import DEFAULT_SYSTEM_MAPPINGS, TypeMappings

logger = logging.getLogger(__name__)

# Language-independent constant for entities without area assignment
UNASSIGNED_AREA = "__unassigned__"

# Persistent data directory. Defaults to the add-on's /data mount; overridable
# via DATA_DIR for local runs, tests and CI where /data is not available.
DATA_DIR = os.getenv("DATA_DIR", "/data")

# Global state
# Type rules replace the flat user mappings; the legacy file is migrated once
# and kept as a backup next to a report of what was merged.
ha_translations = HaTranslations()
naming_rules_store = NamingRules(
    os.path.join(DATA_DIR, "naming_rules.json"),
    legacy_path=os.path.join(DATA_DIR, "user_type_mappings.json"),
    device_class_keys=DEFAULT_SYSTEM_MAPPINGS["device_class"].keys(),
)

renamer_state = {
    "client": None,
    "naming_rules": naming_rules_store,
    "restructurer": None,
    "areas": {},
    "entities_by_area": {},
    "proposed_changes": {},
    "naming_overrides": NamingOverrides(os.path.join(DATA_DIR, "naming_overrides.json")),
    "naming_templates": NamingTemplates(os.path.join(DATA_DIR, "naming_templates.json")),
    "type_mappings": TypeMappings(
        user_mappings_path=os.path.join(DATA_DIR, "user_type_mappings.json"),
        rules=naming_rules_store,
        ha_translations=ha_translations,
    ),
    "swap_store": SwapJobStore(os.path.join(DATA_DIR, "device_swaps")),
    "rename_log": RenameLog(os.path.join(DATA_DIR, "rename_log.jsonl")),
    "api_token_store": ApiTokenStore(os.path.join(DATA_DIR, "api_token.json")),
    # Generic background-job infrastructure for long-running operations. Jobs run
    # serially on a single worker thread, off the request path (load_structure
    # rebuilds the restructurer by reassignment and handlers work on snapshots,
    # so no cross-thread lock is needed).
    "job_store": JobStore(os.path.join(DATA_DIR, "jobs"), terminal_states=TERMINAL_STATES),
}
renamer_state["worker"] = JobWorker(renamer_state["job_store"])

# Share the audit log with every EntityRegistry instance so all rename paths
# (single, batch, device cascade) get recorded centrally.
EntityRegistry.rename_log = renamer_state["rename_log"]

# Several worker threads serve requests, so two of them can find a singleton
# missing at the same moment and both build one; the loser's object is then
# silently dropped along with whatever it had already fetched.
_singletons = new_lock()
# The bridge has its own lock because building it waits on the broker, and a
# client lookup must not queue behind that.
_bridge = new_lock()


async def init_client() -> HomeAssistantClient:
    """The one Home Assistant client and restructurer, built on first use."""
    with _singletons:
        return _build_client()


def _build_client() -> HomeAssistantClient:
    if not renamer_state["client"]:
        # In Add-on mode, use Supervisor API
        base_url = os.getenv("HA_URL", "http://supervisor/core")
        token = os.getenv("HA_TOKEN", os.getenv("SUPERVISOR_TOKEN"))
        logger.info(f"Connecting to Home Assistant at {base_url}")
        renamer_state["client"] = HomeAssistantClient(base_url, token)

    if renamer_state["restructurer"] is None:
        renamer_state["restructurer"] = EntityRestructurer(
            renamer_state["client"],
            renamer_state["naming_overrides"],
            type_mappings=renamer_state["type_mappings"],
            naming_templates=renamer_state["naming_templates"],
            ha_translations=ha_translations,
        )
    return renamer_state["client"]


async def ensure_mqtt_bridge():
    """Lazy MQTT/Z2M-Bridge-Singleton. Gibt None zurück, wenn nicht verfügbar.

    Vollständig optional: Ohne MQTT-Broker, ohne paho, ohne Z2M oder bei
    deaktivierter Option degradiert alles sauber zu None (kein Crash) - der
    Geräte-Austausch läuft dann wie bisher über Matter/Registry.
    """
    if renamer_state.get("mqtt_bridge") is not None:
        return renamer_state["mqtt_bridge"]
    if os.getenv("ENABLE_Z2M_BRIDGE", "true").lower() != "true":
        return None
    # Der Connect dauert bis zu zehn Sekunden. Ohne die Sperre kämen zwei
    # Threads gleichzeitig am Versuchs-Merker vorbei und bauten beide eine
    # Verbindung auf; der Wartende bekommt so stattdessen die fertige Brücke.
    with _bridge:
        if renamer_state.get("mqtt_bridge") is not None:
            return renamer_state["mqtt_bridge"]
        if renamer_state.get("mqtt_bridge_tried"):
            return None  # nur einmal versuchen (Connect ist teuer)
        renamer_state["mqtt_bridge_tried"] = True
        return await _connect_mqtt_bridge()


async def _connect_mqtt_bridge():
    """Baut die Brücke auf. Läuft nur unter _bridge und nur ein einziges Mal."""
    try:
        from mqtt_credentials import get_mqtt_credentials

        creds = await get_mqtt_credentials()
        if not creds:
            return None
        from bridge_mqtt import MqttBridge  # importiert paho - nur hinter dem Guard

        bridge = MqttBridge(
            host=creds["host"],
            port=creds["port"],
            username=creds["username"],
            password=creds["password"],
            ssl=creds["ssl"],
            base_topic=os.getenv("Z2M_BASE_TOPIC", "zigbee2mqtt"),
        )
        loop = asyncio.get_running_loop()
        connected = await loop.run_in_executor(None, bridge.connect, 10.0)
        if not connected:
            logger.warning("MQTT bridge could not connect - Z2M features disabled")
            return None
        renamer_state["mqtt_bridge"] = bridge
        logger.info("MQTT/Z2M bridge ready")
        return bridge
    except ImportError as e:
        logger.info("paho-mqtt not available (%s) - Z2M features disabled (needs add-on rebuild)", e)
        return None
    except Exception as e:  # noqa: BLE001 - MQTT darf das Add-on nie blockieren
        logger.warning("MQTT bridge init failed: %s - Z2M features disabled", e)
        return None


def ws_url() -> str:
    """The WebSocket address of Home Assistant, derived from its HTTP one."""
    base_url = os.getenv("HA_URL") or "http://supervisor/core"
    return base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
