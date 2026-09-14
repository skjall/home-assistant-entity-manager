"""What this add-on offers to anything that is not its own web interface.

One list, three uses: it is the OpenAPI document a reader browses, it is the set
of routes the access gate opens to a token, and it is what the MCP server turns
into tools. Written once so the three can never drift apart - a route missing
here is a route an assistant cannot call and a reader never learns about.

Left out on purpose: the token management (creating the key that opens this API
cannot itself be reachable through it), the preview-and-execute pair whose ids
only mean something inside one browser session, and the routes that exist to
fill fields in the interface.
"""

from typing import Any, Dict, List, Optional, Tuple

# The three kinds a rule can match an entity by; used in several places.
RULE_KINDS = ["translation_key", "device_class", "name"]


class Operation:
    """One callable thing: its address, what it wants, and what it is for."""

    def __init__(
        self,
        path: str,
        method: str,
        name: str,
        summary: str,
        description: str = "",
        query: Optional[Dict[str, Dict[str, Any]]] = None,
        body: Optional[Dict[str, Dict[str, Any]]] = None,
        required: Tuple[str, ...] = (),
        tag: str = "",
    ) -> None:
        self.path = path
        self.method = method.lower()
        self.name = name
        self.summary = summary
        self.description = description or summary
        self.query = query or {}
        self.body = body or {}
        self.required = required
        self.tag = tag

    @property
    def reads(self) -> bool:
        return self.method == "get"

    def path_parameters(self) -> List[str]:
        """The <bracketed> pieces of the address, in the order they appear."""
        found = []
        rest = self.path
        while "<" in rest:
            _, _, rest = rest.partition("<")
            piece, _, rest = rest.partition(">")
            found.append(piece.split(":")[-1])
        return found

    def openapi_path(self) -> str:
        """The address in OpenAPI's spelling: {name} instead of <name>."""
        spelled = self.path
        for parameter in self.path_parameters():
            for pattern in (f"<path:{parameter}>", f"<{parameter}>"):
                spelled = spelled.replace(pattern, "{" + parameter + "}")
        return spelled


def text(description: str) -> Dict[str, Any]:
    return {"type": "string", "description": description}


def flag(description: str) -> Dict[str, Any]:
    return {"type": "boolean", "description": description}


def texts(description: str) -> Dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def thing(description: str) -> Dict[str, Any]:
    return {"type": "object", "additionalProperties": True, "description": description}


OPERATIONS: List[Operation] = [
    # ---------------------------------------------------------------- looking
    Operation(
        "/api/stats",
        "get",
        "stats",
        "How many entities, devices and areas this home has.",
        "The quickest way to see whether the registries are loaded and how big the home is.",
        tag="Overview",
    ),
    Operation(
        "/api/hierarchy",
        "get",
        "hierarchy",
        "Every floor, area, device and entity, with the name each would get.",
        "The whole picture in one answer, including the proposed name and id for every entity. "
        "It is the most expensive call in the add-on; ask for it once and work from the answer.",
        tag="Overview",
    ),
    Operation(
        "/api/all_entities",
        "get",
        "all_entities",
        "Every entity with its id and current name.",
        "A flat list, cheap to fetch, for finding an entity id before asking about it.",
        tag="Overview",
    ),
    Operation(
        "/api/areas",
        "get",
        "areas",
        "Every area with the domains present in it.",
        tag="Overview",
    ),
    Operation(
        "/api/ha/language",
        "get",
        "language",
        "The language Home Assistant is set to, which is the one names are built in.",
        tag="Overview",
    ),
    Operation(
        "/api/network",
        "get",
        "reachability",
        "Who may reach this add-on from outside, and on which port.",
        "The configured networks, whether a port is published, and how far the token and the "
        "assistant interface reach.",
        tag="Overview",
    ),
    # ----------------------------------------------------------------- naming
    Operation(
        "/api/naming/settings",
        "get",
        "naming_settings",
        "The language the rules are written in and the spelling of shown types.",
        tag="Naming",
    ),
    Operation(
        "/api/naming/settings",
        "put",
        "set_naming_settings",
        "Change the naming language or the spelling of types.",
        "Switching the language rebuilds every proposed name, because the rules are kept per "
        "language. display_case is one of first_word, title or lower.",
        body={
            "language": text("Two-letter language code the rules are written in, e.g. de or en."),
            "display_case": text("How a type is spelled: first_word, title or lower."),
        },
        tag="Naming",
    ),
    Operation(
        "/api/naming/rules",
        "get",
        "rules",
        "Every naming rule, with how many entities it reaches.",
        "Answers with rules (each with its match, its name and the number of entities it reaches), "
        "system (the names Home Assistant itself supplies, for the types this home actually has) "
        "and language. The reach is the honest measure of whether a rule matters.",
        query={
            "lang": text("Language to read the rules in; defaults to the configured one."),
        },
        tag="Naming",
    ),
    Operation(
        "/api/naming/rules",
        "post",
        "create_rule",
        "Name every entity of one type.",
        "match says what the rule matches: kind is translation_key, device_class or name, value is "
        "the type as the integration supplies it, and integration and model narrow the rule to "
        'those devices. targets carries the name per language, e.g. {"de": "Raumtemperatur"}. '
        "Look at supplied_names first to get the value right, and at rules to see what a rule "
        "already reaches.",
        body={
            "match": thing("kind, value, and optionally integration and model."),
            "targets": thing('The name per language code, e.g. {"de": "Raumtemperatur"}.'),
            "value": text("The name, when only one language is meant."),
        },
        tag="Naming",
    ),
    Operation(
        "/api/naming/rules/<rule_id>",
        "put",
        "update_rule",
        "Change an existing rule's name or the devices it narrows to.",
        body={
            "targets": thing("The name per language code."),
            "integration": text("Narrow the rule to one integration, or empty to widen it."),
            "model": text("Narrow the rule to one device model."),
        },
        tag="Naming",
    ),
    Operation(
        "/api/naming/rules/<rule_id>",
        "delete",
        "delete_rule",
        "Remove one rule; the entities it named fall back to the next source.",
        tag="Naming",
    ),
    Operation(
        "/api/naming/rules/unused",
        "get",
        "unused_rules",
        "The rules no entity in this home matches.",
        "Usually left over from an integration that is gone, or from a migration.",
        tag="Naming",
    ),
    Operation(
        "/api/naming/rules/unused",
        "delete",
        "delete_unused_rules",
        "Delete every rule that no entity matches.",
        tag="Naming",
    ),
    Operation(
        "/api/naming/originals",
        "get",
        "supplied_names",
        "The distinct names the integrations supply, for picking a rule key.",
        "Search here rather than guessing a key: these are the exact strings rules match on.",
        query={"q": text("Filter the names by this text.")},
        tag="Naming",
    ),
    Operation(
        "/api/naming/preview",
        "post",
        "naming_for",
        "What one entity would be called under the current rules.",
        "Answers with rendered.entity_name and rendered.entity_id - the name and id the entity "
        "would get - and with resolution, which says where that name came from: won_by names the "
        "source (a rule, an exception, the integration's own name) and rule_id the rule, if one "
        "decided it. Read this before renaming anything and say what will happen. type_value "
        "tries out a different type name without saving it.",
        body={
            "entity_id": text("The entity to work out a name for."),
            "type_value": text("Try this type name instead of the resolved one, without saving."),
        },
        required=("entity_id",),
        tag="Naming",
    ),
    Operation(
        "/api/naming/learn",
        "post",
        "learn_from_correction",
        "Turn a corrected name into a rule, letting the server pick the key.",
        "scope is entity, integration or global, and decides how far the new rule reaches.",
        body={
            "entity_id": text("The entity whose type was corrected."),
            "value": text("The name it should have."),
            "scope": text("How far the rule reaches: entity, integration or global."),
        },
        required=("entity_id", "value"),
        tag="Naming",
    ),
    Operation(
        "/api/naming/migration",
        "get",
        "migration_conflicts",
        "Rules from the old flat mappings that still need a decision.",
        tag="Naming",
    ),
    Operation(
        "/api/naming/migration/resolve",
        "post",
        "resolve_migration",
        "Decide one conflict left over from the old flat mappings.",
        "A migrated rule can carry several candidate names; this picks the one to keep. "
        "migration_conflicts lists what is still open.",
        body={
            "rule_id": text("The rule the conflict belongs to."),
            "value": text("The alternative to keep as the rule's name."),
        },
        required=("rule_id", "value"),
        tag="Naming",
    ),
    Operation(
        "/api/set_entity_override",
        "post",
        "set_exception",
        "Name one entity by hand, ahead of every rule.",
        "An exception is the user's own wording for this one entity. Prefer a rule where the same "
        "correction would apply to more than one entity. registry_id is the entity's registry id, "
        "which hierarchy and all_entities report as id. Note that this does not only remember the "
        "exception: the resulting display name is written into Home Assistant straight away. "
        "Leave override_name out to drop an exception again.",
        body={
            "registry_id": text("The entity's registry id."),
            "override_name": text("The name this one entity should have."),
        },
        required=("registry_id",),
        tag="Naming",
    ),
    Operation(
        "/api/naming/exceptions",
        "get",
        "exceptions",
        "Every entity that is named by hand rather than by a rule.",
        "Each entry says where the exception came from - typed here, or adopted from a name set in "
        "Home Assistant itself - and what the rules alone would call the entity. An entry marked "
        "redundant says exactly what the rules already say and could go; one marked orphan belongs "
        "to an entity that no longer exists.",
        tag="Naming",
    ),
    Operation(
        "/api/naming/exceptions/<registry_id>",
        "delete",
        "delete_exception",
        "Drop one exception, so the rules decide that name again.",
        "registry_id is the entity's registry id, which hierarchy and all_entities report as id. "
        "The name already written into Home Assistant stays until the entity is renamed again.",
        tag="Naming",
    ),
    Operation(
        "/api/naming/exceptions/cleanup",
        "post",
        "cleanup_exceptions",
        "Remove the exceptions that no longer change anything.",
        "Only those whose value matches what the rules say anyway, and those whose entity is gone. "
        "Anything that still changes a name is left alone. Returns which ones were removed.",
        tag="Naming",
    ),
    Operation(
        "/api/naming/exceptions/adopt",
        "post",
        "adopt_name",
        "Keep a name that was set in Home Assistant itself.",
        "Use this when an entity is reported with drift: somebody renamed it outside this add-on. "
        "The rendered name is taken apart along the templates to get the type part, which is then "
        "stored as an exception, and the entity counts as named from here again. The other way out "
        "of drift is apply_naming, which writes the proposal over the changed name.",
        body={"registry_id": text("The entity's registry id.")},
        required=("registry_id",),
        tag="Naming",
    ),
    Operation(
        "/api/naming/exceptions/ignore",
        "post",
        "ignore_entity",
        "Leave one entity alone: no proposal, the supplied name stays.",
        "An ignored entity keeps whatever its integration calls it and is never proposed for "
        "renaming. Send ignore false to take it back into the naming again.",
        body={
            "registry_id": text("The entity's registry id."),
            "ignore": flag("False takes the entity back into the naming. Defaults to true."),
        },
        required=("registry_id",),
        tag="Naming",
    ),
    Operation(
        "/api/naming_templates",
        "get",
        "naming_templates",
        "The templates that assemble a name out of area, device and type.",
        tag="Naming",
    ),
    Operation(
        "/api/naming_templates",
        "put",
        "set_naming_templates",
        "Change the templates, or apply a ready-made preset.",
        body={
            "templates": thing("Template string per domain, plus a default."),
            "preset": text("Name of a ready-made set to apply instead."),
        },
        tag="Naming",
    ),
    Operation(
        "/api/naming_templates/preview",
        "post",
        "preview_templates",
        "Render a name from templates without saving them.",
        body={
            "templates": thing("The templates to try."),
            "context": thing("Area, device and type to render with."),
        },
        query={
            "entity_id": text("Render with this real entity instead of a made-up context."),
            "domain": text("Which domain's template to use."),
        },
        tag="Naming",
    ),
    Operation(
        "/api/type_mappings",
        "get",
        "type_mappings",
        "Every type and the name it resolves to, system defaults and your own.",
        query={"lang": text("Language to read the names in.")},
        tag="Naming",
    ),
    Operation(
        "/api/type_mappings/user",
        "post",
        "set_type_mapping",
        "Give one supplied type your own name.",
        "The blunt form of a rule: it matches on the type alone, without narrowing to an "
        "integration or a model. Prefer create_rule where that matters.",
        body={"type_key": text("The type as supplied."), "translation": text("Your name for it.")},
        required=("type_key", "translation"),
        tag="Naming",
    ),
    Operation(
        "/api/type_mappings/user/<type_key>",
        "delete",
        "delete_type_mapping",
        "Drop your own name for a type, falling back to the default.",
        tag="Naming",
    ),
    Operation(
        "/api/learn_mapping",
        "post",
        "learn_type_mapping",
        "Remember a type name learned from a rename.",
        body={"type_key": text("The type as supplied."), "translation": text("The learned name.")},
        required=("type_key", "translation"),
        tag="Naming",
    ),
    # --------------------------------------------------------------- renaming
    Operation(
        "/api/rename_entity",
        "post",
        "rename_entity",
        "Rename one entity, and rewrite everything that refers to it.",
        "Automations, scripts, scenes and dashboards that name the old id are rewritten along "
        "with it. This changes the user's house; ask naming_for first and say what will happen. "
        "Either field may be left out to change only the other.",
        body={
            "old_entity_id": text("The entity as it is called now."),
            "new_entity_id": text("The id it should have."),
            "new_friendly_name": text("The display name it should have."),
        },
        required=("old_entity_id",),
        tag="Renaming",
    ),
    Operation(
        "/api/execute_direct",
        "post",
        "rename_entities",
        "Rename many entities in one background job.",
        "Each element is {old_entity_id, new_entity_id, new_friendly_name}. Returns a job; poll "
        "job for its progress. Use this rather than one call per entity for a whole room.",
        body={
            "entities": {
                "type": "array",
                "description": "The renames to carry out.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_entity_id": {"type": "string"},
                        "new_entity_id": {"type": "string"},
                        "new_friendly_name": {"type": "string"},
                    },
                },
            }
        },
        required=("entities",),
        tag="Renaming",
    ),
    Operation(
        "/api/apply_naming",
        "post",
        "apply_naming",
        "Give several entities the name the rules propose for them.",
        "Preview and rename in one step: for each entity the current proposal is worked out and "
        "written, and everything that referred to the old id is rewritten. Each entity is reported "
        "on its own and one failure does not stop the rest. At most 100 entities per call. This "
        "changes the user's house; read naming_for first and say what will happen.",
        body={"entity_ids": texts("The entities to bring in line with the rules.")},
        required=("entity_ids",),
        tag="Renaming",
    ),
    Operation(
        "/api/normalize",
        "post",
        "normalize_names",
        "Turn display names into the ids they would become.",
        "Nothing is changed; this is the spelling rule made visible.",
        body={"names": texts("The names to convert.")},
        required=("names",),
        tag="Renaming",
    ),
    Operation(
        "/api/rename_log",
        "get",
        "rename_log",
        "What an entity was called before, and what it is called now.",
        "The audit log of every rename this add-on carried out. Without entity_id the whole log " "is returned.",
        query={"entity_id": text("Resolve this id, old or new, against the log.")},
        tag="Renaming",
    ),
    # ---------------------------------------------------------------- devices
    Operation(
        "/api/rename_device",
        "post",
        "rename_device",
        "Rename a device and every entity that follows its name.",
        "Runs as a background job because it touches many entities; poll job for progress.",
        body={"device_id": text("The device to rename."), "new_name": text("Its new name.")},
        required=("device_id", "new_name"),
        tag="Devices",
    ),
    Operation(
        "/api/assign_device_area",
        "post",
        "assign_device_area",
        "Move a device into an area.",
        "Entities that take their area from the device follow, and so do their names.",
        body={"device_id": text("The device to move."), "area_id": text("The area to move it to.")},
        required=("device_id", "area_id"),
        tag="Devices",
    ),
    Operation(
        "/api/sync_z2m_name",
        "post",
        "sync_zigbee_name",
        "Give a Zigbee2MQTT device the name Home Assistant knows it by.",
        "Changes the name inside Zigbee2MQTT only; nothing in Home Assistant is renamed.",
        body={"device_id": text("The device to bring in line.")},
        required=("device_id",),
        tag="Devices",
    ),
    Operation(
        "/api/enable_device",
        "post",
        "enable_device",
        "Switch a disabled device back on.",
        "Its entities come back with it, and are named like any other.",
        body={"device_id": text("The device to enable.")},
        required=("device_id",),
        tag="Devices",
    ),
    Operation(
        "/api/enable_entity",
        "post",
        "enable_entity",
        "Switch a disabled entity back on.",
        "A disabled entity has no state and no name to manage until it is enabled.",
        body={"entity_id": text("The entity to enable.")},
        required=("entity_id",),
        tag="Devices",
    ),
    Operation(
        "/api/enable_all",
        "post",
        "enable_entities",
        "Switch many disabled entities back on, as a background job.",
        body={"entity_ids": texts("The entities to enable.")},
        required=("entity_ids",),
        tag="Devices",
    ),
    Operation(
        "/api/delete_entity",
        "post",
        "delete_entity",
        "Remove an entity whose device is gone from the registry.",
        "Only orphans can be deleted; an entity a device still provides comes back at once.",
        body={"entity_id": text("The orphaned entity to remove.")},
        required=("entity_id",),
        tag="Devices",
    ),
    Operation(
        "/api/bridge/status",
        "get",
        "bridge_status",
        "Which integrations this add-on can act on natively.",
        tag="Devices",
    ),
    # ------------------------------------------------------------- references
    Operation(
        "/api/broken_references",
        "get",
        "broken_references",
        "Every place that names an entity which no longer exists.",
        "Automations, scripts, scenes and dashboards are searched. The answer is cached; " "refresh=true scans again.",
        query={"refresh": text("Set to true to scan again instead of using the cache.")},
        tag="References",
    ),
    Operation(
        "/api/dependencies/<entity_id>",
        "get",
        "dependencies",
        "Everything that refers to one entity.",
        "Ask before renaming or deleting, to know what will be rewritten.",
        tag="References",
    ),
    Operation(
        "/api/suggestions/<path:missing_entity_id>",
        "get",
        "suggestions",
        "Entities that could replace a missing one.",
        tag="References",
    ),
    Operation(
        "/api/fix_reference",
        "post",
        "fix_reference",
        "Point every reference to a missing entity at an existing one.",
        body={
            "old_entity_id": text("The missing entity being referred to."),
            "new_entity_id": text("The entity that should be referred to instead."),
        },
        required=("old_entity_id", "new_entity_id"),
        tag="References",
    ),
    # ------------------------------------------------------------------ swaps
    Operation(
        "/api/swap/devices",
        "get",
        "swap_devices",
        "Every device, for choosing the two sides of a replacement.",
        tag="Device swap",
    ),
    Operation(
        "/api/swap/jobs",
        "get",
        "swap_jobs",
        "Replacements that are still unfinished.",
        "Each one can be looked at, carried on with, or dropped.",
        tag="Device swap",
    ),
    Operation(
        "/api/swap/<job_id>",
        "get",
        "swap_job",
        "Where one replacement stands.",
        "Its state, the proposed mapping, and what has already been carried out.",
        tag="Device swap",
    ),
    Operation(
        "/api/swap/propose",
        "post",
        "propose_swap",
        "Work out how the entities of an old and a new device line up.",
        "Nothing is changed yet: the answer is a proposal to look over and then confirm.",
        body={
            "old_device_id": text("The device being replaced."),
            "new_device_id": text("The device replacing it."),
        },
        required=("old_device_id", "new_device_id"),
        tag="Device swap",
    ),
    Operation(
        "/api/swap/<job_id>/confirm",
        "post",
        "confirm_swap",
        "Freeze the mapping and say what happens to the old device.",
        body={
            "entity_mapping": thing("Which new entity takes over from which old one."),
            "old_device_disposition": text("What becomes of the old device: keep, disable or delete."),
        },
        tag="Device swap",
    ),
    Operation(
        "/api/swap/<job_id>/execute",
        "post",
        "execute_swap",
        "Carry out a confirmed replacement, as a background job.",
        tag="Device swap",
    ),
    Operation(
        "/api/swap/<job_id>/abort",
        "post",
        "abort_swap",
        "Drop a replacement that has not been carried out.",
        tag="Device swap",
    ),
    # ------------------------------------------------------------------- jobs
    Operation(
        "/api/jobs",
        "get",
        "jobs",
        "Background jobs that are still running.",
        "Renaming a device, a batch or a swap runs in the background and shows up here.",
        tag="Jobs",
    ),
    Operation(
        "/api/jobs/<job_id>",
        "get",
        "job",
        "Where one background job stands.",
        "Poll this after anything that returns a job: renaming a device, a batch, a swap.",
        tag="Jobs",
    ),
]


def _parameters(operation: Operation) -> List[Dict[str, Any]]:
    parameters = []
    for name in operation.path_parameters():
        parameters.append(
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
                "description": f"The {name.replace('_', ' ')}.",
            }
        )
    for name, schema in operation.query.items():
        described = dict(schema)
        description = described.pop("description", "")
        parameters.append(
            {
                "name": name,
                "in": "query",
                "required": False,
                "schema": described,
                "description": description,
            }
        )
    return parameters


def _request_body(operation: Operation) -> Optional[Dict[str, Any]]:
    if not operation.body:
        return None
    schema: Dict[str, Any] = {"type": "object", "properties": dict(operation.body)}
    if operation.required:
        schema["required"] = list(operation.required)
    return {"required": bool(operation.required), "content": {"application/json": {"schema": schema}}}


def document(base_url: str = "/") -> Dict[str, Any]:
    """The OpenAPI description of everything above."""
    paths: Dict[str, Dict[str, Any]] = {}
    for operation in OPERATIONS:
        entry = {
            "operationId": operation.name,
            "summary": operation.summary,
            "description": operation.description,
            "tags": [operation.tag] if operation.tag else [],
            "responses": {
                "200": {
                    "description": "The answer.",
                    "content": {"application/json": {"schema": {}}},
                },
                "401": {"description": "No valid token was presented."},
                "403": {"description": "The caller, the route or the method is not allowed."},
            },
        }
        parameters = _parameters(operation)
        if parameters:
            entry["parameters"] = parameters
        body = _request_body(operation)
        if body is not None:
            entry["requestBody"] = body
        paths.setdefault(operation.openapi_path(), {})[operation.method] = entry

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Home Assistant Entity Manager",
            "version": "1.0.0",
            "description": (
                "Read and change how the entities in a Home Assistant installation are named.\n\n"
                "Every call needs the API token from the add-on's settings page, sent as "
                "`Authorization: Bearer <token>` or as `X-API-Key: <token>`. Which callers are "
                "answered at all is the `external_access` option, and how far the token reaches "
                "is `external_api`: `read` opens the GET routes, `write` opens the rest. "
                "The web interface itself is never served over this port."
            ),
        },
        "servers": [{"url": base_url}],
        "components": {
            "securitySchemes": {"token": {"type": "http", "scheme": "bearer", "description": "The add-on's API token."}}
        },
        "security": [{"token": []}],
        "tags": [
            {"name": "Overview", "description": "What this home contains."},
            {"name": "Naming", "description": "The rules and exceptions that decide names."},
            {"name": "Renaming", "description": "Writing names into Home Assistant."},
            {"name": "Devices", "description": "Devices, areas and disabled things."},
            {"name": "References", "description": "What refers to an entity, and what is broken."},
            {"name": "Device swap", "description": "Replacing one device with another."},
            {"name": "Jobs", "description": "Long-running work."},
        ],
        "paths": paths,
    }


def read_routes() -> frozenset:
    """The Flask rules a reading token may call."""
    return frozenset(operation.path for operation in OPERATIONS if operation.reads)


def write_routes() -> frozenset:
    """The Flask rules a writing token may call, on top of the reading ones."""
    return frozenset(operation.path for operation in OPERATIONS if not operation.reads)
