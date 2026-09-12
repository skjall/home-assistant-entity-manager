#!/usr/bin/env python3
"""
Web UI für Home Assistant Entity Renamer - Add-on Version
"""

import asyncio
from datetime import datetime, timezone
import html
import ipaddress
import json
import logging
import os
import re
import time
from typing import Any, Optional
import unicodedata
import uuid

import aiohttp
from flask import Flask, abort, jsonify, make_response, redirect, render_template, request, send_from_directory
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from api_token_store import ApiTokenStore
from bridge_adapters import build_bridge
from dependency_updater import DependencyUpdater
from device_registry import DeviceRegistry
import device_swap
from device_swap import SwapExecutor, SwapJobStore, propose_mapping
from entity_registry import EntityRegistry
from entity_restructurer import EntityRestructurer
from ha_client import HomeAssistantClient
from ha_translations import HaTranslations
from ha_websocket import HomeAssistantWebSocket
from hierarchy_manager import normalize_name
from jobs import TERMINAL_STATES, JobStore, JobWorker, new_job
from lovelace_updater import LovelaceUpdater
from naming_canon import canon
from naming_overrides import NamingOverrides
from naming_rules import NamingRuleError, NamingRules
from naming_templates import NamingTemplateError, NamingTemplates
from reference_checker import ReferenceChecker
from rename_log import RenameLog
from type_mappings import DEFAULT_SYSTEM_MAPPINGS, TypeMappings

# Don't load .env in Add-on mode - use environment variables from Supervisor
# load_dotenv()

# Language-independent constant for entities without area assignment
UNASSIGNED_AREA = "__unassigned__"


class _CapturePeerIP:
    """WSGI middleware recording the real TCP peer address.

    Installed as the outermost layer so it sees the untouched ``REMOTE_ADDR``
    before ProxyFix rewrites it from forwarded headers. This lets the API gate
    tell genuine Ingress traffic (from the Supervisor network) apart from direct
    port access, which a client cannot forge via request headers.
    """

    def __init__(self, wsgi_app: object) -> None:
        self.wsgi_app = wsgi_app

    def __call__(self, environ: dict, start_response: object) -> object:
        environ["entity_manager.peer_addr"] = environ.get("REMOTE_ADDR", "")
        return self.wsgi_app(environ, start_response)


app = Flask(__name__, static_folder="static", static_url_path="/static")
# Ingress proxy header support. _CapturePeerIP wraps the outside so it records
# the real TCP peer before ProxyFix trusts forwarded headers (X-Forwarded-For).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.wsgi_app = _CapturePeerIP(app.wsgi_app)
CORS(app)

# External API access is guarded by a generated token (see ApiTokenStore): it is
# created on demand from the web UI, shown once, and stored only as a hash. When
# a token exists the add-on's HTTP port may be exposed for external, read-only
# access to the rename audit log:
#   - Ingress requests (from the Supervisor network) pass through unchanged, so
#     the web UI keeps working without any token.
#   - Direct (non-Ingress) requests are rejected unless they target
#     GET /api/rename_log with a valid bearer token. Every other /api/* route,
#     including token management and the write endpoints, stays Ingress-exclusive.
# With no token generated the gate is inactive; the port is closed by default,
# so /api/* is only reachable via Ingress anyway.

# HA Supervisor's internal Docker network (hassio). Ingress proxies add-on
# requests from this range; direct host/LAN access originates elsewhere.
_SUPERVISOR_NETWORK = ipaddress.ip_network("172.30.32.0/23")

# /api/* paths reachable with a token over a directly-exposed port. Read-only.
_EXTERNAL_API_PATHS = frozenset({"/api/rename_log"})


def _is_ingress_request() -> bool:
    """Return True when the request's real TCP peer is in the Supervisor network.

    Uses the address captured before ProxyFix, so it cannot be spoofed by a
    client setting forwarded/ingress headers on a direct connection.
    """
    peer = request.environ.get("entity_manager.peer_addr", "")
    try:
        return ipaddress.ip_address(peer) in _SUPERVISOR_NETWORK
    except ValueError:
        return False


def _provided_token() -> str:
    """Extract the bearer token from Authorization (or the X-API-Key header)."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer ") :].strip()
    return request.headers.get("X-API-Key", "").strip()


@app.before_request
def _enforce_api_access() -> None:
    """Gate /api/* routes when an API token is configured.

    Ingress requests are trusted (HA already authenticated the user). Direct
    requests are limited to the read-only rename-log lookup with a valid token;
    everything else is refused.
    """
    store = renamer_state["api_token_store"]
    if not store.exists():
        return None
    path = request.path
    if not path.startswith("/api/"):
        return None
    if _is_ingress_request():
        return None
    # Direct (non-Ingress) access from here on.
    if path not in _EXTERNAL_API_PATHS or request.method != "GET":
        abort(403)
    if not store.verify(_provided_token()):
        abort(401)
    return None


# Setup logging to both console and file
log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logging.basicConfig(
    level=logging.DEBUG,
    format=log_format,
    handlers=[
        logging.StreamHandler(),  # Console output
        logging.FileHandler("web_ui.log", mode="a"),  # File output
    ],
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

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
        user_mappings_path=os.path.join(DATA_DIR, "user_type_mappings.json"), rules=naming_rules_store
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


# =============================================================================
# Input Sanitization
# =============================================================================

# Maximum lengths for different input types
MAX_NAME_LENGTH = 255
MAX_ENTITY_ID_LENGTH = 255
MAX_REGISTRY_ID_LENGTH = 64

# Valid characters for entity IDs (Home Assistant format: domain.object_id)
ENTITY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")

# Valid characters for registry IDs (typically alphanumeric with some special chars)
REGISTRY_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def sanitize_string(value: str, max_length: int = MAX_NAME_LENGTH) -> str:
    """
    Sanitize a general string input.
    - Strips whitespace
    - Removes control characters
    - Escapes HTML entities
    - Limits length
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)

    # Strip whitespace
    value = value.strip()

    # Remove control characters (keep newlines and tabs for multi-line text)
    value = "".join(char for char in value if unicodedata.category(char) != "Cc" or char in "\n\t")

    # Remove null bytes and other dangerous characters
    value = value.replace("\x00", "")

    # Limit length
    value = value[:max_length]

    return value


def sanitize_name(value: str, max_length: int = MAX_NAME_LENGTH) -> str:
    """
    Sanitize a display name (friendly name, area name, device name).
    - All general sanitization
    - Escape HTML to prevent XSS
    - Remove script tags and event handlers
    """
    value = sanitize_string(value, max_length)
    if value is None:
        return None

    # Remove any script tags or event handlers (case insensitive)
    value = re.sub(r"<script[^>]*>.*?</script>", "", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"on\w+\s*=", "", value, flags=re.IGNORECASE)

    # Escape HTML entities to prevent XSS
    value = html.escape(value, quote=True)

    return value


def sanitize_entity_id(value: str) -> str:
    """
    Sanitize and validate an entity ID.
    Entity IDs must be lowercase, alphanumeric with underscores, in format domain.object_id
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None

    # Strip and lowercase
    value = value.strip().lower()

    # Limit length
    value = value[:MAX_ENTITY_ID_LENGTH]

    # Replace spaces and hyphens with underscores
    value = value.replace(" ", "_").replace("-", "_")

    # Remove any characters that aren't valid
    value = re.sub(r"[^a-z0-9_.]", "", value)

    # Validate format
    if not ENTITY_ID_PATTERN.match(value):
        return None

    return value


def sanitize_registry_id(value: str) -> str:
    """
    Sanitize and validate a registry ID.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None

    # Strip whitespace
    value = value.strip()

    # Limit length
    value = value[:MAX_REGISTRY_ID_LENGTH]

    # Validate format (alphanumeric, underscore, hyphen)
    if not REGISTRY_ID_PATTERN.match(value):
        return None

    return value


def validate_json_input(data: dict, required_fields: list = None) -> tuple:
    """
    Validate that JSON input is a dict and has required fields.
    Returns (is_valid, error_message)
    """
    if not isinstance(data, dict):
        return False, "Invalid JSON input"

    if required_fields:
        missing = [f for f in required_fields if f not in data]
        if missing:
            return False, f"Missing required fields: {', '.join(missing)}"

    return True, None


async def init_client() -> HomeAssistantClient:
    """Initialize the Home Assistant client and restructurer."""
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


async def _ensure_mqtt_bridge():
    """Lazy MQTT/Z2M-Bridge-Singleton. Gibt None zurück, wenn nicht verfügbar.

    Vollständig optional: Ohne MQTT-Broker, ohne paho, ohne Z2M oder bei
    deaktivierter Option degradiert alles sauber zu None (kein Crash) - der
    Geräte-Austausch läuft dann wie bisher über Matter/Registry.
    """
    if renamer_state.get("mqtt_bridge") is not None:
        return renamer_state["mqtt_bridge"]
    if os.getenv("ENABLE_Z2M_BRIDGE", "true").lower() != "true":
        return None
    if renamer_state.get("mqtt_bridge_tried"):
        return None  # nur einmal versuchen (Connect ist teuer)
    renamer_state["mqtt_bridge_tried"] = True

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


async def _sync_z2m_name(device_registry, device_id: str, new_name: str) -> dict:
    """Gleicht den Z2M-friendly_name an den neuen HA-Namen an (nur Z2M-Geräte).

    Nicht fatal: Ohne MQTT/Z2M oder bei Fehlern wird nur geloggt; der normale
    Rename läuft unabhängig weiter. Gibt einen Status fürs Reporting zurück.
    """
    try:
        device_data = renamer_state["restructurer"].devices.get(device_id)
        if not device_data:
            return {"synced": False, "supported": False, "error": None}
        mqtt_bridge = await _ensure_mqtt_bridge()
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


async def load_areas_and_entities():
    """Lade alle Areas und ihre Entities"""
    try:
        client = await init_client()
        logger.info(f"Client initialized: {client.base_url}")

        # Create WebSocket connection for structure data
        base_url = os.getenv("HA_URL")
        token = os.getenv("HA_TOKEN")
        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

        # Lade States
        logger.info("Loading states from Home Assistant...")
        states = await client.get_states()
        logger.info(f"Loaded {len(states)} states")

        # Now connect WebSocket for structure data
        ws = HomeAssistantWebSocket(ws_url, token)
        await ws.connect()

        try:
            # Load structure (Areas, Devices, etc) via WebSocket
            logger.info("Loading Home Assistant structure via WebSocket...")
            await renamer_state["restructurer"].load_structure(ws)
            await _sync_ha_language(ws)

            # Ensure that areas were loaded
            areas_count = len(renamer_state["restructurer"].areas)
            devices_count = len(renamer_state["restructurer"].devices)
            logger.info(f"Loaded {areas_count} areas, {devices_count} devices")

            if areas_count == 0:
                logger.warning("No areas loaded, using fallback mode")
        finally:
            await ws.disconnect()

        # Organize entities by area
        areas_dict = {}
        entities_by_area = {}

        # Erstelle Area-Dict
        for area_id, area in renamer_state["restructurer"].areas.items():
            area_name = area.get("name", "Unbekannt")
            areas_dict[area_id] = area_name
            entities_by_area[area_name] = {"domains": {}}
            logger.debug(f"Added area: {area_name} (ID: {area_id})")

        # Add "Not assigned" (using language-independent constant)
        entities_by_area[UNASSIGNED_AREA] = {"domains": {}}

        # Create device-entity mapping from the devices
        device_entities = {}
        for device_id, device in renamer_state["restructurer"].devices.items():
            # Many devices have their entity IDs in the identifiers
            for identifier in device.get("identifiers", []):
                if isinstance(identifier, list) and len(identifier) > 1:
                    device_entities[identifier[1]] = device_id

            # Some have it in the name
            if device.get("name_by_user"):
                device_entities[device["name_by_user"]] = device_id
            if device.get("name"):
                device_entities[device["name"]] = device_id

        # Process all entities
        entities_by_area_count = {}
        for state in states:
            entity_id = state["entity_id"]
            domain = entity_id.split(".")[0]
            area_name = UNASSIGNED_AREA

            # Try to find area from various sources

            # 1. From Entity Registry (if loaded)
            entity_reg = renamer_state["restructurer"].entities.get(entity_id, {})
            if entity_reg:
                device_id = entity_reg.get("device_id")
                # A direct entity assignment overrides its device's area.
                if entity_reg.get("area_id") and entity_reg["area_id"] in areas_dict:
                    area_name = areas_dict[entity_reg["area_id"]]
                elif device_id and device_id in renamer_state["restructurer"].devices:
                    device = renamer_state["restructurer"].devices[device_id]
                    if device.get("area_id") and device["area_id"] in areas_dict:
                        area_name = areas_dict[device["area_id"]]

            # 2. From Entity Attributes (some entities have area_id or device_id)
            if area_name == UNASSIGNED_AREA:
                attributes = state.get("attributes", {})

                # Direct area_id in attributes
                if "area_id" in attributes and attributes["area_id"] in areas_dict:
                    area_name = areas_dict[attributes["area_id"]]

                # Device ID in attributes
                elif "device_id" in attributes:
                    device_id = attributes["device_id"]
                    if device_id in renamer_state["restructurer"].devices:
                        device = renamer_state["restructurer"].devices[device_id]
                        if device.get("area_id") and device["area_id"] in areas_dict:
                            area_name = areas_dict[device["area_id"]]

            # 3. Try to find the device via entity name
            if area_name == UNASSIGNED_AREA:
                # Extract possible device parts from entity ID
                entity_parts = entity_id.split(".")[-1].split("_")

                # Search for device match
                for i in range(len(entity_parts), 0, -1):
                    potential_device_name = "_".join(entity_parts[:i])
                    if potential_device_name in device_entities:
                        device_id = device_entities[potential_device_name]
                        device = renamer_state["restructurer"].devices.get(device_id)
                        if device and device.get("area_id") and device["area_id"] in areas_dict:
                            area_name = areas_dict[device["area_id"]]
                            break

            # 4. Try to recognize the room from entity ID (Fallback)
            if area_name == UNASSIGNED_AREA:
                entity_lower = entity_id.lower()
                for area_id, name in areas_dict.items():
                    # Normalize area names for comparison
                    area_key = area_id.lower().replace("ü", "u").replace("ö", "o").replace("ä", "a")
                    if f".{area_key}_" in entity_lower or entity_lower.startswith(f"{domain}.{area_key}_"):
                        area_name = name
                        break

            # Add entity to the corresponding area and domain
            if domain not in entities_by_area[area_name]["domains"]:
                entities_by_area[area_name]["domains"][domain] = []

            # Check if entity is orphan (restored from storage but no longer provided by integration)
            attributes = state.get("attributes", {})
            is_orphan = attributes.get("restored", False) == True

            entities_by_area[area_name]["domains"][domain].append(
                {
                    "entity_id": entity_id,
                    "friendly_name": attributes.get("friendly_name", entity_id),
                    "state": state.get("state", "unknown"),
                    "is_orphan": is_orphan,
                }
            )

            # Count for debug
            entities_by_area_count[area_name] = entities_by_area_count.get(area_name, 0) + 1

        # Now process disabled AND orphan entities from entity registry
        logger.info("Processing disabled and orphan entities from registry...")
        disabled_count = 0
        orphan_count = 0

        # Build set of entity_ids that have state (for faster lookup)
        entities_with_state = set()
        for area_data in entities_by_area.values():
            for domain_entities in area_data["domains"].values():
                for e in domain_entities:
                    entities_with_state.add(e["entity_id"])

        for entity_id, entity_reg in renamer_state["restructurer"].entities.items():
            # Skip if already processed (entities with state)
            if entity_id in entities_with_state:
                continue

            # Entity is in registry but has no state - either disabled or orphan
            is_disabled = entity_reg.get("disabled_by") is not None
            is_orphan = not is_disabled  # No state AND not disabled = orphan

            if is_disabled:
                disabled_count += 1
            else:
                orphan_count += 1

            domain = entity_id.split(".")[0]
            area_name = UNASSIGNED_AREA

            # Find area from device or entity registry
            device_id = entity_reg.get("device_id")
            if device_id and device_id in renamer_state["restructurer"].devices:
                device = renamer_state["restructurer"].devices[device_id]
                if device.get("area_id") and device["area_id"] in areas_dict:
                    area_name = areas_dict[device["area_id"]]
            elif entity_reg.get("area_id") and entity_reg["area_id"] in areas_dict:
                area_name = areas_dict[entity_reg["area_id"]]

            # Add to entities_by_area
            if domain not in entities_by_area[area_name]["domains"]:
                entities_by_area[area_name]["domains"][domain] = []

            entities_by_area[area_name]["domains"][domain].append(
                {
                    "entity_id": entity_id,
                    "friendly_name": entity_reg.get("name") or entity_reg.get("original_name") or entity_id,
                    "state": "orphan" if is_orphan else "disabled",
                    "disabled_by": entity_reg.get("disabled_by"),
                    "is_orphan": is_orphan,
                }
            )

            # Update count
            entities_by_area_count[area_name] = entities_by_area_count.get(area_name, 0) + 1

        logger.info(f"Added {disabled_count} disabled entities from registry")
        logger.info(f"Added {orphan_count} orphan entities from registry")

        # Debug Output
        logger.info("Entity distribution by area:")
        for area, count in entities_by_area_count.items():
            if count > 0:
                logger.info(f"  {area}: {count} entities")

        renamer_state["areas"] = areas_dict
        renamer_state["entities_by_area"] = entities_by_area

        logger.info(f"Organization complete: {len(entities_by_area)} areas with entities")
        return entities_by_area

    except Exception as e:
        logger.error(f"Error in load_areas_and_entities: {str(e)}", exc_info=True)
        raise


@app.route("/")
def index():
    """Hauptseite"""
    # Use timestamp for cache busting
    version = str(int(time.time()))
    response = make_response(render_template("index.html", version=version))
    # Prevent browser from caching the HTML page
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/test")
def test():
    """Test page for CSS"""
    return send_from_directory("static", "test.html")


@app.route("/static/css/<path:filename>")
def serve_font_workaround(filename):
    """Workaround to serve font files from fonts directory when requested from css directory"""
    if filename.startswith("remixicon.") and filename.endswith((".woff", ".woff2", ".ttf", ".eot", ".svg")):
        # Strip query parameters
        filename = filename.split("?")[0]
        return send_from_directory("static/fonts", filename)
    return send_from_directory("static/css", filename)


@app.route("/static/js/<path:filename>")
def serve_js(filename):
    """Serve JavaScript files"""
    return send_from_directory("static/js", filename)


@app.route("/static/translations/<path:filename>")
@app.route("/static/translations/<version>/<path:filename>")
def serve_translations(filename, version=None):
    """Serve translation files; ``version`` only keys caches and is otherwise ignored."""
    response = send_from_directory("translations/ui", filename)
    # The UI fetches these with a cache-busting query; proxies in front of Home
    # Assistant may still cache by path, so say explicitly that they must not.
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.route("/api/languages")
def get_available_languages():
    """Return available UI languages based on translation files"""
    import glob

    # Language display names
    language_names = {
        "en": "English",
        "de": "Deutsch",
        "es": "Español",
        "fr": "Français",
        "it": "Italiano",
        "nl": "Nederlands",
        "pt": "Português",
        "pl": "Polski",
        "ru": "Русский",
        "zh": "中文",
        "ja": "日本語",
        "ko": "한국어",
    }

    languages = []
    translation_files = glob.glob("translations/ui/*.json")

    for filepath in sorted(translation_files):
        code = os.path.basename(filepath).replace(".json", "")
        name = language_names.get(code, code.upper())
        languages.append({"code": code, "name": name})

    return jsonify({"languages": languages})


@app.route("/test/css-info")
def test_css_info():
    """Test route to check CSS file info"""
    import os

    css_path = os.path.join(app.static_folder, "css", "styles.css")
    if os.path.exists(css_path):
        file_size = os.path.getsize(css_path)
        with open(css_path, "r") as f:
            content = f.read()
        return jsonify(
            {
                "exists": True,
                "size": file_size,
                "lines": len(content.splitlines()),
                "has_bg_red": "bg-red-600" in content,
                "has_utilities": ".bg-gray-50" in content,
                "last_100_chars": content[-100:] if len(content) > 100 else content,
            }
        )
    return jsonify({"exists": False, "path": css_path})


@app.route("/api/areas")
def get_areas():
    """Gibt alle Areas mit ihren Domains zurück"""
    # Create new event loop for this request
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_get_areas_async())
    finally:
        loop.close()


async def _get_areas_async():
    """Async implementation of get_areas"""
    try:
        logger.info("Loading areas and entities...")
        await load_areas_and_entities()

        logger.info(f"Found {len(renamer_state['entities_by_area'])} areas")

        # Prepare data for frontend
        areas_data = []

        # Create reverse mapping from name to ID
        area_name_to_id = {}
        for area_id, area in renamer_state.get("restructurer", {}).areas.items():
            area_name_to_id[area.get("name", "")] = area_id

        for area_name, area_data in renamer_state["entities_by_area"].items():
            if area_data["domains"]:  # Nur Areas mit Entities
                area_id = area_name_to_id.get(area_name, None)

                areas_data.append(
                    {
                        "name": area_name,
                        "display_name": area_name,
                        "area_id": area_id,
                        "domains": sorted(list(area_data["domains"].keys())),
                        "entity_count": sum(len(entities) for entities in area_data["domains"].values()),
                    }
                )
                logger.debug(
                    f"Area '{area_name}': {len(area_data['domains'])} domains, {sum(len(entities) for entities in area_data['domains'].values())} entities"
                )

        # Sortiere nach Name
        areas_data.sort(key=lambda x: x["name"])

        logger.info(f"Returning {len(areas_data)} areas with entities")
        return jsonify(areas_data)
    except Exception as e:
        logger.error(f"Error in get_areas: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/normalize", methods=["POST"])
def normalize_names():
    """Normalize display names to entity-ID slugs.

    The backend is the single source of truth for the naming convention, so the
    frontend must call this instead of reimplementing the slug rules (otherwise
    the two drift apart -- e.g. accented characters get stripped client-side).

    Body: ``{"names": ["Foo Bar", ...]}`` -> ``{"normalized": ["foo_bar", ...]}``
    """
    data = request.json
    if not isinstance(data, dict) or not isinstance(data.get("names"), list):
        return jsonify({"error": "'names' must be a list"}), 400

    names = data["names"]
    if len(names) > 5000:
        return jsonify({"error": "Too many names"}), 400

    normalized = [normalize_name(n) if isinstance(n, str) else "" for n in names]
    return jsonify({"normalized": normalized})


@app.route("/api/preview", methods=["POST"])
def preview_changes():
    """Zeige Vorschau der Änderungen für ausgewählte Area/Domain"""
    # Create new event loop for this request
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_preview_changes_async())
    finally:
        loop.close()


async def _preview_changes_async():
    """Async implementation of preview_changes"""
    data = request.json
    area_name = data.get("area")
    domain = data.get("domain")
    skip_reviewed = data.get("skip_reviewed", False)
    only_changes = data.get("only_changes", False)
    show_disabled = data.get("show_disabled", False)

    if not area_name or not domain:
        return jsonify({"error": "Area und Domain müssen angegeben werden"}), 400

    # Get the entities for this area/domain
    if domain == "all":
        # Collect all entities from all domains for this area
        entities = []
        domains_data = renamer_state["entities_by_area"].get(area_name, {}).get("domains", {})
        for domain_entities in domains_data.values():
            entities.extend(domain_entities)
    else:
        entities = renamer_state["entities_by_area"].get(area_name, {}).get("domains", {}).get(domain, [])

    if not entities:
        return jsonify({"changes": []})

    # Create states for the restructurer
    client = await init_client()
    all_states = await client.get_states()

    # Filtere die relevanten States
    filtered_states = []
    entity_ids = [e["entity_id"] for e in entities]
    logger.info(f"Looking for {len(entity_ids)} entities from area {area_name}, domain {domain}")
    logger.debug(f"Entity IDs to find: {entity_ids}")

    # First add all enabled entities from states
    for state in all_states:
        if state["entity_id"] in entity_ids:
            filtered_states.append(state)

    # Now add disabled entities if show_disabled is True
    if show_disabled:
        # Find entities that were not found in states (these are disabled)
        found_entity_ids = {s["entity_id"] for s in filtered_states}
        for entity in entities:
            entity_id = entity["entity_id"]
            if entity_id not in found_entity_ids and entity.get("state") == "disabled":
                # Create a dummy state for disabled entity
                filtered_states.append(
                    {
                        "entity_id": entity_id,
                        "state": "unavailable",
                        "attributes": {
                            "friendly_name": entity.get("friendly_name", entity_id),
                            "disabled_by": entity.get("disabled_by", "unknown"),
                        },
                    }
                )

    logger.info(f"Found {len(filtered_states)} states matching the entities (including disabled: {show_disabled})")

    # Stelle sicher, dass der Restructurer die aktuelle Struktur hat
    base_url = os.getenv("HA_URL")
    token = os.getenv("HA_TOKEN")
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

    ws = HomeAssistantWebSocket(ws_url, token)
    await ws.connect()

    try:
        # Lade aktuelle Struktur
        await renamer_state["restructurer"].load_structure(ws)

        # Generiere Mapping
        mapping = await renamer_state["restructurer"].analyze_entities(
            filtered_states, skip_reviewed=skip_reviewed, show_reviewed=False
        )

        logger.info(f"Generated mapping with {len(mapping)} entries")

        # Prepare changes for frontend - grouped by device
        devices_map = {}
        entities_registry = renamer_state["restructurer"].entities
        devices_registry = renamer_state["restructurer"].devices

        logger.info(f"Entities in registry: {len(entities_registry)}, Devices in registry: {len(devices_registry)}")

        for old_id, (new_id, friendly_name) in mapping.items():
            # Finde aktuelle Entity Info
            current_info = next((e for e in entities if e["entity_id"] == old_id), {})

            # Hole Device Info
            entity_reg = entities_registry.get(old_id, {})
            device_id = entity_reg.get("device_id")
            device_info = None

            if old_id == "light.buro_bucherregal_indirekt_licht":
                logger.info(f"Debug {old_id}: entity_reg={bool(entity_reg)}, device_id={device_id}")

            if device_id and device_id in devices_registry:
                device = devices_registry[device_id]
                device_info = {
                    "id": device_id,
                    "name": device.get("name_by_user") or device.get("name", "Unbekanntes Gerät"),
                    "manufacturer": device.get("manufacturer", ""),
                    "model": device.get("model", ""),
                    "area_id": device.get("area_id"),
                }

            # Get registry ID for entity
            registry_id = entity_reg.get("id", "")  # The immutable UUID

            # Hole Entity Override (nur für Entity-Suffixe)
            entity_override = (
                renamer_state["naming_overrides"].get_entity_override(registry_id) if registry_id else None
            )

            current_friendly_name = (
                entity_reg.get("name") or entity_reg.get("original_name") or current_info.get("friendly_name", old_id)
            )

            # Extract current basename from friendly_name by removing device name prefix
            current_basename = None
            if device_info and current_friendly_name:
                device_name = device_info["name"]
                # Check if friendly_name starts with device name
                if current_friendly_name.startswith(device_name):
                    current_basename = current_friendly_name[len(device_name) :].strip()
                elif current_friendly_name != device_name:
                    # Friendly name doesn't start with device name, use the whole thing
                    current_basename = current_friendly_name

            entity_change = {
                "old_id": old_id,
                "new_id": new_id,
                "current_name": current_friendly_name,
                "new_name": friendly_name,
                "needs_rename": old_id != new_id or current_friendly_name != friendly_name,
                "selected": False,  # Not selected by default
                "device_id": device_id,
                "registry_id": registry_id,
                "has_override": entity_override is not None,
                "override_name": (entity_override.get("name") if entity_override else None),
                "disabled_by": entity_reg.get("disabled_by"),  # Add disabled status
                "current_basename": current_basename,  # The extracted basename from current friendly_name
            }

            # Gruppiere nach Device
            device_key = device_id or "no_device"
            if device_key not in devices_map:
                device_suggested_name = None
                if device_info:
                    has_real_area = area_name != UNASSIGNED_AREA
                    device_suggested_name = renamer_state["restructurer"].generate_device_name(device_id)

                devices_map[device_key] = {
                    "device_info": device_info,
                    "device": (
                        {
                            "id": device_id,
                            "current_name": (device_info["name"] if device_info else None),
                            "suggested_name": device_suggested_name,
                            "needs_rename": device_info and device_info["name"] != device_suggested_name,
                            "manufacturer": (device_info.get("manufacturer", "") if device_info else None),
                            "model": (device_info.get("model", "") if device_info else None),
                            "has_area": has_real_area,
                        }
                        if device_info
                        else None
                    ),
                    "entities": [],
                }
            devices_map[device_key]["entities"].append(entity_change)

        # Convert to list for frontend
        changes = []
        for device_key, device_data in devices_map.items():
            # Filter entities based on settings
            filtered_entities = device_data["entities"]

            # Apply "only changes" filter
            if only_changes:
                filtered_entities = [e for e in filtered_entities if e["needs_rename"]]

            # Skip device groups with no visible entities
            if filtered_entities:
                changes.append(
                    {
                        "device": device_data["device"],
                        "entities": sorted(
                            filtered_entities,
                            key=lambda x: (not x["needs_rename"], x["old_id"]),
                        ),
                    }
                )

        # Sort devices: first with devices, then without
        changes.sort(
            key=lambda x: (
                x["device"] is None,
                x["device"]["current_name"] if x["device"] else "",
            )
        )

        # Debug logging
        logger.info(f"Preview for {area_name}/{domain}: {len(changes)} device groups")
        for i, change in enumerate(changes):
            device_name = change["device"]["current_name"] if change["device"] else "No device"
            logger.info(f"  Group {i}: {device_name} with {len(change['entities'])} entities")

        # Save for execute
        preview_id = f"{area_name}_{domain}"
        renamer_state["proposed_changes"][preview_id] = {
            "area": area_name,
            "domain": domain,
            "changes": changes,
            "mapping": mapping,
        }

        # Berechne Statistiken
        total_entities = sum(len(device_group["entities"]) for device_group in changes)
        need_rename = sum(
            1 for device_group in changes for entity in device_group["entities"] if entity["needs_rename"]
        )

        return jsonify(
            {
                "preview_id": preview_id,
                "changes": changes,
                "total": total_entities,
                "need_rename": need_rename,
            }
        )

    except Exception as e:
        logger.error(f"Error in _preview_changes_async: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

    finally:
        await ws.disconnect()


@app.route("/api/execute", methods=["POST"])
def execute_changes():
    """Führe ausgewählte Änderungen durch"""
    # Create new event loop for this request
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_execute_changes_async())
    finally:
        loop.close()


async def _execute_changes_async():
    """Async implementation of execute_changes"""
    data = request.json
    preview_id = data.get("preview_id")
    selected_entities = data.get("selected_entities", [])
    selected_devices = data.get("selected_devices", [])

    if not preview_id or preview_id not in renamer_state["proposed_changes"]:
        return jsonify({"error": "Ungültige Preview ID"}), 400

    proposed = renamer_state["proposed_changes"][preview_id]
    full_mapping = proposed["mapping"]

    # Filter only selected entities
    selected_mapping = {
        old_id: (new_id, name) for old_id, (new_id, name) in full_mapping.items() if old_id in selected_entities
    }

    if not selected_mapping and not selected_devices:
        return jsonify({"error": "Keine Entities oder Geräte ausgewählt"}), 400

    # Execute renaming
    base_url = os.getenv("HA_URL")
    token = os.getenv("HA_TOKEN")
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

    results = {
        "success": [],
        "failed": [],
        "skipped": [],
        "dependency_warnings": [],
        "device_success": [],
        "device_failed": [],
    }

    ws = HomeAssistantWebSocket(ws_url, token)
    await ws.connect()

    try:
        entity_registry = EntityRegistry(ws)
        device_registry = DeviceRegistry(ws)

        # Dependency Updater nutzt REST API
        base_url = os.getenv("HA_URL")
        token = os.getenv("HA_TOKEN")
        dependency_updater = DependencyUpdater(base_url, token)

        # Pre-fetch states once for all dependency updates (performance optimization)
        logger.info("Pre-fetching states for dependency updates...")
        cached_states = await dependency_updater.get_states()
        logger.info(f"Cached {len(cached_states)} states")

        # Get states for entity generation
        client = await init_client()
        states = await client.get_states()

        # Process devices first
        for device_data in selected_devices:
            device_id = device_data["device_id"]
            new_device_name = device_data["new_name"]
            device_entities = device_data["entities"]

            try:
                logger.info(f"Renaming device {device_id} to {new_device_name}")
                success = await device_registry.rename_device(device_id, new_device_name)

                if success:
                    # Z2M-friendly_name angleichen (nur Z2M-Geräte, nicht fatal)
                    z2m_sync = await _sync_z2m_name(device_registry, device_id, new_device_name)
                    results["device_success"].append(
                        {
                            "device_id": device_id,
                            "new_name": new_device_name,
                            "message": f"Gerät erfolgreich umbenannt zu: {new_device_name}",
                            "z2m_synced": z2m_sync.get("synced"),
                            "z2m_failed": (
                                z2m_sync.get("error")
                                if z2m_sync.get("supported") and not z2m_sync.get("synced")
                                else None
                            ),
                        }
                    )

                    # Only rename entities that were explicitly selected
                    # Don't automatically rename all device entities when only device is selected
                    await renamer_state["restructurer"].load_structure(ws)

                    device_states = {state["entity_id"]: state for state in states}
                    device_renames = {
                        entity_id: (new_entity_id, friendly_name)
                        for entity_id, new_entity_id, friendly_name in renamer_state[
                            "restructurer"
                        ].deduplicate_entity_ids(
                            [
                                (
                                    entity_id,
                                    *renamer_state["restructurer"].generate_new_entity_id(
                                        entity_id, device_states[entity_id]
                                    ),
                                )
                                for entity_id in device_entities
                                if entity_id in selected_entities and entity_id in device_states
                            ]
                        )
                    }

                    for entity_id in device_entities:
                        # Skip entities that weren't explicitly selected
                        if entity_id not in selected_entities:
                            logger.info(f"Skipping entity {entity_id} - not explicitly selected")
                            continue

                        if entity_id not in device_renames:
                            logger.info(f"Skipping entity {entity_id} - no current state")
                            continue
                        new_entity_id, new_friendly_name = device_renames[entity_id]

                        if entity_id != new_entity_id:
                            try:
                                # Check if entity is disabled and if we should enable it
                                entity_reg = renamer_state["restructurer"].entities.get(entity_id, {})
                                is_disabled = entity_reg.get("disabled_by") is not None
                                should_enable = (
                                    is_disabled and os.getenv("ENABLE_DISABLED_ENTITIES", "false").lower() == "true"
                                )

                                # Rename entity and enable if needed
                                await entity_registry.rename_entity(
                                    entity_id, new_entity_id, new_friendly_name, enable=should_enable
                                )

                                if should_enable:
                                    logger.info(f"Enabled and renamed disabled entity: {entity_id} -> {new_entity_id}")

                                # Update dependencies
                                dep_results = await dependency_updater.update_all_dependencies(
                                    entity_id, new_entity_id, cached_states
                                )

                                results["success"].append(
                                    {
                                        "old_id": entity_id,
                                        "new_id": new_entity_id,
                                        "message": "Entity erfolgreich umbenannt (durch Gerät)",
                                    }
                                )

                            except Exception as e:
                                logger.error(f"Fehler beim Umbenennen der Entity {entity_id}: {e}")
                                results["failed"].append({"entity_id": entity_id, "error": str(e)})
                else:
                    results["device_failed"].append(
                        {
                            "device_id": device_id,
                            "error": "Fehler beim Umbenennen des Geräts in Home Assistant",
                        }
                    )

            except Exception as e:
                logger.error(f"Fehler beim Device {device_id}: {e}")
                results["device_failed"].append({"device_id": device_id, "error": str(e)})

        # Recalculate as one batch so overrides are applied and no two entities
        # are sent to the same ID, which Home Assistant would refuse.
        states_by_id = {state["entity_id"]: state for state in states}
        recalculated = {
            entity_id: (new_entity_id, friendly_name)
            for entity_id, new_entity_id, friendly_name in renamer_state["restructurer"].deduplicate_entity_ids(
                [
                    (old_id, *renamer_state["restructurer"].generate_new_entity_id(old_id, states_by_id[old_id]))
                    for old_id in selected_mapping
                    if old_id in states_by_id
                ]
            )
        }

        # Verarbeite einzelne Entities
        for old_id, (new_id, friendly_name) in selected_mapping.items():
            try:
                if old_id in recalculated:
                    new_id, friendly_name = recalculated[old_id]
                    logger.info(f"Recalculated entity: {old_id} -> {new_id}, friendly_name: {friendly_name}")
                else:
                    logger.info(f"Processing entity: {old_id} -> {new_id}, friendly_name: {friendly_name}")

                # Check if entity ID or friendly name needs to be changed
                entity_reg = renamer_state["restructurer"].entities.get(old_id, {})
                current_friendly_name = entity_reg.get("name") or entity_reg.get("original_name") or ""

                needs_id_change = old_id != new_id
                needs_friendly_name_change = current_friendly_name != friendly_name

                if needs_id_change or needs_friendly_name_change:
                    # Check if entity is disabled and if we should enable it
                    entity_reg = renamer_state["restructurer"].entities.get(old_id, {})
                    disabled_by_value = entity_reg.get("disabled_by")
                    is_disabled = disabled_by_value is not None
                    should_enable = is_disabled and os.getenv("ENABLE_DISABLED_ENTITIES", "false").lower() == "true"

                    # Umbenennen (Entity ID und/oder Friendly Name)
                    logger.info(
                        f"Updating entity: ID change={needs_id_change}, Name change={needs_friendly_name_change}, "
                        f"is_disabled={is_disabled}, disabled_by={disabled_by_value}, should_enable={should_enable}"
                    )

                    if needs_id_change:
                        # Rename entity and enable if needed in a single operation
                        await entity_registry.rename_entity(old_id, new_id, friendly_name, enable=should_enable)
                        if should_enable:
                            logger.info(f"Enabled and renamed disabled entity: {old_id} -> {new_id}")
                    else:
                        # Only change friendly name
                        if should_enable:
                            # Enable and update name in one operation
                            await entity_registry.update_entity(old_id, name=friendly_name, enable=True)
                            logger.info(f"Enabled entity and updated friendly name: {old_id}")
                        else:
                            await entity_registry.update_entity(old_id, name=friendly_name)

                    # Update dependencies only on ID change
                    if needs_id_change:
                        try:
                            logger.info(f"Updating dependencies for: {old_id} -> {new_id}")
                            dep_results = await dependency_updater.update_all_dependencies(
                                old_id, new_id, cached_states
                            )

                            # Erstelle Success Entry
                            success_entry = {
                                "old_id": old_id,
                                "new_id": new_id,
                                "message": "Erfolgreich umbenannt",
                            }

                            # Add dependency updates if available
                            if dep_results["total_success"] > 0:
                                success_entry["dependency_updates"] = {
                                    "scenes": len(dep_results["scenes"]["success"]),
                                    "scripts": len(dep_results["scripts"]["success"]),
                                    "automations": len(dep_results["automations"]["success"]),
                                    "total": dep_results["total_success"],
                                }

                            results["success"].append(success_entry)

                            # Warne bei fehlgeschlagenen Dependencies
                            if dep_results["total_failed"] > 0:
                                failed_items = []
                                failed_items.extend(dep_results["scenes"]["failed"])
                                failed_items.extend(dep_results["scripts"]["failed"])
                                failed_items.extend(dep_results["automations"]["failed"])

                                results["dependency_warnings"].append(
                                    {
                                        "entity_id": new_id,
                                        "warning": f"Einige Dependencies konnten nicht aktualisiert werden: {', '.join(failed_items)}",
                                    }
                                )

                        except Exception as e:
                            logger.error(
                                f"Fehler beim Update der Dependencies: {e}",
                                exc_info=True,
                            )
                            results["dependency_warnings"].append(
                                {
                                    "entity_id": new_id,
                                    "warning": f"Dependencies konnten nicht automatisch aktualisiert werden: {str(e)}",
                                }
                            )
                    else:
                        # Only friendly name changed
                        results["success"].append(
                            {
                                "old_id": old_id,
                                "new_id": old_id,  # ID bleibt gleich
                                "message": f"Friendly Name aktualisiert zu: {friendly_name}",
                            }
                        )
                else:
                    # Keine Änderung nötig
                    results["skipped"].append(
                        {
                            "entity_id": old_id,
                            "message": "Bereits korrekt benannt",
                        }
                    )

            except Exception as e:
                results["failed"].append({"entity_id": old_id, "error": str(e)})

    finally:
        await ws.disconnect()

    # Delete preview
    del renamer_state["proposed_changes"][preview_id]

    # Invalidate broken references cache after changes
    invalidate_reference_checker_cache()

    return jsonify(results)


@app.route("/api/execute_direct", methods=["POST"])
def execute_direct():
    """Enqueue a batch entity rename as a background job and return the job.

    Applying many renames can exceed the Ingress timeout, so the whole batch is
    handed to the worker and the frontend polls the returned job. The entities
    payload is read here in the request thread (no request context in the worker).
    """
    data = request.json or {}
    entities = data.get("entities", [])
    if not entities:
        return jsonify({"error": "No entities selected"}), 400

    job = new_job("execute_direct", {"entities": entities}, job_id=uuid.uuid4().hex)
    renamer_state["job_store"].save(job)
    renamer_state["worker"].enqueue(job)
    return jsonify(job), 202


async def execute_direct_handler(job, ctx):
    """Apply a batch of entity renames, reporting progress per entity.

    Runs inside the worker (serial, off the request path). Renames each entity
    (id + friendly name), enables disabled ones when configured, and rewrites
    references in automations/scenes/scripts. Returns the same result shape the
    endpoint used to return so the UI summary is unchanged.
    """
    entities = job["payload"]["entities"]

    base_url = os.getenv("HA_URL")
    token = os.getenv("HA_TOKEN")
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

    results = {
        "success": [],
        "failed": [],
        "skipped": [],
        "dependency_warnings": [],
    }

    ws = HomeAssistantWebSocket(ws_url, token)
    await ws.connect()

    try:
        entity_registry = EntityRegistry(ws)
        dependency_updater = DependencyUpdater(base_url, token)

        # Pre-fetch states once for all dependency updates (performance optimization)
        logger.info("Pre-fetching states for dependency updates...")
        cached_states = await dependency_updater.get_states()
        logger.info(f"Cached {len(cached_states)} states")

        total = len(entities)
        ctx.progress(0, total)

        for index, entity_data in enumerate(entities):
            old_id = entity_data.get("old_id")
            new_id = entity_data.get("new_id")
            friendly_name = entity_data.get("new_name")

            if not old_id or not new_id:
                results["failed"].append({"entity_id": old_id, "error": "Missing old_id or new_id"})
                ctx.progress(index + 1, total, current=old_id or "")
                continue

            try:
                # Check if entity is disabled and if we should enable it
                entity_reg = renamer_state["restructurer"].entities.get(old_id, {})
                current_name = entity_reg.get("original_name") or entity_reg.get("name")

                # Skip only if BOTH ID and name are unchanged
                id_unchanged = old_id == new_id
                name_unchanged = friendly_name == current_name
                if id_unchanged and name_unchanged:
                    results["skipped"].append({"entity_id": old_id, "reason": "No change needed"})
                    ctx.progress(index + 1, total, current=old_id)
                    continue

                # Log what's changing
                if id_unchanged:
                    logger.info(f"Name-only change for {old_id}: '{current_name}' -> '{friendly_name}'")
                else:
                    logger.info(f"ID change: {old_id} -> {new_id}, name: '{friendly_name}'")

                is_disabled = entity_reg.get("disabled_by") is not None
                should_enable = is_disabled and os.getenv("ENABLE_DISABLED_ENTITIES", "false").lower() == "true"

                # Rename entity
                await entity_registry.rename_entity(old_id, new_id, friendly_name, enable=should_enable)

                if should_enable:
                    logger.info(f"Enabled and renamed disabled entity: {old_id} -> {new_id}")

                # Update dependencies (automations, scenes, scripts)
                dep_results = await dependency_updater.update_all_dependencies(old_id, new_id, cached_states)
                if dep_results.get("total_failed", 0) > 0:
                    # Collect all failed updates from scenes, scripts, automations
                    failed_updates = (
                        dep_results.get("scenes", {}).get("failed", [])
                        + dep_results.get("scripts", {}).get("failed", [])
                        + dep_results.get("automations", {}).get("failed", [])
                    )
                    results["dependency_warnings"].append(
                        {"entity_id": old_id, "new_id": new_id, "failed_updates": failed_updates}
                    )

                results["success"].append(
                    {
                        "old_id": old_id,
                        "new_id": new_id,
                        "message": f"Entity renamed successfully: {old_id} -> {new_id}",
                    }
                )
                logger.info(f"Successfully renamed: {old_id} -> {new_id}")
                ctx.log("RENAME", f"{old_id} -> {new_id}")

            except Exception as e:
                logger.error(f"Error renaming entity {old_id}: {e}")
                results["failed"].append({"entity_id": old_id, "error": str(e)})
                ctx.log("ERROR", f"{old_id}: {e}")

            ctx.progress(index + 1, total, current=old_id)

    finally:
        await ws.disconnect()

    # Invalidate broken references cache after changes
    invalidate_reference_checker_cache()

    results["message"] = f"{len(results['success'])} entities renamed"
    return results


renamer_state["worker"].register("execute_direct", execute_direct_handler)


@app.route("/api/stats")
def get_stats():
    """Hole Statistiken über alle Entities"""
    # Create new event loop for this request
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_get_stats_async())
    finally:
        loop.close()


async def _get_stats_async():
    """Async implementation of get_stats"""
    client = await init_client()
    states = await client.get_states()

    stats = {
        "total_entities": len(states),
        "domains": {},
        "areas": len(renamer_state.get("areas", {})),
    }

    for state in states:
        domain = state["entity_id"].split(".")[0]
        stats["domains"][domain] = stats["domains"].get(domain, 0) + 1

    return jsonify(stats)


@app.route("/api/dependencies/<entity_id>")
def get_dependencies(entity_id):
    """Hole Dependencies für eine Entity"""
    # Create new event loop for this request
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_get_dependencies_async(entity_id))
    finally:
        loop.close()


async def _get_dependencies_async(entity_id):
    """Async implementation of get_dependencies"""
    dependencies = {}
    logger.info(f"Suche Dependencies für: {entity_id}")

    try:
        client = await init_client()
        # Hole alle States um Scenes zu finden
        states = await client.get_states()

        # Suche in Scenes
        scene_refs = []
        for state in states:
            if state["entity_id"].startswith("scene."):
                # Scene Entities sind in den Attributes
                scene_entities = state.get("attributes", {}).get("entity_id", [])
                if isinstance(scene_entities, list) and entity_id in scene_entities:
                    scene_refs.append(state["entity_id"])

        if scene_refs:
            dependencies["Scenes"] = scene_refs

        # Suche in Gruppen
        group_refs = []
        for state in states:
            if state["entity_id"].startswith("group."):
                group_entities = state.get("attributes", {}).get("entity_id", [])
                if isinstance(group_entities, list) and entity_id in group_entities:
                    group_refs.append(state["entity_id"])

        if group_refs:
            dependencies["Groups"] = group_refs

        # Suche in Scripts
        script_refs = []
        for state in states:
            if state["entity_id"].startswith("script."):
                # Check if entity is used in the script
                state_str = json.dumps(state.get("attributes", {}))
                if entity_id in state_str:
                    script_refs.append(state["entity_id"])

        if script_refs:
            dependencies["Scripts"] = script_refs

        # Suche in Automations
        automation_refs = []
        logger.info(f"Suche Automations die {entity_id} verwenden...")

        # Filtere alle Automation States
        automation_states = [s for s in states if s["entity_id"].startswith("automation.")]
        logger.info(f"Gefunden: {len(automation_states)} Automations")

        # Check each automation
        for i, automation_state in enumerate(automation_states):
            automation_entity_id = automation_state["entity_id"]
            automation_name = automation_state.get("attributes", {}).get("friendly_name", automation_entity_id)

            logger.debug(f"Prüfe Automation {i+1}/{len(automation_states)}: {automation_name}")

            # Check the automation attributes
            attributes = automation_state.get("attributes", {})

            # Log die ersten paar Automations komplett
            if i < 3:
                logger.debug(f"Automation {automation_name} attributes keys: {list(attributes.keys())}")

            # Suche in den gesamten Attributes (inkl. last_triggered, etc.)
            attributes_str = json.dumps(attributes)

            # Log wenn "Diele" im Namen ist
            if "diele" in automation_name.lower():
                logger.info(f"Automation mit 'Diele' im Namen: {automation_name}")
                logger.debug(f"Attributes (erste 500 Zeichen): {attributes_str[:500]}")

            # Check if the entity is mentioned in the attributes
            if entity_id in attributes_str:
                logger.info(f"Entity {entity_id} gefunden in Automation: {automation_name}")
                automation_refs.append(automation_entity_id)

            # Special handling for blueprint-based automations
            # Diese haben oft ihre Entity-Referenzen in den "variables" oder "use_blueprint" Feldern
            if "use_blueprint" in attributes:
                blueprint_data = attributes.get("use_blueprint", {})
                blueprint_str = json.dumps(blueprint_data)
                logger.debug(f"Blueprint-Automation gefunden: {automation_name}")
                if entity_id in blueprint_str:
                    logger.info(f"Entity {entity_id} gefunden in Blueprint-Automation: {automation_name}")
                    if automation_entity_id not in automation_refs:
                        automation_refs.append(automation_entity_id)

        # If no automations were found via states, get the configurations via REST API
        if not automation_refs:
            logger.info("Versuche Automation-Konfigurationen über REST API zu laden...")
            try:
                base_url = os.getenv("HA_URL")
                token = os.getenv("HA_TOKEN")
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }

                # Gehe durch alle gefundenen Automations und hole ihre Configs
                for automation_state in automation_states:
                    automation_id = automation_state.get("attributes", {}).get("id")
                    automation_name = automation_state.get("attributes", {}).get(
                        "friendly_name", automation_state["entity_id"]
                    )

                    if automation_id:
                        # Get the automation config via REST API
                        config_url = f"{base_url}/api/config/automation/config/{automation_id}"

                        async with aiohttp.ClientSession() as session:
                            async with session.get(config_url, headers=headers) as response:
                                if response.status == 200:
                                    config = await response.json()
                                    config_str = json.dumps(config)

                                    # Debug for Diele automation
                                    if "diele" in automation_name.lower():
                                        logger.debug(f"Config für {automation_name}: {config_str[:500]}...")

                                    if entity_id in config_str:
                                        logger.info(f"Entity {entity_id} gefunden in Automation: {automation_name}")
                                        automation_refs.append(automation_state["entity_id"])
                                else:
                                    logger.warning(
                                        f"Fehler beim Abrufen der Config für {automation_name}: {response.status}"
                                    )

            except Exception as e:
                logger.error(f"Fehler beim Laden der Automation-Configs über REST API: {e}")

        if automation_refs:
            dependencies["Automations"] = automation_refs
        else:
            logger.info(f"Keine Automations gefunden die {entity_id} verwenden")

    except Exception as e:
        logger.error(f"Fehler beim Laden der Dependencies: {e}")
        dependencies = {"error": str(e)}

    return jsonify(dependencies)


# Global reference checker instance (cached)
_reference_checker: Optional[ReferenceChecker] = None


def get_reference_checker() -> ReferenceChecker:
    """Get or create the reference checker instance."""
    global _reference_checker
    base_url = os.getenv("HA_URL")
    token = os.getenv("HA_TOKEN")
    if _reference_checker is None:
        _reference_checker = ReferenceChecker(base_url, token)
    return _reference_checker


def invalidate_reference_checker_cache():
    """Invalidate the reference checker cache."""
    if _reference_checker is not None:
        _reference_checker.invalidate_cache()


@app.route("/api/broken_references")
def get_broken_references():
    """Hole alle broken references (verwaiste Entity-Referenzen)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_get_broken_references_async())
    finally:
        loop.close()


async def _get_broken_references_async():
    """Async implementation of get_broken_references."""
    force_refresh = request.args.get("refresh", "false").lower() == "true"

    try:
        checker = get_reference_checker()

        # Get entity registry from restructurer if available (for area_id lookup)
        entity_registry = None
        if renamer_state.get("restructurer") and renamer_state["restructurer"].entities:
            entity_registry = renamer_state["restructurer"].entities

        broken = await checker.scan_all_references(use_cache=not force_refresh, entity_registry=entity_registry)

        return jsonify(
            {
                "broken": [ref.to_dict() for ref in broken],
                "total_broken": len(broken),
                "cached": not force_refresh and checker._broken_refs_cache is not None,
            }
        )
    except Exception as e:
        logger.error(f"Error scanning broken references: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/suggestions/<path:missing_entity_id>")
def get_suggestions(missing_entity_id):
    """Hole Ersatz-Vorschläge für eine fehlende Entity."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_get_suggestions_async(missing_entity_id))
    finally:
        loop.close()


async def _get_suggestions_async(missing_entity_id):
    """Async implementation of get_suggestions."""
    try:
        checker = get_reference_checker()
        suggestions = await checker.get_suggestions(missing_entity_id)

        return jsonify({"suggestions": [sug.to_dict() for sug in suggestions], "missing_entity_id": missing_entity_id})
    except Exception as e:
        logger.error(f"Error getting suggestions for {missing_entity_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/fix_reference", methods=["POST"])
def fix_reference():
    """Ersetze eine Entity-Referenz in einer Config."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_fix_reference_async())
    finally:
        loop.close()


async def _fix_reference_async():
    """Async implementation of fix_reference.

    Fixes ALL broken references with the same missing_entity_id, not just one.
    This way, when user maps entity A -> B, it applies everywhere.
    """
    data = request.json
    is_valid, error = validate_json_input(data, ["old_entity_id", "new_entity_id"])
    if not is_valid:
        return jsonify({"error": error}), 400

    old_entity_id = sanitize_entity_id(data.get("old_entity_id"))
    new_entity_id = sanitize_entity_id(data.get("new_entity_id"))

    try:
        base_url = os.getenv("HA_URL")
        token = os.getenv("HA_TOKEN")

        logger.info(f"Fixing ALL references: {old_entity_id} -> {new_entity_id}")

        # Get all broken references to find all configs with this missing entity
        checker = get_reference_checker()
        broken_refs = await checker.scan_all_references(use_cache=True)

        # Filter to only those with matching missing_entity_id
        refs_to_fix = [r for r in broken_refs if r.missing_entity_id == old_entity_id]
        logger.info(f"Found {len(refs_to_fix)} references to fix for {old_entity_id}")

        if not refs_to_fix:
            return jsonify({"success": False, "error": f"No broken references found for {old_entity_id}"}), 404

        # Use dependency updater to replace the references
        updater = DependencyUpdater(base_url, token)
        states = await updater.get_states()

        # Build lookup for numeric IDs
        state_lookup = {s["entity_id"]: s for s in states}

        results = {"fixed": [], "failed": []}

        for ref in refs_to_fix:
            success = False
            config_id = ref.config_id

            if ref.config_type == "automation":
                state = state_lookup.get(config_id)
                if state:
                    numeric_id = state.get("attributes", {}).get("id")
                    if numeric_id:
                        success = await updater.update_automation_entities(
                            config_id, numeric_id, old_entity_id, new_entity_id
                        )

            elif ref.config_type == "scene":
                state = state_lookup.get(config_id)
                if state:
                    numeric_id = state.get("attributes", {}).get("id")
                    if numeric_id:
                        success = await updater.update_scene_entities(
                            config_id, numeric_id, old_entity_id, new_entity_id
                        )

            elif ref.config_type == "script":
                success = await updater.update_script_entities(config_id, old_entity_id, new_entity_id)

            if success:
                results["fixed"].append(config_id)
                logger.info(f"Fixed {ref.config_type} {config_id}")
            else:
                results["failed"].append(config_id)
                logger.warning(f"Failed to fix {ref.config_type} {config_id}")

        # Invalidate cache after fixes
        invalidate_reference_checker_cache()

        total_fixed = len(results["fixed"])
        total_failed = len(results["failed"])
        logger.info(f"Fixed {total_fixed} references, {total_failed} failed")

        if total_fixed > 0:
            return jsonify(
                {
                    "success": True,
                    "old_entity_id": old_entity_id,
                    "new_entity_id": new_entity_id,
                    "fixed_count": total_fixed,
                    "failed_count": total_failed,
                    "fixed": results["fixed"],
                    "failed": results["failed"],
                }
            )
        else:
            return (
                jsonify({"success": False, "error": "Failed to update any references", "failed": results["failed"]}),
                500,
            )

    except Exception as e:
        logger.error(f"Error fixing reference: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/all_entities")
def get_all_entities():
    """Hole alle Entities für Autocomplete."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_get_all_entities_async())
    finally:
        loop.close()


async def _get_all_entities_async():
    """Async implementation of get_all_entities."""
    try:
        checker = get_reference_checker()
        entities = await checker.get_all_entities()

        return jsonify({"entities": entities, "total": len(entities)})
    except Exception as e:
        logger.error(f"Error getting all entities: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/update_mapping", methods=["POST"])
def update_mapping():
    """Aktualisiert das Mapping für eine einzelne Entity"""
    # Create new event loop for this request
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_update_mapping_async())
    finally:
        loop.close()


async def _update_mapping_async():
    """Async implementation of update_mapping"""
    data = request.json
    is_valid, error = validate_json_input(data, ["preview_id", "old_id", "new_id"])
    if not is_valid:
        return jsonify({"error": error}), 400

    preview_id = sanitize_string(data.get("preview_id"), max_length=64)
    old_id = sanitize_entity_id(data.get("old_id"))
    new_id = sanitize_entity_id(data.get("new_id"))
    new_name = sanitize_name(data.get("new_name"))

    if not preview_id or not old_id or not new_id:
        return jsonify({"error": "Invalid preview_id, old_id or new_id"}), 400

    # Hole das gespeicherte Mapping
    if preview_id not in renamer_state["proposed_changes"]:
        return jsonify({"error": "Preview nicht gefunden"}), 404

    # Aktualisiere das Mapping
    proposed = renamer_state["proposed_changes"][preview_id]
    if old_id in proposed["mapping"]:
        proposed["mapping"][old_id] = (new_id, new_name)

        # Also update in the changes list for the UI
        for device_group in proposed["changes"]:
            for entity in device_group["entities"]:
                if entity["old_id"] == old_id:
                    entity["new_id"] = new_id
                    entity["new_name"] = new_name
                    entity["needs_rename"] = old_id != new_id
                    break

        logger.info(f"Updated mapping for {old_id} -> {new_id}")
        return jsonify({"success": True})
    else:
        return jsonify({"error": "Entity nicht im Mapping gefunden"}), 404


@app.route("/api/set_entity_override", methods=["POST"])
def set_entity_override():
    """Setze Entity Name Override"""
    # Create new event loop for this request
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_set_entity_override_async())
    finally:
        loop.close()


async def _set_entity_override_async():
    """Async implementation of set_entity_override"""
    data = request.json
    is_valid, error = validate_json_input(data, ["registry_id"])
    if not is_valid:
        return jsonify({"error": error}), 400

    registry_id = sanitize_registry_id(data.get("registry_id"))
    override_name = sanitize_name(data.get("override_name"))

    if not registry_id:
        return jsonify({"error": "Invalid registry ID"}), 400

    try:
        # Speichere Override
        if override_name:
            renamer_state["naming_overrides"].set_entity_override(registry_id, override_name)
        else:
            renamer_state["naming_overrides"].remove_entity_override(registry_id)

        # Finde die Entity ID basierend auf der Registry ID
        entity_id = None
        for eid, entity in renamer_state["restructurer"].entities.items():
            if entity.get("id") == registry_id:
                entity_id = eid
                break

        # Calculate the new entity ID and friendly name with the override
        new_id = None
        new_friendly_name = None

        if entity_id:
            # Get current entity state for proper calculation
            client = await init_client()
            states = await client.get_states()
            entity_state = next(
                (s for s in states if s["entity_id"] == entity_id), {"entity_id": entity_id, "attributes": {}}
            )

            # Calculate with current override
            new_id, new_friendly_name = renamer_state["restructurer"].generate_new_entity_id(entity_id, entity_state)

            if override_name:
                # Update the friendly name in Home Assistant
                base_url = os.getenv("HA_URL")
                token = os.getenv("HA_TOKEN")
                ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

                ws = HomeAssistantWebSocket(ws_url, token)
                await ws.connect()

                try:
                    entity_registry = EntityRegistry(ws)
                    # Update nur den Friendly Name, nicht die Entity ID
                    await entity_registry.update_entity(entity_id=entity_id, name=new_friendly_name)
                    logger.info(f"Entity {entity_id} Friendly Name aktualisiert zu: {new_friendly_name}")
                finally:
                    await ws.disconnect()

        return jsonify(
            {
                "success": True,
                "new_id": new_id,
                "new_friendly_name": new_friendly_name,
                "has_override": bool(override_name),
            }
        )
    except Exception as e:
        logger.error(f"Fehler beim Setzen des Entity Override: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/enable_entity", methods=["POST"])
def enable_entity():
    """Enable a disabled entity"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_enable_entity_async())
    finally:
        loop.close()


async def _enable_entity_async():
    """Async implementation of enable_entity"""
    data = request.json
    is_valid, error = validate_json_input(data, ["entity_id"])
    if not is_valid:
        return jsonify({"error": error}), 400

    entity_id = sanitize_entity_id(data.get("entity_id"))

    if not entity_id:
        return jsonify({"error": "Invalid entity ID"}), 400

    try:
        base_url = os.getenv("HA_URL")
        token = os.getenv("HA_TOKEN")
        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

        ws = HomeAssistantWebSocket(ws_url, token)
        await ws.connect()

        try:
            entity_registry = EntityRegistry(ws)
            await entity_registry.update_entity(entity_id=entity_id, enable=True)
            logger.info(f"Enabled entity: {entity_id}")

            return jsonify({"success": True, "entity_id": entity_id})
        finally:
            await ws.disconnect()

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error enabling entity {entity_id}: {error_msg}")

        # Check if device is disabled
        if "Device is disabled" in error_msg:
            return (
                jsonify(
                    {
                        "error": "device_disabled",
                        "message": "Cannot enable entity because the device is disabled. Enable the device first.",
                    }
                ),
                400,
            )

        return jsonify({"error": error_msg}), 500


@app.route("/api/enable_all", methods=["POST"])
def enable_all():
    """Enqueue enabling a batch of disabled entities as a background job.

    Enabling many entities one WS call at a time can exceed the Ingress timeout,
    so the batch runs in the worker and the frontend polls the returned job.
    """
    data = request.json or {}
    entity_ids = [eid for eid in (sanitize_entity_id(x) for x in data.get("entity_ids", [])) if eid]
    if not entity_ids:
        return jsonify({"error": "No entities selected"}), 400

    job = new_job("enable_all", {"entity_ids": entity_ids}, job_id=uuid.uuid4().hex)
    renamer_state["job_store"].save(job)
    renamer_state["worker"].enqueue(job)
    return jsonify(job), 202


async def enable_all_handler(job, ctx):
    """Enable a batch of disabled entities, reporting progress per entity."""
    entity_ids = job["payload"]["entity_ids"]

    base_url = os.getenv("HA_URL")
    token = os.getenv("HA_TOKEN")
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

    enabled = []
    failed = []
    ws = HomeAssistantWebSocket(ws_url, token)
    await ws.connect()
    try:
        entity_registry = EntityRegistry(ws)
        total = len(entity_ids)
        ctx.progress(0, total)
        for index, entity_id in enumerate(entity_ids):
            try:
                await entity_registry.update_entity(entity_id=entity_id, enable=True)
                enabled.append(entity_id)
                logger.info(f"Enabled entity: {entity_id}")
                ctx.log("ENABLE", entity_id)
            except Exception as e:
                logger.error(f"Error enabling entity {entity_id}: {e}")
                failed.append({"entity_id": entity_id, "error": str(e)})
                ctx.log("ERROR", f"{entity_id}: {e}")
            ctx.progress(index + 1, total, current=entity_id)
    finally:
        await ws.disconnect()

    return {"enabled": enabled, "failed": failed, "message": f"{len(enabled)} entities enabled"}


renamer_state["worker"].register("enable_all", enable_all_handler)


@app.route("/api/enable_device", methods=["POST"])
def enable_device():
    """Enable a disabled device"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_enable_device_async())
    finally:
        loop.close()


async def _enable_device_async():
    """Async implementation of enable_device"""
    data = request.json
    is_valid, error = validate_json_input(data, ["device_id"])
    if not is_valid:
        return jsonify({"error": error}), 400

    device_id = sanitize_registry_id(data.get("device_id"))

    if not device_id:
        return jsonify({"error": "Invalid device ID"}), 400

    try:
        base_url = os.getenv("HA_URL")
        token = os.getenv("HA_TOKEN")
        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

        ws = HomeAssistantWebSocket(ws_url, token)
        await ws.connect()

        try:
            device_registry = DeviceRegistry(ws)
            await device_registry.enable_device(device_id)
            logger.info(f"Enabled device: {device_id}")

            return jsonify({"success": True, "device_id": device_id})
        finally:
            await ws.disconnect()

    except Exception as e:
        logger.error(f"Error enabling device {device_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/assign_device_area", methods=["POST"])
def assign_device_area():
    """Assign a device to an area"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_assign_device_area_async())
    finally:
        loop.close()


async def _assign_device_area_async():
    """Async implementation of assign_device_area"""
    data = request.json
    is_valid, error = validate_json_input(data, ["device_id"])
    if not is_valid:
        return jsonify({"error": error}), 400

    device_id = sanitize_registry_id(data.get("device_id"))
    area_id = data.get("area_id")  # Can be None to remove area assignment

    if not device_id:
        return jsonify({"error": "Invalid device ID"}), 400

    # Sanitize area_id if provided
    if area_id:
        area_id = sanitize_registry_id(area_id)

    try:
        base_url = os.getenv("HA_URL")
        token = os.getenv("HA_TOKEN")
        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

        ws = HomeAssistantWebSocket(ws_url, token)
        await ws.connect()

        try:
            device_registry = DeviceRegistry(ws)
            await device_registry.assign_area(device_id, area_id)
            logger.info(f"Assigned device {device_id} to area {area_id}")

            return jsonify({"success": True, "device_id": device_id, "area_id": area_id})
        finally:
            await ws.disconnect()

    except Exception as e:
        logger.error(f"Error assigning device {device_id} to area: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/rename_log", methods=["GET"])
def rename_log_lookup():
    """Resolve an entity_id against the rename audit log.

    Query parameter ``entity_id`` (the old / vanished id). Follows the rename
    chain forward and returns the current id plus the hop history, e.g.::

        GET /api/rename_log?entity_id=light.kitchen_old

        {
          "query": "light.kitchen_old",
          "found": true,
          "renamed": true,
          "current_entity_id": "light.kitchen_ceiling",
          "history": [ {"timestamp": ..., "old_entity_id": ...,
                        "new_entity_id": ..., "friendly_name": ...} ]
        }

    ``found`` is ``false`` when the id was never renamed (or is unknown).
    """
    entity_id = request.args.get("entity_id", "").strip()
    if not entity_id:
        return jsonify({"error": "Missing required query parameter: entity_id"}), 400

    rename_log = renamer_state["rename_log"]
    return jsonify(rename_log.search(entity_id))


@app.route("/api/api_token", methods=["GET"])
def api_token_status():
    """Return whether an external API token exists (never the token itself).

    Ingress-only: the access gate refuses direct (non-Ingress) requests here.
    """
    return jsonify(renamer_state["api_token_store"].status())


@app.route("/api/api_token", methods=["POST"])
def api_token_generate():
    """Generate (or replace) the external API token and return it once.

    The plaintext is shown only in this response; only its hash is stored, so it
    cannot be retrieved again. Ingress-only.
    """
    store = renamer_state["api_token_store"]
    token = store.generate()
    result = {"token": token}
    result.update(store.status())
    return jsonify(result)


@app.route("/api/api_token", methods=["DELETE"])
def api_token_revoke():
    """Revoke the external API token, disabling external access. Ingress-only."""
    renamer_state["api_token_store"].revoke()
    return jsonify(renamer_state["api_token_store"].status())


@app.route("/api/rename_entity", methods=["POST"])
def rename_entity():
    """Directly rename a single entity (entity_id and/or friendly_name)"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_rename_entity_async())
    finally:
        loop.close()


async def _rename_entity_async():
    """Async implementation of rename_entity"""
    data = request.json
    is_valid, error = validate_json_input(data, ["old_entity_id"])
    if not is_valid:
        return jsonify({"error": error}), 400

    old_entity_id = sanitize_entity_id(data.get("old_entity_id"))
    new_entity_id = sanitize_entity_id(data.get("new_entity_id")) if data.get("new_entity_id") else None
    new_friendly_name = sanitize_name(data.get("new_friendly_name"))

    if not old_entity_id:
        return jsonify({"error": "Invalid old_entity_id"}), 400

    if not new_entity_id and not new_friendly_name:
        return jsonify({"error": "new_entity_id or new_friendly_name required"}), 400

    try:
        base_url = os.getenv("HA_URL")
        token = os.getenv("HA_TOKEN")
        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

        ws = HomeAssistantWebSocket(ws_url, token)
        await ws.connect()

        try:
            entity_registry = EntityRegistry(ws)

            # Check if anything actually needs to change
            id_changed = new_entity_id and old_entity_id != new_entity_id
            name_needs_update = new_friendly_name is not None

            if not id_changed and not name_needs_update:
                return jsonify({"success": True, "skipped": True, "message": "No changes needed"})

            # Perform the rename
            result = await entity_registry.rename_entity(
                old_entity_id=old_entity_id,
                new_entity_id=new_entity_id if id_changed else None,
                friendly_name=new_friendly_name,
            )

            if result:
                logger.info(
                    f"Renamed entity: {old_entity_id} -> {new_entity_id or old_entity_id} ({new_friendly_name})"
                )

                response_data = {
                    "success": True,
                    "old_entity_id": old_entity_id,
                    "new_entity_id": new_entity_id or old_entity_id,
                    "new_friendly_name": new_friendly_name,
                }

                # Update dependencies (automations, scenes, scripts) if entity ID changed
                if id_changed:
                    try:
                        dependency_updater = DependencyUpdater(base_url, token)
                        dep_results = await dependency_updater.update_all_dependencies(old_entity_id, new_entity_id)

                        # Always include dependency results for debugging
                        response_data["dependencies_checked"] = True
                        response_data["dependencies_updated"] = {
                            "total": dep_results["total_success"],
                            "scenes": dep_results["scenes"]["success"],
                            "scripts": dep_results["scripts"]["success"],
                            "automations": dep_results["automations"]["success"],
                        }

                        if dep_results["total_success"] > 0:
                            logger.info(f"Updated {dep_results['total_success']} dependencies for {old_entity_id}")

                        if dep_results["total_failed"] > 0:
                            response_data["dependencies_failed"] = {
                                "total": dep_results["total_failed"],
                                "scenes": dep_results["scenes"]["failed"],
                                "scripts": dep_results["scripts"]["failed"],
                                "automations": dep_results["automations"]["failed"],
                            }
                            logger.warning(
                                f"Failed to update {dep_results['total_failed']} dependencies for {old_entity_id}"
                            )
                    except Exception as dep_error:
                        logger.error(f"Error updating dependencies for {old_entity_id}: {dep_error}")
                        response_data["dependencies_checked"] = False
                        response_data["dependencies_error"] = str(dep_error)
                else:
                    response_data["dependencies_checked"] = False
                    response_data["dependencies_reason"] = "entity_id_unchanged"

                # Invalidate broken references cache after rename
                invalidate_reference_checker_cache()

                return jsonify(response_data)
            else:
                return jsonify({"error": "Rename failed"}), 500

        finally:
            await ws.disconnect()

    except Exception as e:
        logger.error(f"Error renaming entity {old_entity_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/delete_entity", methods=["POST"])
def delete_entity():
    """Delete an orphaned entity from the registry."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_delete_entity_async())
    finally:
        loop.close()


async def _delete_entity_async():
    """Async implementation of delete_entity."""
    data = request.json
    is_valid, error = validate_json_input(data, ["entity_id"])
    if not is_valid:
        return jsonify({"error": error}), 400

    entity_id = sanitize_entity_id(data.get("entity_id"))

    if not entity_id:
        return jsonify({"error": "Invalid entity_id"}), 400

    logger.info(f"Deleting entity: {entity_id}")

    try:
        base_url = os.getenv("HA_URL")
        token = os.getenv("HA_TOKEN")
        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

        ws = HomeAssistantWebSocket(ws_url, token)
        await ws.connect()

        try:
            entity_registry = EntityRegistry(ws)
            await entity_registry.remove_entity(entity_id)

            return jsonify({"success": True, "entity_id": entity_id, "message": f"Entity {entity_id} deleted"})

        finally:
            await ws.disconnect()

    except Exception as e:
        logger.error(f"Error deleting entity {entity_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/rename_device", methods=["POST"])
def rename_device():
    """Enqueue a device rename as a background job and return the job.

    Renaming a device cascades to all its entities, which can take long enough to
    exceed the Ingress/proxy timeout. Input is validated and sanitized here in the
    request thread (the worker thread has no request context); the work itself
    runs in the background worker and the frontend polls the returned job.
    """
    data = request.json
    is_valid, error = validate_json_input(data, ["device_id", "new_name"])
    if not is_valid:
        return jsonify({"error": error}), 400

    device_id = sanitize_registry_id(data.get("device_id"))
    new_name = sanitize_name(data.get("new_name"))

    if not device_id:
        return jsonify({"error": "Invalid device ID"}), 400

    if not new_name:
        return jsonify({"error": "Invalid device name"}), 400

    # Do not rename the same device twice concurrently.
    for existing in renamer_state["job_store"].list_unfinished():
        if existing.get("type") == "rename_device" and existing.get("payload", {}).get("device_id") == device_id:
            return (
                jsonify({"error": "A rename for this device is already in progress", "job_id": existing["job_id"]}),
                409,
            )

    job = new_job("rename_device", {"device_id": device_id, "new_name": new_name}, job_id=uuid.uuid4().hex)
    renamer_state["job_store"].save(job)
    renamer_state["worker"].enqueue(job)
    return jsonify(job), 202


def _capture_device_entity_names(
    restructurer: EntityRestructurer,
    device_id: str,
    states: list[dict[str, Any]],
) -> dict[str, str]:
    """Capture entity-specific names before changing their device name."""
    states_by_id = {state["entity_id"]: state for state in states}
    return {
        entity_id: restructurer.build_naming_context(entity_id, states_by_id.get(entity_id, {}))["entity"]
        for entity_id, entity_info in restructurer.entities.items()
        if entity_info.get("device_id") == device_id
    }


def _plan_device_entity_changes(
    restructurer: EntityRestructurer,
    device_id: str,
    states: list[dict[str, Any]],
    entity_names: dict[str, str],
) -> list[tuple[str, str, str]]:
    """Generate entity changes for a renamed device with the active templates."""
    states_by_id = {state["entity_id"]: state for state in states}
    changes = [
        (
            entity_id,
            *restructurer.generate_new_entity_id(
                entity_id,
                states_by_id.get(entity_id, {}),
                entity_names.get(entity_id),
            ),
        )
        for entity_id, entity_info in restructurer.entities.items()
        if entity_info.get("device_id") == device_id
    ]
    return restructurer.deduplicate_entity_ids(changes)


async def rename_device_handler(job, ctx):
    """Rename a device and cascade the rename to all of its entities.

    Renames the device, aligns the Z2M friendly name, then for every entity of
    the device rebuilds its friendly name and entity id and rewrites references
    in automations/scenes/scripts. Progress is reported per entity so the UI can
    show a live bar. Runs inside the worker (serial, off the request path).
    """
    payload = job["payload"]
    device_id = payload["device_id"]
    new_name = payload["new_name"]

    base_url = os.getenv("HA_URL")
    token = os.getenv("HA_TOKEN")
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

    ws = HomeAssistantWebSocket(ws_url, token)
    await ws.connect()

    try:
        # Ensure restructurer is loaded
        await init_client()
        await renamer_state["restructurer"].load_structure(ws)

        dependency_updater = DependencyUpdater(base_url, token)
        cached_states = await dependency_updater.get_states()
        entity_names = _capture_device_entity_names(
            renamer_state["restructurer"],
            device_id,
            cached_states,
        )

        device_registry = DeviceRegistry(ws)
        success = await device_registry.rename_device(device_id, new_name)

        if not success:
            raise RuntimeError("Failed to rename device in Home Assistant")

        # Align the Z2M friendly name with the new name (Z2M devices only, non-fatal)
        z2m_sync = await _sync_z2m_name(device_registry, device_id, new_name)

        # The shared generator needs the updated device registry entry to render
        # the active entity ID and entity-name templates correctly.
        await renamer_state["restructurer"].load_structure(ws)

        # Update entities: rename ID + friendly name + update dependencies
        entities_updated = 0
        entities_failed = 0
        entities_skipped = 0
        dependencies_updated = 0

        logger.info("=== Starting entity rename after device rename ===")
        logger.info(f"Device ID: {device_id}")
        logger.info(f"New device name: {new_name}")

        entity_registry = EntityRegistry(ws)

        entity_changes = _plan_device_entity_changes(
            renamer_state["restructurer"],
            device_id,
            cached_states,
            entity_names,
        )
        total = len(entity_changes)
        logger.info(f"Found {total} entities for device {device_id}")
        ctx.progress(0, total)
        processed = 0

        for old_entity_id, new_entity_id, new_friendly_name in entity_changes:
            logger.info(f"  {old_entity_id} -> {new_entity_id} ('{new_friendly_name}')")

            # Skip if nothing would change
            current_name = renamer_state["restructurer"].entities[old_entity_id].get("name") or ""
            if new_entity_id == old_entity_id and new_friendly_name == current_name:
                logger.info("  Skipping - no changes needed")
                entities_skipped += 1
                processed += 1
                ctx.progress(processed, total, current=old_entity_id)
                continue

            try:
                # Rename entity (ID + friendly name)
                id_changed = new_entity_id != old_entity_id
                await entity_registry.rename_entity(
                    old_entity_id, new_entity_id if id_changed else None, new_friendly_name
                )
                entities_updated += 1
                logger.info("  SUCCESS: Renamed entity")
                ctx.log("RENAME", f"{old_entity_id} -> {new_entity_id}")

                # Update dependencies if ID changed
                if id_changed:
                    dep_results = await dependency_updater.update_all_dependencies(
                        old_entity_id, new_entity_id, cached_states
                    )
                    dep_count = dep_results.get("total_success", 0)
                    dependencies_updated += dep_count
                    if dep_count > 0:
                        logger.info(f"  Updated {dep_count} dependencies")

            except Exception as e:
                entities_failed += 1
                logger.error(f"  FAILED: {e}")
                ctx.log("ERROR", f"{old_entity_id}: {e}")

            processed += 1
            ctx.progress(processed, total, current=old_entity_id)

        # Reload structure to reflect changes
        await renamer_state["restructurer"].load_structure(ws)

        logger.info("=== Entity rename complete ===")
        logger.info(
            f"Updated: {entities_updated}, Failed: {entities_failed}, "
            f"Skipped: {entities_skipped}, Dependencies: {dependencies_updated}"
        )

        message = f"Device renamed to: {new_name}"
        if entities_updated > 0:
            message += f" ({entities_updated} entities"
            if dependencies_updated > 0:
                message += f", {dependencies_updated} dependencies"
            message += " updated)"
        if entities_failed > 0:
            message += f" ({entities_failed} failed)"

        return {
            "success": True,
            "message": message,
            "entities_updated": entities_updated,
            "entities_failed": entities_failed,
            "dependencies_updated": dependencies_updated,
            "z2m_synced": z2m_sync.get("synced"),
            "z2m_failed": (z2m_sync.get("error") if z2m_sync.get("supported") and not z2m_sync.get("synced") else None),
        }

    finally:
        await ws.disconnect()


renamer_state["worker"].register("rename_device", rename_device_handler)


@app.route("/api/sync_z2m_name", methods=["POST"])
def sync_z2m_name():
    """Gleicht den Z2M-friendly_name eines Geräts an seinen HA-Namen an (kein HA-Rename)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_sync_z2m_name_request_async())
    finally:
        loop.close()


async def _sync_z2m_name_request_async():
    data = request.json or {}
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    token = os.getenv("HA_TOKEN")
    ws = HomeAssistantWebSocket(_ws_url(), token)
    await ws.connect()
    try:
        await renamer_state["restructurer"].load_structure(ws)
        device = renamer_state["restructurer"].devices.get(device_id)
        if not device:
            return jsonify({"error": "Unknown device"}), 404
        ha_name = device.get("name_by_user") or device.get("name", "")
        device_registry = DeviceRegistry(ws)
        result = await _sync_z2m_name(device_registry, device_id, ha_name)
        if not result.get("supported"):
            return jsonify({"success": False, "supported": False, "message": "Not a Z2M device"}), 400
        if result.get("error"):
            return jsonify({"success": False, "error": result["error"]}), 500
        return jsonify({"success": True, "synced": result.get("synced"), "name": ha_name})
    finally:
        await ws.disconnect()


# === New API Endpoints for Hierarchy and Type Mappings ===


@app.route("/api/hierarchy")
def get_hierarchy():
    """Get complete hierarchy data for the 3-panel UI."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_get_hierarchy_async())
    finally:
        loop.close()


def _strip_prefix(full_name: str, prefix: str) -> str:
    """Strip a prefix from a name (case-insensitive)."""
    if not full_name or not prefix:
        return full_name or ""
    full_lower = full_name.lower().strip()
    prefix_lower = prefix.lower().strip()

    if full_lower.startswith(prefix_lower + " "):
        return full_name[len(prefix) + 1 :].strip()
    if full_lower == prefix_lower:
        return ""
    return full_name


async def _get_hierarchy_async():
    """Async implementation of get_hierarchy."""
    try:
        await load_areas_and_entities()
        restructurer = renamer_state["restructurer"]

        # Build orphan lookup from entities_by_area (where is_orphan is detected)
        orphan_entities = set()
        for area_data in renamer_state.get("entities_by_area", {}).values():
            for domain_entities in area_data.get("domains", {}).values():
                for e in domain_entities:
                    if e.get("is_orphan"):
                        orphan_entities.add(e["entity_id"])

        # Build area lookup for prefix stripping
        area_names = {}
        for area_id, area_data in restructurer.areas.items():
            area_names[area_id] = area_data.get("name", "")

        # Build hierarchy response
        floors = [
            {"id": floor_id, "name": floor_data.get("name", "")} for floor_id, floor_data in restructurer.floors.items()
        ]
        areas = []
        for area_id, area_data in restructurer.areas.items():
            areas.append(
                {
                    "id": area_id,
                    "name": area_data.get("name", ""),
                    "floor_id": area_data.get("floor_id"),
                }
            )

        # Z2M-friendly_names einmal lesen (für Drift-Erkennung); leer ohne MQTT/Z2M.
        from integration_bridge import extract_z2m_ieee

        z2m_names = {}
        try:
            mqtt_bridge = await _ensure_mqtt_bridge()
            if mqtt_bridge is not None:
                z2m_names = await mqtt_bridge.get_z2m_names()
        except Exception as e:  # noqa: BLE001 - Drift-Check darf die Hierarchie nie blockieren
            logger.warning("Z2M name fetch failed: %s", e)

        # Build device lookup with base names (strip area prefix)
        first_entity_by_device = {}
        for candidate_id, candidate in restructurer.entities.items():
            candidate_device_id = candidate.get("device_id")
            if candidate_device_id and candidate_device_id not in first_entity_by_device:
                first_entity_by_device[candidate_device_id] = candidate_id

        devices = []
        for device_id, device_data in restructurer.devices.items():
            raw_name = device_data.get("name_by_user") or device_data.get("name", "")
            area_id = device_data.get("area_id")

            # Strip area prefix from device name
            # e.g., "Büro Homepod" with area "Büro" -> "Homepod"
            representative_entity_id = first_entity_by_device.get(device_id)
            if representative_entity_id:
                naming_context = restructurer.build_naming_context(
                    representative_entity_id,
                    restructurer.entities[representative_entity_id],
                )
                base_name = naming_context["device"]
                suggested_name = restructurer.naming_templates.render("device_name", naming_context)
            else:
                base_name = raw_name
                if area_id and area_id in area_names:
                    base_name = _strip_prefix(raw_name, area_names[area_id])
                suggested_name = raw_name

            # Extract integration(s) from identifiers
            # identifiers is like [["homekit_controller", "xxx"], ["zha", "yyy"]]
            # Some have format like "homekit_controller:accessory-id" - we only want the domain part
            integrations = []
            for identifier in device_data.get("identifiers", []):
                if isinstance(identifier, (list, tuple)) and len(identifier) >= 1:
                    domain = identifier[0]
                    # Strip anything after colon (e.g., "homekit_controller:accessory-id" -> "homekit_controller")
                    if ":" in domain:
                        domain = domain.split(":")[0]
                    if domain and domain not in integrations:
                        integrations.append(domain)

            # Z2M-Namens-Drift: Z2M-friendly_name vs. HA-Name (raw_name)
            z2m_ieee = extract_z2m_ieee(device_data)
            z2m_current = z2m_names.get(z2m_ieee) if z2m_ieee else None
            z2m_drift = bool(z2m_ieee and z2m_current is not None and z2m_current != raw_name)

            devices.append(
                {
                    "id": device_id,
                    "name": raw_name,  # Original HA name
                    "base_name": base_name,  # Stripped base name for display
                    "suggested_name": suggested_name,
                    "area_id": area_id,
                    "manufacturer": device_data.get("manufacturer"),
                    "model": device_data.get("model"),
                    "integrations": integrations,  # e.g., ["homekit", "zha"]
                    "disabled_by": device_data.get("disabled_by"),
                    "is_z2m": bool(z2m_ieee),
                    "z2m_current_name": z2m_current,  # aktueller Z2M-friendly_name (oder None)
                    "z2m_drift": z2m_drift,  # True, wenn Z2M-Name != HA-Name
                }
            )

        # Resolve the suggestions as one batch: two entities of a device often
        # render the same ID, and a suggestion that collides cannot be applied.
        suggestions = {
            entity_id: (new_entity_id, suggested_name)
            for entity_id, new_entity_id, suggested_name in restructurer.deduplicate_entity_ids(
                [
                    (entity_id, *restructurer.generate_new_entity_id(entity_id, entity_data))
                    for entity_id, entity_data in restructurer.entities.items()
                ]
            )
        }

        type_counts = _type_key_counts(restructurer)
        type_integration_counts = _type_key_integration_counts(restructurer)
        type_model_counts = _type_key_model_counts(restructurer)

        entities = []
        for entity_id, entity_data in restructurer.entities.items():
            registry_id = entity_data.get("id", "")
            override = renamer_state["naming_overrides"].get_entity_override(registry_id)
            type_key = _entity_type_key(entity_data)
            device_class = entity_data.get("device_class") or entity_data.get("original_device_class")
            device_id = entity_data.get("device_id")
            device_data = restructurer.devices.get(device_id, {}) if device_id else {}
            area_id = entity_data.get("area_id") or device_data.get("area_id")

            # Get original friendly name
            original_name = entity_data.get("name") or entity_data.get("original_name") or ""

            entity_context = restructurer.build_naming_context(entity_id, entity_data)
            base_name = entity_context["entity"]

            suggested_entity_id, suggested_entity_name = suggestions[entity_id]

            entities.append(
                {
                    "id": entity_id,
                    "registry_id": registry_id,
                    "device_id": device_id,
                    "area_id": area_id,
                    "device_class": device_class,
                    "original_name": original_name,  # Original HA friendly name
                    "base_name": base_name,  # Stripped base name for editing
                    "suggested_name": suggested_entity_name,
                    "suggested_entity_id": suggested_entity_id,
                    "override_name": override.get("name") if override else None,
                    "has_override": override is not None,
                    "disabled_by": entity_data.get("disabled_by"),
                    "labels": entity_data.get("labels", []),
                    "platform": entity_data.get("platform"),  # Integration that provides this entity
                    "is_orphan": entity_id in orphan_entities,  # Entity restored but not provided by integration
                    "translation_key": entity_data.get("translation_key"),
                    "unique_id": entity_data.get("unique_id"),
                    # Where the entity part of the name came from, for the UI to explain.
                    "resolution": restructurer.last_resolutions.get(entity_id),
                    "type_key": type_key,
                    "type_count": type_counts.get(type_key, 0) if type_key else 0,
                    "type_integration_count": (
                        type_integration_counts.get((type_key, entity_data.get("platform")), 0) if type_key else 0
                    ),
                    "device_model": _entity_model(restructurer, entity_data),
                    "type_model_count": (
                        type_model_counts.get((type_key, _entity_model(restructurer, entity_data)), 0)
                        if type_key
                        else 0
                    ),
                    "name_owner": "unknown",
                }
            )

        return jsonify(
            {
                "floors": floors,
                "areas": areas,
                "devices": devices,
                "entities": entities,
                "stats": {
                    "area_count": len(areas),
                    "device_count": len(devices),
                    "entity_count": len(entities),
                },
            }
        )

    except Exception as e:
        logger.error(f"Error getting hierarchy: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/naming_templates", methods=["GET", "PUT"])
def naming_templates_config() -> Any:
    """Read or update the active naming templates."""
    manager = renamer_state["naming_templates"]
    if request.method == "GET":
        return jsonify(manager.get_config())

    data = request.json
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON input"}), 400
    try:
        preset = data.get("preset")
        if preset and preset != "custom" and "templates" not in data:
            config = manager.apply_preset(preset)
        else:
            config = manager.set_templates(data.get("templates", {}))
        return jsonify(config)
    except NamingTemplateError as error:
        return jsonify({"error": str(error)}), 400
    except OSError as error:
        logger.error("Failed to save naming templates: %s", error)
        return jsonify({"error": "Failed to save naming templates"}), 500


@app.route("/api/naming_templates/preview", methods=["POST"])
def preview_naming_templates() -> Any:
    """Render a sample context without persisting template changes."""
    data = request.json
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON input"}), 400
    templates = data.get("templates", {})
    context = data.get("context", {})
    manager = renamer_state["naming_templates"]
    try:
        manager.validate_templates(templates)
        values = {field: str(context.get(field) or "") for field in manager.get_config()["allowed_fields"]}
        rendered = {}
        for key, template in templates.items():
            rendered[key] = manager.render_template(template, values, normalize=key == "entity_id")
        domain = values.get("domain") or "sensor"
        # Mirror generate_new_entity_id: never emit a bare "<domain>." when the
        # template renders empty, or the caller would send it as a rename.
        object_id = rendered["entity_id"] or values.get("entity_id") or ""
        rendered["entity_id"] = f"{domain}.{object_id}" if object_id else ""

        # Previewing a real entity: number the result away from IDs other
        # entities hold, the way a batched rename does, so what the preview
        # offers can actually be applied.
        restructurer = renamer_state.get("restructurer")
        current_entity_id = f"{domain}.{values.get('entity_id')}" if values.get("entity_id") else ""
        if object_id and restructurer and current_entity_id in restructurer.entities:
            taken = set(restructurer.entities) - {current_entity_id}
            suffix = 1
            while rendered["entity_id"] in taken:
                suffix += 1
                rendered["entity_id"] = f"{domain}.{object_id}_{suffix}"
            if suffix > 1 and rendered.get("entity_name"):
                rendered["entity_name"] = f"{rendered['entity_name']} {suffix}"

        return jsonify({"rendered": rendered})
    except (NamingTemplateError, KeyError, ValueError) as error:
        return jsonify({"error": str(error)}), 400


@app.route("/api/type_mappings")
def get_type_mappings():
    """Get all type mappings (system defaults and user overrides)."""
    try:
        language = request.args.get("lang", "en")
        type_mappings = renamer_state["type_mappings"]

        raw_mappings = type_mappings.get_all_known_types(language)

        # Transform to frontend-expected format
        all_mappings = []
        for m in raw_mappings:
            has_user = m.get("user_mapping") is not None
            all_mappings.append(
                {
                    "key": m["key"],
                    "system_default": m.get("system_default"),
                    "effective_value": m.get("user_mapping") or m.get("system_default") or m["key"].title(),
                    "has_user_override": has_user,
                    "source": m.get("source", "unknown"),
                }
            )

        return jsonify(
            {
                "mappings": all_mappings,
                "language": language,
                "user_mapping_count": len(type_mappings.get_all_user_mappings()),
            }
        )

    except Exception as e:
        logger.error(f"Error getting type mappings: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/type_mappings/user", methods=["POST"])
def set_user_type_mapping():
    """Set a user type mapping."""
    try:
        data = request.json
        is_valid, error = validate_json_input(data, ["type_key", "translation"])
        if not is_valid:
            return jsonify({"error": error}), 400

        type_key = sanitize_string(data.get("type_key"), max_length=64)
        # Use sanitize_string instead of sanitize_name to avoid HTML escaping
        # (apostrophes become &#x27; with sanitize_name)
        translation = sanitize_string(data.get("translation"))

        if not type_key or not translation:
            return jsonify({"error": "Invalid type_key or translation"}), 400

        type_mappings = renamer_state["type_mappings"]
        type_mappings.set_user_mapping(type_key, translation)

        return jsonify(
            {
                "success": True,
                "type_key": type_key,
                "translation": translation,
            }
        )

    except Exception as e:
        logger.error(f"Error setting user type mapping: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/type_mappings/user/<type_key>", methods=["DELETE"])
def delete_user_type_mapping(type_key):
    """Delete a user type mapping."""
    try:
        # Sanitize URL parameter
        type_key = sanitize_string(type_key, max_length=64)
        if not type_key:
            return jsonify({"error": "Invalid type_key"}), 400

        type_mappings = renamer_state["type_mappings"]
        removed = type_mappings.remove_user_mapping(type_key)

        if removed:
            return jsonify({"success": True, "type_key": type_key})
        else:
            return jsonify({"error": f"No user mapping found for {type_key}"}), 404

    except Exception as e:
        logger.error(f"Error deleting user type mapping: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/learn_mapping", methods=["POST"])
def learn_type_mapping():
    """Learn a type mapping from entity rename."""
    try:
        data = request.json
        is_valid, error = validate_json_input(data, ["type_key", "translation"])
        if not is_valid:
            return jsonify({"error": error}), 400

        type_key = sanitize_string(data.get("type_key"), max_length=64)
        # Use sanitize_string instead of sanitize_name to avoid HTML escaping
        translation = sanitize_string(data.get("translation"))

        if not type_key or not translation:
            return jsonify({"error": "Invalid type_key or translation"}), 400

        # Direkt über type_mappings (immer initialisiert; restructurer kann None sein,
        # wenn die Hierarchie noch nicht geladen wurde).
        renamer_state["type_mappings"].set_user_mapping(type_key, translation)

        return jsonify(
            {
                "success": True,
                "type_key": type_key,
                "translation": translation,
                "message": f"Learned mapping: {type_key} -> {translation}",
            }
        )

    except Exception as e:
        logger.error(f"Error learning type mapping: {e}")
        return jsonify({"error": str(e)}), 500


async def _sync_ha_language(ws) -> None:
    """Follow Home Assistant's language and load the names it uses.

    Home Assistant is the one place a user sets their language, and its own
    translations cover far more languages than this add-on could maintain.
    """
    rules = renamer_state["naming_rules"]
    try:
        config = await ws.get_config()
        language = (config.get("language") or "").split("-")[0]
    except Exception as error:
        logger.warning("Could not read the Home Assistant language: %s", error)
        language = rules.language
    if language and language != rules.language:
        logger.info("Following the Home Assistant language: %s", language)
        rules.set_language(language)
        renamer_state["type_mappings"]._refresh_user_view()
    integrations = {
        entity.get("platform") for entity in renamer_state["restructurer"].entities.values() if entity.get("platform")
    }
    await ha_translations.load(ws, rules.language, integrations)


@app.route("/api/ha/language")
def get_ha_language():
    """The language the app speaks, which is the one Home Assistant is set to."""
    return jsonify({"language": renamer_state["naming_rules"].language})


def _entity_type_key(entity_data: dict) -> str:
    """The key that groups entities of one type: translation_key, else the canonical supplied name."""
    translation_key = entity_data.get("translation_key")
    if translation_key:
        return f"tk:{translation_key}"
    native = canon(entity_data.get("original_name") or "")
    return f"name:{native}" if native else ""


def _type_key_counts(restructurer) -> dict:
    counts: dict = {}
    for entity_data in restructurer.entities.values():
        key = _entity_type_key(entity_data)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def _type_key_integration_counts(restructurer) -> dict:
    """Entities per type and integration: "Tür" from Matter is a door sensor, from Miele a fridge."""
    counts: dict = {}
    for entity_data in restructurer.entities.values():
        key = _entity_type_key(entity_data)
        integration = entity_data.get("platform")
        if key and integration:
            counts[(key, integration)] = counts.get((key, integration), 0) + 1
    return counts


def _entity_model(restructurer, entity_data: dict) -> str:
    device = restructurer.devices.get(entity_data.get("device_id") or "", {})
    return device.get("model") or ""


def _type_key_model_counts(restructurer) -> dict:
    """Entities per type and device model, the narrowest scope a rule can take."""
    counts: dict = {}
    for entity_data in restructurer.entities.values():
        key = _entity_type_key(entity_data)
        model = _entity_model(restructurer, entity_data)
        if key and model:
            counts[(key, model)] = counts.get((key, model), 0) + 1
    return counts


def _rule_affected_counts(restructurer, rules):
    """How many loaded entities each rule applies to, or None when nothing is loaded.

    Without the entity list a rule's reach is unknown, which is not the same as
    zero: reporting zero would brand every rule as useless right after a start.
    """
    if restructurer is None or not restructurer.entities:
        return None
    language = rules.language
    counts = {rule["id"]: 0 for rule in rules.rules}
    # Entities sharing a type, integration and model all land on the same rule,
    # so group them first: thousands of entities become a few hundred lookups.
    groups: dict = {}
    for entity_data in restructurer.entities.values():
        group = (
            entity_data.get("translation_key") or "",
            entity_data.get("original_name") or "",
            entity_data.get("platform") or None,
            _entity_model(restructurer, entity_data) or None,
        )
        groups[group] = groups.get(group, 0) + 1
    for (translation_key, native, integration, model), size in groups.items():
        rule = rules.find("translation_key", translation_key, integration, language, model) or rules.find(
            "name", native, integration, language, model
        )
        if rule:
            counts[rule["id"]] = counts.get(rule["id"], 0) + size
    return counts


def _rule_payload(rule: dict, affected: dict) -> dict:
    rules = renamer_state["naming_rules"]
    mappings = renamer_state["type_mappings"]
    language = rules.language
    builtin = None
    if rule["match"]["kind"] != "translation_key":
        builtin = mappings.find_system_translation(rule["match"]["value"], language, rule["match"].get("integration"))
    return {
        **rule,
        "affected": affected.get(rule["id"], 0) if affected is not None else None,
        "redundant": rules.is_redundant(rule, language, builtin),
    }


@app.route("/api/naming/settings", methods=["GET", "PUT"])
def naming_settings():
    """Language the type rules are applied in, and the spelling of shown types."""
    rules = renamer_state["naming_rules"]
    if request.method == "PUT":
        data = request.json if isinstance(request.json, dict) else {}
        language = sanitize_string(data.get("language", ""), max_length=8)
        display_case = sanitize_string(data.get("display_case", ""), max_length=16)
        if not language and not display_case:
            return jsonify({"error": "language or display_case required"}), 400
        try:
            if language:
                rules.set_language(language)
            if display_case:
                rules.set_display_case(display_case)
        except NamingRuleError as error:
            return jsonify({"error": str(error)}), 400
        renamer_state["type_mappings"]._refresh_user_view()
    return jsonify({"language": rules.language, "display_case": rules.display_case})


async def _ensure_registry_loaded() -> None:
    """Make sure the entity list is there, without the cost of a full hierarchy.

    Reading the registries takes about a tenth of a second; the states and the
    rename suggestions that /api/hierarchy also builds take ten times that and
    say nothing about how far a rule reaches.
    """
    restructurer = renamer_state.get("restructurer")
    if restructurer is None:
        await init_client()
        restructurer = renamer_state["restructurer"]
    if restructurer.entities:
        return
    token = os.getenv("HA_TOKEN", os.getenv("SUPERVISOR_TOKEN"))
    base_url = os.getenv("HA_URL")
    ws_url = (
        base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
        if base_url
        else "ws://supervisor/core/websocket"
    )
    ws = HomeAssistantWebSocket(ws_url, token)
    try:
        await ws.connect()
        await restructurer.load_structure(ws)
        await _sync_ha_language(ws)
    except Exception as error:
        logger.warning("Could not load the registries: %s", error)
    finally:
        try:
            await ws.disconnect()
        except Exception:
            pass


@app.route("/api/naming/rules", methods=["GET", "POST"])
def naming_rules_collection():
    """List type rules with their reach, or create one."""
    rules = renamer_state["naming_rules"]
    if request.method == "POST":
        data = request.json if isinstance(request.json, dict) else {}
        match = data.get("match") or {}
        targets = data.get("targets") or {}
        language = rules.language
        target = targets.get(language) or data.get("value")
        if not target:
            return jsonify({"error": f"target for language {language} required"}), 400
        try:
            rule = rules.upsert(
                sanitize_string(match.get("kind", "name"), max_length=32),
                sanitize_string(match.get("value", ""), max_length=128),
                sanitize_string(match.get("integration") or "", max_length=64) or None,
                language,
                sanitize_string(target),
                source="user",
                model=sanitize_string(match.get("model") or "", max_length=128) or None,
            )
        except NamingRuleError as error:
            return jsonify({"error": str(error)}), 400
        renamer_state["type_mappings"]._refresh_user_view()
        affected = _rule_affected_counts(renamer_state.get("restructurer"), rules)
        return jsonify({"rule": _rule_payload(rule, affected)})

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_ensure_registry_loaded())
    finally:
        loop.close()
    affected = _rule_affected_counts(renamer_state.get("restructurer"), rules)
    language = request.args.get("lang") or rules.language
    system = []
    for entry in renamer_state["type_mappings"].get_all_known_types(language):
        if entry.get("system_default"):
            system.append({"key": entry["key"], "value": entry["system_default"], "source": entry.get("source")})
    return jsonify(
        {
            "language": rules.language,
            "rules": [_rule_payload(rule, affected) for rule in rules.rules],
            "system": system,
        }
    )


@app.route("/api/naming/rules/<rule_id>", methods=["PUT", "DELETE"])
def naming_rule_item(rule_id):
    rules = renamer_state["naming_rules"]
    rule_id = sanitize_string(rule_id, max_length=32)
    if request.method == "DELETE":
        if not rules.delete(rule_id):
            return jsonify({"error": "unknown rule"}), 404
        renamer_state["type_mappings"]._refresh_user_view()
        return jsonify({"success": True})
    data = request.json if isinstance(request.json, dict) else {}
    try:
        integration = ...
        if "integration" in data:
            integration = sanitize_string(data.get("integration") or "", max_length=64) or None
        model = ...
        if "model" in data:
            model = sanitize_string(data.get("model") or "", max_length=128) or None
        rule = rules.update(rule_id, targets=data.get("targets"), integration=integration, model=model)
    except NamingRuleError as error:
        return jsonify({"error": str(error)}), 400
    renamer_state["type_mappings"]._refresh_user_view()
    affected = _rule_affected_counts(renamer_state.get("restructurer"), rules)
    return jsonify({"rule": _rule_payload(rule, affected)})


@app.route("/api/naming/learn", methods=["POST"])
def naming_learn():
    """Turn a corrected type into a rule; the server decides the key."""
    data = request.json
    is_valid, error = validate_json_input(data, ["entity_id", "value"])
    if not is_valid:
        return jsonify({"error": error}), 400
    entity_id = sanitize_string(data.get("entity_id"), max_length=255)
    value = sanitize_string(data.get("value"))
    scope = data.get("scope") or "global"
    restructurer = renamer_state.get("restructurer")
    entity = restructurer.entities.get(entity_id) if restructurer else None
    if not entity:
        return jsonify({"error": "unknown entity"}), 404
    if not value:
        return jsonify({"error": "value required"}), 400
    integration = (entity.get("platform") or None) if scope in ("integration", "model") else None
    model = _entity_model(restructurer, entity) or None if scope == "model" else None
    if entity.get("translation_key"):
        kind, key = "translation_key", entity["translation_key"]
    else:
        kind, key = "name", entity.get("original_name") or ""
    if not key:
        return jsonify({"error": "entity has no name to derive a rule from"}), 400
    rules = renamer_state["naming_rules"]
    try:
        rule = rules.upsert(
            kind, key, integration, rules.language, value, source="learned", learned_from=entity_id, model=model
        )
    except NamingRuleError as error:
        return jsonify({"error": str(error)}), 400
    renamer_state["type_mappings"]._refresh_user_view()
    affected = _rule_affected_counts(restructurer, rules)
    return jsonify({"rule": _rule_payload(rule, affected)})


@app.route("/api/naming/originals")
def naming_originals():
    """Distinct supplied names, for picking a rule key instead of typing one."""
    restructurer = renamer_state.get("restructurer")
    query = canon(request.args.get("q", ""))
    seen: dict = {}
    for entity_data in restructurer.entities.values() if restructurer else []:
        native = entity_data.get("original_name") or ""
        key = canon(native)
        if not key or (query and query not in key):
            continue
        item = seen.setdefault(
            key,
            {
                "key": key,
                "example": native,
                "translation_key": entity_data.get("translation_key"),
                "count": 0,
                "integrations": set(),
            },
        )
        item["count"] += 1
        if entity_data.get("platform"):
            item["integrations"].add(entity_data["platform"])
    items = sorted(seen.values(), key=lambda item: (-item["count"], item["key"]))[:50]
    for item in items:
        item["integrations"] = sorted(item["integrations"])
    return jsonify({"originals": items})


@app.route("/api/naming/migration/backup", methods=["GET"])
def naming_migration_backup():
    """Hand out the mappings file as it was before the migration."""
    report = renamer_state["naming_rules"].migration_report or {}
    backup = report.get("backup")
    if not backup:
        return jsonify({"error": "no migration backup"}), 404
    name = "user_type_mappings.json"
    if not os.path.isfile(os.path.join(backup, name)):
        return jsonify({"error": "backup file is gone"}), 404
    stamp = os.path.basename(os.path.normpath(backup))
    return send_from_directory(
        backup,
        name,
        as_attachment=True,
        download_name=f"user_type_mappings_{stamp}.json",
    )


@app.route("/api/naming/migration", methods=["GET"])
def naming_migration():
    return jsonify({"report": renamer_state["naming_rules"].migration_report})


@app.route("/api/naming/migration/resolve", methods=["POST"])
def naming_migration_resolve():
    data = request.json
    is_valid, error = validate_json_input(data, ["rule_id", "value"])
    if not is_valid:
        return jsonify({"error": error}), 400
    rules = renamer_state["naming_rules"]
    try:
        rule = rules.choose_alternative(sanitize_string(data["rule_id"], max_length=32), sanitize_string(data["value"]))
    except NamingRuleError as error:
        return jsonify({"error": str(error)}), 400
    renamer_state["type_mappings"]._refresh_user_view()
    return jsonify({"rule": rule})


@app.route("/api/naming/preview", methods=["POST"])
def naming_preview():
    """Render one entity's name and ID with an optional replacement type, server-side."""
    data = request.json
    is_valid, error = validate_json_input(data, ["entity_id"])
    if not is_valid:
        return jsonify({"error": error}), 400
    entity_id = sanitize_string(data.get("entity_id"), max_length=255)
    type_value = data.get("type_value")
    restructurer = renamer_state.get("restructurer")
    entity = restructurer.entities.get(entity_id) if restructurer else None
    if not entity:
        return jsonify({"error": "unknown entity"}), 404
    entity_name = sanitize_string(type_value) if isinstance(type_value, str) else None
    new_entity_id, new_name = restructurer.generate_new_entity_id(entity_id, entity, entity_name)
    resolution = restructurer.last_resolutions.get(entity_id)
    # Number away from IDs other entities hold, as a batched rename would.
    domain, _, object_id = new_entity_id.partition(".")
    taken = set(restructurer.entities) - {entity_id}
    suffix = 1
    while new_entity_id in taken and object_id:
        suffix += 1
        new_entity_id = f"{domain}.{object_id}_{suffix}"
    if suffix > 1 and new_name:
        new_name = f"{new_name} {suffix}"
    return jsonify({"rendered": {"entity_name": new_name, "entity_id": new_entity_id}, "resolution": resolution})


SETTINGS_SECTIONS = ("naming", "rules", "system")


@app.route("/settings")
@app.route("/settings/<section>")
def settings_page(section: str = "naming"):
    """Render one section of the settings.

    Each section is its own address so it survives a reload and can be linked
    to. The nesting depth differs between /settings and /settings/<section>,
    so the page is told where its own root is.
    """
    if request.path.rstrip("/").count("/") < 2:
        # One depth for every section keeps relative asset and API paths valid.
        return redirect("settings/naming")
    if section not in SETTINGS_SECTIONS:
        section = "naming"
    base_href = "../"
    version = str(int(time.time()))
    response = make_response(render_template("settings.html", version=version, section=section, base_href=base_href))
    # Prevent browser from caching the HTML page
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# =============================================================================
# Device Swap (Geräte-Austausch)
# =============================================================================


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ws_url() -> str:
    base_url = os.getenv("HA_URL")
    return base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"


def _device_snapshot(restructurer, device_id: str) -> dict:
    """Erzeugt einen kompakten, persistierbaren Snapshot eines Geräts."""
    from integration_bridge import extract_integrations

    d = restructurer.devices.get(device_id, {}) or {}
    return {
        "device_id": device_id,
        "name": d.get("name_by_user") or d.get("name") or "",
        "integrations": extract_integrations(d),
        "config_entries": d.get("config_entries", []),
        "identifiers": d.get("identifiers", []),
    }


def _device_entities(restructurer, device_id: str) -> list:
    """Alle Entity-Registry-Einträge eines Geräts."""
    return [e for e in restructurer.entities.values() if e.get("device_id") == device_id]


@app.route("/api/bridge/status", methods=["GET"])
def bridge_status():
    """Status der Integrations-Bridge (welche nativen Operationen möglich sind)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        bridge = loop.run_until_complete(_ensure_mqtt_bridge())
    except Exception as e:  # noqa: BLE001 - Status darf nie crashen
        logger.warning("bridge_status MQTT check failed: %s", e)
        bridge = None
    finally:
        loop.close()

    z2m_ok = bridge is not None and getattr(bridge, "connected", False)
    return jsonify(
        {
            "mqtt_available": z2m_ok,
            "z2m_supported": z2m_ok,
            "matter_remove_supported": True,
            "z2m_enabled": os.getenv("ENABLE_Z2M_BRIDGE", "true").lower() == "true",
        }
    )


@app.route("/api/swap/devices", methods=["GET"])
def swap_devices():
    """Liste aller Geräte (für die Auswahl im Swap-Wizard)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_swap_devices_async())
    finally:
        loop.close()


async def _swap_devices_async():
    from integration_bridge import extract_integrations

    await init_client()
    ws = HomeAssistantWebSocket(_ws_url(), os.getenv("HA_TOKEN"))
    await ws.connect()
    try:
        await renamer_state["restructurer"].load_structure(ws)
    finally:
        await ws.disconnect()

    restructurer = renamer_state["restructurer"]
    areas = {aid: a.get("name", "") for aid, a in restructurer.areas.items()}
    devices = []
    for device_id, d in restructurer.devices.items():
        entity_count = len(_device_entities(restructurer, device_id))
        devices.append(
            {
                "device_id": device_id,
                "name": d.get("name_by_user") or d.get("name") or "",
                "area": areas.get(d.get("area_id"), ""),
                "area_id": d.get("area_id"),
                "integrations": extract_integrations(d),
                "entity_count": entity_count,
            }
        )
    devices.sort(key=lambda x: (x["area"] or "~", x["name"]))
    return jsonify({"devices": devices})


@app.route("/api/jobs", methods=["GET"])
def jobs_unfinished():
    """List unfinished background jobs (for reconnect after a reload).

    Read-only and cheap, so it stays responsive while a long-running job is in
    flight. Atomic writes guarantee readers see a complete old-or-new job file.
    """
    return jsonify({"jobs": renamer_state["job_store"].list_unfinished()})


@app.route("/api/jobs/<job_id>", methods=["GET"])
def job_get(job_id):
    """Return the current state of a background job (for polling)."""
    job = renamer_state["job_store"].load(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/swap/jobs", methods=["GET"])
def swap_jobs():
    """Nicht abgeschlossene Swap-Jobs (für Resume)."""
    jobs = renamer_state["swap_store"].list_unfinished()
    return jsonify({"jobs": jobs})


@app.route("/api/swap/<job_id>", methods=["GET"])
def swap_job_get(job_id):
    """Aktueller Stand eines Swap-Jobs."""
    job = renamer_state["swap_store"].load(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/swap/propose", methods=["POST"])
def swap_propose():
    """Legt einen Swap-Job an und schlägt ein Entity-Mapping vor."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_swap_propose_async())
    finally:
        loop.close()


async def _swap_propose_async():
    data = request.json or {}
    old_id = (data.get("old_device_id") or "").strip()
    new_id = (data.get("new_device_id") or "").strip()
    if not old_id or not new_id:
        return jsonify({"error": "old_device_id and new_device_id required"}), 400
    if old_id == new_id:
        return jsonify({"error": "old and new device must differ"}), 400

    client = await init_client()
    states = await client.get_states()
    dashboard_refs = set()
    ws = HomeAssistantWebSocket(_ws_url(), os.getenv("HA_TOKEN"))
    await ws.connect()
    try:
        await renamer_state["restructurer"].load_structure(ws)
        dashboard_refs = await LovelaceUpdater(ws).get_referenced_entity_ids()
    finally:
        await ws.disconnect()

    restructurer = renamer_state["restructurer"]
    if old_id not in restructurer.devices or new_id not in restructurer.devices:
        return jsonify({"error": "Unknown device id"}), 404

    states_by_id = {s["entity_id"]: s for s in states}
    old_ents = _device_entities(restructurer, old_id)
    new_ents = _device_entities(restructurer, new_id)

    # Nur referenzierte (in use) alte Entities mappen - ungenutzte werden ohnehin
    # über die Device-Rename-Logik mitbenannt und brauchen kein Mapping.
    # in use = Automations/Scenes/Scripts (REST) + Dashboards (WS).
    ref_checker = ReferenceChecker(os.getenv("HA_URL"), os.getenv("HA_TOKEN"))
    referenced = await ref_checker.get_all_referenced_entity_ids()
    referenced |= dashboard_refs

    # Präfixe über ALLE Entities bestimmen, gemappt werden nur die in-use.
    proposal = propose_mapping(old_ents, new_ents, states_by_id, in_use_ids=referenced)
    proposal["old_total"] = len(old_ents)
    proposal["old_in_use"] = len([e for e in old_ents if e.get("entity_id") in referenced])

    old_snap = _device_snapshot(restructurer, old_id)
    new_snap = _device_snapshot(restructurer, new_id)

    now = _iso_now()
    job = {
        "version": device_swap.SCHEMA_VERSION,
        "job_id": uuid.uuid4().hex,
        "created": now,
        "updated": now,
        "state": device_swap.STATE_PROPOSED,
        "old_device": old_snap,
        "new_device": new_snap,
        "target_device_name": old_snap["name"],
        "old_device_disposition": device_swap.DISPOSITION_KEEP,
        # ALLE alten Entities (müssen freigemacht werden) und ALLE neuen (werden umbenannt)
        "old_device_entities": sorted(e["entity_id"] for e in old_ents),
        "new_device_entities": sorted(e["entity_id"] for e in new_ents),
        "proposal": proposal,
        "entity_mapping": [],
        "steps": {},
        "log": [],
    }
    renamer_state["swap_store"].save(job)
    return jsonify(job)


@app.route("/api/swap/<job_id>/confirm", methods=["POST"])
def swap_confirm(job_id):
    """Bestätigt Mapping + Disposition und friert den Job ein (CONFIRMED)."""
    data = request.json or {}
    store = renamer_state["swap_store"]
    job = store.load(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["state"] not in (device_swap.STATE_PROPOSED, device_swap.STATE_CONFIRMED):
        return jsonify({"error": f"Job cannot be confirmed in state {job['state']}"}), 409

    mapping = data.get("entity_mapping") or []
    disposition = data.get("old_device_disposition", device_swap.DISPOSITION_KEEP)
    valid_dispositions = {
        device_swap.DISPOSITION_KEEP,
        device_swap.DISPOSITION_DISABLE,
        device_swap.DISPOSITION_DELETE,
    }
    if disposition not in valid_dispositions:
        return jsonify({"error": "Invalid old_device_disposition"}), 400

    entity_mapping = []
    for pair in mapping:
        old_e = sanitize_entity_id(pair.get("old_entity_id"))
        new_e = sanitize_entity_id(pair.get("new_entity_id"))
        if not old_e or not new_e:
            continue
        entity_mapping.append({"old_entity_id": old_e, "new_entity_id_current": new_e, "status": "pending"})

    # Leeres Mapping ist zulässig (keine verwendeten Entities) - dann werden nur
    # Geräte umbenannt/behandelt, ohne Referenzen umzubiegen.
    job["entity_mapping"] = entity_mapping
    job["old_device_disposition"] = disposition
    job["state"] = device_swap.STATE_CONFIRMED
    job["updated"] = _iso_now()
    store.save(job)
    return jsonify(job)


@app.route("/api/swap/<job_id>/execute", methods=["POST"])
def swap_execute(job_id):
    """Enqueue swap execution/continuation as a background job (idempotent).

    The swap keeps its own persisted state machine and resume flow in swap_store;
    the worker just runs it serially, off the request path. The frontend polls
    api/swap/<job_id> for progress. Returns the swap job so the UI can start
    polling immediately.
    """
    store = renamer_state["swap_store"]
    job = store.load(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["state"] in (device_swap.STATE_PROPOSED, device_swap.STATE_ABORTED, device_swap.STATE_COMPLETED):
        return jsonify({"error": f"Job not runnable in state {job['state']}"}), 409

    generic = new_job("swap", {"swap_job_id": job_id}, job_id=uuid.uuid4().hex)
    renamer_state["job_store"].save(generic)
    renamer_state["worker"].enqueue(generic)
    return jsonify(job), 202


async def swap_execute_handler(job, ctx):
    """Run/continue a device swap inside the worker.

    Loads the swap job from swap_store and drives its SwapExecutor state machine.
    The executor persists progress per step/entity in swap_store (which the UI
    polls); this generic wrapper job only records that the run happened.
    """
    swap_job_id = job["payload"]["swap_job_id"]
    store = renamer_state["swap_store"]
    swap_job = store.load(swap_job_id)
    if not swap_job:
        raise RuntimeError(f"Swap job {swap_job_id} not found")

    client = await init_client()
    states = await client.get_states()
    token = os.getenv("HA_TOKEN")
    ws = HomeAssistantWebSocket(_ws_url(), token)
    await ws.connect()
    try:
        await renamer_state["restructurer"].load_structure(ws)
        device_registry = DeviceRegistry(ws)
        entity_registry = EntityRegistry(ws)
        dependency_updater = DependencyUpdater(os.getenv("HA_URL"), token)
        bridge = build_bridge(device_registry, mqtt_bridge=await _ensure_mqtt_bridge())
        executor = SwapExecutor(
            store=store,
            device_registry=device_registry,
            entity_registry=entity_registry,
            dependency_updater=dependency_updater,
            bridge=bridge,
            restructurer=renamer_state["restructurer"],
            states_by_id={s["entity_id"]: s for s in states},
            timestamp=_iso_now(),
            lovelace_updater=LovelaceUpdater(ws),
        )
        swap_job = await executor.run(swap_job)
    finally:
        await ws.disconnect()

    return {"swap_job_id": swap_job_id, "final_state": swap_job.get("state")}


renamer_state["worker"].register("swap", swap_execute_handler)


@app.route("/api/swap/<job_id>/abort", methods=["POST"])
def swap_abort(job_id):
    """Bricht einen noch nicht ausgeführten Job ab."""
    store = renamer_state["swap_store"]
    job = store.load(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["state"] not in (device_swap.STATE_PROPOSED, device_swap.STATE_CONFIRMED):
        return jsonify({"error": "Job already started; cannot abort, use resume instead"}), 409
    # Vor der Ausführung wurde nichts am System geändert -> Job ganz entfernen (keine Leiche).
    store.delete(job_id)
    return jsonify({"success": True, "deleted": job_id})


if __name__ == "__main__":
    # Erstelle Template-Verzeichnis
    os.makedirs("templates", exist_ok=True)

    # In Add-on mode, use port 5000 for Ingress
    port = int(os.getenv("WEB_UI_PORT", 5000))

    # Fail any generic jobs left running by a previous process, then start the
    # background worker before serving requests.
    renamer_state["worker"].reconcile_on_start()
    renamer_state["worker"].start()

    # Serve via Waitress (production-grade WSGI server) instead of the Werkzeug
    # development server, which prints a production warning on every launch.
    #
    # Default to a SINGLE worker thread: the previous Werkzeug dev server ran
    # with threaded=False, i.e. requests were handled serially. A lot of shared
    # global state (renamer_state, the singleton client/MQTT init, and the
    # read-modify-write JSON stores like NamingOverrides/SwapJobStore/RenameLog/
    # ApiTokenStore) has no locking and is only safe under that serial model.
    # threads=1 preserves that behaviour exactly while getting us off the dev
    # server. Raising WEB_UI_THREADS is only safe once those write paths are made
    # thread-safe.
    #
    # Note: the background JobWorker runs on its own thread regardless of this
    # setting, so long-running jobs already execute off the request path; the
    # generic JobStore uses atomic writes so poll requests read a consistent file.
    #
    # Set WEB_UI_DEV_SERVER=1 to fall back to the Werkzeug dev server (e.g. for
    # local debugging with the reloader).
    if os.getenv("WEB_UI_DEV_SERVER") == "1":
        print(f"\nStarting Web UI (Werkzeug dev server) on port {port}\n")
        app.run(debug=False, host="0.0.0.0", port=port)
    else:
        from waitress import serve

        threads = int(os.getenv("WEB_UI_THREADS", 1))
        print(f"\nStarting Web UI (Waitress, {threads} thread(s)) on port {port}\n")
        serve(app, host="0.0.0.0", port=port, threads=threads)
