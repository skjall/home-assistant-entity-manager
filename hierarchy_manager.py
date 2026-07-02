#!/usr/bin/env python3
"""
Hierarchy Manager - Manages Area → Device → Entity hierarchy with cascade updates.

This module provides hierarchical name inheritance where:
- Area names are standalone
- Device names inherit: "{Area} {DeviceName}"
- Entity names inherit: "{Area} {DeviceName} {EntityType}"

Changes at higher levels automatically cascade to all descendants.
"""

from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from slugify import slugify

logger = logging.getLogger(__name__)


def normalize_name(name: str) -> str:
    """
    Normalize names for entity IDs (Home Assistant standard).

    Transliterates non-ASCII characters (diacritics, umlauts, etc.) to their
    closest ASCII equivalents, lowercases the result and joins tokens with
    underscores. This mirrors how Home Assistant itself derives entity IDs,
    which relies on ``python-slugify`` (see ``homeassistant.util.slugify``),
    so accented characters like ``á``, ``é`` or ``ł`` are transliterated
    instead of being stripped.

    Examples:
        normalize_name("Zażółć gęślą jaźń") -> "zazolc_gesla_jazn"
        normalize_name("Wohnzimmer Büro") -> "wohnzimmer_buro"

    Args:
        name: The name to normalize

    Returns:
        Normalized string suitable for entity IDs. Returns an empty string if
        the input is empty or contains no usable characters.
    """
    if not name:
        return ""

    # ``slugify`` lowercases, transliterates via unidecode, replaces every run
    # of unsupported characters with the separator and trims/collapses them.
    return slugify(name, separator="_")


def strip_prefix(full_name: str, prefix: str) -> str:
    """
    Strip a prefix from a name (case-insensitive).

    Examples:
        strip_prefix("Büro Homepod", "Büro") -> "Homepod"
        strip_prefix("Wohnzimmer Deckenleuchte Licht", "Wohnzimmer Deckenleuchte") -> "Licht"

    Args:
        full_name: The complete name with potential prefix
        prefix: The prefix to remove

    Returns:
        Name with prefix stripped, or original name if no match
    """
    if not full_name or not prefix:
        return full_name or ""

    full_lower = full_name.lower().strip()
    prefix_lower = prefix.lower().strip()

    # Check if full_name starts with prefix (with space separator)
    if full_lower.startswith(prefix_lower + " "):
        return full_name[len(prefix) + 1 :].strip()

    # Check exact match (prefix == full name)
    if full_lower == prefix_lower:
        return ""

    return full_name


@dataclass
class AreaNode:
    """Represents an area in the hierarchy."""

    id: str
    name: str  # Name from HA API
    updated_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def display_name(self) -> str:
        """Get the display name (from HA API)."""
        return self.name

    @property
    def normalized_name(self) -> str:
        """Get normalized name for entity IDs."""
        return normalize_name(self.name)


@dataclass
class DeviceNode:
    """Represents a device in the hierarchy."""

    id: str
    name: str  # Device name from HA API
    area_id: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def get_display_name(self, area: Optional[AreaNode]) -> str:
        """
        Get the effective display name with area prefix.

        Args:
            area: The parent area node (if any)

        Returns:
            Full display name like "Wohnzimmer Deckenleuchte"
        """
        if area:
            area_name = area.display_name
            # Avoid duplication if name already starts with area name
            if self.name.lower().startswith(area_name.lower()):
                return self.name
            return f"{area_name} {self.name}"

        return self.name

    def get_normalized_name(self, area: Optional[AreaNode]) -> str:
        """Get normalized name for entity IDs."""
        return normalize_name(self.get_display_name(area))

    @property
    def base_name(self) -> str:
        """Get the base name without area prefix."""
        return self.name


@dataclass
class EntityNode:
    """Represents an entity in the hierarchy."""

    id: str  # Current entity_id (e.g., light.old_name)
    registry_id: str  # Immutable UUID from HA registry
    domain: str  # Entity domain (light, sensor, etc.)
    device_id: Optional[str] = None
    area_id: Optional[str] = None  # Direct area assignment (when no device)
    device_class: Optional[str] = None
    original_name: Optional[str] = None  # Original friendly name
    base_name: Optional[str] = None  # Entity type/name (e.g., "Licht", "Temperatur")
    override_name: Optional[str] = None  # User override for base name
    disabled_by: Optional[str] = None
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def get_effective_base_name(self) -> str:
        """Get the effective base name (override or original)."""
        return self.override_name or self.base_name or ""

    def get_display_name(self, device: Optional[DeviceNode], area: Optional[AreaNode]) -> str:
        """
        Get the full display name with inheritance.

        Args:
            device: The parent device node (if any)
            area: The parent area node (if any)

        Returns:
            Full display name like "Wohnzimmer Deckenleuchte Licht"
        """
        base = self.get_effective_base_name()

        if device:
            device_name = device.get_display_name(area)
            if base:
                return f"{device_name} {base}"
            return device_name
        elif area:
            area_name = area.display_name
            if base:
                return f"{area_name} {base}"
            return area_name

        return base or self.original_name or self.id

    def get_entity_id(self, device: Optional[DeviceNode], area: Optional[AreaNode]) -> str:
        """
        Generate the new entity ID based on hierarchy.

        Args:
            device: The parent device node (if any)
            area: The parent area node (if any)

        Returns:
            New entity ID like "light.wohnzimmer_deckenleuchte_licht"
        """
        display_name = self.get_display_name(device, area)
        normalized = normalize_name(display_name)
        return f"{self.domain}.{normalized}"


class HierarchyManager:
    """
    Manages the complete Area → Device → Entity hierarchy.

    Provides methods to:
    - Load structure from Home Assistant registries
    - Compute names for all entities
    - Update names at any level with automatic cascade
    - Track dependencies for efficient updates
    """

    def __init__(self, naming_overrides: Optional[Any] = None):
        """
        Initialize the hierarchy manager.

        Args:
            naming_overrides: Optional NamingOverrides instance for persistence
        """
        self.naming_overrides = naming_overrides

        # Node storage
        self.areas: Dict[str, AreaNode] = {}
        self.devices: Dict[str, DeviceNode] = {}
        self.entities: Dict[str, EntityNode] = {}  # Keyed by registry_id

        # Relationship tracking for cascade updates
        self._area_to_devices: Dict[str, Set[str]] = {}  # area_id -> set of device_ids
        self._device_to_entities: Dict[str, Set[str]] = {}  # device_id -> set of registry_ids
        self._area_to_entities: Dict[str, Set[str]] = {}  # area_id -> set of registry_ids (direct)

        # Entity ID to registry ID mapping
        self._entity_id_to_registry: Dict[str, str] = {}

    def load_from_ha(
        self,
        areas: Dict[str, Any],
        devices: Dict[str, Any],
        entities: Dict[str, Any],
    ) -> None:
        """
        Load hierarchy from Home Assistant registry data.

        Args:
            areas: Dict of area_id -> area data from HA
            devices: Dict of device_id -> device data from HA
            entities: Dict of entity_id -> entity data from HA
        """
        # Clear existing data
        self.areas.clear()
        self.devices.clear()
        self.entities.clear()
        self._area_to_devices.clear()
        self._device_to_entities.clear()
        self._area_to_entities.clear()
        self._entity_id_to_registry.clear()

        # Load areas (names come directly from HA API)
        for area_id, area_data in areas.items():
            self.areas[area_id] = AreaNode(
                id=area_id,
                name=area_data.get("name", ""),
            )
            self._area_to_devices[area_id] = set()
            self._area_to_entities[area_id] = set()

        # Load devices (names come directly from HA API, strip area prefix)
        for device_id, device_data in devices.items():
            area_id = device_data.get("area_id")

            # Get raw device name from HA
            raw_device_name = device_data.get("name_by_user") or device_data.get("name", "")

            # Strip area prefix to get base name
            # e.g., "Büro Homepod" with area "Büro" -> "Homepod"
            base_device_name = raw_device_name
            if area_id and area_id in self.areas:
                area_name = self.areas[area_id].name
                base_device_name = strip_prefix(raw_device_name, area_name)

            self.devices[device_id] = DeviceNode(
                id=device_id,
                name=base_device_name,  # Store stripped base name
                area_id=area_id,
                manufacturer=device_data.get("manufacturer"),
                model=device_data.get("model"),
            )

            if area_id and area_id in self._area_to_devices:
                self._area_to_devices[area_id].add(device_id)

            self._device_to_entities[device_id] = set()

        # Load entities (strip device+area prefix from entity names)
        for entity_id, entity_data in entities.items():
            registry_id = entity_data.get("id", entity_id)
            device_id = entity_data.get("device_id")
            area_id = entity_data.get("area_id")
            domain = entity_id.split(".")[0] if "." in entity_id else ""

            override = None
            if self.naming_overrides:
                override_data = self.naming_overrides.get_entity_override(registry_id)
                if override_data:
                    override = override_data.get("name")

            # Get device and area for prefix stripping
            device = self.devices.get(device_id) if device_id else None
            area = None
            if device and device.area_id:
                area = self.areas.get(device.area_id)
            elif area_id:
                area = self.areas.get(area_id)

            # Determine base_name by stripping hierarchy prefix from friendly name
            # e.g., "Büro Raumluftsensor Kohlendioxid" → "Kohlendioxid"
            base_name = self._extract_base_name(entity_id, entity_data, device, area)

            self.entities[registry_id] = EntityNode(
                id=entity_id,
                registry_id=registry_id,
                domain=domain,
                device_id=device_id,
                area_id=area_id if not device_id else None,
                device_class=entity_data.get("device_class") or entity_data.get("original_device_class"),
                original_name=entity_data.get("name") or entity_data.get("original_name"),
                base_name=base_name,
                override_name=override,
                disabled_by=entity_data.get("disabled_by"),
            )

            self._entity_id_to_registry[entity_id] = registry_id

            # Build relationships
            if device_id and device_id in self._device_to_entities:
                self._device_to_entities[device_id].add(registry_id)
            elif area_id and area_id in self._area_to_entities:
                self._area_to_entities[area_id].add(registry_id)

        logger.info(
            f"Hierarchy loaded: {len(self.areas)} areas, " f"{len(self.devices)} devices, {len(self.entities)} entities"
        )

    def _extract_base_name(
        self,
        entity_id: str,
        entity_data: Dict[str, Any],
        device: Optional["DeviceNode"] = None,
        area: Optional["AreaNode"] = None,
    ) -> str:
        """
        Extract a meaningful base name for the entity by stripping hierarchy prefixes.

        Priority:
        1. Strip device+area prefix from friendly name (e.g., "Büro Sensor Temperatur" → "Temperatur")
        2. device_class (e.g., "temperature" → "Temperature")
        3. Last part of entity_id (e.g., "brightness" from light.room_lamp_brightness)
        4. Domain as fallback

        Args:
            entity_id: The entity ID
            entity_data: Entity registry data
            device: Parent device node (if any)
            area: Parent area node (if any)

        Returns:
            Base name for the entity
        """
        # Get original friendly name from HA
        original_name = entity_data.get("name") or entity_data.get("original_name") or ""

        # Try to strip device+area prefix from original name
        if original_name:
            stripped_name = original_name

            # If entity has a device, strip the device's full display name
            if device:
                device_display = device.get_display_name(area)
                stripped_name = strip_prefix(stripped_name, device_display)

                # Also try stripping just device base name (in case HA name doesn't have area)
                if stripped_name == original_name:
                    stripped_name = strip_prefix(stripped_name, device.base_name)

            # If no device but has area, strip area name
            elif area:
                stripped_name = strip_prefix(stripped_name, area.display_name)

            # If we successfully stripped something and have a meaningful result
            if stripped_name and stripped_name != original_name:
                return stripped_name.strip()

        # Fallback 1: Try device_class
        device_class = entity_data.get("device_class") or entity_data.get("original_device_class")
        if device_class:
            return device_class.replace("_", " ").title()

        # Fallback 2: Use stripped name even if it equals original (no prefix was present)
        if original_name:
            # Try to extract meaningful part from original name
            # Remove device/area name if it appears at start
            if device:
                device_display = device.get_display_name(area)
                if original_name.lower().startswith(device_display.lower()):
                    remainder = original_name[len(device_display) :].strip()
                    if remainder:
                        return remainder
            return original_name

        # Fallback 3: Extract from entity_id
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        entity_part = entity_id.split(".")[-1] if "." in entity_id else entity_id

        # Try to get the last meaningful part
        parts = entity_part.split("_")
        if len(parts) > 1:
            # Return last part, capitalized
            return parts[-1].replace("_", " ").title()

        # Fallback 4: Domain
        return domain.title() if domain else ""

    def compute_all_names(self) -> Dict[str, Tuple[str, str]]:
        """
        Compute names for all entities in the hierarchy.

        Returns:
            Dict of registry_id -> (new_entity_id, friendly_name)
        """
        result = {}

        for registry_id, entity in self.entities.items():
            device = self.devices.get(entity.device_id) if entity.device_id else None
            area = self._get_area_for_entity(entity, device)

            new_id = entity.get_entity_id(device, area)
            friendly_name = entity.get_display_name(device, area)

            result[registry_id] = (new_id, friendly_name)

        return result

    def _get_area_for_entity(self, entity: EntityNode, device: Optional[DeviceNode]) -> Optional[AreaNode]:
        """Get the area for an entity (via device or direct assignment)."""
        if device and device.area_id:
            return self.areas.get(device.area_id)
        elif entity.area_id:
            return self.areas.get(entity.area_id)
        return None

    def update_area_name(self, area_id: str, new_name: str) -> Dict[str, Tuple[str, str]]:
        """
        Preview area name change and cascade effect to all affected devices/entities.

        Note: This is for preview purposes only. Actual area renaming should be done
        via Home Assistant API, then reload the hierarchy.

        Args:
            area_id: The area to update
            new_name: The new display name

        Returns:
            Dict of affected registry_id -> (new_entity_id, friendly_name)
        """
        if area_id not in self.areas:
            logger.warning(f"Area not found: {area_id}")
            return {}

        area = self.areas[area_id]
        area.name = new_name  # Temporarily update for cascade calculation
        area.updated_at = datetime.utcnow()

        # Collect all affected entities
        affected = {}

        # Cascade to devices and their entities
        for device_id in self._area_to_devices.get(area_id, set()):
            device = self.devices[device_id]
            device.updated_at = datetime.utcnow()

            # Cascade to device's entities
            for registry_id in self._device_to_entities.get(device_id, set()):
                entity = self.entities[registry_id]
                entity.updated_at = datetime.utcnow()

                new_id = entity.get_entity_id(device, area)
                friendly_name = entity.get_display_name(device, area)
                affected[registry_id] = (new_id, friendly_name)

        # Cascade to directly assigned entities (no device)
        for registry_id in self._area_to_entities.get(area_id, set()):
            entity = self.entities[registry_id]
            entity.updated_at = datetime.utcnow()

            new_id = entity.get_entity_id(None, area)
            friendly_name = entity.get_display_name(None, area)
            affected[registry_id] = (new_id, friendly_name)

        logger.info(f"Area '{area_id}' updated to '{new_name}', {len(affected)} entities affected")
        return affected

    def update_device_name(self, device_id: str, new_name: str) -> Dict[str, Tuple[str, str]]:
        """
        Preview device name change and cascade effect to all affected entities.

        Note: This is for preview purposes only. Actual device renaming should be done
        via Home Assistant API, then reload the hierarchy.

        Args:
            device_id: The device to update
            new_name: The new base name (without area prefix)

        Returns:
            Dict of affected registry_id -> (new_entity_id, friendly_name)
        """
        if device_id not in self.devices:
            logger.warning(f"Device not found: {device_id}")
            return {}

        device = self.devices[device_id]
        device.name = new_name  # Temporarily update for cascade calculation
        device.updated_at = datetime.utcnow()

        area = self.areas.get(device.area_id) if device.area_id else None

        # Collect affected entities
        affected = {}

        for registry_id in self._device_to_entities.get(device_id, set()):
            entity = self.entities[registry_id]
            entity.updated_at = datetime.utcnow()

            new_id = entity.get_entity_id(device, area)
            friendly_name = entity.get_display_name(device, area)
            affected[registry_id] = (new_id, friendly_name)

        logger.info(f"Device '{device_id}' updated to '{new_name}', {len(affected)} entities affected")
        return affected

    def update_entity_name(self, registry_id: str, new_name: str) -> Tuple[str, str]:
        """
        Update entity base name.

        Args:
            registry_id: The entity registry ID
            new_name: The new base name

        Returns:
            Tuple of (new_entity_id, friendly_name)
        """
        if registry_id not in self.entities:
            logger.warning(f"Entity not found: {registry_id}")
            return ("", "")

        entity = self.entities[registry_id]
        entity.override_name = new_name
        entity.updated_at = datetime.utcnow()

        # Save to persistent storage
        if self.naming_overrides:
            self.naming_overrides.set_entity_override(registry_id, new_name)

        device = self.devices.get(entity.device_id) if entity.device_id else None
        area = self._get_area_for_entity(entity, device)

        new_id = entity.get_entity_id(device, area)
        friendly_name = entity.get_display_name(device, area)

        logger.info(f"Entity '{registry_id}' updated to '{new_name}'")
        return (new_id, friendly_name)

    def get_entity_by_id(self, entity_id: str) -> Optional[EntityNode]:
        """Get entity node by entity_id (not registry_id)."""
        registry_id = self._entity_id_to_registry.get(entity_id)
        if registry_id:
            return self.entities.get(registry_id)
        return None

    def get_entity_names(self, registry_id: str) -> Tuple[str, str]:
        """
        Get computed names for a single entity.

        Args:
            registry_id: The entity registry ID

        Returns:
            Tuple of (new_entity_id, friendly_name)
        """
        entity = self.entities.get(registry_id)
        if not entity:
            return ("", "")

        device = self.devices.get(entity.device_id) if entity.device_id else None
        area = self._get_area_for_entity(entity, device)

        return (
            entity.get_entity_id(device, area),
            entity.get_display_name(device, area),
        )

    def get_hierarchy_for_entity(self, registry_id: str) -> Dict[str, Any]:
        """
        Get the complete hierarchy chain for an entity.

        Args:
            registry_id: The entity registry ID

        Returns:
            Dict with area, device, and entity info
        """
        entity = self.entities.get(registry_id)
        if not entity:
            return {}

        device = self.devices.get(entity.device_id) if entity.device_id else None
        area = self._get_area_for_entity(entity, device)

        result = {
            "entity": {
                "id": entity.id,
                "registry_id": entity.registry_id,
                "base_name": entity.get_effective_base_name(),
                "has_override": entity.override_name is not None,
            }
        }

        if device:
            result["device"] = {
                "id": device.id,
                "base_name": device.base_name,
                "display_name": device.get_display_name(area),
            }

        if area:
            result["area"] = {
                "id": area.id,
                "display_name": area.display_name,
            }

        return result

    def get_entities_for_area(self, area_id: str) -> List[str]:
        """Get all entity registry_ids belonging to an area (directly or via devices)."""
        entities = []

        # Direct assignments
        entities.extend(self._area_to_entities.get(area_id, set()))

        # Via devices
        for device_id in self._area_to_devices.get(area_id, set()):
            entities.extend(self._device_to_entities.get(device_id, set()))

        return entities

    def get_entities_for_device(self, device_id: str) -> List[str]:
        """Get all entity registry_ids belonging to a device."""
        return list(self._device_to_entities.get(device_id, set()))

    def get_devices_for_area(self, area_id: str) -> List[str]:
        """Get all device_ids belonging to an area."""
        return list(self._area_to_devices.get(area_id, set()))
