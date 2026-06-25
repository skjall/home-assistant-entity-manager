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
from naming_overrides import NamingOverrides

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
        language: str = "en",
    ):
        """
        Initialize the entity restructurer.

        Args:
            client: Home Assistant REST API client
            naming_overrides: Optional override storage for custom names
            type_mappings: Optional TypeMappings instance for translations
            language: Language code for translations (default: "en")
        """
        self.client = client
        self.devices = {}
        self.areas = {}
        self.entities = {}
        self.naming_overrides = naming_overrides or NamingOverrides()
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
        """Normalize names for entity IDs (HA standard)"""
        if not name:
            return ""

        # Replace umlauts according to HA standard
        replacements = {
            "ä": "a",
            "ö": "o",
            "ü": "u",
            "ß": "ss",
            "Ä": "a",
            "Ö": "o",
            "Ü": "u",
        }

        normalized = name.lower()
        for old, new in replacements.items():
            normalized = normalized.replace(old, new)

        # Only alphanumeric and underscores
        import re

        normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
        normalized = re.sub(r"_+", "_", normalized)
        normalized = normalized.strip("_")

        return normalized

    async def load_structure(self, ws_client=None):
        """
        Load the complete structure from Home Assistant via WebSocket.

        Populates:
        - self.areas: Dict of area_id -> area data
        - self.devices: Dict of device_id -> device data
        - self.entities: Dict of entity_id -> entity data
        - self.hierarchy_manager: If available, also populated for cascade updates
        """
        # If no WebSocket client was provided, use REST API fallback
        if not ws_client:
            logger.warning("No WebSocket client available, using limited mode")
            self.areas = {}
            self.devices = {}
            self.entities = {}
            return

        try:
            # Load areas via WebSocket
            logger.info("Loading areas via WebSocket...")
            msg_id = await ws_client._send_message({"type": "config/area_registry/list"})
            response = await ws_client._receive_message()
            while response.get("id") != msg_id:
                response = await ws_client._receive_message()

            if response.get("success"):
                areas_data = response.get("result", [])
                self.areas = {area["area_id"]: area for area in areas_data}
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

    def generate_new_entity_id(self, entity_id: str, state_info: Dict) -> Tuple[str, str]:
        """
        Generiere neue Entity ID basierend auf:
        1. Area of the device or entity
        2. Device Name
        3. Entity Type
        """
        domain = entity_id.split(".")[0]

        # Hole Entity Registry Info
        entity_reg = self.entities.get(entity_id, {})
        device_id = entity_reg.get("device_id")

        # Determine area
        room = None
        room_display = None  # For friendly name with area override
        device = None

        if device_id and device_id in self.devices:
            device_info = self.devices[device_id]
            device = device_info

            # Area from device
            if device_info.get("area_id"):
                area_id = device_info["area_id"]
                area = self.areas.get(area_id)
                if area:
                    room = self.normalize_name(area.get("name", ""))
                    room_display = area.get("name", "")

        # If no area from device, try directly from entity
        if not room and entity_reg.get("area_id"):
            area_id = entity_reg["area_id"]
            area = self.areas.get(area_id)
            if area:
                room = self.normalize_name(area.get("name", ""))
                room_display = area.get("name", "")

        # If still no area, leave it empty
        # (We don't try to guess from entity ID as that's unreliable and language-specific)

        # Determine device name (from HA API directly)
        device_name = ""
        if device:
            device_name = device.get("name_by_user") or device.get("name") or device.get("model", "")
            device_name = self.normalize_name(device_name)
        else:
            # No device found - use entity name parts as fallback
            entity_parts = entity_id.split(".")[-1].split("_")
            # Use all parts as we don't want to make language-specific assumptions
            if entity_parts:
                device_name = "_".join(entity_parts)

        # Determine entity type
        device_class = state_info.get("attributes", {}).get("device_class")
        entity_type = self.get_entity_type(entity_id, device_class)

        # Check for entity override
        registry_id = entity_reg.get("id", "")  # The immutable UUID
        entity_override = self.naming_overrides.get_entity_override(registry_id) if registry_id else None

        # If override exists, use it as basis for entity type
        if entity_override and entity_override.get("name"):
            # Override is the "nice" name (e.g. "Ceiling Light")
            # Normalize for entity ID
            entity_type = self.normalize_name(entity_override["name"])

        # Baue neue Entity ID
        parts = []

        # Check if device_name already starts with room
        if room and device_name:
            # Normalize both for comparison
            room_normalized = room.lower()
            device_name_normalized = device_name.lower()

            # If device_name does NOT start with area, add area
            if not device_name_normalized.startswith(room_normalized):
                parts.append(room)
            parts.append(device_name)
        elif room:
            parts.append(room)
        elif device_name:
            parts.append(device_name)

        parts.append(entity_type)

        new_entity_id = f"{domain}.{'_'.join(parts)}"

        # Friendly Name
        friendly_parts = []

        # Get device name for friendly name (from HA API directly)
        device_friendly_name = None
        if device:
            device_friendly_name = device.get("name_by_user") or device.get("name")

        # Check if device name already starts with room
        if room and device_friendly_name:
            # Use room_display if available
            if not room_display:
                room_display = room.title()

            # If device name doesn't start with area, add area
            if not device_friendly_name.lower().startswith(room_display.lower()):
                friendly_parts.append(room_display)
            friendly_parts.append(device_friendly_name)
        elif room:
            # Use room_display if available
            if not room_display:
                room_display = room
            friendly_parts.append(room_display.title())
        elif device_friendly_name:
            friendly_parts.append(device_friendly_name)

        # Entity type for friendly name
        if entity_override and entity_override.get("name"):
            # Use the override directly (already formatted nicely)
            friendly_entity_type = entity_override["name"]
        else:
            # Convert generated type to nice name
            friendly_entity_type = entity_type.replace("_", " ").title()

        friendly_parts.append(friendly_entity_type)

        friendly_name = " ".join(friendly_parts)

        return new_entity_id, friendly_name

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
