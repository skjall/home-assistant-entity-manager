#!/usr/bin/env python3
"""
Web UI für Home Assistant Entity Renamer - Add-on Version
"""

import asyncio
from datetime import datetime, timezone
import html
import json
import logging
import os
import re
import time
from typing import Optional
import unicodedata
import uuid

import aiohttp
from flask import Flask, jsonify, make_response, render_template, request, send_from_directory
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from bridge_adapters import build_bridge
from dependency_updater import DependencyUpdater
from device_registry import DeviceRegistry
import device_swap
from device_swap import SwapExecutor, SwapJobStore, propose_mapping
from entity_registry import EntityRegistry
from entity_restructurer import EntityRestructurer
from ha_client import HomeAssistantClient
from ha_websocket import HomeAssistantWebSocket
from hierarchy_manager import normalize_name
from lovelace_updater import LovelaceUpdater
from naming_overrides import NamingOverrides
from reference_checker import ReferenceChecker
from type_mappings import TypeMappings

# Don't load .env in Add-on mode - use environment variables from Supervisor
# load_dotenv()

# Language-independent constant for entities without area assignment
UNASSIGNED_AREA = "__unassigned__"

app = Flask(__name__, static_folder="static", static_url_path="/static")
# Support for Ingress proxy headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
CORS(app)

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

# Global state
renamer_state = {
    "client": None,
    "restructurer": None,
    "areas": {},
    "entities_by_area": {},
    "proposed_changes": {},
    "naming_overrides": NamingOverrides("/data/naming_overrides.json"),
    "type_mappings": TypeMappings(user_mappings_path="/data/user_type_mappings.json"),
    "swap_store": SwapJobStore("/data/device_swaps"),
}


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


async def init_client():
    """Initialize the Home Assistant client and restructurer."""
    if not renamer_state["client"]:
        # In Add-on mode, use Supervisor API
        base_url = os.getenv("HA_URL", "http://supervisor/core")
        token = os.getenv("HA_TOKEN", os.getenv("SUPERVISOR_TOKEN"))
        logger.info("BETA VERSION - Entity Manager Add-on")
        logger.info(f"Connecting to Home Assistant at {base_url}")
        renamer_state["client"] = HomeAssistantClient(base_url, token)
        renamer_state["restructurer"] = EntityRestructurer(
            renamer_state["client"],
            renamer_state["naming_overrides"],
            type_mappings=renamer_state["type_mappings"],
        )
    return renamer_state["client"]


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
                if device_id and device_id in renamer_state["restructurer"].devices:
                    device = renamer_state["restructurer"].devices[device_id]
                    if device.get("area_id") and device["area_id"] in areas_dict:
                        area_name = areas_dict[device["area_id"]]
                elif entity_reg.get("area_id") and entity_reg["area_id"] in areas_dict:
                    area_name = areas_dict[entity_reg["area_id"]]

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
def serve_translations(filename):
    """Serve translation files"""
    return send_from_directory("translations/ui", filename)


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

            current_friendly_name = current_info.get("friendly_name", old_id)

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
                # Device naming logic:
                # - Device with area: suggested_name = "{area} {device_name}" (if not already prefixed)
                # - Device without area: suggested_name = current device name (no change)
                device_suggested_name = None
                if device_info:
                    current_device_name = device_info["name"]
                    has_real_area = area_name != UNASSIGNED_AREA

                    if has_real_area:
                        # Check if device name already starts with area name
                        area_normalized = area_name.lower()
                        device_normalized = current_device_name.lower()
                        if device_normalized.startswith(area_normalized):
                            # Already has area prefix, keep as is
                            device_suggested_name = current_device_name
                        else:
                            # Add area prefix
                            device_suggested_name = f"{area_name} {current_device_name}"
                    else:
                        # No area, keep device name as is
                        device_suggested_name = current_device_name

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
                    results["device_success"].append(
                        {
                            "device_id": device_id,
                            "new_name": new_device_name,
                            "message": f"Gerät erfolgreich umbenannt zu: {new_device_name}",
                        }
                    )

                    # Only rename entities that were explicitly selected
                    # Don't automatically rename all device entities when only device is selected
                    await renamer_state["restructurer"].load_structure(ws)

                    for entity_id in device_entities:
                        # Skip entities that weren't explicitly selected
                        if entity_id not in selected_entities:
                            logger.info(f"Skipping entity {entity_id} - not explicitly selected")
                            continue

                        # Hole Entity Info aus states
                        entity_state = next((s for s in states if s["entity_id"] == entity_id), None)
                        if entity_state:
                            # Generiere neuen Namen basierend auf aktuellem Device Namen
                            new_entity_id, new_friendly_name = renamer_state["restructurer"].generate_new_entity_id(
                                entity_id, entity_state
                            )

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

        # Verarbeite einzelne Entities
        for old_id, (new_id, friendly_name) in selected_mapping.items():
            try:
                # Recalculate the entity name to ensure overrides are applied
                current_state = next((s for s in states if s["entity_id"] == old_id), {})
                if current_state:
                    # Use restructurer to get the current naming with overrides
                    recalculated_new_id, recalculated_friendly_name = renamer_state[
                        "restructurer"
                    ].generate_new_entity_id(old_id, current_state)
                    # Use the recalculated names instead of the preview mapping
                    new_id = recalculated_new_id
                    friendly_name = recalculated_friendly_name
                    logger.info(f"Recalculated entity: {old_id} -> {new_id}, friendly_name: {friendly_name}")
                else:
                    logger.info(f"Processing entity: {old_id} -> {new_id}, friendly_name: {friendly_name}")

                # Check if entity ID or friendly name needs to be changed
                current_states = next((s for s in states if s["entity_id"] == old_id), {})
                current_friendly_name = current_states.get("attributes", {}).get("friendly_name", "")

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
    """Execute entity renames directly without preview (for hierarchy UI)"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_execute_direct_async())
    finally:
        loop.close()


async def _execute_direct_async():
    """Async implementation of execute_direct"""
    data = request.json
    entities = data.get("entities", [])

    if not entities:
        return jsonify({"error": "Keine Entities ausgewählt"}), 400

    # Execute renaming
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

        for entity_data in entities:
            old_id = entity_data.get("old_id")
            new_id = entity_data.get("new_id")
            friendly_name = entity_data.get("new_name")

            if not old_id or not new_id:
                results["failed"].append({"entity_id": old_id, "error": "Missing old_id or new_id"})
                continue

            try:
                # Check if entity is disabled and if we should enable it
                entity_reg = renamer_state["restructurer"].entities.get(old_id, {})
                current_name = entity_reg.get("original_name") or entity_reg.get("name")

                # Skip only if BOTH ID and name are unchanged
                id_unchanged = old_id == new_id
                name_unchanged = friendly_name == current_name
                if id_unchanged and name_unchanged:
                    results["skipped"].append({"entity_id": old_id, "reason": "Keine Änderung nötig"})
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
                        "message": f"Entity erfolgreich umbenannt: {old_id} -> {new_id}",
                    }
                )
                logger.info(f"Successfully renamed: {old_id} -> {new_id}")

            except Exception as e:
                logger.error(f"Error renaming entity {old_id}: {e}")
                results["failed"].append({"entity_id": old_id, "error": str(e)})

    finally:
        await ws.disconnect()

    # Invalidate broken references cache after changes
    invalidate_reference_checker_cache()

    return jsonify(results)


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
    """Benennt ein Gerät in Home Assistant um und aktualisiert Entity-Namen"""
    # Create new event loop for this request
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_rename_device_async())
    finally:
        loop.close()


async def _rename_device_async():
    """Async implementation of rename_device.

    When renaming a device, also updates all entity friendly names that belong
    to this device by replacing the old device name with the new one.
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

    try:
        # Erstelle WebSocket Verbindung
        base_url = os.getenv("HA_URL")
        token = os.getenv("HA_TOKEN")
        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

        ws = HomeAssistantWebSocket(ws_url, token)
        await ws.connect()

        try:
            # Ensure restructurer is loaded
            if renamer_state["restructurer"] is None:
                renamer_state["restructurer"] = EntityRestructurer()
            await renamer_state["restructurer"].load_structure(ws)

            # Get old device name before renaming
            old_device_name = None
            if device_id in renamer_state["restructurer"].devices:
                device = renamer_state["restructurer"].devices[device_id]
                old_device_name = device.get("name_by_user") or device.get("name")

            device_registry = DeviceRegistry(ws)
            success = await device_registry.rename_device(device_id, new_name)

            if not success:
                return (
                    jsonify({"error": "Fehler beim Umbenennen des Geräts in Home Assistant"}),
                    500,
                )

            # Update entities: rename ID + friendly name + update dependencies
            entities_updated = 0
            entities_failed = 0
            entities_skipped = 0
            dependencies_updated = 0

            logger.info("=== Starting entity rename after device rename ===")
            logger.info(f"Device ID: {device_id}")
            logger.info(f"Old device name: {old_device_name}")
            logger.info(f"New device name: {new_name}")

            entity_registry = EntityRegistry(ws)

            # Initialize dependency updater
            base_url = os.getenv("HA_URL")
            token = os.getenv("HA_TOKEN")
            dependency_updater = DependencyUpdater(base_url, token)
            cached_states = await dependency_updater.get_states()

            # Get area name for this device
            device_info = renamer_state["restructurer"].devices.get(device_id, {})
            area_id = device_info.get("area_id")
            area_name = ""
            if area_id and area_id in renamer_state["restructurer"].areas:
                area_name = renamer_state["restructurer"].areas[area_id].get("name", "")

            logger.info(f"Area ID: {area_id}, Area name: {area_name}")

            # Get the device base_name (without area prefix)
            device_base_name = _strip_prefix(new_name, area_name) if area_name else new_name

            # Build old device display name for stripping from entity names
            old_device_base = (
                _strip_prefix(old_device_name, area_name) if (old_device_name and area_name) else old_device_name
            )
            old_device_display = f"{area_name} {old_device_base}" if area_name else old_device_base

            # Count entities for this device
            device_entities = [
                eid
                for eid, einfo in renamer_state["restructurer"].entities.items()
                if einfo.get("device_id") == device_id
            ]
            logger.info(f"Found {len(device_entities)} entities for device {device_id}")

            # Find all entities belonging to this device
            for old_entity_id, entity_info in list(renamer_state["restructurer"].entities.items()):
                if entity_info.get("device_id") != device_id:
                    continue

                # Get current entity name
                original_name = entity_info.get("name") or entity_info.get("original_name") or ""
                logger.info(f"Processing entity {old_entity_id}: original_name='{original_name}'")

                if not original_name:
                    logger.info("  Skipping - no original_name")
                    entities_skipped += 1
                    continue

                # Compute entity base_name (suffix) by stripping area and device prefixes
                entity_suffix = original_name
                if old_device_display:
                    entity_suffix = _strip_prefix(entity_suffix, old_device_display)
                if entity_suffix == original_name and old_device_base:
                    entity_suffix = _strip_prefix(entity_suffix, old_device_base)
                if entity_suffix == original_name and area_name:
                    entity_suffix = _strip_prefix(entity_suffix, area_name)

                # Build new friendly name: Area + Device Base + Entity Suffix
                parts = []
                if area_name:
                    parts.append(area_name)
                parts.append(device_base_name)
                if entity_suffix and entity_suffix != original_name:
                    parts.append(entity_suffix)

                new_friendly_name = " ".join(parts)

                # Build new entity ID
                domain = old_entity_id.split(".")[0]
                new_entity_id = f"{domain}.{normalize_name(new_friendly_name)}"

                logger.info(f"  {old_entity_id} -> {new_entity_id} ('{new_friendly_name}')")

                # Skip if nothing would change
                if new_entity_id == old_entity_id and new_friendly_name == original_name:
                    logger.info("  Skipping - no changes needed")
                    entities_skipped += 1
                    continue

                try:
                    # Rename entity (ID + friendly name)
                    id_changed = new_entity_id != old_entity_id
                    await entity_registry.rename_entity(
                        old_entity_id, new_entity_id if id_changed else None, new_friendly_name
                    )
                    entities_updated += 1
                    logger.info("  SUCCESS: Renamed entity")

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

            # Reload structure to reflect changes
            await renamer_state["restructurer"].load_structure(ws)

            logger.info("=== Entity rename complete ===")
            logger.info(
                f"Updated: {entities_updated}, Failed: {entities_failed}, Skipped: {entities_skipped}, Dependencies: {dependencies_updated}"
            )

            message = f"Gerät erfolgreich umbenannt zu: {new_name}"
            if entities_updated > 0:
                message += f" ({entities_updated} Entities"
                if dependencies_updated > 0:
                    message += f", {dependencies_updated} Dependencies"
                message += " aktualisiert)"
            if entities_failed > 0:
                message += f" ({entities_failed} fehlgeschlagen)"

            return jsonify(
                {
                    "success": True,
                    "message": message,
                    "entities_updated": entities_updated,
                    "entities_failed": entities_failed,
                    "dependencies_updated": dependencies_updated,
                }
            )

        finally:
            await ws.disconnect()

    except Exception as e:
        logger.error(f"Fehler beim Umbenennen des Geräts: {e}")
        return jsonify({"error": str(e)}), 500


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
        areas = []
        for area_id, area_data in restructurer.areas.items():
            areas.append(
                {
                    "id": area_id,
                    "name": area_data.get("name", ""),
                }
            )

        # Build device lookup with base names (strip area prefix)
        device_base_names = {}
        device_area_map = {}
        devices = []
        for device_id, device_data in restructurer.devices.items():
            raw_name = device_data.get("name_by_user") or device_data.get("name", "")
            area_id = device_data.get("area_id")

            # Strip area prefix from device name
            # e.g., "Büro Homepod" with area "Büro" -> "Homepod"
            base_name = raw_name
            if area_id and area_id in area_names:
                base_name = _strip_prefix(raw_name, area_names[area_id])

            device_base_names[device_id] = base_name
            device_area_map[device_id] = area_id

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

            devices.append(
                {
                    "id": device_id,
                    "name": raw_name,  # Original HA name
                    "base_name": base_name,  # Stripped base name for display
                    "area_id": area_id,
                    "manufacturer": device_data.get("manufacturer"),
                    "model": device_data.get("model"),
                    "integrations": integrations,  # e.g., ["homekit", "zha"]
                    "disabled_by": device_data.get("disabled_by"),
                }
            )

        entities = []
        for entity_id, entity_data in restructurer.entities.items():
            registry_id = entity_data.get("id", "")
            override = renamer_state["naming_overrides"].get_entity_override(registry_id)
            device_class = entity_data.get("device_class") or entity_data.get("original_device_class")
            device_id = entity_data.get("device_id")
            area_id = entity_data.get("area_id")

            # Get original friendly name
            original_name = entity_data.get("name") or entity_data.get("original_name") or ""

            # Debug logging for specific entities
            if "wallbox" in entity_id.lower():
                logger.info(
                    f"DEBUG {entity_id}: name={entity_data.get('name')!r}, original_name={entity_data.get('original_name')!r}, computed={original_name!r}"
                )

            # Strip device+area prefix from entity name
            # e.g., "Büro Raumluftsensor Kohlendioxid" -> "Kohlendioxid"
            base_name = original_name
            if device_id and device_id in device_base_names:
                # Build full device display name (area + device base)
                dev_area_id = device_area_map.get(device_id)
                dev_base = device_base_names[device_id]
                if dev_area_id and dev_area_id in area_names:
                    device_display = f"{area_names[dev_area_id]} {dev_base}"
                else:
                    device_display = dev_base
                base_name = _strip_prefix(original_name, device_display)
                # Also try just device base name
                if base_name == original_name:
                    base_name = _strip_prefix(original_name, dev_base)
            elif area_id and area_id in area_names:
                base_name = _strip_prefix(original_name, area_names[area_id])

            # Fallback: If base_name is still empty or equals domain, try extracting from entity_id
            domain = entity_id.split(".")[0] if "." in entity_id else ""
            if not base_name or base_name.lower() == domain:
                # Try to extract suffix from entity_id
                # e.g., sensor.tiefgarage_wallbox_angebotene_leistung -> angebotene_leistung
                entity_slug = entity_id.split(".")[-1] if "." in entity_id else entity_id
                # Build expected prefix from device/area
                expected_prefix = ""
                if device_id and device_id in device_base_names:
                    dev_area_id = device_area_map.get(device_id)
                    dev_base = device_base_names[device_id]
                    if dev_area_id and dev_area_id in area_names:
                        expected_prefix = f"{area_names[dev_area_id]}_{dev_base}".lower().replace(" ", "_")
                    else:
                        expected_prefix = dev_base.lower().replace(" ", "_")
                elif area_id and area_id in area_names:
                    expected_prefix = area_names[area_id].lower().replace(" ", "_")

                if expected_prefix and entity_slug.startswith(expected_prefix + "_"):
                    suffix_slug = entity_slug[len(expected_prefix) + 1 :]
                    # Convert slug to human-readable: replace underscores with spaces, title case
                    base_name = suffix_slug.replace("_", " ").title()

            # Debug logging for wallbox entities
            if "wallbox" in entity_id.lower():
                logger.info(
                    f"DEBUG {entity_id}: base_name={base_name!r}, device_id={device_id}, has_device={device_id in device_base_names if device_id else False}"
                )

            entities.append(
                {
                    "id": entity_id,
                    "registry_id": registry_id,
                    "device_id": device_id,
                    "area_id": area_id,
                    "device_class": device_class,
                    "original_name": original_name,  # Original HA friendly name
                    "base_name": base_name,  # Stripped base name for editing
                    "override_name": override.get("name") if override else None,
                    "has_override": override is not None,
                    "disabled_by": entity_data.get("disabled_by"),
                    "labels": entity_data.get("labels", []),
                    "platform": entity_data.get("platform"),  # Integration that provides this entity
                    "is_orphan": entity_id in orphan_entities,  # Entity restored but not provided by integration
                }
            )

        return jsonify(
            {
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

        # Use restructurer's learn method which handles the logic
        restructurer = renamer_state["restructurer"]
        restructurer.learn_type_mapping(type_key, translation)

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


@app.route("/settings")
def settings_page():
    """Render the settings page for type mappings management."""
    version = str(int(time.time()))
    response = make_response(render_template("settings.html", version=version))
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
    # MQTT/Z2M-Unterstützung wird in einer späteren Ausbaustufe ergänzt.
    return jsonify(
        {
            "mqtt_available": False,
            "z2m_supported": False,
            "matter_remove_supported": True,
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
    old_ents_in_use = [e for e in old_ents if e.get("entity_id") in referenced]

    proposal = propose_mapping(old_ents_in_use, new_ents, states_by_id)
    proposal["old_total"] = len(old_ents)
    proposal["old_in_use"] = len(old_ents_in_use)

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
    """Führt den Job aus bzw. setzt ihn fort (idempotent)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_swap_execute_async(job_id))
    finally:
        loop.close()


async def _swap_execute_async(job_id):
    store = renamer_state["swap_store"]
    job = store.load(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["state"] in (device_swap.STATE_PROPOSED, device_swap.STATE_ABORTED, device_swap.STATE_COMPLETED):
        return jsonify({"error": f"Job not runnable in state {job['state']}"}), 409

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
        bridge = build_bridge(device_registry, mqtt_bridge=None)
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
        job = await executor.run(job)
    finally:
        await ws.disconnect()

    return jsonify(job)


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
    print("\nBETA VERSION - Entity Manager Add-on")
    print(f"\nStarting Web UI on port {port}\n")

    # Run without debug in production
    app.run(debug=False, host="0.0.0.0", port=port)
