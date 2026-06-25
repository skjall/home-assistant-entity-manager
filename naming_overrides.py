#!/usr/bin/env python3
"""
Naming Override System - Speichert benutzerdefinierte Entity-Suffix-Mappings.

Schema Version History:
- v1: Original flat structure {entities: {}, devices: {}, areas: {}}
- v2: Added version field
- v3: Removed device and area overrides (use HA API directly)

Only entity suffix overrides are stored. Device and area names
come directly from the Home Assistant API.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Current schema version
SCHEMA_VERSION = 3


class NamingOverrides:
    """
    Manages persistent storage of user naming overrides.

    Only stores entity suffix overrides. Device and area names
    are sourced directly from Home Assistant.
    """

    def __init__(self, storage_path: str = "naming_overrides.json"):
        """
        Initialize the naming overrides manager.

        Args:
            storage_path: Path to the JSON storage file
        """
        self.storage_path = Path(storage_path)
        # Ensure directory exists
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load_data()
        self._migrate_if_needed()

    def _load_data(self) -> Dict[str, Any]:
        """Load stored overrides from file."""
        default_data = {"version": SCHEMA_VERSION, "entities": {}}

        if self.storage_path.exists():
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
                    # Ensure entities key exists
                    if "entities" not in existing_data:
                        existing_data["entities"] = {}
                    return existing_data
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading overrides: {e}")

        return default_data

    def _migrate_if_needed(self) -> None:
        """Migrate data from older schema versions if needed."""
        current_version = self.data.get("version", 1)

        if current_version < SCHEMA_VERSION:
            logger.info(f"Migrating naming overrides from v{current_version} to v{SCHEMA_VERSION}")
            self._migrate_to_v3()
            self.data["version"] = SCHEMA_VERSION
            self._save_data()

    def _migrate_to_v3(self) -> None:
        """Migrate to v3 schema - remove device and area overrides."""
        # Remove device and area overrides (no longer used)
        if "devices" in self.data:
            del self.data["devices"]
            logger.info("Migration to v3: Removed device overrides")
        if "areas" in self.data:
            del self.data["areas"]
            logger.info("Migration to v3: Removed area overrides")

    def _save_data(self) -> None:
        """Speichere Overrides"""
        try:
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            logger.info(f"Overrides gespeichert: {len(self.data['entities'])} entities")
        except Exception as e:
            logger.error(f"Fehler beim Speichern der Overrides: {e}")

    # === Entity Overrides ===

    def set_entity_override(self, registry_id: str, name: str, type_override: Optional[str] = None) -> None:
        """Setze Entity Name Override"""
        if "entities" not in self.data:
            self.data["entities"] = {}
        self.data["entities"][registry_id] = {"name": name}
        if type_override:
            self.data["entities"][registry_id]["type"] = type_override
        self._save_data()
        logger.info(f"Entity override gesetzt: {registry_id} -> {name}")

    def get_entity_override(self, registry_id: str) -> Optional[Dict[str, str]]:
        """Hole Entity Override"""
        return self.data.get("entities", {}).get(registry_id)

    def remove_entity_override(self, registry_id: str) -> None:
        """Entferne Entity Override"""
        if "entities" in self.data and registry_id in self.data["entities"]:
            del self.data["entities"][registry_id]
            self._save_data()
            logger.info(f"Entity override entfernt: {registry_id}")

    # === Bulk Operations ===

    def get_all_entity_overrides(self) -> Dict[str, Dict[str, str]]:
        """Hole alle Entity Overrides"""
        return self.data.get("entities", {}).copy()

    def clear_all(self) -> None:
        """Clear all overrides while preserving schema version."""
        self.data = {"version": SCHEMA_VERSION, "entities": {}}
        self._save_data()
        logger.info("All overrides cleared")

    # === Statistics ===

    def get_stats(self) -> Dict[str, int]:
        """Get statistics about stored overrides."""
        return {
            "version": self.data.get("version", 1),
            "entity_overrides": len(self.data.get("entities", {})),
        }

    def has_any_overrides(self) -> bool:
        """Check if any overrides are stored."""
        return bool(self.data.get("entities"))
