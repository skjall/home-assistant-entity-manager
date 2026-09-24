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
from naming_canon import canon
import naming_overrides
from naming_rules import (
    MAX_PATTERN_LENGTH,
    VERBATIM_KINDS,
    NamingRuleError,
    pattern_of,
    readable_pattern,
    target_of,
)
from naming_templates import NamingTemplateError
from registry import ensure_registry_loaded
from sanitize import sanitize_name, sanitize_registry_id, sanitize_string, validate_json_input

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
        translation = sanitize_name(data.get("translation"))

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
        translation = sanitize_name(data.get("translation"))

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


def type_pattern_counts(restructurer) -> dict:
    """Entities per integration and pattern: names that differ only in their numbers.

    Keyed by (integration, expression), which is what a pattern rule learned
    from any one of them would match on.
    """
    counts: dict = {}
    for entity_data in restructurer.entities.values():
        integration = entity_data.get("platform")
        learned = pattern_of(entity_data.get("original_name") or "")
        if integration and learned:
            counts[(integration, learned[0])] = counts.get((integration, learned[0]), 0) + 1
    return counts


def type_pattern_of(entity_data: dict, counts: dict) -> Optional[dict]:
    """The pattern this entity's supplied name makes, for offering it as a scope.

    Only where it reaches more than this one entity: a pattern that matches a
    single name says nothing the exact rule would not.
    """
    integration = entity_data.get("platform")
    learned = pattern_of(entity_data.get("original_name") or "")
    if not integration or not learned:
        return None
    count = counts.get((integration, learned[0]), 0)
    if count < 2:
        return None
    return {"label": readable_pattern(learned[0]), "numbers": learned[1], "count": count}


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


def type_key_model_domain_counts(restructurer) -> dict:
    """Entities per type, device model and domain: the narrowest scope but one.

    An integration supplies one name for what it measures and what it sets, so
    a model on its own reaches both. Adding the domain separates them.
    """
    counts: dict = {}
    for entity_id, entity_data in restructurer.entities.items():
        key = entity_type_key(entity_data)
        model = entity_model(restructurer, entity_data)
        domain = entity_id.partition(".")[0]
        if key and model and domain:
            counts[(key, model, domain)] = counts.get((key, model, domain), 0) + 1
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


def _names_by_rule(restructurer) -> dict:
    """entity_id -> the rule that decides its type, as the naming itself decides it.

    Asking the rules directly answers which rule could apply, which is not the
    same as which one does: a rule on the device class "update" is found for
    UniFi's "regenerate password" button and then dropped, because the name is
    about something else entirely. Counting the finds said four entities where
    two are named, and the list under the count showed the two it does not
    touch. Resolving each entity costs well under a second for a home of a few
    thousand, and it cannot drift from what the user sees.
    """
    named = {}
    was_reading_only = restructurer.reading_only
    restructurer.reading_only = True
    try:
        for entity_id, entity_data in restructurer.entities.items():
            try:
                behind = restructurer.rule_behind(entity_id, entity_data)
            except Exception as error:  # noqa: BLE001 - one entity must not fail the count
                logger.debug("Could not resolve %s: %s", entity_id, error)
                continue
            if behind:
                named[entity_id] = behind
    finally:
        restructurer.reading_only = was_reading_only
    return named


def _rule_affected_counts(restructurer, rules):
    """How many loaded entities each rule names, or None when nothing is loaded.

    Without the entity list a rule's reach is unknown, which is not the same as
    zero: reporting zero would brand every rule as useless right after a start.
    """
    if restructurer is None or not restructurer.entities:
        return None
    counts = {rule["id"]: 0 for rule in rules.rules}
    for resolution in _names_by_rule(restructurer).values():
        rule_id = resolution["rule_id"]
        counts[rule_id] = counts.get(rule_id, 0) + 1
    return counts


def _rule_builtins(rules) -> dict:
    """The built-in name each rule competes with, by rule id."""
    mappings = renamer_state["type_mappings"]
    language = rules.language
    builtins = {}
    for rule in rules.rules:
        if rule["match"]["kind"] == "translation_key":
            continue
        builtin = mappings.find_system_translation(rule["match"]["value"], language, rules.sole_integration(rule))
        if builtin is not None:
            builtins[rule["id"]] = builtin
    return builtins


def _entity_ids_by_registry_id() -> dict:
    """registry id -> entity_id, built once for a whole list of rules.

    Looking each one up on its own would walk every entity again per rule,
    which on a large home is a hundred scans of several thousand entries.
    """
    restructurer = renamer_state.get("restructurer")
    return {
        entity["id"]: entity_id
        for entity_id, entity in (restructurer.entities if restructurer else {}).items()
        if entity.get("id")
    }


def _rule_payload(rule: dict, affected: dict, entity_ids: Optional[dict] = None) -> dict:
    rules = renamer_state["naming_rules"]
    mappings = renamer_state["type_mappings"]
    language = rules.language
    builtin = None
    if rule["match"]["kind"] not in VERBATIM_KINDS:
        builtin = mappings.find_system_translation(rule["match"]["value"], language, rules.sole_integration(rule))
    return {
        **rule,
        "affected": affected.get(rule["id"], 0) if affected is not None else None,
        "redundant": rules.is_redundant(rule, language, builtin),
        # A pattern as a person reads it; the expression stays in match.
        "label": readable_pattern(rule["match"]["value"]) if rule["match"]["kind"] == "pattern" else None,
        # A rule written for one entity is about that entity, and a registry id
        # says nothing to a reader. The entity it belongs to does.
        "entities": [
            {"registry_id": registry_id, "entity_id": (entity_ids or {}).get(registry_id)}
            for registry_id in rules._entities_of(rule)
        ],
    }


@naming.route("/api/naming/settings", methods=["GET", "PUT"])
def naming_settings():
    """Language the type rules are applied in, and the spelling of shown types."""
    rules = renamer_state["naming_rules"]
    if request.method == "PUT":
        data = request.json if isinstance(request.json, dict) else {}
        language = sanitize_string(data.get("language", ""), max_length=8)
        display_case = sanitize_string(data.get("display_case", ""), max_length=16)
        pattern_rules = data.get("pattern_rules")
        if not language and not display_case and not isinstance(pattern_rules, bool):
            return jsonify({"error": "language, display_case or pattern_rules required"}), 400
        try:
            if language:
                rules.set_language(language)
            if display_case:
                rules.set_display_case(display_case)
            if isinstance(pattern_rules, bool):
                rules.set_pattern_rules(pattern_rules)
        except NamingRuleError as error:
            return jsonify({"error": str(error)}), 400
        renamer_state["type_mappings"]._refresh_user_view()
    return jsonify(
        {"language": rules.language, "display_case": rules.display_case, "pattern_rules": rules.pattern_rules}
    )


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
        kind = sanitize_string(match.get("kind", "name"), max_length=32)
        try:
            rule = rules.upsert(
                kind,
                sanitize_string(match.get("value", ""), max_length=MAX_PATTERN_LENGTH if kind == "pattern" else 128),
                sanitize_string(match.get("integration") or "", max_length=64) or None,
                language,
                sanitize_string(target),
                source="user",
                model=sanitize_string(match.get("model") or "", max_length=128) or None,
                domain=sanitize_string(match.get("domain") or "", max_length=64) or None,
            )
        except NamingRuleError as error:
            return jsonify({"error": str(error)}), 400
        renamer_state["type_mappings"]._refresh_user_view()
        affected = _rule_affected_counts(renamer_state.get("restructurer"), rules)
        return jsonify({"rule": _rule_payload(rule, affected, _entity_ids_by_registry_id())})

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
    entity_ids = _entity_ids_by_registry_id()
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
            "rules": [_rule_payload(rule, affected, entity_ids) for rule in rules.rules],
            "system": system,
        }
    )


@naming.route("/api/naming/rules/<rule_id>/entities", methods=["GET"])
def rule_entities(rule_id: str):
    """Which entities a rule reaches, and what it does to each of them.

    The list said how many and never which, so a rule that worded something
    wrongly could not be checked against the entities it words. Each one comes
    back with the word Home Assistant supplies, the name it carries today and
    the name the rule would give it - the three values a reader needs to say
    whether the rule is right.
    """
    rules = renamer_state["naming_rules"]
    rule = next((one for one in rules.rules if one["id"] == rule_id), None)
    if rule is None:
        return jsonify({"error": "unknown rule"}), 404

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(ensure_registry_loaded())
    finally:
        loop.close()

    restructurer = renamer_state.get("restructurer")
    if restructurer is None or not restructurer.entities:
        return jsonify({"error": "the registry is not loaded"}), 503

    found = []
    for entity_id, behind in _names_by_rule(restructurer).items():
        if behind["rule_id"] != rule_id:
            continue
        entity_data = restructurer.entities.get(entity_id) or {}
        try:
            proposed_id, proposed = restructurer.calculate_new_entity_name(entity_id)
        except Exception as error:  # noqa: BLE001 - one entity must not fail the list
            logger.debug("Could not work out a name for %s: %s", entity_id, error)
            proposed_id, proposed = "", ""
        device = restructurer.devices.get(entity_data.get("device_id") or "", {}) or {}
        found.append(
            {
                "entity_id": entity_id,
                "domain": entity_id.split(".")[0],
                "name": entity_data.get("name") or entity_data.get("original_name") or "",
                # What the rule caught this entity on, not what it might have:
                # a device-class rule named after a translation key sends the
                # reader looking for a rule that does not exist.
                "caught_on": behind["kind"],
                "caught_value": behind["value"],
                "supplied": entity_data.get("original_name") or "",
                "platform": entity_data.get("platform") or "",
                "proposed": proposed,
                "proposed_id": proposed_id,
                # Where to go and look at it.
                "device_id": entity_data.get("device_id") or "",
                "device_name": device.get("name_by_user") or device.get("name") or "",
                "area_id": device.get("area_id") or entity_data.get("area_id") or "",
                # Named one by one rather than caught by what it supplies.
                "by_name": bool(entity_data.get("id")) and entity_data["id"] in rules._entities_of(rule),
            }
        )
    found.sort(key=lambda one: one["entity_id"])
    return jsonify({"rule_id": rule_id, "entities": found, "total": len(found)})


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
        entity_ids = _entity_ids_by_registry_id()
        return jsonify({"rules": [_rule_payload(rule, affected, entity_ids) for rule in unused]})
    removed = rules.delete_many(rule["id"] for rule in unused)
    renamer_state["type_mappings"]._refresh_user_view()
    return jsonify({"removed": removed})


def _filter_from(data: dict) -> dict:
    """The one filter a request is about, sanitised."""
    return {
        key: sanitize_string(data.get(key) or "", max_length=128)
        for key in ("registry_id", "integration", "model", "domain")
        if data.get(key)
    }


@naming.route("/api/naming/rules/<rule_id>/filters", methods=["POST", "DELETE"])
def naming_rule_filters(rule_id):
    """Add or remove one of the places a rule applies.

    A rule reaches a list of filters, so widening or narrowing it is adding or
    removing one of them - not editing a single scope, which could only ever be
    exchanged.
    """
    rules = renamer_state["naming_rules"]
    rule_id = sanitize_string(rule_id, max_length=32)
    rule = rules.get(rule_id)
    if rule is None:
        return jsonify({"error": "unknown rule"}), 404
    data = request.json if isinstance(request.json, dict) else {}
    one = _filter_from(data)
    if not one:
        return jsonify({"error": "a filter names an entity, an integration or a model"}), 400
    try:
        if request.method == "DELETE":
            rule = rules.remove_filter(rule_id, one)
        else:
            rule = rules.add_filter(
                rule["match"]["kind"],
                rule["match"]["value"],
                rules.language,
                rule["targets"].get(rules.language) or "",
                one,
            )
    except NamingRuleError as error:
        return jsonify({"error": str(error)}), 400
    renamer_state["type_mappings"]._refresh_user_view()
    if rule.get("deleted"):
        # Its last place went with it; a rule left without one would apply to
        # everything of its type, which removing a place never means.
        return jsonify({"deleted": True, "rule_id": rule_id})
    affected = _rule_affected_counts(renamer_state.get("restructurer"), rules)
    return jsonify({"rule": _rule_payload(rule, affected, _entity_ids_by_registry_id())})


@naming.route("/api/naming/filters", methods=["GET"])
def naming_filters_available():
    """The integrations and models this home actually has, to pick a filter from.

    Each model is answered with its maker. "Zigbee smart water valve" is not
    something anyone looks for; "SONOFF" is, and under MQTT every model belongs
    to a different maker.
    """
    restructurer = renamer_state.get("restructurer")
    entities = (restructurer.entities if restructurer else {}) or {}
    integrations: dict = {}
    for entity in entities.values():
        integration = entity.get("platform")
        if not integration:
            continue
        seen = integrations.setdefault(integration, {})
        model = entity_model(restructurer, entity)
        if not model:
            continue
        device = restructurer.devices.get(entity.get("device_id") or "", {})
        row = seen.setdefault(model, {"model": model, "manufacturer": device.get("manufacturer") or "", "count": 0})
        row["count"] += 1
        if not row["manufacturer"]:
            row["manufacturer"] = device.get("manufacturer") or ""
    return jsonify(
        {
            "integrations": [
                {
                    "integration": name,
                    "models": [models[key] for key in sorted(models)],
                }
                for name, models in sorted(integrations.items())
            ]
        }
    )


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
    # The expression of a pattern rule, which is the one kind written to be
    # adjusted afterwards: the others are matched on a word the integration
    # supplies, and that word is not the writer's to change.
    expression = data.get("match_value")
    if expression is not None:
        expression = sanitize_string(expression, max_length=500)
    try:
        # Where a rule applies is added and removed one filter at a time; a
        # single scope here would have to throw the rest of the list away.
        rule = rules.update(rule_id, targets=data.get("targets"), value=expression)
    except NamingRuleError as error:
        return jsonify({"error": str(error)}), 400
    renamer_state["type_mappings"]._refresh_user_view()
    affected = _rule_affected_counts(renamer_state.get("restructurer"), rules)
    return jsonify({"rule": _rule_payload(rule, affected, _entity_ids_by_registry_id())})


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
    kind, key = _rule_key_for(entity)
    if scope == "pattern":
        # The supplied name with its numbers left open, and the typed name
        # carrying each number on where it repeats it.
        learned = pattern_of(entity.get("original_name") or "")
        if not learned or not entity.get("platform"):
            return jsonify({"error": "a pattern needs a supplied name with a number, from an integration"}), 400
        kind, key = "pattern", learned[0]
        value = target_of(value, learned[1])
    if not key:
        return jsonify({"error": "entity has no name to derive a rule from"}), 400
    # Where the correction should apply, as the one filter it is. "Everywhere"
    # is no filter at all, and each step below it narrows the one above:
    # this integration, this model, this model's entities of one domain - an
    # integration supplies one name for what it measures and what it sets, and
    # only the domain tells the two apart - and finally this entity alone.
    one = None
    if scope == "entity":
        one = {"registry_id": entity.get("id") or ""}
    elif scope in ("integration", "model", "domain", "pattern"):
        one = {"integration": entity.get("platform") or ""}
        if scope in ("model", "domain"):
            one["model"] = entity_model(restructurer, entity) or ""
        if scope == "domain":
            one["domain"] = entity_id.partition(".")[0]
    rules = renamer_state["naming_rules"]
    try:
        # The rule that already says this gains the place; a second rule saying
        # the same thing somewhere else would have to be kept in step by hand.
        rule = rules.add_filter(kind, key, rules.language, value, one, source="learned", learned_from=entity_id)
    except NamingRuleError as error:
        return jsonify({"error": str(error)}), 400
    renamer_state["type_mappings"]._refresh_user_view()
    affected = _rule_affected_counts(restructurer, rules)
    return jsonify({"rule": _rule_payload(rule, affected, _entity_ids_by_registry_id())})


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


# --------------------------------------------------------------------------- #
# Exceptions: one entity, named its own way
# --------------------------------------------------------------------------- #


def _entity_of(registry_id: str):
    """The entity_id and registry entry behind a registry id, or (None, None)."""
    restructurer = renamer_state.get("restructurer")
    for entity_id, entity in (restructurer.entities if restructurer else {}).items():
        if entity.get("id") == registry_id:
            return entity_id, entity
    return None, None


def _type_without_exception(entity_id: str, entity: dict) -> str:
    """What the rules alone would call this entity."""
    restructurer = renamer_state.get("restructurer")
    if not restructurer:
        return ""
    return restructurer.build_naming_context(entity_id, entity, ignore_exception=True).get("entity", "")


def _exception_rows():
    """Every exception with the entity it belongs to and what it still changes."""
    stored = renamer_state["naming_overrides"].get_all_entity_overrides()
    rows = []
    for registry_id, entry in stored.items():
        entity_id, entity = _entity_of(registry_id)
        would_be = _type_without_exception(entity_id, entity) if entity_id else ""
        name = (entry or {}).get("name") or ""
        rows.append(
            {
                "registry_id": registry_id,
                "entity_id": entity_id,
                "name": name,
                "source": (entry or {}).get("source") or naming_overrides.USER,
                "keep_original": bool((entry or {}).get("keep_original")),
                "created_at": (entry or {}).get("created_at"),
                "would_be": would_be,
                # An exception that says exactly what the rules already say
                # changes nothing and only makes the list harder to read.
                "redundant": bool(entity_id) and not (entry or {}).get("keep_original") and name == would_be,
                "orphan": entity_id is None,
            }
        )
    rows.sort(key=lambda row: (row["entity_id"] or "￿", row["registry_id"]))
    return rows


@naming.route("/api/naming/exceptions", methods=["GET"])
def naming_exceptions():
    """List the exceptions, saying which of them still change anything."""
    return jsonify({"exceptions": _exception_rows()})


@naming.route("/api/naming/exceptions/<registry_id>", methods=["DELETE"])
def naming_exception_delete(registry_id: str):
    """Drop one exception; the rules decide the name again from now on."""
    renamer_state["naming_overrides"].remove_entity_override(sanitize_registry_id(registry_id))
    return jsonify({"success": True})


@naming.route("/api/naming/exceptions/cleanup", methods=["POST"])
def naming_exceptions_cleanup():
    """Remove the exceptions a rule has meanwhile caught up with.

    Only those whose value matches what the rules say anyway, and those whose
    entity no longer exists. Anything that still changes a name is left alone.
    """
    overrides = renamer_state["naming_overrides"]
    removed = [row for row in _exception_rows() if row["redundant"] or row["orphan"]]
    for row in removed:
        overrides.remove_entity_override(row["registry_id"])
    return jsonify({"success": True, "removed": [row["registry_id"] for row in removed], "count": len(removed)})


@naming.route("/api/naming/exceptions/adopt", methods=["POST"])
def naming_exception_adopt():
    """Keep a name that was set in Home Assistant itself.

    The stored name is the whole rendered name; what an exception holds is the
    type part alone. The templates say how to take the one apart to get the
    other; a name that fits no template is kept as it stands, because guessing
    would be worse than a value the user can see and correct.
    """
    data = request.json
    is_valid, error = validate_json_input(data, ["registry_id"])
    if not is_valid:
        return jsonify({"error": error}), 400

    registry_id = sanitize_registry_id(data.get("registry_id"))
    entity_id, entity = _entity_of(registry_id)
    if not entity_id:
        return jsonify({"error": "unknown entity"}), 404

    current = entity.get("name") or ""
    if not current:
        return jsonify({"error": "this entity carries no name of its own"}), 400

    restructurer = renamer_state["restructurer"]
    context = restructurer.build_naming_context(entity_id, entity, ignore_exception=True)
    adopted = restructurer.naming_templates.extract_field("entity_name", current, "entity", context) or current

    renamer_state["naming_overrides"].set_entity_override(registry_id, adopted, source=naming_overrides.HA_UI)
    # The name in the registry is now the one the exception describes, so it
    # belongs here again and stops counting as changed elsewhere.
    renamer_state["naming_state"].record(
        registry_id,
        applied_name=current,
        applied_entity_id=entity_id,
        base_entity=adopted,
        template_hash=restructurer.naming_templates.fingerprint(),
        won_by="override",
    )
    return jsonify({"success": True, "name": adopted, "entity_id": entity_id})


@naming.route("/api/naming/exceptions/ignore", methods=["POST"])
def naming_exception_ignore():
    """Leave one entity alone: no proposal, the supplied name stays."""
    data = request.json
    is_valid, error = validate_json_input(data, ["registry_id"])
    if not is_valid:
        return jsonify({"error": error}), 400

    registry_id = sanitize_registry_id(data.get("registry_id"))
    if not registry_id:
        return jsonify({"error": "Invalid registry ID"}), 400

    if data.get("ignore") is False:
        renamer_state["naming_overrides"].remove_entity_override(registry_id)
        return jsonify({"success": True, "ignored": False})

    renamer_state["naming_overrides"].ignore_entity(registry_id)
    return jsonify({"success": True, "ignored": True})


SETTINGS_SECTIONS = ("naming", "rules", "log", "system")
