"""The add-on as a set of tools an assistant can use.

The web interface shows a person what an entity is called, where that name came
from and what a rule would change. These tools answer the same questions to a
program, and — when the setting allows it — let it change the same things.

Two settings decide what exists here. ``mcp`` turns the server on and says
whether it may write; ``external_access`` decides who may reach it at all. A
caller always has to present the add-on's API token.
"""

import logging
import os
from typing import Any, Dict, List, Optional

from app_state import renamer_state
import naming_service
from naming_canon import canon
from registry import ensure_registry_loaded
from sanitize import sanitize_entity_id, sanitize_name, sanitize_string

logger = logging.getLogger(__name__)

# What the mcp setting can be: no server, a reading one, or one that may write.
OFF = "off"
READ = "read"
WRITE = "write"
MODES = (OFF, READ, WRITE)

INSTRUCTIONS = """
This add-on gives every entity in Home Assistant a name built from the area, the
device and what the entity is, following rules the user has set.

Look before you change: naming_for tells you what an entity is called now, what
it would be called, and which rule decided that. A rule reaches every entity of
the same type, so writing one is rarely a small change — say how far it reaches
before you write it.
""".strip()


def mode() -> str:
    """Return the configured mode, falling back to off for anything unknown."""
    configured = (os.getenv("MCP") or "").strip().lower()
    if configured in MODES:
        return configured
    if configured:
        logger.warning("Unknown mcp value %r, leaving the server off", configured)
    return OFF


def _entity_summary(entity_id: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """The few fields that identify an entity, without the registry noise."""
    restructurer = renamer_state["restructurer"]
    device = restructurer.devices.get(entry.get("device_id") or "", {})
    area_id = entry.get("area_id") or device.get("area_id")
    area = restructurer.areas.get(area_id or "", {})
    return {
        "entity_id": entity_id,
        "name": entry.get("name") or entry.get("original_name") or "",
        "supplied_name": entry.get("original_name") or "",
        "area": area.get("name") or "",
        "device": device.get("name_by_user") or device.get("name") or "",
        "integration": entry.get("platform") or "",
        "device_class": entry.get("device_class") or entry.get("original_device_class") or "",
    }


async def find_entities(
    query: str = "",
    area: str = "",
    integration: str = "",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Search entities by id, name, area or integration.

    An empty query returns the first entities in id order, which is only useful
    to see what the shape of the data is; pass something to search for.
    """
    await ensure_registry_loaded()
    restructurer = renamer_state["restructurer"]
    wanted = canon(query)
    found = []
    for entity_id, entry in sorted(restructurer.entities.items()):
        summary = _entity_summary(entity_id, entry)
        if integration and summary["integration"] != integration:
            continue
        if area and canon(area) not in canon(summary["area"]):
            continue
        if wanted:
            haystack = canon(f"{entity_id} {summary['name']} {summary['supplied_name']} {summary['device']}")
            if wanted not in haystack:
                continue
        found.append(summary)
        if len(found) >= max(1, min(limit, 200)):
            break
    return found


async def naming_for(entity_id: str) -> Dict[str, Any]:
    """What an entity is called, what it would be called, and why.

    ``name_comes_from`` says which source won: an exception the user set, a
    rule, a name Home Assistant supplied, or the fallback.
    """
    return await naming_service.proposed_naming(sanitize_entity_id(entity_id) or entity_id)


async def list_areas() -> List[Dict[str, Any]]:
    """Every area with how many entities sit in it."""
    await ensure_registry_loaded()
    restructurer = renamer_state["restructurer"]
    counted: Dict[str, int] = {}
    for entry in restructurer.entities.values():
        device = restructurer.devices.get(entry.get("device_id") or "", {})
        area_id = entry.get("area_id") or device.get("area_id") or ""
        counted[area_id] = counted.get(area_id, 0) + 1
    areas = [
        {
            "area_id": area_id,
            "name": area.get("name") or "",
            "floor": (restructurer.floors.get(area.get("floor_id") or "", {}) or {}).get("name") or "",
            "entities": counted.get(area_id, 0),
        }
        for area_id, area in restructurer.areas.items()
    ]
    areas.sort(key=lambda item: item["name"])
    if counted.get(""):
        areas.append({"area_id": "", "name": "", "floor": "", "entities": counted[""]})
    return areas


def _rule_summary(rule: Dict[str, Any], language: str) -> Dict[str, Any]:
    """One rule in the shape a caller can act on: what it matches, what it says."""
    match = rule.get("match", {})
    return {
        "id": rule.get("id"),
        "kind": match.get("kind"),
        "key": match.get("value"),
        "value": (rule.get("targets") or {}).get(language),
        "integration": match.get("integration"),
        "model": match.get("model"),
        "source": rule.get("source"),
    }


def list_rules(query: str = "", limit: int = 100) -> List[Dict[str, Any]]:
    """The naming rules; ``query`` filters by what they match and what they say."""
    rules = renamer_state["naming_rules"]
    wanted = canon(query)
    found = []
    for rule in rules.rules:
        summary = _rule_summary(rule, rules.language)
        if wanted and wanted not in canon(f"{summary['key'] or ''} {summary['value'] or ''}"):
            continue
        found.append(summary)
        if len(found) >= max(1, min(limit, 500)):
            break
    return found


def naming_settings() -> Dict[str, Any]:
    """The language, the letter case and the templates names are built from."""
    rules = renamer_state["naming_rules"]
    configured = renamer_state["naming_templates"].get_config()
    return {
        "language": rules.language,
        "display_case": rules.display_case,
        "templates": configured.get("templates", {}),
        "preset": configured.get("preset"),
    }


async def entities_affected_by(kind: str, key: str) -> List[Dict[str, Any]]:
    """Which entities a rule for ``kind``/``key`` would decide the name of.

    Use this before writing a rule: it is the same reach the settings page
    shows, and it is often larger than expected.
    """
    await ensure_registry_loaded()
    restructurer = renamer_state["restructurer"]
    wanted = canon(key)
    affected = []
    for entity_id, entry in sorted(restructurer.entities.items()):
        if kind == "translation_key":
            matches = canon(entry.get("translation_key") or "") == wanted
        elif kind == "device_class":
            matches = canon(entry.get("device_class") or entry.get("original_device_class") or "") == wanted
        else:
            matches = canon(entry.get("original_name") or "") == wanted
        if matches:
            affected.append(_entity_summary(entity_id, entry))
    return affected


def set_rule(
    kind: str,
    key: str,
    value: str,
    integration: str = "",
    model: str = "",
) -> Dict[str, Any]:
    """Name every entity of one type. Ask entities_affected_by first.

    ``kind`` is translation_key, device_class or name; ``key`` is the type as
    the integration supplies it. Naming the integration, and the model on top of
    it, narrows the rule to those devices.
    """
    rules = renamer_state["naming_rules"]
    rule = rules.upsert(
        sanitize_string(kind, max_length=32),
        sanitize_string(key),
        sanitize_string(integration) or None,
        rules.language,
        sanitize_name(value),
        source="learned",
        model=sanitize_string(model) or None,
    )
    renamer_state["type_mappings"]._refresh_user_view()
    return _rule_summary(rule, rules.language)


def delete_rule(rule_id: str) -> Dict[str, Any]:
    """Remove one rule; the entities it named fall back to the next source."""
    removed = renamer_state["naming_rules"].delete(sanitize_string(rule_id, max_length=32))
    renamer_state["type_mappings"]._refresh_user_view()
    return {"deleted": bool(removed), "rule_id": rule_id}


async def set_exception(entity_id: str, name: str) -> Dict[str, Any]:
    """Name one entity by hand, ahead of every rule.

    An exception is the user's own wording for this one entity. Prefer a rule
    where the same correction would apply to more than one.
    """
    await ensure_registry_loaded()
    entity_id = sanitize_entity_id(entity_id) or entity_id
    restructurer = renamer_state["restructurer"]
    entry = restructurer.entities.get(entity_id)
    if not entry:
        raise LookupError(f"unknown entity: {entity_id}")
    renamer_state["naming_overrides"].set_entity_override(entry["id"], sanitize_name(name))
    return await naming_service.proposed_naming(entity_id)


# One call may rename this many entities. A whole home in a single call would
# run for many minutes with nobody able to see how far it got, and a mistaken
# one would be that much harder to undo.
APPLY_LIMIT = 100


async def apply_naming(entity_ids: List[str]) -> Dict[str, Any]:
    """Write the proposed names and ids to Home Assistant.

    Takes as many entities as should change together, so a whole room is one
    call rather than one call per entity. Everything that refers to an old id —
    automations, scripts, scenes — is rewritten along with it. This changes the
    user's house; read naming_for first and say what will happen.

    Each entity is reported on its own and one that fails does not stop the
    rest. The proposal is worked out per entity as its turn comes, so a name
    that only collides because of an earlier rename in the same call is
    numbered against what is by then already there.
    """
    if not entity_ids:
        raise ValueError("name at least one entity")
    if len(entity_ids) > APPLY_LIMIT:
        raise ValueError(f"at most {APPLY_LIMIT} entities per call, got {len(entity_ids)}")

    results = []
    for wanted in entity_ids:
        entity_id = sanitize_entity_id(wanted) or wanted
        try:
            proposed = await naming_service.proposed_naming(entity_id)
            outcome = await naming_service.rename_entity(
                entity_id,
                proposed["proposed_entity_id"],
                proposed["proposed_name"],
            )
        except Exception as error:  # noqa: BLE001 - one bad id must not stop the rest
            logger.warning("apply_naming failed for %s: %s", entity_id, error)
            results.append({"entity_id": entity_id, "success": False, "error": str(error)})
            continue
        results.append({"entity_id": entity_id, **outcome})

    renamed = [row for row in results if row.get("success") and not row.get("skipped")]
    return {
        "renamed": len(renamed),
        "unchanged": sum(1 for row in results if row.get("skipped")),
        "failed": sum(1 for row in results if not row.get("success")),
        "results": results,
    }


READ_TOOLS = (find_entities, naming_for, list_areas, list_rules, naming_settings, entities_affected_by)
WRITE_TOOLS = (set_rule, delete_rule, set_exception, apply_naming)


def build(configured: Optional[str] = None) -> Optional[Any]:
    """Return the MCP server for the configured mode, or None when it is off."""
    configured = configured or mode()
    if configured == OFF:
        return None

    from fastmcp import FastMCP

    server = FastMCP("Entity Manager", instructions=INSTRUCTIONS)
    for tool in READ_TOOLS:
        server.tool(tool)
    if configured == WRITE:
        for tool in WRITE_TOOLS:
            server.tool(tool)
    logger.info(
        "MCP server ready (%s), %d tools",
        configured,
        len(READ_TOOLS) + (len(WRITE_TOOLS) if configured == WRITE else 0),
    )
    return server
