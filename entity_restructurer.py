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

        # Legacy entity type mappings - used as fallback
        # These are now primarily handled by TypeMappings
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

    def normalize_name(self, name: str) -> str:
        """Normalize names for entity IDs (HA standard).

        Delegates to :func:`hierarchy_manager.normalize_name` so the whole
        code base shares a single normalization implementation.
        """
        return normalize_name(name)

    async def load_structure(self, ws_client=None):
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

        try:
            logger.info("Loading floors via WebSocket...")
            msg_id = await ws_client._send_message({"type": "config/floor_registry/list"})
            response = await ws_client._receive_message()
            while response.get("id") != msg_id:
                response = await ws_client._receive_message()

            if response.get("success"):
                floors_data = response.get("result", [])
                self.floors = {}
                for floor in floors_data:
                    floor_id = floor.get("floor_id") or floor.get("id")
                    if not floor_id:
                        logger.warning("Ignoring floor registry entry without an id: %s", floor)
                        continue
                    normalized_floor = dict(floor)
                    normalized_floor.setdefault("floor_id", floor_id)
                    self.floors[floor_id] = normalized_floor
                logger.info(f"Loaded {len(self.floors)} floors via WebSocket")
            else:
                logger.warning(f"Failed to load floors: {response}")
                self.floors = {}
        except Exception as e:
            # Floors are optional on older Home Assistant versions.
            logger.warning(f"Error loading floors via WebSocket: {e}")
            self.floors = {}

        try:
            # Load areas via WebSocket
            logger.info("Loading areas via WebSocket...")
            msg_id = await ws_client._send_message({"type": "config/area_registry/list"})
            response = await ws_client._receive_message()
            while response.get("id") != msg_id:
                response = await ws_client._receive_message()

            if response.get("success"):
                areas_data = response.get("result", [])
                self.areas = {}
                for area in areas_data:
                    area_id = area.get("area_id") or area.get("id")
                    if not area_id:
                        logger.warning("Ignoring area registry entry without an id: %s", area)
                        continue
                    normalized_area = dict(area)
                    normalized_area.setdefault("area_id", area_id)
                    self.areas[area_id] = normalized_area
                logger.info(f"Loaded {len(self.areas)} areas via WebSocket")
            else:
                logger.error(f"Failed to load areas: {response}")

        except Exception as e:
            logger.error(f"Error loading areas via WebSocket: {e}")

        try:
            # Load devices via WebSocket
            logger.info("Loading devices via WebSocket...")
            msg_id = await ws_client._send_message({"type": "config/device_registry/list"})
            response = await ws_client._receive_message()
            while response.get("id") != msg_id:
                response = await ws_client._receive_message()

            if response.get("success"):
                devices_data = response.get("result", [])
                self.devices = {device["id"]: device for device in devices_data}
                logger.info(f"Loaded {len(self.devices)} devices via WebSocket")
            else:
                logger.error(f"Failed to load devices: {response}")

        except Exception as e:
            logger.error(f"Error loading devices via WebSocket: {e}")

        # Load entity registry directly
        try:
            logger.info("Loading entity registry...")

            msg_id = await ws_client._send_message({"type": "config/entity_registry/list"})

            response = await ws_client._receive_message()
            while response.get("id") != msg_id:
                response = await ws_client._receive_message()

            if response.get("success"):
                entities = response.get("result", [])
                self.entities = {e["entity_id"]: e for e in entities}
                logger.info(f"Loaded {len(self.entities)} entities from registry")

                # Count maintained labels
                maintained_count = sum(1 for e in self.entities.values() if "maintained" in e.get("labels", []))
                if maintained_count > 0:
                    logger.info(f"Found {maintained_count} entities with maintained label")
            else:
                logger.error(f"Failed to load entity registry: {response}")
                self.entities = {}

        except Exception as e:
            logger.error(f"Failed to load entity registry: {e}")
            self.entities = {}

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
        to legacy entity_types dict.

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

        # Fallback to legacy behavior
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

    def build_naming_context(self, entity_id: str, state_info: Dict) -> Dict[str, str]:
        """Build the complete template context for an entity."""
        domain, _, object_id = entity_id.partition(".")
        entity_reg = self.entities.get(entity_id, {})
        device_id = entity_reg.get("device_id") or ""
        device = self.devices.get(device_id, {}) if device_id else {}

        # An entity-level area is an explicit override of its device's area in
        # Home Assistant, so it must take precedence.
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

        raw_device_name = device.get("name_by_user") or device.get("name") or device.get("model", "")
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
        device_name = self.naming_templates.extract_field("device_name", raw_device_name, "device", partial_context)
        if not device_name:
            device_name = raw_device_name
            for prefix in (partial_context["floor"], partial_context["area"]):
                if prefix and device_name.lower().startswith(prefix.lower() + " "):
                    device_name = device_name[len(prefix) :].strip()
        partial_context["device"] = device_name

        # `{entity}` is the entity's own HA-provided name, not a translated
        # domain or device-class label.  `original_name` is what integrations
        # supply for has_entity_name entities (for example "Button BL",
        # "Battery", or "Restart").  Replacing it with "Event", "Sensor", or
        # "Button" loses information and can make several entities collide on
        # the same target entity_id.
        entity_name = next(
            (
                name
                for name in (
                    entity_override.get("name") if entity_override else None,
                    entity_reg.get("original_name"),
                    entity_reg.get("name"),
                    state_info.get("original_name"),
                    state_info.get("name"),
                )
                if name
            ),
            None,
        )

        # REST state data only exposes the composed friendly name.  Use it as a
        # final HA-name fallback and remove a known device/area prefix so the
        # templates do not add that hierarchy twice.
        if entity_name is None:
            entity_name = state_info.get("attributes", {}).get("friendly_name")
            for prefix in (
                raw_device_name,
                partial_context["area"],
                device_name,
            ):
                if not prefix or entity_name is None:
                    continue
                if entity_name.lower() == prefix.lower():
                    entity_name = ""
                elif entity_name.lower().startswith(prefix.lower() + " "):
                    entity_name = entity_name[len(prefix) :].strip()

        # Older integrations may provide neither registry name. Preserve the
        # previous type-based behavior only as the last resort.
        if entity_name is None:
            entity_type = self.get_entity_type(entity_id, device_class)
            entity_name = entity_type.replace("_", " ").title()

        partial_context["entity"] = entity_name
        return {key: str(value or "") for key, value in partial_context.items()}

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
            raw_name = device.get("name_by_user") or device.get("name") or device.get("model", "")
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

    def generate_new_entity_id(self, entity_id: str, state_info: Dict) -> Tuple[str, str]:
        """Generate an entity ID and entity-registry name from active templates."""
        domain = entity_id.split(".", 1)[0]
        context = self.build_naming_context(entity_id, state_info)
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
