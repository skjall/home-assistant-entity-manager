#!/usr/bin/env python3
"""
Web UI für Home Assistant Entity Renamer - Add-on Version
"""

import asyncio
import json
import logging
import os
import time
import uuid

import aiohttp
from flask import (
    Flask,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
)
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

import access
from app_state import UNASSIGNED_AREA, ensure_mqtt_bridge, init_client, renamer_state
import asgi
from dependency_updater import DependencyUpdater
from device_registry import DeviceRegistry
from entity_registry import EntityRegistry
from ha_websocket import HomeAssistantWebSocket
from hierarchy_manager import normalize_name
from jobs import new_job
import mcp_server
from reference_cache import get_reference_checker, invalidate_reference_checker_cache
from registry import sync_ha_language
from routes_entities import entities as entity_routes
from routes_naming import (
    SETTINGS_SECTIONS,
    entity_model,
    entity_type_key,
    naming as naming_routes,
    type_key_counts,
    type_key_integration_counts,
    type_key_model_counts,
)
from routes_swap import swap as swap_routes
from routes_system import system as system_routes
from sanitize import (
    sanitize_entity_id,
    sanitize_name,
    sanitize_string,
    validate_json_input,
)
from z2m import sync_z2m_name

# Don't load .env in Add-on mode - use environment variables from Supervisor
# load_dotenv()

# Language-independent constant for entities without area assignment
UNASSIGNED_AREA = "__unassigned__"


app = Flask(__name__, static_folder="static", static_url_path="/static")
# Ingress proxy header support. The access gate wraps the outside of this so it
# records the real TCP peer before ProxyFix trusts forwarded headers.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
CORS(app)
# Read the store per request: it is replaced in tests and created further down.
access.install(app, lambda: renamer_state["api_token_store"])
app.register_blueprint(entity_routes)
app.register_blueprint(naming_routes)
app.register_blueprint(swap_routes)
app.register_blueprint(system_routes)


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
            await sync_ha_language(ws)

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
                    z2m_sync = await sync_z2m_name(device_registry, device_id, new_device_name)
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


def ownership_of(entity_data: dict, template_hash: str) -> dict:
    """The provenance fields the entity list carries for one entity.

    Only what the interface needs to explain a name: who it belongs to, whether
    somebody changed it elsewhere, and whether the templates have moved on
    since it was written - the last being a reason for a different proposal,
    not a sign that anybody touched the name.
    """
    state = renamer_state["naming_state"].ownership(entity_data, template_hash)
    return {
        "name_owner": state["name_owner"],
        "drift": state["drift"],
        "template_changed": state["template_changed"],
        "applied_name": state["applied_name"],
    }


def blocked_of(restructurer, entity_id: str):
    """Why a proposal could not have the id it rendered, or None.

    Two entities of one device often carry the same name from their
    integration, so the template renders one id for both. Numbering keeps the
    rename possible, but the number says nothing about what the two are; only
    the user can say that, so the case is reported rather than hidden.
    """
    numbered = (getattr(restructurer, "last_numbering", None) or {}).get(entity_id)
    if not numbered:
        return None
    return {"reason": "id_taken", "wanted": numbered["wanted"], "holder": numbered["holder"]}


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
            mqtt_bridge = await ensure_mqtt_bridge()
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

        type_counts = type_key_counts(restructurer)
        type_integration_counts = type_key_integration_counts(restructurer)
        type_model_counts = type_key_model_counts(restructurer)

        # One mark for the templates as they are now; every entity compares its
        # stored one against it.
        template_hash = renamer_state["naming_templates"].fingerprint()

        entities = []
        for entity_id, entity_data in restructurer.entities.items():
            registry_id = entity_data.get("id", "")
            override = renamer_state["naming_overrides"].get_entity_override(registry_id)
            type_key = entity_type_key(entity_data)
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
                    "device_model": entity_model(restructurer, entity_data),
                    "type_model_count": (
                        type_model_counts.get((type_key, entity_model(restructurer, entity_data)), 0) if type_key else 0
                    ),
                    # Who the name in the registry belongs to right now, and
                    # whether it was changed outside this add-on since.
                    **ownership_of(entity_data, template_hash),
                    # Set when the proposal only got an id by numbering away
                    # from another entity that renders the same name.
                    "blocked": blocked_of(restructurer, entity_id),
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


if __name__ == "__main__":
    # Erstelle Template-Verzeichnis
    os.makedirs("templates", exist_ok=True)

    # In Add-on mode, use port 5000 for Ingress
    port = int(os.getenv("WEB_UI_PORT", 5000))

    # Fail any generic jobs left running by a previous process, then start the
    # background worker before serving requests.
    renamer_state["worker"].reconcile_on_start()
    renamer_state["worker"].start()

    # Set WEB_UI_DEV_SERVER=1 for the Werkzeug development server, which has a
    # reloader and readable tracebacks.
    if os.getenv("WEB_UI_DEV_SERVER") == "1":
        print(f"\nStarting Web UI (Werkzeug dev server) on port {port}\n")
        app.run(debug=False, host="0.0.0.0", port=port)
    else:
        # The MCP server is off unless the mcp option says otherwise; when it is
        # on it is mounted beside the web interface and guarded the same way.
        server = mcp_server.build(app)
        mcp_app = None
        if server is not None:
            mcp_app = access.Guard(server.http_app(path="/"), lambda: renamer_state["api_token_store"])
        asgi.serve(asgi.build(app, mcp_app), port)
