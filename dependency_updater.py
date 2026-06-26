#!/usr/bin/env python3
"""
Dependency Updater - Aktualisiert Entity IDs in Scenes, Scripts und Automations
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

import aiohttp
from dotenv import load_dotenv

from entity_ref_utils import replace_entity_in_obj

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()


class DependencyUpdater:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def get_states(self) -> List[Dict]:
        """Hole alle States"""
        async with aiohttp.ClientSession() as session:
            url = f"{self.base_url}/api/states"
            async with session.get(url, headers=self.headers) as response:
                return await response.json()

    # ===== SCENES =====
    async def get_scene_config(self, scene_numeric_id: str) -> Optional[Dict]:
        """Hole Scene Konfiguration"""
        url = f"{self.base_url}/api/config/scene/config/{scene_numeric_id}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"Fehler beim Abrufen der Scene {scene_numeric_id}: {response.status}")
                    return None

    async def update_scene_config(self, scene_numeric_id: str, config: Dict) -> bool:
        """Aktualisiere Scene Konfiguration"""
        url = f"{self.base_url}/api/config/scene/config/{scene_numeric_id}"

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=config) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get("result") == "ok"
                else:
                    logger.error(f"Fehler beim Update der Scene: {response.status}")
                    return False

    async def update_scene_entities(
        self,
        scene_id: str,
        scene_numeric_id: str,
        old_entity_id: str,
        new_entity_id: str,
    ) -> bool:
        """Aktualisiere Entity in einer Scene"""
        config = await self.get_scene_config(scene_numeric_id)
        if not config:
            return False

        # Prüfe ob die alte Entity in der Scene ist
        if old_entity_id not in config.get("entities", {}):
            return False

        # Ersetze die Entity
        entity_config = config["entities"].pop(old_entity_id)
        config["entities"][new_entity_id] = entity_config

        logger.info(f"Aktualisiere Scene {scene_id}: {old_entity_id} -> {new_entity_id}")
        return await self.update_scene_config(scene_numeric_id, config)

    # ===== SCRIPTS =====
    async def get_script_config(self, script_id: str) -> Optional[Dict]:
        """Hole Script Konfiguration"""
        # script.name -> name extrahieren
        script_name = script_id.replace("script.", "")
        url = f"{self.base_url}/api/config/script/config/{script_name}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"Fehler beim Abrufen des Scripts {script_id}: {response.status}")
                    return None

    async def update_script_config(self, script_id: str, config: Dict) -> bool:
        """Aktualisiere Script Konfiguration"""
        script_name = script_id.replace("script.", "")
        url = f"{self.base_url}/api/config/script/config/{script_name}"

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=config) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get("result") == "ok"
                else:
                    logger.error(f"Fehler beim Update des Scripts: {response.status}")
                    return False

    def replace_entity_in_dict(self, data: Any, old_entity_id: str, new_entity_id: str) -> bool:
        """Rekursiv Entity IDs in einem Dictionary ersetzen (in-place).

        Unterstützt:
        - entity_id: "entity.id" (direkter Wert)
        - entity_id: ["entity.id", ...] (Liste unter entity_id Key)
        - beliebiger_key: "entity.id" (z.B. Blueprint-Inputs)
        - beliebiger_key: ["entity.id", ...] (z.B. Blueprint-Input Listen)
        - Templates mit {{ entity.id }} (wortgrenzen-sicher)

        Delegiert an die zentrale Logik in entity_ref_utils, damit Scanner,
        Updater und Lovelace-Updater dieselbe Ersetzung verwenden.
        """
        return replace_entity_in_obj(data, old_entity_id, new_entity_id)

    async def update_script_entities(self, script_id: str, old_entity_id: str, new_entity_id: str) -> bool:
        """Aktualisiere Entity in einem Script"""
        config = await self.get_script_config(script_id)
        if not config:
            return False

        # Ersetze Entity IDs rekursiv
        changed = self.replace_entity_in_dict(config, old_entity_id, new_entity_id)

        if changed:
            logger.info(f"Aktualisiere Script {script_id}: {old_entity_id} -> {new_entity_id}")
            return await self.update_script_config(script_id, config)

        return False

    # ===== AUTOMATIONS =====
    async def get_automation_config(self, automation_numeric_id: str) -> Optional[Dict]:
        """Hole Automation Konfiguration"""
        url = f"{self.base_url}/api/config/automation/config/{automation_numeric_id}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    text = await response.text()
                    logger.error(
                        f"Fehler beim Abrufen der Automation {automation_numeric_id}: {response.status}, Response: {text}"
                    )
                    return None

    async def update_automation_config(self, automation_numeric_id: str, config: Dict) -> bool:
        """Aktualisiere Automation Konfiguration"""
        url = f"{self.base_url}/api/config/automation/config/{automation_numeric_id}"

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=config) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get("result") == "ok"
                else:
                    logger.error(f"Fehler beim Update der Automation: {response.status}")
                    return False

    async def update_automation_entities(
        self,
        automation_id: str,
        automation_numeric_id: str,
        old_entity_id: str,
        new_entity_id: str,
    ) -> bool:
        """Aktualisiere Entity in einer Automation"""
        logger.debug(f"Fetching config for automation {automation_id} (numeric: {automation_numeric_id})")
        config = await self.get_automation_config(automation_numeric_id)
        if not config:
            logger.error(f"Could not fetch config for automation {automation_id}")
            return False

        logger.debug(f"Got config, checking for entity {old_entity_id}")
        # Ersetze Entity IDs rekursiv
        changed = self.replace_entity_in_dict(config, old_entity_id, new_entity_id)

        if changed:
            logger.info(f"Aktualisiere Automation {automation_id}: {old_entity_id} -> {new_entity_id}")
            return await self.update_automation_config(automation_numeric_id, config)
        else:
            logger.debug(f"No changes needed for automation {automation_id}")

        return False

    # ===== MAIN UPDATE =====
    async def update_all_dependencies(
        self, old_entity_id: str, new_entity_id: str, cached_states: Optional[List[Dict]] = None
    ) -> Dict[str, List[str]]:
        """Aktualisiere alle Dependencies"""
        logger.info("=== DependencyUpdater.update_all_dependencies called ===")
        logger.info(f"Old entity: {old_entity_id}, New entity: {new_entity_id}")

        results = {
            "scenes": {"success": [], "failed": []},
            "scripts": {"success": [], "failed": []},
            "automations": {"success": [], "failed": []},
            "total_success": 0,
            "total_failed": 0,
        }

        # Use cached states if provided, otherwise fetch
        if cached_states is not None:
            states = cached_states
            logger.info(f"Using cached states ({len(states)} states)")
        else:
            logger.info("Fetching states from Home Assistant...")
            states = await self.get_states()
            logger.info(f"Got {len(states)} states")

        # Count automations for debugging
        automation_count = sum(1 for s in states if s["entity_id"].startswith("automation."))
        logger.info(f"Found {automation_count} automations in states")

        for state in states:
            entity_id = state["entity_id"]
            attributes = state.get("attributes", {})

            # SCENES
            if entity_id.startswith("scene."):
                entity_ids = attributes.get("entity_id", [])
                if old_entity_id in entity_ids:
                    scene_numeric_id = attributes.get("id")
                    if scene_numeric_id:
                        success = await self.update_scene_entities(
                            entity_id, scene_numeric_id, old_entity_id, new_entity_id
                        )
                        if success:
                            results["scenes"]["success"].append(entity_id)
                            results["total_success"] += 1
                        else:
                            results["scenes"]["failed"].append(entity_id)
                            results["total_failed"] += 1

            # SCRIPTS
            elif entity_id.startswith("script."):
                # Script state attributes don't contain the sequence, so checking
                # them never finds entity references. Fetch the real config via REST
                # and check there (same approach as automations below).
                config = await self.get_script_config(entity_id)
                if config and old_entity_id in json.dumps(config):
                    logger.info(f"Found script {entity_id} using {old_entity_id}")
                    success = await self.update_script_entities(entity_id, old_entity_id, new_entity_id)
                    if success:
                        results["scripts"]["success"].append(entity_id)
                        results["total_success"] += 1
                    else:
                        results["scripts"]["failed"].append(entity_id)
                        results["total_failed"] += 1

            # AUTOMATIONS
            elif entity_id.startswith("automation."):
                # Skip - we'll handle automations separately via REST API
                pass

        # Handle automations via REST API
        logger.info("Checking automations via REST API...")
        automation_states = [s for s in states if s["entity_id"].startswith("automation.")]
        logger.info(f"Found {len(automation_states)} automations to check")

        for automation_state in automation_states:
            automation_entity_id = automation_state["entity_id"]
            automation_numeric_id = automation_state.get("attributes", {}).get("id")

            if automation_numeric_id:
                # Get automation config via REST
                config = await self.get_automation_config(automation_numeric_id)
                if config:
                    config_str = json.dumps(config)
                    if old_entity_id in config_str:
                        logger.info(f"Found automation {automation_entity_id} using {old_entity_id}")
                        success = await self.update_automation_entities(
                            automation_entity_id,
                            automation_numeric_id,
                            old_entity_id,
                            new_entity_id,
                        )
                        if success:
                            results["automations"]["success"].append(automation_entity_id)
                            results["total_success"] += 1
                        else:
                            results["automations"]["failed"].append(automation_entity_id)
                            results["total_failed"] += 1
                else:
                    logger.debug(f"Could not fetch config for automation {automation_entity_id}")
            else:
                logger.debug(f"Automation {automation_entity_id} has no numeric ID")

        return results


async def main():
    """Test des Dependency Updaters"""
    base_url = os.getenv("HA_URL")
    token = os.getenv("HA_TOKEN")

    updater = DependencyUpdater(base_url, token)

    # Test
    old_entity = "light.buro_bucherregal_spots_licht"  # Die neue ID von vorhin
    new_entity = "light.buro_bucherregal_spots"  # Zurück zur alten als Test

    print(f"Aktualisiere {old_entity} -> {new_entity} in allen Dependencies...")

    results = await updater.update_all_dependencies(old_entity, new_entity)

    print("\n" + "=" * 60)
    print("ERGEBNISSE")
    print("=" * 60)

    # Scenes
    if results["scenes"]["success"] or results["scenes"]["failed"]:
        print("\nSCENES:")
        for scene in results["scenes"]["success"]:
            print(f"  ✓ {scene}")
        for scene in results["scenes"]["failed"]:
            print(f"  ✗ {scene}")

    # Scripts
    if results["scripts"]["success"] or results["scripts"]["failed"]:
        print("\nSCRIPTS:")
        for script in results["scripts"]["success"]:
            print(f"  ✓ {script}")
        for script in results["scripts"]["failed"]:
            print(f"  ✗ {script}")

    # Automations
    if results["automations"]["success"] or results["automations"]["failed"]:
        print("\nAUTOMATIONS:")
        for auto in results["automations"]["success"]:
            print(f"  ✓ {auto}")
        for auto in results["automations"]["failed"]:
            print(f"  ✗ {auto}")

    print(f"\n{'='*60}")
    print(f"Gesamt: {results['total_success']} erfolgreich, {results['total_failed']} fehlgeschlagen")


if __name__ == "__main__":
    asyncio.run(main())
