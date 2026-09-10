#!/usr/bin/env python3
"""
Home Assistant Entity Restructurer

Creates completely new entity IDs based on the actual structure:
- Area
- Device
- Entity (what it is)

Integrates with:
- HierarchyManager: For cascade updates when renaming areas/devices
- TypeMappings: For multilingual entity type translations
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from ha_client import HomeAssistantClient
from hierarchy_manager import normalize_name
from naming_overrides import NamingOverrides
from naming_templates import NamingTemplates

# Import new modules - optional for backward compatibility
try:
    from hierarchy_manager import HierarchyManager
except ImportError:
    HierarchyManager = None

try:
    from type_mappings import TypeMappings
except ImportError:
    TypeMappings = None

logger = logging.getLogger(__name__)


class EntityRestructurer:
    """
    Restructures Home Assistant entity names based on hierarchy.

    Generates new entity IDs following the pattern:
    {domain}.{area}_{device}_{entity_type}

    And friendly names like:
    "{Area} {Device} {EntityType}"
    """

    def __init__(
        self,
        client: HomeAssistantClient,
        naming_overrides: Optional[NamingOverrides] = None,
        type_mappings: Optional[Any] = None,
        naming_templates: Optional[NamingTemplates] = None,
        language: str = "en",
    ):
        """
        Initialize the entity restructurer.

        Args:
            client: Home Assistant REST API client
            naming_overrides: Optional override storage for custom names
            type_mappings: Optional TypeMappings instance for translations
            naming_templates: Optional naming-template configuration
            language: Language code for translations (default: "en")
        """
        self.client = client
        self.devices = {}
        self.areas = {}
        self.floors = {}
        self.entities = {}
        self.naming_overrides = naming_overrides or NamingOverrides()
        self.naming_templates = naming_templates or NamingTemplates()
        self.language = language

        # Initialize type mappings for translations
        if type_mappings:
            self.type_mappings = type_mappings
        elif TypeMappings:
            self.type_mappings = TypeMappings()
        else:
            self.type_mappings = None

        # Initialize hierarchy manager for cascade updates
        if HierarchyManager:
            self.hierarchy_manager = HierarchyManager(self.naming_overrides)
        else:
            self.hierarchy_manager = None

        # Built-in entity type mappings used when TypeMappings is unavailable.
        self.entity_types = {
            "light": "light",
            "switch": "switch",
            "sensor": {
                "temperature": "temperature",
                "humidity": "humidity",
                "power": "power",
                "energy": "energy",
                "battery": "battery",
                "illuminance": "illuminance",
                "motion": "motion",
                "co2": "co2",
                "pressure": "pressure",
                "voltage": "voltage",
                "current": "current",
            },
            "binary_sensor": {
                "motion": "motion",
                "door": "door",
                "window": "window",
                "smoke": "smoke",
                "moisture": "moisture",
                "connectivity": "connectivity",
            },
            "climate": "climate",
            "cover": "cover",
            "media_player": "media_player",
        }

    @staticmethod
    async def _list_registry(ws_client: Any, registry: str) -> List[Dict[str, Any]]:
        """Return all entries from a Home Assistant registry."""
        message_id = await ws_client._send_message({"type": f"config/{registry}_registry/list"})
        response = await ws_client._receive_message()
        while response.get("id") != message_id:
            response = await ws_client._receive_message()
        if not response.get("success"):
            raise RuntimeError(f"Failed to load {registry} registry: {response}")
        return response.get("result", [])

    @staticmethod
    def _index_registry(
        entries: List[Dict[str, Any]],
        primary_key: str,
        fallback_key: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Index registry entries and normalize a renamed identifier field."""
        indexed = {}
        for entry in entries:
            entry_id = entry.get(primary_key) or (entry.get(fallback_key) if fallback_key else None)
            if not entry_id:
                logger.warning("Ignoring registry entry without %s: %s", primary_key, entry)
                continue
            normalized = dict(entry)
            normalized.setdefault(primary_key, entry_id)
            indexed[entry_id] = normalized
        return indexed

    async def load_structure(self, ws_client: Optional[Any] = None) -> None:
        """
        Load the complete structure from Home Assistant via WebSocket.

        Populates:
        - self.floors: Dict of floor_id -> floor data
        - self.areas: Dict of area_id -> area data
        - self.devices: Dict of device_id -> device data
        - self.entities: Dict of entity_id -> entity data
        - self.hierarchy_manager: If available, also populated for cascade updates
        """
        # If no WebSocket client was provided, use REST API fallback
        if not ws_client:
            logger.warning("No WebSocket client available, using limited mode")
            self.areas = {}
            self.floors = {}
            self.devices = {}
            self.entities = {}
            return

        registry_specs = (
            ("floors", "floor", "floor_id", "id", logging.WARNING),
            ("areas", "area", "area_id", "id", logging.ERROR),
            ("devices", "device", "id", None, logging.ERROR),
            ("entities", "entity", "entity_id", None, logging.ERROR),
        )
        for attribute, registry, key, fallback_key, error_level in registry_specs:
            try:
                entries = await self._list_registry(ws_client, registry)
                indexed = self._index_registry(entries, key, fallback_key)
                setattr(self, attribute, indexed)
                logger.info("Loaded %d %s registry entries", len(indexed), registry)
            except Exception as error:
                # Floors do not exist on older Home Assistant versions.
                logger.log(error_level, "Failed to load %s registry: %s", registry, error)
                setattr(self, attribute, {})

        maintained_count = sum(1 for entity in self.entities.values() if "maintained" in entity.get("labels", []))
        if maintained_count:
            logger.info("Found %d entities with maintained label", maintained_count)

        # Populate hierarchy manager for cascade updates
        if self.hierarchy_manager:
            self._populate_hierarchy_manager()

    def _populate_hierarchy_manager(self) -> None:
        """Populate the hierarchy manager with loaded data."""
        if not self.hierarchy_manager:
            return

        try:
            self.hierarchy_manager.load_from_ha(
                areas=self.areas,
                devices=self.devices,
                entities=self.entities,
            )
            logger.info("Hierarchy manager populated")
        except Exception as e:
            logger.error(f"Error populating hierarchy manager: {e}")

    def get_entity_type(
        self,
        entity_id: str,
        device_class: Optional[str] = None,
        language: Optional[str] = None,
    ) -> str:
        """
        Determine entity type based on domain and device class.

        Uses TypeMappings for translations if available, otherwise falls back
        to the built-in entity type mappings.

        Args:
            entity_id: The entity ID
            device_class: Optional device class for sensors
            language: Optional language code for translation

        Returns:
            Translated entity type name
        """
        domain = entity_id.split(".")[0]
        lang = language or self.language

        # If type_mappings is available, use it for translation
        if self.type_mappings:
            # Detect integration for more specific translations
            integration = self.type_mappings.detect_integration(entity_id)

            # Use device_class if available, otherwise domain
            type_key = device_class if device_class else domain

            return self.type_mappings.get_translation(
                type_key=type_key,
                language=lang,
                integration=integration,
                domain=domain,
            )

        # Fall back to built-in mappings.
        if domain in ["light", "switch", "climate", "cover", "media_player"]:
            return self.entity_types.get(domain, domain)

        if domain in ["sensor", "binary_sensor"] and device_class:
            type_map = self.entity_types.get(domain, {})
            if isinstance(type_map, dict):
                return type_map.get(device_class, device_class)

        # Fallback: Try to guess from entity name
        entity_name = entity_id.split(".")[-1].lower()
        for key, value in self.entity_types.get(domain, {}).items():
            if key in entity_name:
                return value

        return "sensor"  # Default

    def build_naming_context(self, entity_id: str, state_info: Dict[str, Any]) -> Dict[str, str]:
        """Build the complete template context for an entity."""
        domain, _, object_id = entity_id.partition(".")
        entity_reg = self.entities.get(entity_id, {})
        device_id = entity_reg.get("device_id") or ""
        device = self.devices.get(device_id, {}) if device_id else {}

        area_id = entity_reg.get("area_id") or device.get("area_id") or ""
        area = self.areas.get(area_id, {}) if area_id else {}
        floor_id = area.get("floor_id") or ""
        floor = self.floors.get(floor_id, {}) if floor_id else {}

        device_class = (
            state_info.get("attributes", {}).get("device_class")
            or entity_reg.get("device_class")
            or entity_reg.get("original_device_class")
            or ""
        )
        registry_id = entity_reg.get("id", "")
        entity_override = self.naming_overrides.get_entity_override(registry_id) if registry_id else None

        integration = entity_reg.get("platform") or ""
        if not integration and self.type_mappings:
            integration = self.type_mappings.detect_integration(entity_id) or ""

        raw_device_name = device.get("name_by_user") or device.get("name") or device.get("model") or ""
        partial_context = {
            "floor": floor.get("name", ""),
            "floor_id": floor_id,
            "area": area.get("name", ""),
            "area_id": area_id,
            "device": "",
            "device_id": device_id,
            "entity": "",
            "entity_id": object_id,
            "domain": domain,
            "device_class": device_class,
            "manufacturer": device.get("manufacturer", ""),
            "model": device.get("model", ""),
            "integration": integration,
        }
        device_name = self._base_device_name(raw_device_name, partial_context)
        partial_context["device"] = device_name
        partial_context["entity"] = self._base_entity_name(
            entity_id,
            entity_reg,
            state_info,
            entity_override,
            device_class,
            (raw_device_name, partial_context["area"], device_name),
            partial_context,
        )
        return {key: str(value or "") for key, value in partial_context.items()}

    def _base_device_name(self, name: str, context: Dict[str, str]) -> str:
        """Remove hierarchy previously added by a known device template."""
        name = name or ""
        extracted = self.naming_templates.extract_field("device_name", name, "device", context)
        if extracted:
            return extracted
        for prefix in (context["floor"], context["area"]):
            if prefix and name.lower().startswith(prefix.lower() + " "):
                name = name[len(prefix) :].strip()
        return name

    def _strip_applied_entity_name(
        self,
        name: str,
        prefixes: Tuple[str, ...],
        context: Optional[Dict[str, str]],
    ) -> str:
        """Remove hierarchy previously added by a known entity-name template."""
        if context is not None:
            extracted = self.naming_templates.extract_field("entity_name", name, "entity", context)
            if extracted:
                return extracted
        for prefix in filter(None, prefixes):
            if name.lower() == prefix.lower():
                return ""
            if name.lower().startswith(prefix.lower() + " "):
                name = name[len(prefix) :].strip()
        return name

    def _translate_entity_name(self, name: str, entity_id: str) -> str:
        """
        Apply a configured type mapping to a name Home Assistant supplied.

        Integrations name their entities in English ("Linkquality", "Power-on
        behavior"). The type mappings — including everything the user taught via
        ``/api/learn_mapping`` — are keyed by exactly that name, so the lookup
        happens here. A name no mapping covers is kept as it is.
        """
        if not self.type_mappings:
            return name
        integration = self.type_mappings.detect_integration(entity_id)
        # No domain fallback: it would replace a specific name with "Sensor".
        return self.type_mappings.find_translation(name, self.language, integration) or name

    def _base_entity_name(
        self,
        entity_id: str,
        registry: Dict[str, Any],
        state: Dict[str, Any],
        override: Optional[Dict[str, Any]],
        device_class: str,
        prefixes: Tuple[str, ...],
        context: Optional[Dict[str, str]] = None,
    ) -> str:
        """Return the entity-specific name supplied by Home Assistant."""
        override_name = override.get("name") if override else None
        if override_name:
            return override_name

        native = (registry.get("original_name"), state.get("original_name"))
        name = next((candidate for candidate in native if candidate), None)
        if name is not None:
            return self._translate_entity_name(name, entity_id)

        # ``name`` fields may hold a name this add-on wrote on a previous run.
        # Unwind the entity template before reusing them, otherwise each run
        # prepends the hierarchy again and the name grows without bound.
        applied = next(
            (candidate for candidate in (registry.get("name"), state.get("name")) if candidate),
            None,
        )
        if applied is not None:
            base = self._strip_applied_entity_name(applied, prefixes, context)
            if base:
                return self._translate_entity_name(base, entity_id)

        name = state.get("attributes", {}).get("friendly_name")
        if name is not None:
            for prefix in filter(None, prefixes):
                if name.lower() == prefix.lower():
                    name = ""
                elif name.lower().startswith(prefix.lower() + " "):
                    name = name[len(prefix) :].strip()
            if name:
                return name

            # Integrations without native entity names may expose only the
            # device name. Preserve their existing object-ID suffix.
            object_id = entity_id.partition(".")[2]
            for prefix in filter(None, prefixes):
                normalized_prefix = normalize_name(prefix)
                if object_id == normalized_prefix:
                    object_id = ""
                elif normalized_prefix and object_id.startswith(normalized_prefix + "_"):
                    object_id = object_id[len(normalized_prefix) + 1 :]
            if object_id and not registry.get("has_entity_name"):
                return object_id.replace("_", " ").title()
            return ""

        entity_type = self.get_entity_type(entity_id, device_class)
        return entity_type.replace("_", " ").title()

    def generate_device_name(self, device_id: str) -> str:
        """Generate a configured device name using its first entity for context."""
        entity_id = next(
            (entity_id for entity_id, entity in self.entities.items() if entity.get("device_id") == device_id),
            "",
        )
        if entity_id:
            context = self.build_naming_context(entity_id, self.entities.get(entity_id, {}))
        else:
            device = self.devices.get(device_id, {})
            area_id = device.get("area_id") or ""
            area = self.areas.get(area_id, {}) if area_id else {}
            floor_id = area.get("floor_id") or ""
            floor = self.floors.get(floor_id, {}) if floor_id else {}
            raw_name = device.get("name_by_user") or device.get("name") or device.get("model") or ""
            context = {
                "floor": floor.get("name", ""),
                "floor_id": floor_id,
                "area": area.get("name", ""),
                "area_id": area_id,
                "device": raw_name,
                "device_id": device_id,
                "entity": "",
                "entity_id": "",
                "domain": "",
                "device_class": "",
                "manufacturer": device.get("manufacturer", ""),
                "model": device.get("model", ""),
                "integration": "",
            }
        return self.naming_templates.render("device_name", context)

    def generate_new_entity_id(
        self,
        entity_id: str,
        state_info: Dict[str, Any],
        entity_name: Optional[str] = None,
    ) -> Tuple[str, str]:
        """Generate an entity ID and entity-registry name from active templates."""
        domain = entity_id.split(".", 1)[0]
        context = self.build_naming_context(entity_id, state_info)
        if entity_name is not None:
            context["entity"] = entity_name
        object_id = self.naming_templates.render("entity_id", context, normalize=True)
        entity_name = self.naming_templates.render("entity_name", context)
        if not object_id:
            object_id = entity_id.split(".", 1)[-1]
        return f"{domain}.{object_id}", entity_name

    def calculate_new_entity_name(self, entity_id: str, force_recalculate: bool = False) -> Tuple[str, str]:
        """
        Berechne neuen Entity Namen basierend auf aktuellen Device/Area Daten

        Returns:
            Tuple[new_entity_id, new_friendly_name]
        """
        # Hole Entity aus Registry
        entity = self.entities.get(entity_id, {})
        if not entity:
            return entity_id, entity_id  # Fallback wenn Entity nicht gefunden

        # Calculate new name with current data
        new_id, friendly_name = self.generate_new_entity_id(entity_id, entity)

        return new_id, friendly_name

    async def analyze_entities(
        self,
        states: List[Dict],
        skip_reviewed: bool = False,
        show_reviewed: bool = False,
    ) -> Dict[str, Tuple[str, str]]:
        """Analyze all entities and create mapping"""
        # Structure should already be loaded - don't load again!

        mapping = {}
        skipped_count = 0

        for state in states:
            entity_id = state["entity_id"]

            # Check if entity has already been processed
            entity_reg = self.entities.get(entity_id, {})
            has_maintained_label = "maintained" in entity_reg.get("labels", [])

            # Filter basierend auf Optionen
            if skip_reviewed and has_maintained_label:
                skipped_count += 1
                continue
            elif show_reviewed and not has_maintained_label:
                continue

            new_entity_id, friendly_name = self.generate_new_entity_id(entity_id, state)

            # ALWAYS include in mapping, even if nothing changes
            # The maintained label decides whether it's skipped
            mapping[entity_id] = (new_entity_id, friendly_name)
            logger.info(f"Would process: {entity_id} -> {new_entity_id}")

        if skipped_count > 0:
            logger.info(f"Skipped {skipped_count} entities with maintained label")

        return mapping

    # === Cascade Update Methods ===

    def update_area_name(self, area_id: str, new_name: str) -> Dict[str, Tuple[str, str]]:
        """
        Update area name and get all affected entity names.

        Uses HierarchyManager for efficient cascade if available.

        Args:
            area_id: The area to update
            new_name: The new display name

        Returns:
            Dict of affected entity_id -> (new_entity_id, friendly_name)
        """
        if self.hierarchy_manager:
            # Use hierarchy manager for efficient cascade
            affected = self.hierarchy_manager.update_area_name(area_id, new_name)
            # Convert registry_id keys to entity_id keys
            return {
                self.hierarchy_manager.entities[rid].id: names
                for rid, names in affected.items()
                if rid in self.hierarchy_manager.entities
            }

        # No hierarchy manager - areas are renamed via HA API directly
        return {}

    def update_device_name(self, device_id: str, new_name: str) -> Dict[str, Tuple[str, str]]:
        """
        Update device name and get all affected entity names.

        Uses HierarchyManager for efficient cascade if available.

        Args:
            device_id: The device to update
            new_name: The new base name

        Returns:
            Dict of affected entity_id -> (new_entity_id, friendly_name)
        """
        if self.hierarchy_manager:
            # Use hierarchy manager for efficient cascade
            affected = self.hierarchy_manager.update_device_name(device_id, new_name)
            # Convert registry_id keys to entity_id keys
            return {
                self.hierarchy_manager.entities[rid].id: names
                for rid, names in affected.items()
                if rid in self.hierarchy_manager.entities
            }

        # No hierarchy manager - devices are renamed via HA API directly
        return {}

    def update_entity_name(self, registry_id: str, new_name: str, learn_mapping: bool = False) -> Tuple[str, str]:
        """
        Update entity base name.

        Args:
            registry_id: The entity registry ID
            new_name: The new base name
            learn_mapping: If True, also learn this as a type mapping

        Returns:
            Tuple of (new_entity_id, friendly_name)
        """
        # If learning is enabled and we have type_mappings
        if learn_mapping and self.type_mappings:
            # Find the entity to get its device_class
            entity = None
            for eid, edata in self.entities.items():
                if edata.get("id") == registry_id:
                    entity = edata
                    break

            if entity:
                device_class = entity.get("device_class") or entity.get("original_device_class")
                if device_class:
                    self.type_mappings.set_user_mapping(device_class, new_name)
                    logger.info(f"Learned type mapping: {device_class} -> {new_name}")

        if self.hierarchy_manager:
            return self.hierarchy_manager.update_entity_name(registry_id, new_name)

        # Fallback: Just save the override
        self.naming_overrides.set_entity_override(registry_id, new_name)
        return ("", "")

    # === Type Mapping Methods ===

    def set_language(self, language: str) -> None:
        """Set the language for type translations."""
        self.language = language
        logger.info(f"Language set to: {language}")

    def get_type_suggestion(self, entity_id: str, device_class: Optional[str] = None) -> str:
        """
        Get a translated type suggestion for an entity.

        Checks user mappings first, then system defaults.

        Args:
            entity_id: The entity ID
            device_class: Optional device class

        Returns:
            Translated type suggestion
        """
        return self.get_entity_type(entity_id, device_class, self.language)

    def learn_type_mapping(self, type_key: str, translation: str) -> None:
        """
        Learn a user's preferred translation for a type key.

        Args:
            type_key: The type key (e.g., "battery")
            translation: The user's preferred translation (e.g., "Batterieladung")
        """
        if self.type_mappings:
            self.type_mappings.set_user_mapping(type_key, translation)

    def get_all_type_mappings(self) -> List[Dict[str, Any]]:
        """
        Get all known type mappings with user overrides.

        Returns:
            List of type info dicts with key, system_default, user_mapping
        """
        if self.type_mappings:
            return self.type_mappings.get_all_known_types(self.language)
        return []

    def get_hierarchy_info(self, entity_id: str) -> Dict[str, Any]:
        """
        Get hierarchy information for an entity.

        Args:
            entity_id: The entity ID

        Returns:
            Dict with area, device, entity info
        """
        if self.hierarchy_manager:
            # Find registry_id from entity_id
            entity = self.hierarchy_manager.get_entity_by_id(entity_id)
            if entity:
                return self.hierarchy_manager.get_hierarchy_for_entity(entity.registry_id)
        return {}
