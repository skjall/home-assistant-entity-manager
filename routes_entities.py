"""Changing a single entity or a single device.

Renaming, deleting, enabling and assigning an area all end up writing to Home
Assistant's registries. The long ones (a device with many entities) run as
background jobs on the shared worker; the short ones answer in the request.
"""

import asyncio
import logging
import os
from typing import Any
import uuid

from flask import Blueprint, jsonify, request

from app_state import init_client, renamer_state, ws_url
from dependency_updater import DependencyUpdater
from device_registry import DeviceRegistry
from entity_registry import EntityRegistry
from entity_restructurer import EntityRestructurer
from ha_websocket import HomeAssistantWebSocket
from jobs import new_job
import naming_service
from routes_naming import ensure_registry_loaded
from sanitize import (
    sanitize_entity_id,
    sanitize_name,
    sanitize_registry_id,
    validate_json_input,
)
from z2m import sync_z2m_name

logger = logging.getLogger(__name__)

entities = Blueprint("entities", __name__)


@entities.route("/api/set_entity_override", methods=["POST"])
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
        # The registry says which entity the registry id belongs to, so it has
        # to be there before the override can be turned into a name.
        await ensure_registry_loaded()

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


@entities.route("/api/enable_entity", methods=["POST"])
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


@entities.route("/api/enable_all", methods=["POST"])
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


@entities.route("/api/enable_device", methods=["POST"])
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


@entities.route("/api/assign_device_area", methods=["POST"])
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


@entities.route("/api/rename_entity", methods=["POST"])
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
        result = await naming_service.rename_entity(old_entity_id, new_entity_id, new_friendly_name)
    except Exception as error:
        logger.error(f"Error renaming entity {old_entity_id}: {error}")
        return jsonify({"error": str(error)}), 500
    return jsonify(result)


@entities.route("/api/apply_naming", methods=["POST"])
def apply_naming():
    """Write the proposed names of several entities, in one request."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_apply_naming_async())
    finally:
        loop.close()


async def _apply_naming_async():
    data = request.json or {}
    wanted = data.get("entity_ids")
    if not isinstance(wanted, list) or not all(isinstance(one, str) for one in wanted):
        return jsonify({"error": "entity_ids must be a list of entity ids"}), 400

    entity_ids = [sanitize_entity_id(one) or one for one in wanted]
    try:
        return jsonify(await naming_service.apply_naming(entity_ids))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@entities.route("/api/delete_entity", methods=["POST"])
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


@entities.route("/api/rename_device", methods=["POST"])
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
        z2m_sync = await sync_z2m_name(device_registry, device_id, new_name)

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


@entities.route("/api/sync_z2m_name", methods=["POST"])
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
    ws = HomeAssistantWebSocket(ws_url(), token)
    await ws.connect()
    try:
        await renamer_state["restructurer"].load_structure(ws)
        device = renamer_state["restructurer"].devices.get(device_id)
        if not device:
            return jsonify({"error": "Unknown device"}), 404
        ha_name = device.get("name_by_user") or device.get("name", "")
        device_registry = DeviceRegistry(ws)
        result = await sync_z2m_name(device_registry, device_id, ha_name)
        if not result.get("supported"):
            return jsonify({"success": False, "supported": False, "message": "Not a Z2M device"}), 400
        if result.get("error"):
            return jsonify({"success": False, "error": result["error"]}), 500
        return jsonify({"success": True, "synced": result.get("synced"), "name": ha_name})
    finally:
        await ws.disconnect()


# === New API Endpoints for Hierarchy and Type Mappings ===
