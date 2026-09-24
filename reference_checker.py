#!/usr/bin/env python3
"""
Reference Checker - Prüft Automations/Scenes/Scripts auf verwaiste Entity-Referenzen.
"""

import asyncio
from dataclasses import asdict, dataclass
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set

import aiohttp
from dotenv import load_dotenv

from config_files import shared as shared_config_files
from device_swap import INTERIM_SUFFIX
from energy_prefs import EnergyPrefs, ws_url_for
from helper_options import HelperOptions

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()


@dataclass
class BrokenReference:
    """Eine verwaiste Entity-Referenz."""

    config_type: str  # "automation" | "scene" | "script" | "helper" | "energy"
    config_id: str  # automation.xyz
    config_name: str  # Friendly name
    missing_entity_id: str  # light.schlafzimmer_2
    context: str  # "trigger" | "action" | "condition" | "entity"
    numeric_id: Optional[str] = None  # For automation/scene edit links
    area_id: Optional[str] = None  # Area assigned to the automation/scene/script
    yaml_path: Optional[str] = None  # Path in YAML, e.g. "use_blueprint -> input -> button_1 -> entity_id"
    # Set when the reference lives in a file the configuration API will not
    # write. Nothing here can be repaired for the user; the interface says which
    # file and which line to edit instead.
    file_path: Optional[str] = None  # As the user sees it, e.g. "/config/packages/water.yaml"
    file_line: Optional[int] = None
    line_text: Optional[str] = None
    fixable: bool = True
    object_id: Optional[str] = None  # script.x, where the file's shape names one

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
    """Prüft Automations/Scenes/Scripts/Helfer auf verwaiste Entity-Referenzen."""

    # Set once the application state is built, as EntityRegistry's is. Without
    # it only a guess is available, which is what a fresh installation has.
    rename_log = None

    # Entity-ID Pattern für Extraktion
    ENTITY_ID_PATTERN = re.compile(r"\b([a-z_]+\.[a-z0-9_]+)\b")

    # A whole id and nothing else: what an `entity_id:` has to hold to name one.
    WHOLE_ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")

    # Where a template picks up. A name built out of one is finished at runtime,
    # so the part standing in the YAML is not an id and never will be.
    TEMPLATE_STARTS = "{"
    TEMPLATE_ENDS = "}"

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
        # Helpers built in the interface: their templates name entities in
        # prose, so nothing carries a rename into them and a dead reference
        # sits there silently.
        self.helpers = HelperOptions(self.base_url, self.token)
        # The energy dashboard's own store, which no file or REST scan reaches.
        self.energy = EnergyPrefs(ws_url_for(self.base_url), self.token)
        # The YAML the configuration API does not hand out: packages, includes
        # and YAML-mode dashboards. Present only when the mount is there.
        self.config_files = shared_config_files(self.VALID_DOMAINS)
        # Cache
        self._existing_entities: Optional[Set[str]] = None
        self._entity_details: Optional[Dict[str, Dict]] = None
        self._broken_refs_cache: Optional[List[BrokenReference]] = None

    def invalidate_cache(self):
        """Invalidiert den Cache."""
        self._broken_refs_cache = None
        self._existing_entities = None
        self._entity_details = None
        self.config_files.forget()
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

    def _runs_into_a_template(self, text: str, start: int, end: int) -> bool:
        """Whether the match is one half of a name a template finishes.

        `input_text.negativstrom_{{ dev.name | lower }}_titel` matches as far as
        the brace and looks like an id from there on, so a helper that exists is
        reported missing under a name nobody wrote.

        An id inside a template is a different thing and stays: `states(
        'sensor.real')` names `sensor.real`, and the quote around it is what
        says so.
        """
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        return after == self.TEMPLATE_STARTS or before == self.TEMPLATE_ENDS

    def _names_one_entity(self, value: str) -> bool:
        """Whether an `entity_id:` value is an id rather than something to render.

        The value was taken as read before, so `entity_id: input_boolean.x_{{ y }}_z`
        became a missing entity. A target either names an entity outright or is
        worked out at runtime, and only the first is something to check.
        """
        if not self.WHOLE_ENTITY_ID.match(value):
            return False
        return value.split(".")[0] in self.VALID_DOMAINS and not self._is_service_call(value)

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
            for found in self.ENTITY_ID_PATTERN.finditer(data):
                match = found.group(1)
                domain = match.split(".")[0]
                if domain not in self.VALID_DOMAINS:
                    continue
                # Skip if it's a known service call
                if self._is_service_call(match):
                    continue
                if self._runs_into_a_template(data, found.start(), found.end()):
                    continue
                entity_paths[match] = current_path or "(root)"

        elif isinstance(data, dict):
            # Spezielle Keys die Entity-IDs enthalten
            if "entity_id" in data:
                entity_id_path = f"{current_path} -> entity_id" if current_path else "entity_id"
                val = data["entity_id"]
                if isinstance(val, str):
                    if self._names_one_entity(val):
                        entity_paths[val] = entity_id_path
                elif isinstance(val, list):
                    for v in val:
                        if isinstance(v, str) and self._names_one_entity(v):
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

        # Scan helpers built in the interface
        logger.info("Scanning helpers...")
        try:
            for helper in await self.helpers.broken(existing):
                broken_refs.append(
                    BrokenReference(
                        config_type="helper",
                        config_id=helper["entry_id"],
                        config_name=helper["title"],
                        missing_entity_id=helper["missing_entity_id"],
                        context="template",
                        yaml_path=helper["field"],
                    )
                )
        except Exception as error:  # noqa: BLE001 - the other findings still stand
            logger.error("Could not scan the helpers: %s", error)

        # Scan the energy dashboard
        logger.info("Scanning the energy dashboard...")
        try:
            for place in await self.energy.broken(existing):
                broken_refs.append(
                    BrokenReference(
                        config_type="energy",
                        config_id="energy",
                        config_name="Energy dashboard",
                        missing_entity_id=place["missing_entity_id"],
                        context=place["field"],
                        yaml_path=place["path"],
                    )
                )
        except Exception as error:  # noqa: BLE001 - the other findings still stand
            logger.error("Could not scan the energy dashboard: %s", error)

        broken_refs.extend(self._broken_in_the_yaml(existing))

        logger.info(f"Found {len(broken_refs)} broken references")
        self._broken_refs_cache = broken_refs
        return broken_refs

    def _broken_in_the_yaml(self, existing: Set[str]) -> List[BrokenReference]:
        """Dead references in the files the configuration API will not write.

        One entry per line, because the user edits the file line by line and a
        count of occurrences would not tell them where to go.
        """
        if not self.config_files.available():
            logger.info("No configuration mount, skipping the YAML kept by hand")
            return []
        logger.info("Scanning the YAML kept by hand...")
        found: List[BrokenReference] = []
        for entity_id, mentions in self.config_files.index().items():
            if entity_id in existing or entity_id.split(".", 1)[0] not in self.VALID_DOMAINS:
                continue
            for one in mentions:
                shown = self.config_files.shown_as(one.path)
                holder = self.config_files.holder_of(one)
                found.append(
                    BrokenReference(
                        config_type="yaml",
                        config_id=f"{shown}:{one.line}",
                        # What the object is called, where the file says so at
                        # all; the file name is the last resort.
                        config_name=(holder.described() if holder else "") or shown.rsplit("/", 1)[-1],
                        missing_entity_id=entity_id,
                        context="yaml",
                        yaml_path=(" → ".join(holder.trail) if holder else shown),
                        file_path=shown,
                        file_line=one.line,
                        line_text=one.text,
                        fixable=False,
                        object_id=(holder.object_id if holder else None),
                    )
                )
        return found

    async def get_suggestions(self, missing_entity_id: str) -> List[Suggestion]:
        """What this entity became, according to what was actually renamed.

        Only the rename log answers. Resembling names were weighed before, and
        the weighing could not tell apart what the names themselves do not say:
        `buro_rechtes_fenster_status` was answered with that window's
        `hardwarefehler`, `netzwerkfehler`, `funkmodulfehler` and `zustand`, all
        four scoring the same and ordered by nothing. A name carries a place, a
        device and a type, and counting shared words reads a shared place as
        evidence while the type - the part that differs - costs the same as any
        other word.

        So where the log has nothing to say, neither does this. Nothing is
        better than something wrong: a bad suggestion sends the user to an
        entity that has nothing to do with the one they lost.
        """
        existing = await self._load_existing_entities()
        if self._entity_details is None:
            await self._load_existing_entities()

        return [carried] if (carried := self._where_it_went(missing_entity_id, existing)) else []

    def _where_it_went(self, missing_entity_id: str, existing: Set[str]) -> Optional[Suggestion]:
        """What the rename log says this entity became, if it still exists.

        The log records one hop at a time and an entity can be renamed again, so
        the chain is followed to its end. It ends nowhere often enough to check:
        a device swap parks the old entity on an interim id, and where the old
        device was deleted the chain stops at something that is gone.
        """
        if self.rename_log is None:
            return None
        try:
            answer = self.rename_log.search(missing_entity_id)
        except Exception as error:  # noqa: BLE001 - an unreadable log is not a broken scan
            logger.debug("Could not read the rename log for %s: %s", missing_entity_id, error)
            return None
        if not answer.get("found"):
            return None
        current = answer.get("current_entity_id")
        if not current or current not in existing:
            logger.debug("The rename chain for %s ends at %s, which is gone", missing_entity_id, current)
            return None
        # A swap can keep the old device, renamed, and then the chain ends on the
        # interim id. It exists, so the check above lets it through, and it is
        # still the entity that was replaced rather than the one replacing it.
        # Where the new device has no matching entity there is nothing to offer.
        if current.endswith(INTERIM_SUFFIX):
            logger.debug("The rename chain for %s ends on the replaced entity %s", missing_entity_id, current)
            return None
        details = (self._entity_details or {}).get(current, {})
        return Suggestion(
            entity_id=current,
            friendly_name=details.get("friendly_name", current),
            score=1.0,
            reasons=["renamed_here"],
        )

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
