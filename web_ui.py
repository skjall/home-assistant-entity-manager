#!/usr/bin/env python3
"""
Web UI für Home Assistant Entity Renamer - Add-on Version
"""
import asyncio
import json
import logging
import os

import aiohttp
from flask import Flask, jsonify, render_template, request, send_from_directory
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from dependency_updater import DependencyUpdater
from device_registry import DeviceRegistry
from entity_registry import EntityRegistry
from entity_restructurer import EntityRestructurer
from ha_client import HomeAssistantClient
from ha_websocket import HomeAssistantWebSocket
from naming_overrides import NamingOverrides

# Don't load .env in Add-on mode - use environment variables from Supervisor
# load_dotenv()

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
}


async def init_client():
    """Initialisiere den Home Assistant Client"""
    if not renamer_state["client"]:
        # In Add-on mode, use Supervisor API
        base_url = os.getenv("HA_URL", "http://supervisor/core")
        token = os.getenv("HA_TOKEN", os.getenv("SUPERVISOR_TOKEN"))
        logger.info("⚠️  ALPHA VERSION - Entity Manager Add-on")
        logger.info(f"Connecting to Home Assistant at {base_url}")
        renamer_state["client"] = HomeAssistantClient(base_url, token)
        renamer_state["restructurer"] = EntityRestructurer(renamer_state["client"], renamer_state["naming_overrides"])
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

        # Add "Not assigned"
        entities_by_area["Nicht zugeordnet"] = {"domains": {}}

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
            area_name = "Nicht zugeordnet"

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
            if area_name == "Nicht zugeordnet":
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
            if area_name == "Nicht zugeordnet":
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
            if area_name == "Nicht zugeordnet":
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

            entities_by_area[area_name]["domains"][domain].append(
                {
                    "entity_id": entity_id,
                    "friendly_name": state.get("attributes", {}).get("friendly_name", entity_id),
                    "state": state.get("state", "unknown"),
                }
            )

            # Count for debug
            entities_by_area_count[area_name] = entities_by_area_count.get(area_name, 0) + 1

        # Now process disabled entities from entity registry
        logger.info("Processing disabled entities from registry...")
        disabled_count = 0
        for entity_id, entity_reg in renamer_state["restructurer"].entities.items():
            # Skip if already processed (enabled entities)
            if any(
                entity_id == e["entity_id"]
                for area_data in entities_by_area.values()
                for entities in area_data["domains"].values()
                for e in entities
            ):
                continue

            # Check if entity is disabled
            if entity_reg.get("disabled_by") is not None:
                disabled_count += 1
                domain = entity_id.split(".")[0]
                area_name = "Nicht zugeordnet"

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
                        "state": "disabled",
                        "disabled_by": entity_reg.get("disabled_by"),
                    }
                )

                # Update count
                entities_by_area_count[area_name] = entities_by_area_count.get(area_name, 0) + 1

        logger.info(f"Added {disabled_count} disabled entities from registry")

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
    import time

    # Use timestamp for cache busting
    version = str(int(time.time()))
    return render_template("index.html", version=version)


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

                # Get override if available
                area_override = None
                override_name = area_name
                if area_id:
                    area_override = renamer_state["naming_overrides"].get_area_override(area_id)
                    if area_override:
                        override_name = area_override.get("name", area_name)

                areas_data.append(
                    {
                        "name": area_name,
                        "display_name": override_name,
                        "area_id": area_id,
                        "has_override": area_override is not None,
                        "override_name": (area_override.get("name") if area_override else None),
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

            # Hole Overrides
            entity_override = (
                renamer_state["naming_overrides"].get_entity_override(registry_id) if registry_id else None
            )
            device_override = renamer_state["naming_overrides"].get_device_override(device_id) if device_id else None

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
                "has_maintained_label": "maintained" in entity_reg.get("labels", []),
                "registry_id": registry_id,
                "has_override": entity_override is not None,
                "override_name": (entity_override.get("name") if entity_override else None),
                "disabled_by": entity_reg.get("disabled_by"),  # Add disabled status
                "current_basename": current_basename,  # The extracted basename from current friendly_name
            }

            # Gruppiere nach Device
            device_key = device_id or "no_device"
            if device_key not in devices_map:
                # Generiere Device Suggestion
                device_suggested_name = None
                if device_info:
                    # Extrahiere aktuellen Device Namen ohne Raum
                    current_device_name = device_info["name"]

                    # Check if device has a real area (not "Nicht zugeordnet")
                    has_real_area = area_name != "Nicht zugeordnet"

                    # Entferne Raumnamen vom Anfang des Device Names
                    # Normalize for comparison
                    area_normalized = area_name.lower()
                    device_normalized = current_device_name.lower()

                    # Extrahiere Basis-Device-Namen ohne Raum
                    base_device_name = current_device_name
                    if has_real_area and device_normalized.startswith(area_normalized):
                        # Entferne Raumnamen vom Anfang
                        base_device_name = current_device_name[len(area_name) :].strip()

                    # Neuer Vorschlag: Aktueller Raum + Device Name (oder Override)
                    # Only prepend area if device is actually assigned to an area
                    if device_override:
                        if has_real_area:
                            device_suggested_name = f"{area_name} {device_override['name']}"
                        else:
                            device_suggested_name = device_override["name"]
                    else:
                        if has_real_area:
                            device_suggested_name = f"{area_name} {base_device_name}"
                        else:
                            device_suggested_name = base_device_name

                devices_map[device_key] = {
                    "device_info": device_info,
                    "device": (
                        {
                            "id": device_id,
                            "current_name": (device_info["name"] if device_info else None),
                            "suggested_name": device_suggested_name,
                            "suggested_base_name": (base_device_name if device_info else None),
                            "has_override": device_override is not None,
                            "override_name": (device_override.get("name") if device_override else None),
                            "needs_rename": device_info and device_info["name"] != device_suggested_name,
                            "manufacturer": (device_info.get("manufacturer", "") if device_info else None),
                            "model": (device_info.get("model", "") if device_info else None),
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

        # Get states for entity generation
        client = await init_client()
        states = await client.get_states()

        # Process devices first
        for device_data in selected_devices:
            device_id = device_data["device_id"]
            new_device_name = device_data["new_name"]
            base_name = device_data.get("base_name", new_device_name)  # Fallback zum vollen Namen
            device_entities = device_data["entities"]

            try:
                # Benenne Device um
                logger.info(f"Renaming device {device_id} to {new_device_name}")
                success = await device_registry.rename_device(device_id, new_device_name)

                if success:
                    # Speichere Override mit Basisname
                    renamer_state["naming_overrides"].set_device_override(device_id, base_name)

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
                                await entity_registry.add_labels(new_entity_id, ["maintained"])

                                # Update dependencies
                                dep_results = await dependency_updater.update_all_dependencies(entity_id, new_entity_id)

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
                        # Label setzen
                        await entity_registry.add_labels(new_id, ["maintained"])
                    else:
                        # Only change friendly name
                        if should_enable:
                            # Enable and update name in one operation
                            await entity_registry.update_entity(old_id, name=friendly_name, enable=True)
                            logger.info(f"Enabled entity and updated friendly name: {old_id}")
                        else:
                            await entity_registry.update_entity(old_id, name=friendly_name)
                        # Label setzen
                        await entity_registry.add_labels(old_id, ["maintained"])

                    # Update dependencies only on ID change
                    if needs_id_change:
                        try:
                            logger.info(f"Updating dependencies for: {old_id} -> {new_id}")
                            dep_results = await dependency_updater.update_all_dependencies(old_id, new_id)

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
                    # Nur Label setzen
                    await entity_registry.add_labels(old_id, ["maintained"])
                    results["skipped"].append(
                        {
                            "entity_id": old_id,
                            "message": "Bereits korrekt benannt, Label gesetzt",
                        }
                    )

            except Exception as e:
                results["failed"].append({"entity_id": old_id, "error": str(e)})

    finally:
        await ws.disconnect()

    # Delete preview
    del renamer_state["proposed_changes"][preview_id]

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


@app.route("/api/rename_device", methods=["POST"])
def rename_device():
    """Benennt ein Gerät um"""
    # Create new event loop for this request
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_rename_device_async())
    finally:
        loop.close()


async def _rename_device_async():
    """Async implementation of rename_device"""
    data = request.json
    device_id = data.get("device_id")
    new_name = data.get("new_name")

    if not device_id or not new_name:
        return (
            jsonify({"error": "Device ID und neuer Name müssen angegeben werden"}),
            400,
        )

    base_url = os.getenv("HA_URL")
    token = os.getenv("HA_TOKEN")
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

    ws = HomeAssistantWebSocket(ws_url, token)
    await ws.connect()

    try:
        device_registry = DeviceRegistry(ws)
        result = await device_registry.rename_device(device_id, new_name)

        # Reload the structure so the preview has the updated device names
        await renamer_state["restructurer"].load_structure(ws)

        return jsonify(result)

    except Exception as e:
        logger.error(f"Fehler beim Umbenennen des Geräts: {e}")
        return jsonify({"error": str(e)}), 500

    finally:
        await ws.disconnect()


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
    preview_id = data.get("preview_id")
    old_id = data.get("old_id")
    new_id = data.get("new_id")
    new_name = data.get("new_name")

    if not preview_id or not old_id or not new_id:
        return (
            jsonify({"error": "Preview ID, alte und neue Entity ID müssen angegeben werden"}),
            400,
        )

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
    registry_id = data.get("registry_id")
    override_name = data.get("override_name")

    if not registry_id:
        return jsonify({"error": "Registry ID muss angegeben werden"}), 400

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


@app.route("/api/set_device_override", methods=["POST"])
def set_device_override():
    """Setze Device Name Override"""
    # Create new event loop for this request
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_set_device_override_async())
    finally:
        loop.close()


async def _set_device_override_async():
    """Async implementation of set_device_override"""
    data = request.json
    device_id = data.get("device_id")
    override_name = data.get("override_name")

    if not device_id:
        return jsonify({"error": "Device ID muss angegeben werden"}), 400

    try:
        if override_name:
            renamer_state["naming_overrides"].set_device_override(device_id, override_name)
        else:
            renamer_state["naming_overrides"].remove_device_override(device_id)

        # Calculate updated entity suggestions for this device
        updated_entities = []

        # Get all entities for this device
        client = await init_client()
        states = await client.get_states()

        # Find entities that belong to this device
        for entity_id, entity in renamer_state["restructurer"].entities.items():
            if entity.get("device_id") == device_id:
                # Get current state
                entity_state = next(
                    (s for s in states if s["entity_id"] == entity_id), {"entity_id": entity_id, "attributes": {}}
                )

                # Calculate new names with updated device override
                new_id, new_friendly_name = renamer_state["restructurer"].generate_new_entity_id(
                    entity_id, entity_state
                )

                # Get entity registry info for additional data
                entity_reg = renamer_state["restructurer"].entities.get(entity_id, {})

                updated_entities.append(
                    {
                        "old_id": entity_id,
                        "new_id": new_id,
                        "new_name": new_friendly_name,
                        "needs_rename": entity_id != new_id,
                        "registry_id": entity_reg.get("id", ""),
                        "has_override": bool(
                            renamer_state["naming_overrides"].get_entity_override(entity_reg.get("id", ""))
                        ),
                    }
                )

        return jsonify(
            {"success": True, "updated_entities": updated_entities, "device_has_override": bool(override_name)}
        )
    except Exception as e:
        logger.error(f"Fehler beim Setzen des Device Override: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/set_area_override", methods=["POST"])
def set_area_override():
    """Setze Area Name Override"""
    # Create new event loop for this request
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_set_area_override_async())
    finally:
        loop.close()


async def _set_area_override_async():
    """Async implementation of set_area_override"""
    data = request.json
    area_id = data.get("area_id")
    override_name = data.get("override_name")

    if not area_id:
        return jsonify({"error": "Area ID muss angegeben werden"}), 400

    try:
        if override_name:
            renamer_state["naming_overrides"].set_area_override(area_id, override_name)
        else:
            renamer_state["naming_overrides"].remove_area_override(area_id)

        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Fehler beim Setzen des Area Override: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/rename_device_in_ha", methods=["POST"])
def rename_device_in_ha():
    """Benennt ein Gerät tatsächlich in Home Assistant um"""
    # Create new event loop for this request
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_rename_device_in_ha_async())
    finally:
        loop.close()


async def _rename_device_in_ha_async():
    """Async implementation of rename_device_in_ha"""
    data = request.json
    device_id = data.get("device_id")
    new_name = data.get("new_name")

    if not device_id or not new_name:
        return (
            jsonify({"error": "Device ID und neuer Name müssen angegeben werden"}),
            400,
        )

    try:
        # Erstelle WebSocket Verbindung
        base_url = os.getenv("HA_URL")
        token = os.getenv("HA_TOKEN")
        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

        ws = HomeAssistantWebSocket(ws_url, token)
        await ws.connect()

        try:
            device_registry = DeviceRegistry(ws)
            success = await device_registry.rename_device(device_id, new_name)

            if success:
                # Speichere auch als Override
                renamer_state["naming_overrides"].set_device_override(device_id, new_name)
                return jsonify(
                    {
                        "success": True,
                        "message": f"Gerät erfolgreich umbenannt zu: {new_name}",
                    }
                )
            else:
                return (
                    jsonify({"error": "Fehler beim Umbenennen des Geräts in Home Assistant"}),
                    500,
                )

        finally:
            await ws.disconnect()

    except Exception as e:
        logger.error(f"Fehler beim Umbenennen des Geräts: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Erstelle Template-Verzeichnis
    os.makedirs("templates", exist_ok=True)

    # In Add-on mode, use port 5000 for Ingress
    port = int(os.getenv("WEB_UI_PORT", 5000))
    print("\n⚠️  ALPHA VERSION - Entity Manager Add-on")
    print(f"\n🚀 Starting Web UI on port {port}\n")

    # Run without debug in production
    app.run(debug=False, host="0.0.0.0", port=port)
