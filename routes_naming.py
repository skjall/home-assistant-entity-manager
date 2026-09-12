"""Everything about what an entity is called: templates, rules and mappings.

The routes here are thin. What they do — resolving a name, counting how far a
rule reaches, repairing rules whose kind was guessed wrong — is done in
functions next to them, so the same work can be driven from somewhere other
than an HTTP request.
"""

import asyncio
import logging
import random
from typing import Any, Optional

from flask import Blueprint, jsonify, request

from app_state import ha_translations, renamer_state
from registry import ensure_registry_loaded
from naming_canon import canon
from naming_rules import NamingRuleError
from naming_templates import NamingTemplateError
from sanitize import sanitize_string, validate_json_input

logger = logging.getLogger(__name__)

naming = Blueprint("naming", __name__)


@naming.route("/api/naming_templates", methods=["GET", "PUT"])
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


SAMPLE_FALLBACK = {
    "floor": "Ground floor",
    "floor_id": "ground_floor",
    "floor_level": "0",
    "area": "Living room",
    "area_id": "living_room",
    "device": "Thermostat",
    "device_id": "device_123",
    "entity": "Temperature",
    "entity_id": "thermostat_temperature",
    "domain": "sensor",
    "device_class": "temperature",
    "manufacturer": "Acme",
    "model": "T1000",
    "integration": "matter",
}


def _sample_rows(restructurer: Any) -> list:
    """Entities that can carry a full example: they have a device and an area."""
    rows = []
    for entity_id, entity_data in restructurer.entities.items():
        device = restructurer.devices.get(entity_data.get("device_id") or "")
        if not device:
            continue
        area_id = entity_data.get("area_id") or device.get("area_id") or ""
        if not area_id:
            continue
        rows.append(
            {
                "entity_id": entity_id,
                "name": entity_data.get("name") or entity_data.get("original_name") or entity_id,
                "area": restructurer.areas.get(area_id, {}).get("name", ""),
            }
        )
    return rows


def _sample_context(restructurer: Any, entity_id: str) -> Optional[dict]:
    """The template context of one entity, or ``None`` when it shows too little."""
    entity_data = restructurer.entities.get(entity_id)
    if entity_data is None:
        return None
    context = restructurer.build_naming_context(entity_id, entity_data)
    if not context.get("entity") or not context.get("device"):
        return None
    sample = {field: context.get(field, "") for field in SAMPLE_FALLBACK}
    sample["entity_id"] = entity_id.partition(".")[2]
    sample["domain"] = entity_id.partition(".")[0]
    sample["integration"] = entity_data.get("platform") or ""
    return sample


@naming.route("/api/naming_templates/sample")
def naming_templates_sample() -> Any:
    """A real entity to preview templates with, so the example is the user's own home.

    ``entity_id`` asks for one by name, ``pick=random`` for any other one.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(ensure_registry_loaded())
    finally:
        loop.close()
    restructurer = renamer_state.get("restructurer")
    if not restructurer or not restructurer.entities:
        return jsonify({"context": SAMPLE_FALLBACK, "entity_id": None})

    wanted = request.args.get("entity_id") or ""
    if wanted:
        sample = _sample_context(restructurer, wanted)
        if sample is not None:
            return jsonify({"context": sample, "entity_id": wanted})

    rows = _sample_rows(restructurer)
    if request.args.get("pick") == "random" and rows:
        for candidate in random.sample(rows, min(len(rows), 25)):
            sample = _sample_context(restructurer, candidate["entity_id"])
            if sample is not None:
                return jsonify({"context": sample, "entity_id": candidate["entity_id"]})
    for row in rows:
        sample = _sample_context(restructurer, row["entity_id"])
        if sample is not None:
            return jsonify({"context": sample, "entity_id": row["entity_id"]})
    return jsonify({"context": SAMPLE_FALLBACK, "entity_id": None})


@naming.route("/api/naming_templates/sample/entities")
def naming_templates_sample_entities() -> Any:
    """Every entity the preview can use, for the search field to filter."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(ensure_registry_loaded())
    finally:
        loop.close()
    restructurer = renamer_state.get("restructurer")
    if not restructurer or not restructurer.entities:
        return jsonify({"entities": []})
    return jsonify({"entities": _sample_rows(restructurer)})


@naming.route("/api/naming_templates/preview", methods=["POST"])
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
        restructurer = renamer_state.get("restructurer")
        sample_entity = f"{values.get('domain') or 'sensor'}.{values.get('entity_id') or ''}"
        spelled = restructurer.spelled_out_context(values, sample_entity) if restructurer is not None else dict(values)
        rendered = {}
        for key, template in templates.items():
            # An entity ID is technical; a name reads as words.
            source = values if key == "entity_id" else spelled
            rendered[key] = manager.render_template(template, source, normalize=key == "entity_id")
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


@naming.route("/api/type_mappings")
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


@naming.route("/api/type_mappings/user", methods=["POST"])
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


@naming.route("/api/type_mappings/user/<type_key>", methods=["DELETE"])
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


@naming.route("/api/learn_mapping", methods=["POST"])
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


@naming.route("/api/ha/language")
def get_ha_language():
    """The language the app speaks, which is the one Home Assistant is set to."""
    return jsonify({"language": renamer_state["naming_rules"].language})


def entity_type_key(entity_data: dict) -> str:
    """The key that groups entities of one type: translation_key, else the canonical supplied name."""
    translation_key = entity_data.get("translation_key")
    if translation_key:
        return f"tk:{translation_key}"
    native = canon(entity_data.get("original_name") or "")
    return f"name:{native}" if native else ""


def type_key_counts(restructurer) -> dict:
    counts: dict = {}
    for entity_data in restructurer.entities.values():
        key = entity_type_key(entity_data)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def type_key_integration_counts(restructurer) -> dict:
    """Entities per type and integration: "Tür" from Matter is a door sensor, from Miele a fridge."""
    counts: dict = {}
    for entity_data in restructurer.entities.values():
        key = entity_type_key(entity_data)
        integration = entity_data.get("platform")
        if key and integration:
            counts[(key, integration)] = counts.get((key, integration), 0) + 1
    return counts


def entity_model(restructurer, entity_data: dict) -> str:
    device = restructurer.devices.get(entity_data.get("device_id") or "", {})
    return device.get("model") or ""


def type_key_model_counts(restructurer) -> dict:
    """Entities per type and device model, the narrowest scope a rule can take."""
    counts: dict = {}
    for entity_data in restructurer.entities.values():
        key = entity_type_key(entity_data)
        model = entity_model(restructurer, entity_data)
        if key and model:
            counts[(key, model)] = counts.get((key, model), 0) + 1
    return counts


def _rule_key_for(entity: dict) -> tuple:
    """What a rule learned from this entity should match on.

    An integration that declares a translation key is the surest anchor. Where
    it does not, the supplied name is only usable when it names the entity
    alone: integrations without native entity names write the device into it,
    as in "Tomate air humidity", and no two of those names are alike. Their
    device class says what the entity measures and holds for all of them.
    """
    if entity.get("translation_key"):
        return "translation_key", entity["translation_key"]
    device_class = entity.get("device_class") or entity.get("original_device_class")
    if device_class and not entity.get("has_entity_name"):
        return "device_class", device_class
    return "name", entity.get("original_name") or ""


def _keys_in_use(restructurer) -> Optional[set]:
    """Every value this home could match a rule on, or ``None`` when unknown."""
    if restructurer is None or not restructurer.entities:
        return None
    keys = set()
    for entity in restructurer.entities.values():
        for value in (
            entity.get("translation_key"),
            entity.get("device_class") or entity.get("original_device_class"),
            entity.get("original_name"),
        ):
            if value:
                keys.add(canon(value))
        entity_id = entity.get("entity_id") or ""
        if entity_id:
            keys.add(entity_id.partition(".")[0])
    return keys


def _repair_rule_kinds(restructurer, rules) -> None:
    """Once, move rules whose value is a device class or a translation key.

    Which is which can only be told against a real home, so it waits until the
    entities are there.
    """
    if restructurer is None or not restructurer.entities:
        return
    report = rules.repair_kinds(
        translation_keys={entity.get("translation_key") for entity in restructurer.entities.values()},
        names={entity.get("original_name") for entity in restructurer.entities.values()},
        # What counts as a device class is Home Assistant's to say; the ones in
        # this home cover what a custom integration invents on top.
        device_classes=ha_translations.device_classes()
        | {
            entity.get("device_class") or entity.get("original_device_class")
            for entity in restructurer.entities.values()
        },
    )
    if report and report["moved"]:
        renamer_state["type_mappings"]._refresh_user_view()


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
            entity_data.get("device_class") or entity_data.get("original_device_class") or "",
            entity_data.get("platform") or None,
            entity_model(restructurer, entity_data) or None,
        )
        groups[group] = groups.get(group, 0) + 1
    for (translation_key, native, device_class, integration, model), size in groups.items():
        rule = (
            rules.find("translation_key", translation_key, integration, language, model)
            or rules.find("name", native, integration, language, model)
            or rules.find("device_class", device_class, integration, language, model)
        )
        if rule:
            counts[rule["id"]] = counts.get(rule["id"], 0) + size
    return counts


def _rule_builtins(rules) -> dict:
    """The built-in name each rule competes with, by rule id."""
    mappings = renamer_state["type_mappings"]
    language = rules.language
    builtins = {}
    for rule in rules.rules:
        if rule["match"]["kind"] == "translation_key":
            continue
        builtin = mappings.find_system_translation(rule["match"]["value"], language, rule["match"].get("integration"))
        if builtin is not None:
            builtins[rule["id"]] = builtin
    return builtins


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


@naming.route("/api/naming/settings", methods=["GET", "PUT"])
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


@naming.route("/api/naming/rules", methods=["GET", "POST"])
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
        loop.run_until_complete(ensure_registry_loaded())
    finally:
        loop.close()
    _repair_rule_kinds(renamer_state.get("restructurer"), rules)
    affected = _rule_affected_counts(renamer_state.get("restructurer"), rules)
    language = request.args.get("lang") or rules.language
    wanted = _keys_in_use(renamer_state.get("restructurer"))
    system = []
    for entry in renamer_state["type_mappings"].get_all_known_types(language):
        if not entry.get("system_default"):
            continue
        # Home Assistant knows hundreds of device classes; a list of the ones
        # this home does not have would bury the ones it does.
        if wanted is not None and entry["key"] not in wanted:
            continue
        system.append({"key": entry["key"], "value": entry["system_default"], "source": entry.get("source")})
    return jsonify(
        {
            "language": rules.language,
            "rules": [_rule_payload(rule, affected) for rule in rules.rules],
            "system": system,
        }
    )


@naming.route("/api/naming/rules/unused", methods=["GET", "DELETE"])
def naming_rules_unused():
    """The rules no entity in this home matches, and a way to be rid of them."""
    rules = renamer_state["naming_rules"]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(ensure_registry_loaded())
    finally:
        loop.close()
    restructurer = renamer_state.get("restructurer")
    affected = _rule_affected_counts(restructurer, rules)
    if affected is None:
        return jsonify({"error": "the entities are not loaded yet"}), 409
    unused = rules.unused(affected, rules.language, _rule_builtins(rules))
    if request.method == "GET":
        return jsonify({"rules": [_rule_payload(rule, affected) for rule in unused]})
    removed = rules.delete_many(rule["id"] for rule in unused)
    renamer_state["type_mappings"]._refresh_user_view()
    return jsonify({"removed": removed})


@naming.route("/api/naming/rules/<rule_id>", methods=["PUT", "DELETE"])
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


@naming.route("/api/naming/learn", methods=["POST"])
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
    model = entity_model(restructurer, entity) or None if scope == "model" else None
    kind, key = _rule_key_for(entity)
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


@naming.route("/api/naming/originals")
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


@naming.route("/api/naming/migration", methods=["GET"])
def naming_migration():
    return jsonify({"report": renamer_state["naming_rules"].migration_report})


@naming.route("/api/naming/migration/resolve", methods=["POST"])
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


@naming.route("/api/naming/preview", methods=["POST"])
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
