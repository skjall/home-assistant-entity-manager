#!/usr/bin/env python3
"""
Reference Checker - Prüft Automations/Scenes/Scripts auf verwaiste Entity-Referenzen.
"""

import asyncio
from dataclasses import asdict, dataclass
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()


@dataclass
class BrokenReference:
    """Eine verwaiste Entity-Referenz."""

    config_type: str  # "automation" | "scene" | "script"
    config_id: str  # automation.xyz
    config_name: str  # Friendly name
    missing_entity_id: str  # light.schlafzimmer_2
    context: str  # "trigger" | "action" | "condition" | "entity"
    numeric_id: Optional[str] = None  # For automation/scene edit links
    area_id: Optional[str] = None  # Area assigned to the automation/scene/script
    yaml_path: Optional[str] = None  # Path in YAML, e.g. "use_blueprint -> input -> button_1 -> entity_id"

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class Suggestion:
    """Ein Ersatz-Vorschlag für eine fehlende Entity."""

    entity_id: str
    friendly_name: str
    score: float  # 0.0 - 1.0
    reasons: List[str]

    def to_dict(self) -> Dict:
        return asdict(self)


class ReferenceChecker:
    """Prüft Automations/Scenes/Scripts auf verwaiste Entity-Referenzen."""

    # Entity-ID Pattern für Extraktion
    ENTITY_ID_PATTERN = re.compile(r"\b([a-z_]+\.[a-z0-9_]+)\b")

    # Known services that look like entity IDs but aren't
    KNOWN_SERVICES = {
        "toggle",
        "turn_on",
        "turn_off",
        "reload",
        "set_value",
        "set_datetime",
        "set_options",
        "increment",
        "decrement",
        "set_cover_position",
        "open_cover",
        "close_cover",
        "stop_cover",
        "set_hvac_mode",
        "set_temperature",
        "set_fan_mode",
        "set_preset_mode",
        "set_humidity",
        "play_media",
        "media_play",
        "media_pause",
        "media_stop",
        "media_next_track",
        "media_previous_track",
        "volume_up",
        "volume_down",
        "volume_set",
        "volume_mute",
        "select_source",
        "select_option",
        "press",
        "start",
        "cancel",
        "pause",
        "finish",
        "trigger",
        "lock",
        "unlock",
        "open",
        "close",
    }

    # Keys to skip when extracting entity IDs
    # Note: "path" catches blueprint paths like "Blackshome/sensor-light.yaml"
    # "use_blueprint" was removed - we need to scan entity IDs in blueprint inputs
    SKIP_KEYS = {"path", "action", "service"}

    # Domains die wir als Entity-Referenzen betrachten
    VALID_DOMAINS = {
        "automation",
        "binary_sensor",
        "button",
        "calendar",
        "camera",
        "climate",
        "cover",
        "device_tracker",
        "fan",
        "group",
        "humidifier",
        "input_boolean",
        "input_button",
        "input_datetime",
        "input_number",
        "input_select",
        "input_text",
        "light",
        "lock",
        "media_player",
        "notify",
        "number",
        "person",
        "remote",
        "scene",
        "schedule",
        "script",
        "select",
        "sensor",
        "siren",
        "sun",
        "switch",
        "timer",
        "update",
        "vacuum",
        "water_heater",
        "weather",
        "zone",
    }

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        # Cache
        self._existing_entities: Optional[Set[str]] = None
        self._entity_details: Optional[Dict[str, Dict]] = None
        self._broken_refs_cache: Optional[List[BrokenReference]] = None

    def invalidate_cache(self):
        """Invalidiert den Cache."""
        self._broken_refs_cache = None
        self._existing_entities = None
        self._entity_details = None
        logger.info("Reference checker cache invalidated")

    async def get_states(self) -> List[Dict]:
        """Hole alle States von Home Assistant."""
        async with aiohttp.ClientSession() as session:
            url = f"{self.base_url}/api/states"
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                logger.error(f"Failed to get states: {response.status}")
                return []

    async def _load_existing_entities(self) -> Set[str]:
        """Lädt alle existierenden Entity-IDs."""
        if self._existing_entities is not None:
            return self._existing_entities

        states = await self.get_states()
        self._existing_entities = {s["entity_id"] for s in states}
        self._entity_details = {}

        for state in states:
            entity_id = state["entity_id"]
            attrs = state.get("attributes", {})
            self._entity_details[entity_id] = {
                "entity_id": entity_id,
                "friendly_name": attrs.get("friendly_name", entity_id),
                "domain": entity_id.split(".")[0],
                "device_class": attrs.get("device_class"),
            }

        logger.info(f"Loaded {len(self._existing_entities)} existing entities")
        return self._existing_entities

    def _is_service_call(self, entity_like: str) -> bool:
        """Prüft ob ein String ein Service-Aufruf ist (domain.service_name)."""
        parts = entity_like.split(".")
        if len(parts) != 2:
            return False
        service_name = parts[1]
        return service_name in self.KNOWN_SERVICES

    def _extract_entity_ids_with_path(self, data: Any, current_path: str = "") -> Dict[str, str]:
        """Extrahiert alle Entity-IDs mit ihrem YAML-Pfad aus einer Datenstruktur.

        Returns:
            Dict mapping entity_id -> yaml_path (e.g. "use_blueprint -> input -> button_1 -> entity_id")
        """
        entity_paths: Dict[str, str] = {}

        # Get the last key in path to check for skip
        path_parts = current_path.split(" -> ") if current_path else []
        last_key = path_parts[-1] if path_parts else None

        # Skip certain keys entirely (blueprints paths, service calls, etc.)
        if last_key in self.SKIP_KEYS:
            return entity_paths

        if isinstance(data, str):
            # Don't extract from strings that look like file paths
            if "/" in data or data.endswith(".yaml") or data.endswith(".yml"):
                return entity_paths

            # Finde alle Entity-ID-Patterns im String
            matches = self.ENTITY_ID_PATTERN.findall(data)
            for match in matches:
                domain = match.split(".")[0]
                if domain in self.VALID_DOMAINS:
                    # Skip if it's a known service call
                    if not self._is_service_call(match):
                        entity_paths[match] = current_path or "(root)"

        elif isinstance(data, dict):
            # Spezielle Keys die Entity-IDs enthalten
            if "entity_id" in data:
                entity_id_path = f"{current_path} -> entity_id" if current_path else "entity_id"
                val = data["entity_id"]
                if isinstance(val, str):
                    domain = val.split(".")[0]
                    if domain in self.VALID_DOMAINS and not self._is_service_call(val):
                        entity_paths[val] = entity_id_path
                elif isinstance(val, list):
                    for v in val:
                        if isinstance(v, str):
                            domain = v.split(".")[0]
                            if domain in self.VALID_DOMAINS and not self._is_service_call(v):
                                entity_paths[v] = entity_id_path

            # Rekursiv alle Werte durchsuchen, aber bestimmte Keys überspringen
            for key, value in data.items():
                if key not in self.SKIP_KEYS and key != "entity_id":  # entity_id already handled
                    new_path = f"{current_path} -> {key}" if current_path else key
                    entity_paths.update(self._extract_entity_ids_with_path(value, new_path))

        elif isinstance(data, list):
            for i, item in enumerate(data):
                # For lists, add index only if it's meaningful (more than one item or dict items)
                if len(data) > 1 or isinstance(item, dict):
                    new_path = f"{current_path}[{i}]" if current_path else f"[{i}]"
                else:
                    new_path = current_path
                entity_paths.update(self._extract_entity_ids_with_path(item, new_path))

        return entity_paths

    def _extract_entity_ids(self, data: Any, parent_key: str = None) -> Set[str]:
        """Extrahiert alle Entity-IDs aus einer Datenstruktur (ohne Pfad)."""
        return set(self._extract_entity_ids_with_path(data).keys())

    async def _get_automation_configs(self) -> List[Dict]:
        """Hole alle Automation-Konfigurationen."""
        async with aiohttp.ClientSession() as session:
            # Erst die Liste aller Automations
            url = f"{self.base_url}/api/states"
            async with session.get(url, headers=self.headers) as response:
                if response.status != 200:
                    return []
                states = await response.json()

            automations = []
            for state in states:
                if not state["entity_id"].startswith("automation."):
                    continue

                automation_id = state.get("attributes", {}).get("id")
                if not automation_id:
                    continue

                # Hole die vollständige Config
                config_url = f"{self.base_url}/api/config/automation/config/{automation_id}"
                async with session.get(config_url, headers=self.headers) as resp:
                    if resp.status == 200:
                        config = await resp.json()
                        automations.append(
                            {
                                "entity_id": state["entity_id"],
                                "numeric_id": automation_id,
                                "name": state.get("attributes", {}).get("friendly_name", state["entity_id"]),
                                "config": config,
                            }
                        )

            return automations

    async def _get_scene_configs(self) -> List[Dict]:
        """Hole alle Scene-Konfigurationen."""
        async with aiohttp.ClientSession() as session:
            url = f"{self.base_url}/api/states"
            async with session.get(url, headers=self.headers) as response:
                if response.status != 200:
                    return []
                states = await response.json()

            scenes = []
            for state in states:
                if not state["entity_id"].startswith("scene."):
                    continue

                scene_id = state.get("attributes", {}).get("id")
                if not scene_id:
                    continue

                config_url = f"{self.base_url}/api/config/scene/config/{scene_id}"
                async with session.get(config_url, headers=self.headers) as resp:
                    if resp.status == 200:
                        config = await resp.json()
                        scenes.append(
                            {
                                "entity_id": state["entity_id"],
                                "numeric_id": scene_id,
                                "name": state.get("attributes", {}).get("friendly_name", state["entity_id"]),
                                "config": config,
                            }
                        )

            return scenes

    async def _get_script_configs(self) -> List[Dict]:
        """Hole alle Script-Konfigurationen."""
        async with aiohttp.ClientSession() as session:
            url = f"{self.base_url}/api/states"
            async with session.get(url, headers=self.headers) as response:
                if response.status != 200:
                    return []
                states = await response.json()

            scripts = []
            for state in states:
                if not state["entity_id"].startswith("script."):
                    continue

                script_name = state["entity_id"].replace("script.", "")
                config_url = f"{self.base_url}/api/config/script/config/{script_name}"

                async with session.get(config_url, headers=self.headers) as resp:
                    if resp.status == 200:
                        config = await resp.json()
                        scripts.append(
                            {
                                "entity_id": state["entity_id"],
                                "name": state.get("attributes", {}).get("friendly_name", state["entity_id"]),
                                "config": config,
                            }
                        )

            return scripts

    async def get_all_referenced_entity_ids(self) -> Set[str]:
        """Alle in Automations/Scenes/Scripts referenzierten Entity-IDs.

        Dient dem Geräte-Austausch: nur tatsächlich verwendete (in use) Entities
        müssen gemappt werden; ungenutzte werden über die Rename-Logik mitbenannt.
        """
        referenced: Set[str] = set()
        for getter in (self._get_automation_configs, self._get_scene_configs, self._get_script_configs):
            try:
                for item in await getter():
                    referenced |= self._extract_entity_ids(item.get("config", {}))
            except Exception as e:  # noqa: BLE001 - ein fehlerhafter Config-Typ darf den Rest nicht stoppen
                logger.warning(f"Failed to scan configs for references: {e}")
        return referenced

    async def scan_all_references(
        self, use_cache: bool = True, entity_registry: Optional[Dict[str, Dict]] = None
    ) -> List[BrokenReference]:
        """Scannt alle Configs und findet fehlende Entities.

        Args:
            use_cache: Whether to use cached results
            entity_registry: Optional dict of entity_id -> entity data (with area_id)
        """
        if use_cache and self._broken_refs_cache is not None:
            logger.info("Using cached broken references")
            return self._broken_refs_cache

        logger.info("Scanning all references...")
        existing = await self._load_existing_entities()
        broken_refs: List[BrokenReference] = []

        # Helper to get area_id from entity registry
        def get_area_id(config_entity_id: str) -> Optional[str]:
            if entity_registry and config_entity_id in entity_registry:
                return entity_registry[config_entity_id].get("area_id")
            return None

        # Scan Automations
        logger.info("Scanning automations...")
        automations = await self._get_automation_configs()
        for auto in automations:
            # Get entity IDs with their YAML paths
            referenced_with_paths = self._extract_entity_ids_with_path(auto["config"])
            for entity_id, yaml_path in referenced_with_paths.items():
                if entity_id not in existing:
                    # Determine context from yaml_path
                    context = "action"
                    if yaml_path.startswith("trigger"):
                        context = "trigger"
                    elif yaml_path.startswith("condition"):
                        context = "condition"
                    elif "trigger" in yaml_path:
                        context = "trigger"
                    elif "condition" in yaml_path:
                        context = "condition"

                    broken_refs.append(
                        BrokenReference(
                            config_type="automation",
                            config_id=auto["entity_id"],
                            config_name=auto["name"],
                            missing_entity_id=entity_id,
                            context=context,
                            numeric_id=auto.get("numeric_id"),
                            area_id=get_area_id(auto["entity_id"]),
                            yaml_path=yaml_path,
                        )
                    )

        # Scan Scenes
        logger.info("Scanning scenes...")
        scenes = await self._get_scene_configs()
        for scene in scenes:
            entities = scene["config"].get("entities", {})
            for entity_id in entities.keys():
                if entity_id not in existing:
                    broken_refs.append(
                        BrokenReference(
                            config_type="scene",
                            config_id=scene["entity_id"],
                            config_name=scene["name"],
                            missing_entity_id=entity_id,
                            context="entity",
                            numeric_id=scene.get("numeric_id"),
                            area_id=get_area_id(scene["entity_id"]),
                            yaml_path="entities",
                        )
                    )

        # Scan Scripts
        logger.info("Scanning scripts...")
        scripts = await self._get_script_configs()
        for script in scripts:
            referenced_with_paths = self._extract_entity_ids_with_path(script["config"])
            for entity_id, yaml_path in referenced_with_paths.items():
                if entity_id not in existing:
                    broken_refs.append(
                        BrokenReference(
                            config_type="script",
                            config_id=script["entity_id"],
                            config_name=script["name"],
                            missing_entity_id=entity_id,
                            context="action",
                            area_id=get_area_id(script["entity_id"]),
                            yaml_path=yaml_path,
                        )
                    )

        logger.info(f"Found {len(broken_refs)} broken references")
        self._broken_refs_cache = broken_refs
        return broken_refs

    def _levenshtein_distance(self, s1: str, s2: str) -> int:
        """Berechnet die Levenshtein-Distanz zwischen zwei Strings."""
        if len(s1) < len(s2):
            return self._levenshtein_distance(s2, s1)

        if len(s2) == 0:
            return len(s1)

        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row

        return previous_row[-1]

    def _calculate_similarity(self, missing_id: str, candidate_id: str) -> Tuple[float, List[str]]:
        """Berechnet die Ähnlichkeit zwischen zwei Entity-IDs."""
        score = 0.0
        reasons = []

        missing_parts = missing_id.split(".")
        candidate_parts = candidate_id.split(".")

        missing_domain = missing_parts[0]
        candidate_domain = candidate_parts[0]
        missing_name = missing_parts[1] if len(missing_parts) > 1 else ""
        candidate_name = candidate_parts[1] if len(candidate_parts) > 1 else ""

        # 1. Domain Match (+30%)
        if missing_domain == candidate_domain:
            score += 0.30
            reasons.append("same_domain")

        # 2. Area Match (+25%) - Prüfe ob der erste Teil des Namens übereinstimmt
        missing_area = missing_name.split("_")[0] if "_" in missing_name else ""
        candidate_area = candidate_name.split("_")[0] if "_" in candidate_name else ""
        if missing_area and candidate_area and missing_area == candidate_area:
            score += 0.25
            reasons.append("same_area")

        # 3. Name Similarity - Levenshtein (+0-45%)
        if missing_name and candidate_name:
            max_len = max(len(missing_name), len(candidate_name))
            if max_len > 0:
                distance = self._levenshtein_distance(missing_name, candidate_name)
                lev_ratio = 1 - (distance / max_len)
                score += lev_ratio * 0.45
                if lev_ratio > 0.5:
                    reasons.append("similar_name")

        return (score, reasons)

    async def get_suggestions(self, missing_entity_id: str, limit: int = 5) -> List[Suggestion]:
        """Generiert Ersatz-Vorschläge basierend auf Ähnlichkeit.

        Nur Entities mit der gleichen Domain werden vorgeschlagen.
        Andere Domains können über die Suchfunktion gefunden werden.
        """
        existing = await self._load_existing_entities()
        if self._entity_details is None:
            await self._load_existing_entities()

        suggestions = []
        missing_domain = missing_entity_id.split(".")[0]

        for entity_id in existing:
            # Überspringe gleiche Entity
            if entity_id == missing_entity_id:
                continue

            # Nur Entities mit gleicher Domain vorschlagen
            candidate_domain = entity_id.split(".")[0]
            if candidate_domain != missing_domain:
                continue

            # Berechne Ähnlichkeit
            score, reasons = self._calculate_similarity(missing_entity_id, entity_id)

            # Nur Vorschläge mit Mindest-Score
            if score >= 0.2:
                details = self._entity_details.get(entity_id, {})
                suggestions.append(
                    Suggestion(
                        entity_id=entity_id,
                        friendly_name=details.get("friendly_name", entity_id),
                        score=round(score, 3),
                        reasons=reasons,
                    )
                )

        # Sortiere nach Score (absteigend)
        suggestions.sort(key=lambda s: s.score, reverse=True)

        return suggestions[:limit]

    async def get_all_entities(self) -> List[Dict]:
        """Gibt alle Entities für Autocomplete zurück."""
        if self._entity_details is None:
            await self._load_existing_entities()

        return list(self._entity_details.values()) if self._entity_details else []


async def main():
    """Test des Reference Checkers."""
    base_url = os.getenv("HA_URL")
    token = os.getenv("HA_TOKEN")

    if not base_url or not token:
        print("HA_URL und HA_TOKEN müssen gesetzt sein")
        return

    checker = ReferenceChecker(base_url, token)

    print("Scanning for broken references...")
    broken = await checker.scan_all_references()

    print(f"\nFound {len(broken)} broken references:\n")
    for ref in broken:
        print(f"  [{ref.config_type}] {ref.config_name}")
        print(f"    Missing: {ref.missing_entity_id} (in {ref.context})")

        # Get suggestions
        suggestions = await checker.get_suggestions(ref.missing_entity_id)
        if suggestions:
            print("    Suggestions:")
            for sug in suggestions[:3]:
                print(f"      - {sug.entity_id} ({sug.score:.0%}) {sug.reasons}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
