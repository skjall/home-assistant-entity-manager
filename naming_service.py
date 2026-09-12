"""The work behind naming, without an HTTP request around it.

A route and an MCP tool want the same thing to happen when an entity is
renamed: write the registry, carry the change into automations, scripts and
scenes that named the old id, and drop the cached reference scan. That work
lives here so both callers do it the same way, and so it can be tested without
a web server.
"""

import logging
import os
from typing import Any, Dict, Optional

from app_state import renamer_state, ws_url
from dependency_updater import DependencyUpdater
from entity_registry import EntityRegistry
from ha_websocket import HomeAssistantWebSocket
from reference_cache import invalidate_reference_checker_cache
from registry import ensure_registry_loaded

logger = logging.getLogger(__name__)


async def rename_entity(
    old_entity_id: str,
    new_entity_id: Optional[str] = None,
    friendly_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Rename one entity and carry the change through what refers to it.

    Returns what happened, including which dependencies were rewritten. A
    rename that changes nothing is reported as skipped rather than done.
    """
    id_changed = bool(new_entity_id) and old_entity_id != new_entity_id
    if not id_changed and friendly_name is None:
        return {"success": True, "skipped": True, "message": "No changes needed"}

    token = os.getenv("HA_TOKEN")
    ws = HomeAssistantWebSocket(ws_url(), token)
    await ws.connect()
    try:
        registry = EntityRegistry(ws)
        renamed = await registry.rename_entity(
            old_entity_id=old_entity_id,
            new_entity_id=new_entity_id if id_changed else None,
            friendly_name=friendly_name,
        )
    finally:
        await ws.disconnect()

    if not renamed:
        raise RuntimeError(f"Home Assistant refused to rename {old_entity_id}")

    logger.info("Renamed entity: %s -> %s (%s)", old_entity_id, new_entity_id or old_entity_id, friendly_name)
    result: Dict[str, Any] = {
        "success": True,
        "old_entity_id": old_entity_id,
        "new_entity_id": new_entity_id or old_entity_id,
        "new_friendly_name": friendly_name,
    }

    if not id_changed:
        result["dependencies_checked"] = False
        result["dependencies_reason"] = "entity_id_unchanged"
        invalidate_reference_checker_cache()
        return result

    # An id that changed leaves every automation, script and scene that named
    # the old one pointing at nothing.
    try:
        updater = DependencyUpdater(os.getenv("HA_URL"), token)
        updated = await updater.update_all_dependencies(old_entity_id, new_entity_id)
        result["dependencies_checked"] = True
        result["dependencies_updated"] = {
            "total": updated["total_success"],
            "scenes": updated["scenes"]["success"],
            "scripts": updated["scripts"]["success"],
            "automations": updated["automations"]["success"],
        }
        if updated["total_success"]:
            logger.info("Updated %d dependencies for %s", updated["total_success"], old_entity_id)
        if updated["total_failed"]:
            result["dependencies_failed"] = {
                "total": updated["total_failed"],
                "scenes": updated["scenes"]["failed"],
                "scripts": updated["scripts"]["failed"],
                "automations": updated["automations"]["failed"],
            }
            logger.warning("Failed to update %d dependencies for %s", updated["total_failed"], old_entity_id)
    except Exception as error:
        logger.error("Error updating dependencies for %s: %s", old_entity_id, error)
        result["dependencies_checked"] = False
        result["dependencies_error"] = str(error)

    invalidate_reference_checker_cache()
    return result


async def proposed_naming(entity_id: str, entity_name: Optional[str] = None) -> Dict[str, Any]:
    """What the entity would be called under the current rules and templates.

    ``entity_name`` tries out a different type name without saving anything.
    """
    await ensure_registry_loaded()
    restructurer = renamer_state["restructurer"]
    entry = restructurer.entities.get(entity_id)
    if not entry:
        raise LookupError(f"unknown entity: {entity_id}")

    new_entity_id, new_name = restructurer.generate_new_entity_id(entity_id, entry, entity_name)
    # Number away from ids other entities hold, as a batched rename would.
    domain, _, object_id = new_entity_id.partition(".")
    taken = set(restructurer.entities) - {entity_id}
    suffix = 1
    while new_entity_id in taken and object_id:
        suffix += 1
        new_entity_id = f"{domain}.{object_id}_{suffix}"
    if suffix > 1 and new_name:
        new_name = f"{new_name} {suffix}"

    resolution = restructurer.last_resolutions.get(entity_id) or {}
    return {
        "entity_id": entity_id,
        "current_name": entry.get("name") or entry.get("original_name") or "",
        "proposed_entity_id": new_entity_id,
        "proposed_name": new_name,
        "name_comes_from": resolution.get("won_by"),
        "rule_id": resolution.get("rule_id"),
        "supplied_name": resolution.get("input"),
    }


# One call applies this many names. A whole home in one request would run for
# minutes with nobody able to see how far it got, and a mistaken one would be
# that much harder to undo.
APPLY_LIMIT = 100


async def apply_naming(entity_ids: list) -> Dict[str, Any]:
    """Write the proposed name and id of each entity into Home Assistant.

    Each entity is reported on its own and one that fails does not stop the
    rest, so a single wrong id costs one rename instead of the whole batch. The
    proposal is worked out per entity as its turn comes, so a name that only
    collides because of an earlier rename in the same call is numbered against
    what is by then already there.
    """
    if not entity_ids:
        raise ValueError("name at least one entity")
    if len(entity_ids) > APPLY_LIMIT:
        raise ValueError(f"at most {APPLY_LIMIT} entities per call, got {len(entity_ids)}")

    results = []
    for entity_id in entity_ids:
        try:
            proposed = await proposed_naming(entity_id)
            outcome = await rename_entity(
                entity_id,
                proposed["proposed_entity_id"],
                proposed["proposed_name"],
            )
        except Exception as error:  # noqa: BLE001 - one bad id must not stop the rest
            logger.warning("apply_naming failed for %s: %s", entity_id, error)
            results.append({"entity_id": entity_id, "success": False, "error": str(error)})
            continue
        results.append({"entity_id": entity_id, **outcome})

    return {
        "renamed": sum(1 for row in results if row.get("success") and not row.get("skipped")),
        "unchanged": sum(1 for row in results if row.get("skipped")),
        "failed": sum(1 for row in results if not row.get("success")),
        "results": results,
    }
